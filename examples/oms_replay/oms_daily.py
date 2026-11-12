"""Adapt the contracted three OMS workbooks into as-of manager pages (stdlib only)."""
import argparse
import json
import re
import sys
import zipfile
from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal as D, InvalidOperation
from pathlib import Path
from xml.etree import ElementTree as ET

import replay
import render

ROOT = Path(__file__).resolve().parents[2]
FILES = {'hold': '各产品持仓.xlsx', 'fill': '成交查询.xlsx', 'asset': '资产查询.xlsx'}
NOTES = ['来源为 OMS 导出。仓位和现金占比均以当日总资产为分母；收益率按本币成本计算。',
         '近 60 个估值日高点回撤超过 10% 且最近三日逐日下跌提示组合风险。窗口和阈值均为试用参数。']
NS = {'m': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
ZERO = D(0)
TEXT = {
    'hold': '日期 产品 产品序号 单元 交易市场 证券代码 证券名称 币种 资产类别 证券类别'.split(),
    'fill': '发生日期 产品 产品序号 单元 交易市场 证券代码 证券名称 币种 证券类别 委托方向'.split(),
    'asset': '日期 产品 产品序号'.split(),
}
NUM = {
    'hold': '持仓数量 期初数量 当日买入数量 当日卖出数量 最新价 当日涨跌幅（%） 持仓市值(人民币) 持仓市值（本币） 当前成本（全价本币） 浮动盈亏 当日买金额 当日卖金额'.split(),
    'fill': '成交数量 成交金额 成交金额(RMB) 总费用'.split(),
    'asset': '总资产 现金资产 股票资产 单位净值 单位分红金额 产品份额'.split(),
}
# Optional source columns for explanations and simulation. Never include identity columns.
EXTRA = {
    'hold': {'单元编号', '当前成本（全价人民币）', '当日买入均价', '当日卖出均价'},
    'fill': set(),
    'asset': {'净资产', '证券市值', '累计单位分红金额', '回购资产', '期初投入'},
}


def require(ok, message):
    if not ok:
        raise ValueError(message)


def is_total(row):
    """The real label carries trailing whitespace."""
    return str(row.get('日期') or '').strip() == '汇总'


def number(value):
    try:
        result = D(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError('required numeric cell is not a number') from None
    require(result.is_finite(), 'required numeric cell is not finite')
    return result


def pct(value, base):
    return value * 100 / base


def denominator_assets(account):
    total = account['总资产']
    stock_cash = account['股票资产'] + account['现金资产']
    return stock_cash if stock_cash - total > total * D('0.005') else total


def denominator_net(account):
    assets = denominator_assets(account)
    try:
        net = D(str(account.get('净资产')))
    except (InvalidOperation, ValueError, TypeError):
        return assets
    if not net.is_finite():
        return assets
    net += assets - account['总资产']
    return net if net > 0 else assets


def read_workbook(path, allowed):
    """Retain only allowlisted cells; headers are retained verbatim for simulation."""
    with zipfile.ZipFile(path) as book:
        sheets = ET.fromstring(book.read('xl/workbook.xml')).find('m:sheets', NS)
        require(sheets is not None and len(sheets) == 1, 'expected one worksheet')
        sheet = sheets[0].get('name')
        data = ET.fromstring(book.read('xl/worksheets/sheet1.xml')).find('m:sheetData', NS)
    require(data is not None and len(data) > 0, 'missing workbook header')

    def index(cell):
        match = re.fullmatch(r'([A-Z]+)[0-9]+', cell.get('r', ''))
        require(match is not None, 'invalid cell reference')
        n = 0
        for ch in match[1]:
            n = n * 26 + ord(ch) - 64
        return n - 1

    def value(cell):
        if cell.get('t') == 'inlineStr':
            return ''.join(t.text or '' for t in cell.findall('.//m:t', NS))
        v = cell.find('m:v', NS)
        return v.text if v is not None else None

    head = {index(c): value(c) for c in data[0]}
    require(head and set(head) == set(range(len(head))), 'invalid header layout')
    header = [head[i] for i in range(len(head))]
    require(all(isinstance(h, str) and h for h in header) and len(set(header)) == len(header), 'invalid header names')
    selected = {i: h for i, h in enumerate(header) if h in allowed}
    rows = []
    for line in data[1:]:
        row = {selected[index(c)]: value(c) for c in line if index(c) in selected}
        if any(v is not None and v != '' for v in row.values()):
            rows.append(row)
    return sheet, header, rows


def output_guard(inputs, out):
    target = out.resolve()
    require(not out.is_symlink(), 'output cannot be a symlink')
    for folder in inputs:
        source = folder.resolve()
        require(target != source and source not in target.parents and target not in source.parents,
                'input and output trees must be separate')
    if target == ROOT or ROOT in target.parents:
        require(all((p / 'SYNTHETIC.txt').is_file() for p in inputs),
                'unmarked input cannot write inside the repository')


def load_input(folder):
    tables = {}
    for kind, filename in FILES.items():
        source = folder / filename
        require(source.is_file(), 'missing ' + filename)
        table = read_workbook(source, set(TEXT[kind] + NUM[kind]) | EXTRA[kind])
        require(set(TEXT[kind] + NUM[kind]) <= set(table[1]), 'missing required columns in ' + filename)
        tables[kind] = table
    hold_days = {r['日期'] for r in tables['hold'][2]}
    fill_days = {r['发生日期'] for r in tables['fill'][2]}
    if not hold_days:
        # A completely liquidated account still has a daily asset valuation.
        asset_days = [r['日期'] for r in tables['asset'][2] if not is_total(r)]
        hold_days = fill_days or ({max(asset_days)} if asset_days else set())
    require(len(hold_days) == 1 and (not fill_days or fill_days == hold_days), 'holding and fill dates differ')
    day = next(iter(hold_days))
    require(date.fromisoformat(day).isoformat() == day, 'invalid valuation date')
    return {'day': day, 'tables': tables, 'simulated': (folder / 'SIMULATED.txt').is_file()}


def product_rows(export, kind, fid):
    return [r for r in export['tables'][kind][2] if r.get('产品序号') == fid]


def numeric_rows(rows, kind):
    result = []
    for row in rows:
        clean = {k: row.get(k) for k in TEXT[kind]}
        clean.update({k: number(row.get(k)) for k in NUM[kind]})
        if kind == 'asset':
            clean['净资产'] = row.get('净资产')
        result.append(clean)
    return result


def market_group(market):
    return '沪深' if market in ('上海', '深圳') else '港股通' if market.startswith('港股通') else market


def tell_apart(names):
    """Two listings of one company share a display name, and a page must not read as one position."""
    shared = Counter(names.values())
    return {s: f"{name}（{market_group(s.split('|')[0])}）" if shared[name] > 1 else name for s, name in names.items()}


def security(row):
    require(isinstance(row.get('交易市场'), str) and row['交易市场'] and
            isinstance(row.get('证券代码'), str) and row['证券代码'], 'missing security key')
    return row['交易市场'] + '|' + row['证券代码']


def positions(rows):
    merged = {}
    summed = [k for k in NUM['hold'] if k not in ('最新价', '当日涨跌幅（%）')]
    for row in rows:
        key = security(row)
        require(row['持仓数量'] > 0 and row['最新价'] > 0 and row['持仓市值（本币）'] > 0
                and row['当日涨跌幅（%）'] > -100, 'invalid holding values')
        require(all(row[k] >= 0 for k in ('期初数量', '当日买入数量', '当日卖出数量')), 'negative holding quantity')
        if key not in merged:
            merged[key] = dict(row)
        else:
            old = merged[key]
            require(all(old[k] == row[k] for k in ('最新价', '当日涨跌幅（%）', '币种')), 'inconsistent security marks')
            for col in summed:
                old[col] += row[col]
    return merged


def asset_history(exports, fid, cutoff):
    """A later holding export cannot change a page already visible at cutoff."""
    by_day = {}
    for export in exports:
        if export['day'] > cutoff:
            break
        for row in product_rows(export, 'asset', fid):
            day = row.get('日期')
            if is_total(row) or day > cutoff:
                continue
            date.fromisoformat(day)
            clean = numeric_rows([row], 'asset')[0]
            require(clean['总资产'] > 0 and clean['单位净值'] > 0 and clean['产品份额'] > 0, 'invalid account assets, NAV or shares')
            by_day[day] = clean
    require(cutoff in by_day, 'missing product valuation on holding date')
    return by_day


def account_state(accounts):
    history, event = [], None
    for day, row in sorted(accounts.items()):
        if row['单位分红金额'] != 0:
            history, event = [], None
        history.append((day, row['单位净值']))
        state = replay.fund_state(history)
        event = replay.alert_event(event, state['fund_alert'], state['drawdown_pct'])
    return state, event, history


def build_product(exports, fid):
    # Validate every visible asset row before publishing any page for this product.
    asset_history(exports, fid, exports[-1]['day'])
    output, prior_pos, prior_rates, prior_alerts, segments = [], None, {}, {}, {}
    first_day = exports[0]['day']
    for export in exports:
        day = export['day']
        accounts = asset_history(exports, fid, day)
        account = accounts[day]
        earlier = [d for d in sorted(accounts) if d < day]
        prior_account = accounts[earlier[-1]] if earlier else None
        previous_assets = denominator_assets(prior_account) if prior_account else None
        previous_net = denominator_net(prior_account) if prior_account else None
        state, fund_event, history = account_state(accounts)
        dividend = account['单位分红金额'] != 0
        ret = pct(account['单位净值'] - prior_account['单位净值'], prior_account['单位净值']) if prior_account and not dividend else None
        raw_hold = [r for r in product_rows(export, 'hold', fid) if r.get('资产类别') == '股票资产']
        raw_fills = [r for r in product_rows(export, 'fill', fid) if r.get('证券类别') == '股票']
        held = positions(numeric_rows(raw_hold, 'hold'))
        fills = numeric_rows(raw_fills, 'fill')
        trades = defaultdict(list)
        names = {s: r['证券名称'] for s, r in held.items()}
        for row in fills:
            require(row['委托方向'] in ('买入', '卖出'), 'unknown trade direction')
            require(row['成交数量'] > 0 and row['成交金额'] >= 0 and row['成交金额(RMB)'] >= 0 and row['总费用'] >= 0,
                    'invalid trade values')
            key = security(row)
            trades[key].append(row)
            names.setdefault(key, row['证券名称'])
        names = tell_apart(names)
        assets = denominator_assets(account)
        rates, weights, prior_weights, contribution, protection, alerts = {}, {}, {}, {}, {}, {}
        verified, kinds, approx, cost_nonpositive, same_close, diluted = {}, {}, {}, {}, {}, {}
        reconciled = True
        # Sorted: string hashes change with each process, and the facts must be byte-identical when rerun.
        for s in sorted(set(held) | set(trades) | set(prior_pos or {})):
            p = held.get(s)
            ts = trades.get(s, [])
            buy = sum((t['成交数量'] for t in ts if t['委托方向'] == '买入'), ZERO)
            sell = sum((t['成交数量'] for t in ts if t['委托方向'] == '卖出'), ZERO)
            previous = prior_pos.get(s) if prior_pos is not None else None
            if p:
                opening, qty = p['期初数量'], p['持仓数量']
                okay = opening + p['当日买入数量'] - p['当日卖出数量'] == qty and buy == p['当日买入数量'] and sell == p['当日卖出数量']
                if prior_pos is not None:
                    okay &= opening == (previous['持仓数量'] if previous else ZERO)
                cost = p['当前成本（全价本币）']
                rates[s] = pct(p['浮动盈亏'], cost) if cost > 0 else None
                cost_nonpositive[s] = cost <= 0
                weights[s] = pct(p['持仓市值(人民币)'], assets)
                fx = p['持仓市值(人民币)'] / p['持仓市值（本币）']
                close = p['最新价']
                prior_close = close / (1 + p['当日涨跌幅（%）'] / 100)
                if previous_assets:
                    if opening:
                        prior_weights[s] = pct(opening * prior_close * fx, previous_assets)
                    # Translate security P&L at today's rate; currency appreciation alone contributes zero.
                    pnl = (opening * (close - prior_close) + p['当日买入数量'] * close - p['当日买金额']
                           - p['当日卖出数量'] * close + p['当日卖金额']) * fx - sum((t['总费用'] for t in ts), ZERO)
                    if ret is not None:
                        contribution[s] = pct(pnl, previous_net)
                new_segment = s not in segments
                if new_segment:
                    segments[s] = {'peak': None, 'armed': False, 'complete': opening == 0, 'cold_start': opening > 0}
                seg = segments[s]
                protection[s] = replay.protection_step(seg, close, rates[s])
                protection[s]['cold_start'] = seg['cold_start']
                last_alerts = {} if new_segment else prior_alerts.get(s, {})
                loss = replay.alert_event(last_alerts.get('loss'), rates[s] < -10 if rates[s] is not None else None,
                                          -rates[s] if rates[s] is not None else None)
                if new_segment and opening > 0 and loss['active']:
                    loss['state'] = 'seen'
                guard = replay.alert_event(last_alerts.get('guard') or ({'active': None} if seg['cold_start'] else None),
                                           protection[s]['alert'], protection[s]['drawdown_pct'])
                alerts[s] = {'loss': loss, 'guard': guard}
                same_close[s] = previous is not None and close == previous['最新价']
                diluted[s] = bool(previous and previous['持仓数量'] and cost / qty < previous['当前成本（全价本币）'] / previous['持仓数量'])
            else:
                qty = ZERO
                opening = previous['持仓数量'] if previous else sell - buy
                okay = opening > 0 and opening + buy - sell == 0 and (prior_pos is None or previous is not None)
                if previous_assets:
                    if previous and output:
                        prior_weights[s] = output[-1]['weights'][s]
                    else:
                        prior_weights[s] = pct(sum((t['成交金额(RMB)'] for t in ts if t['委托方向'] == '卖出'), ZERO), previous_assets)
                        approx[s] = True
                segments.pop(s, None)
            reconciled &= okay
            if ts:
                verified[s] = bool(okay)
                kinds[s] = '新开' if opening == 0 else '清仓' if qty == 0 else '加仓' if qty > opening else '减仓' if qty < opening else '调整'
        buys = sum((t['成交金额(RMB)'] for t in fills if t['委托方向'] == '买入'), ZERO)
        sells = sum((t['成交金额(RMB)'] for t in fills if t['委托方向'] == '卖出'), ZERO)
        headline = replay.pick_headline(previous_assets, buys, sells, bool(reconciled), ret, state['fund_alert'], state['drawdown_pct'])
        stock_weight, cash_weight = pct(account['股票资产'], assets), pct(account['现金资产'], assets)
        closed = [s for s, kind in kinds.items() if kind == '清仓']
        gaps = []
        if not state['window_complete']:
            if state['fund_alert']:
                gaps.append('近期净值窗口不足 60 日，实际高点可能更高')
            elif state['fund_alert'] is None:
                gaps.append('近期净值窗口不足 60 日')
        if any(p['cold_start'] and p['alert'] is None for p in protection.values()):
            gaps.append(f'持仓历史从 {render.label(first_day)}起，更早的盈利保护状态未知')
        if not reconciled:
            gaps.append('部分持仓数量变化未对应成交')
        if any(cost_nonpositive.values()):
            gaps.append('部分持仓成本不为正，收益率无法按成本计算')
        other = 100 - stock_weight - cash_weight
        if other > D('0.5'):
            gaps.append(f'股票和现金之外另有 {other:.2f}%')
        if dividend:
            gaps.append('当日分红，净值历史从此日重新起算')
        markets = {s: market_group(p['交易市场']) for s, p in held.items()}
        output.append(dict(date=day, headline=headline, names=names, assets=assets, cash=account['现金资产'], nav=account['单位净值'],
                           daily_return_pct=ret, quantities={s: p['持仓数量'] for s, p in held.items()}, loss_rates=rates,
                           holdings_pnl_pct=sum((contribution[s] for s in sorted(contribution)), ZERO) if contribution else None,
                           cash_excluded=assets != account['总资产'],
                           weights=weights, prior_weights=prior_weights, prior_rates=prior_rates, protection=protection,
                           alert_states=alerts, fund_event=fund_event, stock_weight_pct=stock_weight, cash_weight_pct=cash_weight,
                           prior_stock_weight_pct=pct(prior_account['股票资产'], previous_assets) if prior_account else None,
                           prior_cash_weight_pct=pct(prior_account['现金资产'], previous_assets) if prior_account else None,
                           share_change_pct=pct(account['产品份额'] - prior_account['产品份额'], prior_account['产品份额']) if prior_account else None,
                           kinds=kinds, changes_verified=verified, approx_prior=approx, cost_nonpositive=cost_nonpositive,
                           same_close=same_close, cost_diluted=diluted, contribution_pct=contribution, markets=markets,
                           closed=closed, reconciled=bool(reconciled), coverage='；'.join(gaps) or '完整',
                           partial_reason='当日分红' if dividend else '缺前一日账户估值', **state))
        prior_pos, prior_rates, prior_alerts = held, rates, alerts
    return account['产品'], output


def publish(out, product, rows, simulated):
    title = f'# {product}收盘复盘' + ('（模拟数据）' if simulated else '')
    payload = {'daily.json': (json.dumps(rows, ensure_ascii=False, sort_keys=True, indent=2, default=str) + '\n').encode(),
               'REPORT.md': render.render_report(rows, title=title, notes=NOTES).encode()}
    receipt = {'format': 'oms-daily-v1', 'as_of': rows[-1]['date'], 'sha256': {k: replay.digest(v) for k, v in payload.items()}}
    payload['receipt.json'] = (json.dumps(receipt, sort_keys=True, indent=2) + '\n').encode()
    # Existing reports are replaceable only when their receipt proves tool ownership.
    require(not out.is_symlink(), 'product output cannot be a symlink')
    if out.exists() and any(out.iterdir()):
        require({p.name for p in out.iterdir()} == set(payload), 'unrecognized output directory')
        old = json.loads((out / 'receipt.json').read_text())
        require(old.get('format') == 'oms-daily-v1' and all(not (out / k).is_symlink() and replay.digest((out / k).read_bytes()) == old.get('sha256', {}).get(k)
                                                         for k in ('daily.json', 'REPORT.md')), 'unrecognized output contents')
    out.mkdir(parents=True, exist_ok=True)
    for key, content in payload.items():
        (out / key).write_bytes(content)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', action='append', required=True, type=Path)
    parser.add_argument('--product', required=True)
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args()
    try:
        output_guard(args.input, args.out)
        exports = sorted((load_input(p) for p in args.input), key=lambda e: e['day'])
        require(len({e['day'] for e in exports}) == len(exports), 'duplicate holding date')
        products = {r.get('产品序号') for e in exports for r in e['tables']['asset'][2] if not is_total(r) and r.get('产品序号')}
        require(products, 'no products')
        selected = sorted(products) if args.product == 'all' else [args.product]
    except (ValueError, OSError, KeyError, TypeError, zipfile.BadZipFile, ET.ParseError) as err:
        print(f'REJECTED: {err}', file=sys.stderr)
        print('SUMMARY: products=0 pages=0 rejected=1')
        return 1
    succeeded = pages = rejected = 0
    for fid in selected:
        try:
            require(fid in products, 'unknown product')
            require(fid not in ('.', '..') and '/' not in fid and '\\' not in fid, 'unsafe product path')
            product, rows = build_product(exports, fid)
            publish(args.out / fid, product, rows, any(e['simulated'] for e in exports))
            succeeded += 1
            pages += len(rows)
        except (ValueError, InvalidOperation, OSError, KeyError, TypeError, ArithmeticError) as err:
            # Diagnostic labels only; never print raw input rows or identity values.
            print(f'REJECTED: product {fid}: {err}', file=sys.stderr)
            rejected += 1
    print(f'SUMMARY: products={succeeded} pages={pages} rejected={rejected}')
    return 0 if succeeded else 1


if __name__ == '__main__':
    sys.exit(main())
