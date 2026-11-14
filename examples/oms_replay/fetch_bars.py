"""Unadjusted closes for closed positions; injected transport keeps verification offline."""
import csv
import io
import json
import os
import queue
import re
import tempfile
import threading
import time
import urllib.request
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

import fetch_index

FETCH_TIMEOUT = 8
BATCH_TIMEOUT = 20
LOOKBACK_DAYS = 14
ROW_COUNT = 12


class BarsWriteError(OSError):
    """Publication failure, distinct from an unavailable remote endpoint."""


class BarsDataError(ValueError):
    """Validated, user-readable data diagnosis, safe for the main message."""


class BatchTimeoutError(TimeoutError):
    pass


def symbol_for(security):
    try:
        market, code = security.split('|')
    except (AttributeError, ValueError) as exc:
        raise BarsDataError('证券市场或代码格式无效') from exc
    if market in ('上海', '深圳'):
        if not re.fullmatch(r'[0-9]{6}', code):
            raise BarsDataError('证券代码必须是六位数字')
        return ('sh' if market == '上海' else 'sz') + code
    if market.startswith('港股通'):
        if not re.fullmatch(r'[0-9]{1,5}', code):
            raise BarsDataError('港股代码必须是一至五位数字')
        return 'hk' + code.zfill(5)
    raise BarsDataError('这个市场的日线接口不支持')


def is_ex_rights(row):
    if len(row) <= 6 or not isinstance(row[6], dict) or row[6].get('cqr') != row[0]:
        return False
    event = row[6]
    # Unknown event fields stay conservative; only a known buyback-only record is exempt.
    buyback_only = (bool(event.get('HGcontent'))
                    and not any(event.get(key) for key in ('FHcontent', 'hgcgContent', 'ggContent'))
                    and event.keys() <= {'cqr', 'FHcontent', 'HGcontent', 'paixiri', 'hgcgContent', 'ggContent'})
    return not buyback_only


def tencent_fetch(*, symbol, start_date, end_date, opener=None):
    if not re.fullmatch(r'(?:sh|sz)[0-9]{6}|hk[0-9]{5}', symbol):
        raise BarsDataError('证券代码格式无效')
    start, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
    if start > end:
        raise BarsDataError('起始日期晚于截止日期')
    url = ('https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param='
           f'{symbol},day,{start.isoformat()},{end.isoformat()},{ROW_COUNT},')
    request = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with (opener or urllib.request.urlopen)(request, timeout=FETCH_TIMEOUT) as response:
        payload = json.load(response)
    try:
        if payload['code'] != 0:
            raise BarsDataError('个股接口返回失败')
        rows = payload['data'][symbol]['day']
        if not isinstance(rows, list) or not rows:
            raise BarsDataError('个股接口没有日线数据')
        if any(not isinstance(row, list) or len(row) < 6 for row in rows):
            raise BarsDataError('个股日线格式不符')
        return [{'trade_date': row[0], 'close': row[2],
                 'ex_rights': is_ex_rights(row)}
                for row in rows]
    except (KeyError, TypeError) as exc:
        raise BarsDataError('个股 JSON 结构不符') from exc


def validated_rows(records):
    if not isinstance(records, (list, tuple)) or not records:
        raise BarsDataError('个股日线为空或格式无效；原文件未改动')
    rows = []
    previous = None
    for record in records:
        try:
            day = date.fromisoformat(str(record['trade_date']))
            close = Decimal(str(record['close']))
        except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
            raise BarsDataError('个股日期或收盘格式无效；原文件未改动') from exc
        if previous is not None and day <= previous:
            raise BarsDataError('个股日期重复或不升序；原文件未改动')
        if not close.is_finite() or close <= 0:
            raise BarsDataError('个股收盘必须为正数；原文件未改动')
        previous = day
        ex_rights = record.get('ex_rights', False)
        if type(ex_rights) is not bool:
            raise BarsDataError('个股除权除息标记无效；原文件未改动')
        rows.append({'date': day.isoformat(), 'close': format(close, 'f'), 'ex_rights': ex_rights})
    return rows


def read_rows(target):
    if not target.exists():
        return []
    try:
        with target.open(encoding='utf-8-sig', newline='') as stream:
            reader = csv.reader(stream)
            if next(reader, None) != ['日期', '收盘', '除权除息']:
                raise BarsDataError('个股日线缓存表头无效；原文件未改动')
            rows = list(reader)
        if any(len(row) != 3 or row[2] not in ('', '是') for row in rows):
            raise BarsDataError('个股日线缓存格式无效；原文件未改动')
        return validated_rows([{'trade_date': row[0], 'close': row[1], 'ex_rights': row[2] == '是'} for row in rows])
    except (UnicodeError, csv.Error) as exc:
        raise BarsDataError('个股日线缓存格式无效；原文件未改动') from exc


def pair_from(rows, day):
    for i, row in enumerate(rows):
        if row['date'] == day and i > 0:
            return rows[i-1:i+1]
    return []


def read_pair(directory, security, day):
    if directory is None:
        return []
    try:
        date.fromisoformat(day)
        return pair_from(read_rows(Path(directory) / (symbol_for(security) + '.csv')), day)
    except (ValueError, OSError):
        return []


def bounded_fetch(fetcher, timeout, **request):
    # Only this caller publishes. A late daemon response cannot mutate the cache.
    response = queue.Queue(maxsize=1)
    def work():
        try:
            response.put((True, fetcher(**request)))
        except Exception as exc:
            response.put((False, exc))
    threading.Thread(target=work, daemon=True, name='stock-bars-fetch').start()
    try:
        okay, value = response.get(timeout=timeout)
    except queue.Empty as exc:
        if timeout < FETCH_TIMEOUT:
            raise BatchTimeoutError('整批取数超过 20 秒') from exc
        raise TimeoutError('取数超过 8 秒') from exc
    if not okay:
        raise value
    return value


def publish(target, rows):
    buffer = io.StringIO(newline='')
    writer = csv.writer(buffer)
    writer.writerow(['日期', '收盘', '除权除息'])
    writer.writerows((r['date'], r['close'], '是' if r['ex_rights'] else '') for r in rows)
    pending = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix='.bars-', suffix='.tmp', dir=target.parent, delete=False) as output:
            pending = Path(output.name)
            output.write(buffer.getvalue().encode('utf-8'))
            output.flush()
            os.fsync(output.fileno())
        os.replace(pending, target)
    except OSError as exc:
        raise BarsWriteError(str(exc)) from exc
    finally:
        if pending is not None and pending.exists():
            pending.unlink()


def run(directory, securities, day, *, fetcher=None, monotonic=None, now=None):
    clock = monotonic or time.monotonic
    started = clock()
    errors = {}
    end = date.fromisoformat(day)
    now = now or fetch_index.east8_now()
    now = now.replace(tzinfo=fetch_index.EAST8) if now.tzinfo is None else now.astimezone(fetch_index.EAST8)
    start = (end - timedelta(days=LOOKBACK_DAYS)).isoformat()
    # Distinct product keys can map to one quote (notably the two HK Connect routes).
    by_symbol = {}
    for security in sorted(set(securities)):
        try:
            by_symbol.setdefault(symbol_for(security), []).append(security)
        except ValueError as exc:
            errors[security] = exc
    for symbol, keys in by_symbol.items():
        try:
            close_hour = 16 if symbol.startswith('hk') else 15
            if end == now.date() and (now.hour, now.minute) < (close_hour, 30):
                raise BarsDataError('当日尚未收盘')
            target = Path(directory) / (symbol + '.csv')
            old = read_rows(target)
            if pair_from(old, day):
                continue
            call_started = clock()
            remaining = BATCH_TIMEOUT - (call_started - started)
            if remaining <= 0:
                raise BatchTimeoutError('整批取数超过 20 秒')
            response = bounded_fetch(fetcher or tencent_fetch, min(FETCH_TIMEOUT, remaining),
                                     symbol=symbol, start_date=start, end_date=day)
            completed = clock()
            if completed - started > BATCH_TIMEOUT:
                raise BatchTimeoutError('整批取数超过 20 秒')
            if completed - call_started > FETCH_TIMEOUT:
                raise TimeoutError('取数超过 8 秒')
            fresh = validated_rows(response)
            fresh = [r for r in fresh if start <= r['date'] <= day]
            # Do not fabricate the predecessor by combining an incomplete response with old history.
            if not pair_from(fresh, day):
                raise BarsDataError('缺少成交日或前一交易日收盘；原文件未改动')
            merged = {r['date']: r for r in old}
            merged.update((r['date'], r) for r in fresh)
            publish(target, [merged[d] for d in sorted(merged)])
        except Exception as exc:
            for security in keys:
                errors[security] = exc
    return errors


def failure_message(errors):
    if not errors:
        return ''
    unsupported = {key for key, error in errors.items()
                   if isinstance(error, BarsDataError) and str(error) == '这个市场的日线接口不支持'}
    if unsupported:
        permanent = f'个股日线：{len(unsupported)} 只所在市场没有日线接口，未判追涨杀跌'
        retry = failure_message({key: error for key, error in errors.items() if key not in unsupported})
        return permanent + ('\n' + retry if retry else '')
    reasons, details = [], []
    for error in errors.values():
        if isinstance(error, BarsWriteError):
            # A3 can reload fetch_index; translate using its current exception class.
            error = fetch_index.IndexWriteError(str(error))
        if isinstance(error, BatchTimeoutError):
            reason, detail = '整批取数超过 20 秒', ''
        elif isinstance(error, ValueError) and not isinstance(error, (BarsDataError, json.JSONDecodeError, UnicodeDecodeError)):
            reason = '取数程序出错'
            raw = (type(error).__name__ + (': ' + str(error) if str(error) else '')).replace('\r\n', ' ').replace('\r', ' ').replace('\n', ' ')
            detail = '（系统提示：' + raw[:120] + ('…' if len(raw) > 120 else '') + '）' if raw else ''
        else:
            message = fetch_index.failure_message(error)
            first, _, detail = message.partition('\n')
            reason = first.removeprefix('沪深300 未取到：').removesuffix('，本次页面不含当日指数；稍后重新生成会补上并另出一版')
            reason = reason.replace('指数文件写不进去', '日线文件写不进去')
        if reason not in reasons:
            reasons.append(reason)
        if detail and detail not in details:
            details.append(detail)
    # Count quotes rather than products/routes when more than one key uses the same symbol.
    symbols = set()
    for key in errors:
        try: symbols.add(symbol_for(key))
        except ValueError: symbols.add(key)
    return (f'个股日线：{len(symbols)} 只未取到（' + '；'.join(reasons) +
            '），这几只清仓卖出未判追涨杀跌；稍后重新生成会补上并另出一版' +
            ('\n' + '\n'.join(details) if details else ''))
