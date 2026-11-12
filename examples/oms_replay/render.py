"""Render manager pages from the replay's as-of facts; no state is reconstructed here."""
from collections import defaultdict
import csv
import io
import re
from decimal import Decimal as D

NAMES = {'A': '甲', 'B': '乙', 'C': '丙'}
HEADLINES = {
    'normal': '收盘概况', 'deep_decline': '回撤加深', 'rebound_deep': '回撤中反弹',
    'net_selling': '明显减仓', 'net_buying': '明显加仓', 'rebalancing': '换仓', 'partial': '部分数据待补',
}
DETAIL_CAP = CHANGE_CAP = FOLD_CAP = 5
ZERO = D(0)
LARGE_FLOW_PCT = D('1')
SMALL_WEIGHT_PCT, SMALL_DEEP_PCT = D('1'), D('20')


def dec(value):
    return D(str(value)) if value is not None else ZERO


def p2(value):
    return f'{abs(dec(value)):.2f}'


def signed(value):
    return ('+' if dec(value) >= 0 else '−') + p2(value)


def label(day):
    _, month, dom = day.split('-')
    return f'{int(month)} 月 {int(dom)} 日'


def nm(row, symbol):
    return row.get('names', {}).get(symbol, NAMES.get(symbol, symbol))


def event(row, symbol, kind):
    return row['alert_states'].get(symbol, {}).get(kind, {})


def triggered(row, symbol):
    return any(event(row, symbol, kind).get('active') is True for kind in ('loss', 'guard'))


def alert_phrase(row):
    state = row['fund_event']['state']
    if state == 'cleared':
        return ('连跌中断，' if row['streak'] == 0 else '') + '组合提醒解除'
    return {'first': '首次触发组合提醒', 'resumed': '触发组合提醒',
            'persist': '组合提醒持续', 'worse': '组合提醒恶化'}.get(state, '')


def drawdown_text(row, visible=False):
    if row['drawdown_pct'] is None:
        return '距高点回撤未知'
    lead = f"距 {label(row['peak_date'])}可见高点" if visible else f"较 {label(row['peak_date'])}高点"
    text = f"{lead}回撤 {p2(row['drawdown_pct'])}%"
    if dec(row['record_peak_nav']) > dec(row['peak_nav']):
        text += f"，距有记录以来高点回撤 {p2(row['record_drawdown_pct'])}%"
    return text


def positive_sources(row):
    if 'holdings_pnl_pct' in row or row['daily_return_pct'] is None or dec(row['daily_return_pct']) <= 1:
        return ''
    values = row['contribution_pct']
    top = sorted((s for s,v in values.items() if dec(v)>0), key=lambda s: (-dec(values[s]), s))[:3]
    return '来自' + '、'.join(f'{nm(row, s)} {signed(values[s])}' for s in top) + ' 个百分点' if top else ''


def status(row):
    kind = row['headline']
    nav = f"{dec(row['nav']):.5f}" if row['nav'] is not None else None
    stock, cash = row['stock_weight_pct'], row['cash_weight_pct']
    hold = f'股票 {p2(stock)}%，现金 {p2(cash)}%。' if stock is not None and cash is not None else '股票和现金占比待补。'
    peak, alert, sources = drawdown_text(row), alert_phrase(row), positive_sources(row)
    if kind in ('net_selling', 'net_buying', 'rebalancing'):
        def ratio(value):
            return f'{p2(value)}%' if value is not None else '待补'
        text = f"股票仓位 {ratio(row['prior_stock_weight_pct'])} → {ratio(stock)}，"
        text += f"现金 {ratio(row['prior_cash_weight_pct'])} → {ratio(cash)}。净值 {nav}，{peak}。"
        if alert:
            text += alert + '。'
        if sources:
            text += f"当日 {signed(row['daily_return_pct'])}%，{sources}。"
        return text
    if kind == 'partial':
        if nav is None:
            text = '净值未知（缺当日账户估值），当日收益和连跌日数未知。'
        else:
            text = f"净值 {nav}，当日收益和连跌日数未知（{row.get('partial_reason', '缺前一日账户估值')}）。"
        rule = '组合提醒暂不可判定' if row['fund_alert'] is None else '组合提醒已触发' if row['fund_alert'] else '组合提醒未触发'
        return text + drawdown_text(row, visible=True) + f'，{rule}。' + hold
    if kind == 'deep_decline':
        streak = f"连跌 {row['streak']} 日" if row['streak'] is not None else '连跌日数未知'
        return f"净值 {nav}，当日 {signed(row['daily_return_pct'])}%，{streak}，{peak}，{alert}。{hold}"
    if kind == 'rebound_deep':
        text = f"净值回升 {p2(row['daily_return_pct'])}% 至 {nav}" + (f'，{sources}' if sources else '') + '。'
        return text + (alert + '，' if alert else '') + peak + '。' + hold
    place = '处于近期高点' if row['drawdown_pct'] == 0 and dec(row['record_peak_nav']) <= dec(row['peak_nav']) else peak
    text = f"净值 {nav}，当日 {signed(row['daily_return_pct'])}%" + (f'，{sources}' if sources else '')
    text += f'，{place}。' + (alert + '。' if alert else '')
    if row['fund_alert'] is None:
        text += '组合提醒暂不可判定。'
    return text + hold


def share_note(row):
    """A flow can move NAV and weights without any trade, so say so where those numbers are read."""
    change = dec(row.get('share_change_pct'))
    if not change:
        return ''
    size = f'{abs(change):.2f}%' if abs(change) >= D('0.01') else '不足 0.01%'
    flow = '申购' if change > 0 else '赎回'
    direction = '增加' if change > 0 else '减少'
    if abs(change) < LARGE_FLOW_PCT:
        text = f'今日产品份额{direction} {size}（{flow}），当日净值和仓位变化可能受此影响。'
    else:
        text = f'今日{flow}，产品份额{direction} {size}。当日净值、仓位和现金占比的变化可能受{flow}影响，不全来自持仓盈亏。'
    if row['headline'] not in ('net_selling', 'net_buying', 'rebalancing'):
        text += f"上一日股票 {p2(row['prior_stock_weight_pct'])}%，现金 {p2(row['prior_cash_weight_pct'])}%。"
    return text


def holdings_pnl(row):
    total = row.get('holdings_pnl_pct')
    if total is None:
        return ''
    text = f'**当日盈亏：** 持仓合计 {signed(round(dec(total), 2))} 个百分点。'
    values = {s: dec(v) for s, v in row['contribution_pct'].items() if round(dec(v), 2) != 0}
    parts = []
    for direction, title in ((-1, '拖累最大'), (1, '贡献最大')):
        top = sorted((s for s, v in values.items() if v * direction > 0),
                     key=lambda s: (-abs(values[s]), s))[:3]
        if top:
            parts.append(title + '：' + '、'.join(f'{nm(row, s)} {signed(round(values[s], 2))}' for s in top))
    return text + ('；'.join(parts) + '。' if parts else '')


def changes(row):
    kinds, weights, prior = row['kinds'], row['weights'], row['prior_weights']
    if not kinds:
        return ''
    movement = lambda s: abs(dec(weights.get(s))-dec(prior.get(s)))
    moved = sorted(kinds, key=lambda s: (-movement(s), s))
    parts = []
    for s in moved[:CHANGE_CAP]:
        before, now = prior.get(s), weights.get(s)
        left = f'{p2(before)}%' if before is not None else '0' if kinds[s]=='新开' else '仓位待补'
        if before is not None and row.get('approx_prior', {}).get(s):
            left = '约 ' + left
        right = f'{p2(now)}%' if now is not None else '0' if kinds[s]=='清仓' else '仓位待补'
        tag = {'新开': '（新开）', '清仓': '（清仓）'}.get(kinds[s], '')
        if not row['changes_verified'].get(s):
            tag += '（未核实成交）'
        parts.append(f'{nm(row, s)} {left} → {right}{tag}')
    text = '；'.join(parts) + '。'
    if len(moved)>CHANGE_CAP:
        text += f'其余调整均不超过 {p2(max(movement(s) for s in moved[CHANGE_CAP:]))} 个百分点。'
    return text


def drag_ranks(row):
    if row['daily_return_pct'] is None or dec(row['daily_return_pct']) >= -1:
        return {}
    values = row['contribution_pct']
    ranked = sorted((s for s,v in values.items() if dec(v)<0 and s in row['quantities']), key=lambda s: (dec(values[s]),s))
    return {s:i+1 for i,s in enumerate(ranked[:3])}


def depth(row, s):
    values = [ZERO, -dec(row['loss_rates'].get(s))]
    if row['protection'][s]['alert'] is True:
        values.append(dec(row['protection'][s]['drawdown_pct']))
    if s in drag_ranks(row) and dec(row['prior_weights'].get(s))>0:
        values.append(abs(dec(row['contribution_pct'][s]))*100/dec(row['prior_weights'][s]))
    return max(values)


def rank(row, names):
    """Weight x severity respects dominance and resolves crossed exposures."""
    return sorted(names, key=lambda s: (-dec(row['weights'].get(s))*depth(row,s), -dec(row['weights'].get(s)),s))


def candidates(row):
    held, rates, weights = set(row['quantities']), row['loss_rates'], row['weights']
    chosen = {s for s in held if triggered(row,s)}
    chosen |= {s for s in held if s in row['kinds'] and (dec(rates.get(s))<0 or triggered(row,s))}
    chosen |= {s for s in held if any(event(row,s,k).get('state')=='cleared' for k in ('loss','guard'))}
    chosen |= {s for s in held if (rates.get(s) is None and not row.get('cost_nonpositive', {}).get(s))
               or weights.get(s) is None
               or (row['protection'][s]['alert'] is None and not row['protection'][s].get('cold_start'))}
    chosen |= set(drag_ranks(row))
    losers = [s for s in held if dec(rates.get(s))<0]
    first = {a for a in losers if not any(
        dec(weights.get(b)) >= dec(weights.get(a)) and dec(rates[b]) <= dec(rates[a])
        and (dec(weights.get(b)) > dec(weights.get(a)) or dec(rates[b]) < dec(rates[a])) for b in losers if b!=a)}
    return chosen | first, first


def small_deep(row):
    """Held, under 1% and more than 20% down on cost: its own line, apart from the account-level list. Top drags stay listed."""
    weights, rates, drags = row['weights'], row['loss_rates'], drag_ranks(row)
    picked = [s for s in row['quantities'] if s not in drags and weights.get(s) is not None and rates.get(s) is not None
              and dec(weights[s]) < SMALL_WEIGHT_PCT and -dec(rates[s]) > SMALL_DEEP_PCT]
    return sorted(picked, key=lambda s: (dec(rates[s]), -dec(weights[s]), s))


def became_largest(row):
    weights, prior = row['weights'], row['prior_weights']
    if not prior or not weights:
        return None
    now = min(weights, key=lambda s: (-dec(weights[s]),s))
    before = min(prior, key=lambda s: (-dec(prior[s]),s))
    return now if now!=before else None


def holding_pair(row, s):
    weight, rate = row['weights'].get(s), row['loss_rates'].get(s)
    w = f'仓位 {p2(weight)}%' if weight is not None else '仓位待补'
    r = ('浮亏' if dec(rate)<0 else '浮盈') + f' {p2(rate)}%' if rate is not None else '浮亏率待补'
    if rate is None and row.get('cost_nonpositive', {}).get(s):
        r = '成本不为正'
    return f'{w}，{r}'


def item_text(row, s, first_group):
    rate = row['loss_rates'].get(s)
    why = []
    if became_largest(row)==s:
        why.append('成为第一大持仓')
    kind = row['kinds'].get(s)
    loss, guard = event(row,s,'loss'), event(row,s,'guard')
    prior_rate = row['prior_rates'].get(s)
    diluted = (kind=='加仓' and prior_rate is not None and rate is not None and dec(prior_rate)<dec(rate)<0
               and row['cost_diluted'].get(s))
    if diluted:
        tail = '，收盘价未变' if row['same_close'].get(s) else ''
        cleared = '，退回亏损线内' if loss['state']=='cleared' else ''
        why.append(f'今日加仓，浮亏率从 {p2(prior_rate)}% 降到 {p2(rate)}%，来自成本摊薄{tail}{cleared}')
    elif kind:
        why.append('今日' + kind)
    ranks = drag_ranks(row)
    if s in ranks:
        value = row['contribution_pct'][s]
        majority = abs(dec(value)) > abs(dec(row['daily_return_pct']))/2
        word = {1:'今日最大拖累',2:'今日第二大拖累',3:'今日第三大拖累'}[ranks[s]]
        why.append(f'{word} {p2(value)} 个百分点' + ('，跌幅过半来自它' if majority else ''))
    state = loss.get('state')
    if state=='cleared' and not diluted:
        why.append('退回亏损线内')
    elif loss.get('active') is True:
        why.append({'seen':'已过亏损线','first':'首次过亏损线','resumed':'触发亏损线提醒','worse':'亏损线提醒恶化',
                    'persist':f"亏损线提醒第 {loss['days']} 日"}[state])
    if guard.get('state')=='cleared':
        why.append('盈利保护提醒解除')
    elif guard.get('active') is True:
        prefix = {'first':'首次进入盈利保护提醒','resumed':'进入盈利保护提醒','worse':'盈利保护提醒恶化',
                  'persist':f"盈利保护提醒第 {guard['days']} 日"}[guard['state']]
        why.append(prefix + f"，较持有期高点回撤 {p2(row['protection'][s]['drawdown_pct'])}%")
    elif row['protection'][s]['missing_close']:
        why.append('缺当日行情，盈利保护暂不可判定')
    elif guard.get('active') is None and not row['protection'][s].get('cold_start'):
        why.append('持有期行情不全，盈利保护暂不可判定')
    if not why and s in first_group:
        why.append('未过亏损线')
    if not why:
        why.append('持仓数据待补')
    return f'**{nm(row, s)}**（{holding_pair(row,s)}）：' + '；'.join(why) + '。'


def structure(row):
    weights = sorted(((dec(v),s) for s,v in row['weights'].items() if v is not None), key=lambda x:(-x[0],x[1]))
    stock = row['stock_weight_pct']
    if not weights or not stock:
        return ''
    top = f'第一大 {nm(row, weights[0][1])} {p2(weights[0][0])}%（占股票持仓 {p2(weights[0][0]*100/dec(stock))}%）'
    parts = [top]
    if len(weights)>3:
        parts.append(f'前三 {p2(sum(w for w,_ in weights[:3]))}%')
    if len(weights)>5:
        parts.append(f'前五 {p2(sum(w for w,_ in weights[:5]))}%')
    if row['markets'] and all(s in row['markets'] for _,s in weights):
        totals = defaultdict(D)
        for w,s in weights:
            totals[row['markets'][s]] += w
        if len(totals)>1:
            parts.append('，'.join(f'{m} {p2(v)}%' for m,v in sorted(totals.items(), key=lambda x:(-x[1],x[0]))))
    return '；'.join(parts) + '。'


def render_day(row):
    chosen, first = candidates(row)
    apart = small_deep(row)
    ordered = rank(row, chosen - set(apart))
    parts = [f"### {label(row['date'])}收盘｜{HEADLINES[row['headline']]}"]
    pnl = holdings_pnl(row)
    if row.get('cash_excluded'):
        note = '数据提示：OMS 总资产没有计入现金，净值、回撤和组合提醒可能不准'
        parts.append(note + ('，当日表现看持仓盈亏。' if pnl else '。'))
    if abs(dec(row.get('share_change_pct'))) >= LARGE_FLOW_PCT:
        parts.append(share_note(row))
    small_flow = share_note(row) if abs(dec(row.get('share_change_pct'))) < LARGE_FLOW_PCT else ''
    parts.append(status(row) + small_flow)
    if pnl:
        parts.append(pnl)
    struct = structure(row)
    if struct:
        parts.append('**持仓结构：** ' + struct)
    change = changes(row)
    if change:
        parts.append('**持仓变化：** ' + change)
    parts.append('**需要注意（按重要程度）**')
    parts.append('\n'.join(f'{i}. {item_text(row,s,first)}' for i,s in enumerate(ordered[:DETAIL_CAP],1))
                 if ordered else '今日没有需要注意的事项。')
    folded = [s for s in ordered[DETAIL_CAP:] if triggered(row,s)]
    shown = sorted(folded[:FOLD_CAP], key=lambda s:(-dec(row['weights'].get(s)),s))
    if shown:
        line = '另有已触发提醒：' + '；'.join(f'{nm(row, s)} {holding_pair(row,s)}' for s in shown) + '。'
        rest = folded[FOLD_CAP:]
        if rest:
            deepest = min(dec(row['loss_rates'].get(s)) for s in rest)
            line += f"其余已触发持仓合计仓位 {p2(sum(dec(row['weights'].get(s)) for s in rest))}%"
            line += (f'，最深浮亏 {p2(deepest)}%' if deepest < 0 else '') + '。'
        parts.append(line)
    if apart:
        trade = lambda s: f"（今日{row['kinds'][s]}）" if s in row['kinds'] else ''
        parts.append(f'**小仓位深亏（仓位不足 {SMALL_WEIGHT_PCT}%，浮亏超过 {SMALL_DEEP_PCT}%）：** '
                     + '；'.join(f'{nm(row, s)} {holding_pair(row, s)}{trade(s)}' for s in apart) + '。')
    gaps = row['coverage'] if row['coverage']!='完整' else ''
    # The partial lead already describes missing account/return/streak data.
    if row['headline']=='partial':
        gaps = '；'.join(g for g in gaps.split('；') if g not in (
            '缺当日 OMS 账户估值','当日归因覆盖不足','缺当日持仓快照','近期窗口缺估值日，连续回撤规则待定'))
    if gaps:
        parts.append('**数据覆盖：** ' + gaps + '。')
    return '\n\n'.join(parts)


def render_report(rows, title=None, notes=None):
    parts = [title if title is not None else '# 合成账户收盘复盘']
    parts.extend(notes if notes is not None else [
             '来源为模拟 OMS 账户、持仓、成交及收盘价。各估值日截至 16:00（+08:00），仓位和现金占比均以当日总资产为分母。',
             '本次试算以近 60 个估值日高点回撤超过 10% 且最近三日逐日下跌提示组合风险。窗口和阈值均为试用参数。'])
    parts.extend(render_day(row) for row in rows[-3:])
    if not rows:
        parts.append('缺少收盘估值日，账户状态待补。')
    return '\n\n'.join(parts) + '\n'


CSV_HEADER = ('序号 股票代码 股票名称 交易市场 成本价 数量 成本 收盘价 市值 总资产占比 总持仓占比 '
              '浮动盈亏 浮盈率 交易单元 当日买入量 当日买入价 当日卖出量 当日卖出价 风控提示').split()


def csv_text(value):
    """Keep OMS text literal when a spreadsheet imports formulas from CSV."""
    if isinstance(value, str) and re.match(r'[\s\ufeff]*[=+@＝＋－＠-]', value):
        return "'" + value
    return value


def csv_overview(page):
    """Split the already-rendered page; keep its wording and paragraph order."""
    paragraphs = page.split('\n\n')
    rows = [['产品概览', paragraphs[0].removeprefix('### ')]]
    attention = False
    for paragraph in paragraphs[1:]:
        if paragraph == '**需要注意（按重要程度）**':
            attention = True
        elif attention:
            rows.extend(['需要注意', re.sub(r'^\d+\. ', '', line).replace('**', '')]
                        for line in paragraph.splitlines())
            attention = False
        else:
            heading = re.fullmatch(r'\*\*(.+?)：\*\* (.*)', paragraph, re.S)
            if heading:
                rows.append([heading[1], heading[2].replace('**', '')])
            elif paragraph.startswith(('另有已触发提醒：', '数据提示：')):
                rows.append(paragraph.replace('**', '').split('：', 1))
            else:
                rows.append(['账户状态', paragraph.replace('**', '')])
    return [[csv_text(cell) for cell in row] for row in rows]


def csv_nav(assets):
    dates = sorted(assets)
    rows = [['近5日', '日期'], ['', '单位净值'], ['', '当日涨跌']]
    for index in range(max(0, len(dates) - 5), len(dates)):
        day = dates[index]
        current = assets[day]
        nav = current['单位净值']
        prior = assets[dates[index - 1]]['单位净值'] if index else ZERO
        change = f'{(nav / prior - 1) * 100:+.2f}%' if prior > 0 and current['单位分红金额'] == 0 else '未知'
        for row, value in zip(rows, (day, f'{nav:.4f}', change)):
            row.append(value)
    return rows


def csv_holdings(holdings):
    groups = {}
    summed = ('持仓数量', '当前成本（全价人民币）', '当前成本（全价本币）', '持仓市值(人民币)',
              '浮动盈亏', '当日买入数量', '当日卖出数量')
    for holding in holdings:
        unit = '两融' if '两融' in str(holding.get('单元') or '') else '普通'
        key = (holding['交易市场'], holding['证券代码'], unit)
        if key not in groups:
            groups[key] = {**holding, **dict.fromkeys(summed, ZERO), 'buy_value': ZERO, 'sell_value': ZERO}
        group = groups[key]
        for field in summed:
            group[field] += holding[field]
        group['buy_value'] += holding['当日买入数量'] * holding['当日买入均价']
        group['sell_value'] += holding['当日卖出数量'] * holding['当日卖出均价']
    total = sum((g['持仓市值(人民币)'] for g in groups.values()), ZERO)
    rows = []
    ordered = sorted(groups, key=lambda key: (-groups[key]['持仓市值(人民币)'], key[1], key[2], key[0]))
    for index, (market, code, unit) in enumerate(ordered, 1):
        group = groups[market, code, unit]
        qty, cost, local = (group[field] for field in ('持仓数量', '当前成本（全价人民币）', '当前成本（全价本币）'))
        mv, pnl = group['持仓市值(人民币)'], group['浮动盈亏']
        close = group['最新价']
        rounded = close.quantize(D('0.0001'))
        places = 2 if rounded * 100 == (rounded * 100).to_integral_value() else 3
        row = [str(index), code, group['证券名称'], market, f'{cost / qty:.3f}', f'{qty:.0f}',
               f'{cost:.2f}', f'{close:.{places}f}', f'{mv:.2f}', '',
               f'{mv / (total or 1) * 100:.1f}%', f'{pnl:.2f}',
               f'{pnl / local * 100:.1f}%' if local > 0 else '', unit]
        for quantity, value in (('当日买入数量', 'buy_value'), ('当日卖出数量', 'sell_value')):
            q = group[quantity]
            row.extend([f'{q:.0f}', f'{group[value] / q:.{places}f}'] if q else ['', ''])
        rows.append(row)
    return rows, total


def csv_asset_ratio(amount, ending):
    return f'=TEXT(IF({ending}=0,0,{amount}/{ending}),"0.0%")'


def csv_tail(asset, total, first_row):
    """first_row is the sheet row of the blank line preceding the totals."""
    holdings = f'I{first_row + 1}'
    outside = f'I{first_row + 5}'
    cash = f'I{first_row + 6}'
    repo = f'I{first_row + 7}'
    cash_total = f'I{first_row + 8}'
    account_row = first_row + 9
    ending = f'E{account_row}'
    profit = f'={ending}-C{account_row}'
    tail = [
        [],
        ['', '持仓总计', '', '', '', '', '', '', f'{total:.2f}', csv_asset_ratio(holdings, ending)],
        ['现金', '场外资产', '名称代码', '股票名称', '申购价', '申购数量', '', '', '冻结金额', '总资产占比'],
        ['', '', '', '场外货基'],
        [],
        ['', '', '小计', '', '', '', '', '', f'=SUM(I{first_row + 3}:I{first_row + 4})'],
        ['', '可用现金', '', '', '', '', '', '', f"{asset['现金资产']:.2f}", csv_asset_ratio(cash, ending)],
        ['', '逆回购', '', '', '', '', '', '', f"{asset['回购资产']:.2f}"],
        ['', '现金合计', '', '', '', '', '', '', f'={cash}+{repo}+{outside}', csv_asset_ratio(cash_total, ending)],
        ['期初资产', '', f"{asset['期初投入']:.2f}", '期末资产', f'={holdings}+{cash_total}-L{account_row}',
         '当期盈亏：', profit, '当前净值', f"={ending}/{asset['产品份额']:f}", '', '当日负债',
         f"{asset['总资产'] - asset['净资产']:.2f}"],
        ['现金分红（万）', '', '', '管理费', '', '累计总盈亏', profit, '累计收益率'],
        ['指数收益率', '', '', '', '', '基金总盈亏', profit],
        ['风控预警', '', '', '总仓位：'],
        ['', '', '', '个股仓位：'],
        ['风控触发', '', '', '总盈亏：'],
        ['', '', '', '个股盈亏：'],
    ]
    return tail, ending


def csv_risk_rows(row):
    ratio = lambda value: f'{p2(value)}%' if value is not None else '未知'
    weights = {s: dec(w) for s, w in row['weights'].items() if w is not None and dec(w) > 0}
    top = sorted(weights, key=lambda s: (-weights[s], s))
    concentration = (f'第一大 {nm(row, top[0])} {p2(weights[top[0]])}%，'
                     f'前五 {p2(sum(weights[s] for s in top[:5]))}%' if top else '无股票持仓')
    state = row['fund_event']['state']
    phrase = {'unknown': '组合提醒无法判定', 'off': '未触发组合提醒',
              'persist': f"组合提醒持续第 {row['fund_event']['days']} 日"}.get(state) or alert_phrase(row)
    hit = [s for s in row['alert_states'] if triggered(row, s)]
    holding_risk = '无'
    if hit:
        holding_risk = f'已触发持仓合计仓位 {p2(sum(weights.get(s, ZERO) for s in hit))}%'
        rates = [dec(row['loss_rates'][s]) for s in hit if row['loss_rates'].get(s) is not None]
        if rates and min(rates) < 0:
            holding_risk += f'，最深浮亏 {p2(min(rates))}%'
    return [f"股票 {ratio(row['stock_weight_pct'])}，现金 {ratio(row['cash_weight_pct'])}",
            concentration, f'{phrase}；{drawdown_text(row)}', holding_risk]


def csv_risk_hints(row):
    chosen, first = candidates(row)
    apart = set(small_deep(row))
    visible = set(rank(row, chosen - apart)[:DETAIL_CAP])
    hints = {}
    for symbol in row['quantities']:
        reason = lambda: item_text(row, symbol, first).split('）：', 1)[1].removesuffix('。')
        if symbol in visible:
            hints[symbol] = reason()
        elif symbol in apart or triggered(row, symbol):
            parts = ['小仓位深亏'] if symbol in apart else []
            if triggered(row, symbol):
                parts.append(reason())
            hints[symbol] = '；'.join(parts)
    return hints


def product_csv(name, day, page=None, facts=None, holdings=None, assets=None, *, rejected=None):
    """Render one product's sheet from its page, facts and normalized OMS cells."""
    rows = [[csv_text(name)], ['基金持仓'], ['日期', day, '基金经理：']]
    if rejected is not None:
        rows.append(['产品概览', f'今日没有持仓表：数据未通过校验（{rejected}）。'])
    else:
        overview = csv_overview(page)
        rows.extend([overview[0], *csv_nav(assets), *overview[1:], [], CSV_HEADER])
        body, total = csv_holdings(holdings)
        tail, ending = csv_tail(assets[day], total, len(rows) + len(body) + 1)
        hints = csv_risk_hints(facts)
        for row_number, holding in enumerate(body, len(rows) + 1):
            holding[9] = csv_asset_ratio(f'I{row_number}', ending)
            hint = hints.get(f'{holding[3]}|{holding[1]}', '')
            holding.append(csv_text(hint))
            # Resolve the risk hint with the original market and security code first.
            holding[1:4] = [csv_text(cell) for cell in holding[1:4]]
        rows.extend(body)
        for line, text in zip(tail[-4:], csv_risk_rows(facts)):
            line.append(text)
        rows.extend(tail)
    output = io.StringIO(newline='')
    writer = csv.writer(output, lineterminator='\r\n')
    writer.writerows(row + [''] * (len(CSV_HEADER) - len(row)) for row in rows)
    return output.getvalue().removesuffix('\r\n').encode('utf-8')
