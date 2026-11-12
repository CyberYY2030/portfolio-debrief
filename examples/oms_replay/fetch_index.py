"""Fetch CSI 300 closes with stdlib; offline callers inject transport and clock."""
import argparse
import csv
import io
import json
import queue
import threading
import urllib.request
from urllib.error import HTTPError, URLError
from http.client import HTTPException
import os
import tempfile
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path


EAST8 = timezone(timedelta(hours=8))
FETCH_TIMEOUT = 8


class IndexWriteError(OSError):
    """Distinguish index publication from transport failures without losing OSError compatibility."""


def failure_message(error):
    """Console/dialog only; never include these diagnostics in persisted artifacts."""
    raw = str(error)
    if isinstance(error, IndexWriteError):
        reason = '指数文件写不进去（可能正被其他程序打开）'
    elif isinstance(error, TimeoutError) or (isinstance(error, URLError) and isinstance(error.reason, TimeoutError)):
        reason = '取数超过 8 秒'
    elif isinstance(error, HTTPError):
        reason = f'行情接口返回 HTTP {error.code}'
    elif isinstance(error, (OSError, HTTPException)):
        reason = '连不上行情接口（网络、代理或证书问题）'
    elif isinstance(error, (json.JSONDecodeError, UnicodeDecodeError)):
        reason = '行情接口返回的不是预期格式'
    elif isinstance(error, ValueError):
        reason, raw = str(error), ''
    else:
        reason = '取数程序出错'
        raw = type(error).__name__ + (': ' + raw if raw else '')
    message = ('沪深300 未取到：' + reason +
               '，本次页面不含当日指数；稍后重新生成会补上并另出一版')
    raw = raw.replace('\r\n', ' ').replace('\r', ' ').replace('\n', ' ')
    if raw and raw != reason:
        message += '\n（系统提示：' + raw[:120] + ('…' if len(raw) > 120 else '') + '）'
    return message


def east8_now():
    return datetime.now(EAST8)


def tencent_fetch(*, index_code, start_date, end_date, opener=None):
    """The transport accepts only the fixed index and validated calendar dates."""
    if index_code != '000300':
        raise ValueError('仅支持沪深300')
    start, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
    if start > end:
        raise ValueError('起始日期晚于截止日期')
    count = min((end-start).days+1, 2000)
    url = ('https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param='
           f'sh000300,day,{start.isoformat()},{end.isoformat()},{count},')
    request = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with (opener or urllib.request.urlopen)(request, timeout=FETCH_TIMEOUT) as response:
        payload = json.load(response)
    try:
        if payload['code'] != 0:
            raise ValueError('指数接口返回失败')
        rows = payload['data']['sh000300']['day']
        if not isinstance(rows, list) or not rows:
            raise ValueError('指数接口没有日线数据')
        if any(not isinstance(row, list) or len(row) != 6 for row in rows):
            raise ValueError('指数日线格式不符')
        return [{'trade_date': row[0], 'close': row[2]} for row in rows]
    except (KeyError, TypeError) as exc:
        raise ValueError('指数 JSON 结构不符') from exc


def bounded_fetch(fetcher, **request):
    # Socket timeouts alone do not bound DNS or a slow streaming response.
    # The worker only returns data: after timeout it can never publish a file.
    response = queue.Queue(maxsize=1)
    def work():
        try:
            response.put((True, fetcher(**request)))
        except Exception as exc:
            response.put((False, exc))
    threading.Thread(target=work, daemon=True, name='csi300-fetch').start()
    try:
        okay, value = response.get(timeout=FETCH_TIMEOUT)
    except queue.Empty as exc:
        raise TimeoutError('取数超过 8 秒') from exc
    if not okay:
        raise value
    return value


def run(index_dir, start_date, *, fetcher=None, now=None):
    """Validate the entire response before replacing the previous complete CSV."""
    start=date.fromisoformat(start_date)
    clock=now if now is not None else east8_now()
    if clock.tzinfo is None:
        raise ValueError('取数时钟必须带时区')
    clock=clock.astimezone(EAST8);today=clock.date()
    if start>today:
        raise ValueError('起始日期晚于上海当前日期')
    directory=Path(index_dir);target=directory/'沪深300.csv'
    if (directory/'沪深300.xlsx').exists():
        raise ValueError('指数目录已有沪深300.xlsx；未写入 CSV，避免两种格式并存')
    response=bounded_fetch(fetcher or tencent_fetch,index_code='000300',start_date=start.isoformat(),end_date=today.isoformat())
    if not isinstance(response,(list,tuple)) or not response:
        raise ValueError('取数结果为空或格式无效；原文件未改动')
    rows=[];previous=None
    for record in response:
        try:
            day=date.fromisoformat(str(record['trade_date']))
            close=Decimal(str(record['close']))
        except (KeyError,TypeError,ValueError,InvalidOperation) as exc:
            raise ValueError('指数日期或收盘格式无效；原文件未改动') from exc
        if previous is not None and day<=previous:
            raise ValueError('指数日期重复或不升序；原文件未改动')
        if not close.is_finite() or close<=0:
            raise ValueError('指数收盘必须为正数；原文件未改动')
        previous=day
        if day<start or day>today:
            continue
        if day==today and clock.time()<time(15,30):
            continue
        rows.append((day.isoformat(),format(close,'f')))
    if not rows:
        raise ValueError('日期范围内没有已收盘指数数据；原文件未改动')
    buffer=io.StringIO(newline='');writer=csv.writer(buffer)
    writer.writerow(['日期','收盘']);writer.writerows(rows)
    payload=buffer.getvalue().encode('utf-8')
    try:
        if target.is_file() and target.read_bytes()==payload:
            return target
        directory.mkdir(parents=True,exist_ok=True)
        pending=None
        try:
            with tempfile.NamedTemporaryFile(prefix='.index-',suffix='.tmp',dir=directory,delete=False) as output:
                pending=Path(output.name)
                output.write(payload);output.flush();os.fsync(output.fileno())
            os.replace(pending,target)
        finally:
            if pending is not None and pending.exists():
                pending.unlink()
    except OSError as exc:
        raise IndexWriteError(str(exc)) from exc
    return target


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--index-dir',type=Path,required=True)
    parser.add_argument('--start-date',required=True,help='YYYY-MM-DD')
    args=parser.parse_args()
    try:
        target=run(args.index_dir,args.start_date)
    except Exception as exc:
        parser.exit(1,failure_message(exc)+'\n')
    print(f'INDEX FETCH: {target}')


if __name__=='__main__':
    main()
