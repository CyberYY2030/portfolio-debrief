"""Local twenty-day behavior review, consuming the accepted homepage replay."""
from decimal import Decimal as D
from hashlib import sha256
import re

import homepage_v2 as hp
import fetch_bars
import oms_daily as od

ZERO = D(0)
WINDOW = 20
LONG_WINDOW = 60
AFTER_DAYS = 5
DEEPER = D(2)
HOLDINGS = 3
UNITS = D(1)
CHASE_SHARE = D(15)
CHASE_DAYS = 5
CHASE_TRADE_DAYS = 10
UNJUDGED = D(1) / 3
NEW_LOSS = 5
TURNOVER = D(10)
NAMES = D(20)
TITLES = {'A': '回撤提醒后的仓位', 'B': '浮亏警示后的处置', 'C': '追涨杀跌', 'D': '交易频率'}
HEADER = ['项目', '结论', '行为（当时的操作）', '结果（之后的净值和价格）']
EMPTY = '窗口内没有股票持仓，也没有成交，本页没有可评价的内容。'
FIRST = '账本只有首日，从第二个有账本的交易日起出现内容。'
FAILED = '本页未能生成，首页不受影响。'


def storage_name(product):
    """Product identifiers are data, never directories or path components."""
    safe = re.fullmatch(r'[A-Z0-9_-]{1,32}', product) and not re.fullmatch(r'CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9]', product)
    return product if safe else 'product-' + sha256(product.encode('utf-8')).hexdigest()


def parameters():
    return {key: globals()[key] for key in ('WINDOW', 'LONG_WINDOW', 'AFTER_DAYS', 'DEEPER', 'HOLDINGS',
            'UNITS', 'CHASE_SHARE', 'CHASE_DAYS', 'CHASE_TRADE_DAYS', 'UNJUDGED', 'NEW_LOSS', 'TURNOVER', 'NAMES')} | {'CONCLUSION_FLOW': hp.CONCLUSION_FLOW}


def pct(value, signed=False):
    return hp.fmt(value, signed=signed)


def pp(value):
    return hp.fmt(value, ' 个百分点')


def row(key, verdict, behavior, outcome='—', *, facts=None, examples=()):
    return dict(key=key, verdict=verdict, behavior=behavior, result=outcome,
                facts={} if facts is None else facts, examples=list(examples))


def episodes(result, obj, rule):
    groups = {}
    for state in result['timeline']:
        if state['object'] != obj or state['rule'] != rule or not state.get('start'):
            continue
        start = state['start']
        if start not in groups:
            if not state['active']:
                continue
            groups[start] = []
        if not groups[start] or groups[start][-1]['active']:
            groups[start].append(state)
    return list(groups.values())


def window_facts(result, index, bars_dir=None):
    calendar = sorted(d for d in result['accounts'] if d <= result['as_of'])
    ledger_days = sorted(d for d, data in result['days'].items()
                         if d <= result['as_of'] and data['held'] is not None)
    days = ledger_days[1:][-WINDOW:]
    if not days:
        return None
    base = max(d for d in calendar if d < days[0])
    pending = set()
    trades = {}; used_bars = {}; fees = ZERO
    for day in days:
        data = result['days'][day]
        amount = sum((t['总费用'] for ts in (data['trades'] or {}).values() for t in ts), ZERO)
        fees += od.pct(amount, od.denominator_assets(data['account'])) or ZERO
        if data['trades'] is None or data['facts'] is None:
            pending.add(day)
            continue
        pairs = {}
        for symbol in data['trades']:
            if symbol not in data['held'] and data['verified'].get(symbol, False):
                pairs[symbol] = (fetch_bars.read_pair(bars_dir, symbol, day) if bars_dir is not None
                                 else result.get('bars', {}).get(symbol, []) if day == result['as_of'] else [])
        if pairs:
            used_bars[day] = pairs
        items = hp.trade_facts(dict(result, as_of=day, bars=pairs))
        if items:
            trades[day] = items
    dividend = any(a['单位分红金额'] for d, a in result['accounts'].items() if base < d <= result['as_of'])
    start_nav = result['accounts'][base]['单位净值']; end_nav = result['accounts'][result['as_of']]['单位净值']
    nav = (end_nav / start_nav - 1) * 100 if not dividend and start_nav and end_nav is not None else None
    index_value = hp.index_return(index, base, result['as_of'])
    return dict(days=days, base=base, as_of=result['as_of'], trades=trades, pending=sorted(pending),
                nav=nav, dividend=dividend, fees=fees, index=index_value, has_index=index_value is not None,
                nav_days=len(calendar), held_days=sum(bool(result['days'][d]['held']) for d in days),
                name=result['name'], product=result['product'], bars=used_bars)


def drawdown_fact(result, states):
    start = states[0]['start']; end = states[-1]['date']
    action = hp.operation_facts(dict(result, as_of=end), start)
    weights = []
    for day in (start, end):
        account = result['accounts'].get(day)
        weights.append(od.pct(account['股票资产'], od.denominator_assets(account)) if account else None)
    metrics = [s['metric'] for s in states if s['metric'] is not None]
    first = metrics[0] if metrics else None; deepest = max(metrics) if metrics else None
    first_tier = states[0]['tier'] or 1; highest = states[-1]['highest']
    deeper = deepest - first if first is not None and deepest is not None else None
    bad = highest > first_tier or deeper is not None and deeper >= DEEPER
    known = action['status'] == 'known' and action['net'] is not None and all(v is not None for v in weights)
    label = None
    if known:
        net = action['net']; delta = weights[1] - weights[0]
        label = ('不降反升' if net >= hp.CONCLUSION_FLOW and delta >= hp.CONCLUSION_FLOW
                 else '已减仓' if net <= -hp.CONCLUSION_FLOW and delta <= -hp.CONCLUSION_FLOW else '未减仓')
    after = len(states) - 1
    if after < AFTER_DAYS:
        verdict = '样本不足'
    elif not known:
        verdict = '待核实'
    elif label == '已减仓':
        verdict = '未见'
    else:
        verdict = '缺陷' if bad else '特征'
    return dict(start=start, end=end, after=after, weight=weights, net=action['net'], status=action['status'],
                action=label, first_metric=first, deepest=deepest, first_tier=first_tier, highest=highest,
                bad=bad, ongoing=end == result['as_of'] and states[-1]['active'], verdict=verdict,
                origin_unknown=states[0]['origin_unknown'], last=states[-1]['status'], inside=states[-1]['inside'],
                pending=drawdown_pending(states))


def drawdown_pending(states):
    missing = [i for i, state in enumerate(states) if state['metric'] is None]
    if not missing:
        return ''
    if missing == list(range(missing[0], len(states))):
        return f'；{states[missing[0]]["date"][5:]} 起回撤待核实'
    return f'；其中 {len(missing)} 个估值日回撤待核实'


def row_drawdown(result, w):
    lines = result['rules']['fund']
    facts = [drawdown_fact(result, states) for states in episodes(result, 'fund', 'fund')
             if states[-1]['date'] > w['base']]
    if not facts:
        states = [s for s in result['timeline'] if s['object'] == 'fund' and s['rule'] == 'fund'
                  and w['base'] < s['date'] <= w['as_of']]
        return row('A', '未触发', f'净值未跌破 {lines[0]}% 警示线' + drawdown_pending(states), facts=[])
    behaviors = []; outcomes = []
    for f in facts:
        level = lines[f['first_tier']-1]
        lead = (f'账本首日（{f["start"][5:]}）已超 {level}% 警示线，此前的操作未知'
                if f['origin_unknown'] else f'{f["start"][5:]} 首次跌破 {level}% 警示线')
        if f['after'] < AFTER_DAYS and f['action'] is None:
            text = f'{lead}，之后只有 {f["after"]} 个估值日，满 {AFTER_DAYS} 个才下结论'
        elif f['action'] is None:
            if f['status'] == 'known' and f['net'] is not None:
                text = lead + '，之后的仓位变化待核实'
            elif all(v is not None for v in f['weight']):
                text = f'{lead}，之后 {f["after"]} 个估值日股票仓位由 {pct(f["weight"][0])} 到 {pct(f["weight"][1])}，期间部分成交待核实'
            else:
                text = lead + '，之后部分成交待核实'
        else:
            net = f['net']
            flow = '买卖相抵' if net.quantize(D('.01')) == 0 else ('净买入 ' if net > 0 else '净卖出 ') + pp(abs(net))
            text = f'{lead}，之后 {f["after"]} 个估值日累计{flow}，股票仓位由 {pct(f["weight"][0])} 到 {pct(f["weight"][1])}：{f["action"]}'
            if f['after'] < AFTER_DAYS:
                text += f'；满 {AFTER_DAYS} 个估值日才下结论'
        if f['highest'] > f['first_tier']:
            outcome = f'之后升到 {lines[f["highest"]-1]}% 警示线，回撤由 {pct(f["first_metric"])} 最深到 {pct(f["deepest"])}'
        elif f['deepest'] is not None and f['first_metric'] is not None and f['deepest'] - f['first_metric'] >= DEEPER:
            outcome = f'未升到更高警示线，但回撤由 {pct(f["first_metric"])} 加深到 {pct(f["deepest"])}'
        else:
            outcome = f'未升到更高警示线，最深 {pct(f["deepest"])}'
        outcome += f['pending']
        if f['ongoing']:
            outcome += (f'；已回到 {lines[0]}% 警示线内第 {f["inside"]} 日，提醒未解除'
                        if f['last'].startswith('观察') else '；提醒仍在')
        elif f['last'] == '解除':
            outcome += f'；{f["end"][5:]} 解除'
        behaviors.append(text); outcomes.append(outcome)
    verdicts = [f['verdict'] for f in facts]
    ranking = ['缺陷', '特征', '未见', '样本不足', '未触发', '待核实']
    verdict = '缺陷（反复）' if verdicts.count('缺陷') >= 2 else min(verdicts, key=ranking.index)
    def numbered(texts):
        return texts[0] if len(texts) == 1 else '；'.join(f'（{n}）{text}' for n, text in enumerate(texts, 1))
    return row('A', verdict, numbered(behaviors), numbered(outcomes), facts=facts)


def loss_fact(result, symbol, states):
    start = states[0]['start']; end = states[-1]['date']
    op = hp.operation_facts(dict(result, as_of=end), start, symbol)
    net = op['by_symbol'].get(symbol, ZERO) if op['status'] == 'known' else None
    quantities = []; raw_quantities = []; fund_units = []
    for day in (start, end):
        data = result['days'].get(day); account = result['accounts'].get(day, {})
        held = (data or {}).get('held'); units = account.get('产品份额')
        holding = (held or {}).get(symbol)
        quantities.append(holding['持仓数量'] / units if holding and units and units > 0 else None)
        raw_quantities.append(holding['持仓数量'] if holding else None);fund_units.append(units)
    change = (quantities[1] / quantities[0] - 1) * 100 if quantities[0] and quantities[1] is not None else None
    if states[-1]['status'] == '段结束' and net is not None:
        action = '清仓'
    elif net is None or change is None:
        action = '待核实'
    elif net > 0 and raw_quantities[1] * fund_units[0] * 100 > raw_quantities[0] * fund_units[1] * (100 + UNITS):
        action = '加仓'
    elif net < 0 and raw_quantities[1] * fund_units[0] * 100 < raw_quantities[0] * fund_units[1] * (100 - UNITS):
        action = '减仓'
    else:
        action = '未动'
    known = [s['metric'] for s in states if s['metric'] is not None]
    metrics_known = all(s['metric'] is not None or s['status'] == '段结束' for s in states)
    names = [(d, h['证券名称']) for d, data in result['days'].items() for s, h in (data['held'] or {}).items() if s == symbol]
    lines = result['rules']['loss']; first_tier = states[0]['tier'] or 1
    return dict(symbol=symbol, name=max(names)[1] if names else symbol, start=start, end=end,
                unknown=states[0]['origin_unknown'], action=action, net=net, change=change, units=quantities,
                first=known[0] if known else None, last=known[-1] if known else None, deepest=max(known) if known else None,
                first_line=lines[first_tier-1], high_line=lines[states[-1]['highest']-1],
                up=states[-1]['highest'] > first_tier, metrics_known=metrics_known)


def row_losses(result, w):
    lines = result['rules']['loss']; items = []; brief = 0
    symbols = sorted({s['object'] for s in result['timeline'] if s['rule'] == 'loss'})
    for symbol in symbols:
        candidates = [states for states in episodes(result, symbol, 'loss') if states[-1]['date'] > w['base']]
        mature = [states for states in candidates if len(states)-1 >= AFTER_DAYS]
        if mature:
            measured = [loss_fact(result, symbol, states) for states in mature]
            items.append(max(measured, key=lambda item: (
                item['action'] != '待核实', item['action'] in ('未动', '加仓'),
                item['up'], item['action'] == '加仓', item['start'])))
        elif candidates:
            brief += 1
    facts = dict(items=items, brief=brief)
    if not items:
        text = f'没有持仓在浮亏 {lines[0]}% 警示线外满 {AFTER_DAYS} 个估值日'
        if brief:
            text += f'（另有 {brief} 只不足 {AFTER_DAYS} 日）'
        return row('B', '未触发', text, facts=facts)
    counts = {a: sum(i['action'] == a for i in items) for a in ('减仓', '清仓', '未动', '加仓', '待核实')}
    kept = [i for i in items if i['action'] in ('未动', '加仓')]
    grew = [i for i in kept if i['up']]; added = [i for i in grew if i['action'] == '加仓']
    verdict = ('待核实' if counts['待核实'] == len(items) else
               '缺陷' if len(grew) >= HOLDINGS else '特征' if len(kept) >= HOLDINGS else '未见')
    text = f'浮亏超 {lines[0]}% 警示线满 {AFTER_DAYS} 个估值日的持仓 {len(items)} 只'
    if verdict == '待核实':
        text += '，之后的操作待核实'
    else:
        categories = [('减仓或清仓', counts['减仓'] + counts['清仓']),
                      ('未动', counts['未动']), ('加仓', counts['加仓']), ('待核实', counts['待核实'])]
        text += '：' + '，'.join(f'{name} {count} 只' for name, count in categories if count)
    outcome = '—'
    if kept:
        outcome = (f'未减仓的 {len(kept)} 只中 {len(grew)} 只之后升到更高的浮亏警示线' if grew else
                   f'未减仓的 {len(kept)} 只之后' + ('都' if len(kept) > 1 else '') + '没有升到更高的浮亏警示线')
        if added:
            outcome += f'，其中加仓的 {len(added)} 只'
        if verdict == '缺陷':
            outcome = '浮亏扩大未处置' + ('、越跌越买' if len(added) >= HOLDINGS else '') + '：' + outcome
    examples = []
    if verdict in ('缺陷', '特征'):
        typical = sorted(grew, key=lambda i: (-(i['deepest']-i['first']) if i['first'] is not None else ZERO, i['symbol']))[:3]
        for i in typical:
            start = f'账本首日已超 {i["first_line"]}% 警示线' if i['unknown'] else f'{i["start"][5:]} 跌破 {i["first_line"]}% 警示线'
            action = '未动' if i['action'] == '未动' else '加仓 ' + pp(i['net'])
            examples.append(f'{i["name"]}：{start}，之后{action}，浮亏由 {pct(i["first"])} 最深到 {pct(i["deepest"])}')
    return row('B', verdict, text, outcome, facts=facts, examples=examples)


def reverse_direction(trade):
    if trade['relative'] is None or trade['deviation'] is None:
        return False
    threshold, _ = hp.trade_band(trade['symbol'])
    if threshold is None:
        return False
    sign = 1 if trade['side'] == '买入' else -1
    return -trade['relative'] * sign >= threshold or (trade['relative'] * sign < 0 and -trade['deviation'] * sign >= hp.TRADE_DEVIATION_PCT)


def row_chase(result, w):
    all_trades = [(day, t) for day, ts in sorted(w['trades'].items()) for t in ts]
    if not all_trades:
        return row('C', '未触发', '窗口内无成交')
    verified = [(d, t) for d, t in all_trades if t['verified']]
    hits = [(d, t) for d, t in verified if t['behavior'] in ('追涨', '杀跌')]
    reverse = [(d, t) for d, t in verified if reverse_direction(t)]
    blind = [(d, t) for d, t in verified if t['skip_reason'] or t['relative'] is None]
    total = sum((t['amount_rmb'] for _, t in verified), ZERO)
    hit_amount = sum((t['amount_rmb'] for _, t in hits), ZERO)
    blind_amount = sum((t['amount_rmb'] for _, t in blind), ZERO)
    share = hit_amount / total * 100 if total else None
    blind_share = blind_amount / total * 100 if total else None
    hit_days = len({d for d, _ in hits}); trade_days = len(w['trades'])
    gain = sum((abs(t['pp']) * (1 / (1 + t['deviation']/100) - 1) * (1 if t['side']=='买入' else -1) for _, t in hits), ZERO)
    new_loss = len({s['object'] for s in result['timeline'] if s['rule']=='loss' and s['status']=='首次' and s['date'] in w['days']})
    chase = sum(t['behavior']=='追涨' for _, t in hits); kill = len(hits)-chase
    text = f'有成交 {trade_days} 日 {len(verified)} 笔，'
    if hits:
        text += f'追涨 {chase} 笔、杀跌 {kill} 笔，出现在 {hit_days} 个交易日，占成交额 {pct(share)}；反方向（低吸、高抛）{len(reverse)} 笔'
    else:
        text += '没有追涨杀跌'
        if reverse:
            text += f'；反方向（低吸、高抛）{len(reverse)} 笔'
    if blind:
        text += f'；{pct(blind_share)} 的成交额未能判定'
    unverified = len(all_trades)-len(verified)
    if unverified:
        text += f'；另有 {unverified} 笔待核实，未计'
    pattern = share is not None and share >= CHASE_SHARE and hit_days >= CHASE_DAYS and len(hits) > len(reverse)
    enough = len(w['days']) >= WINDOW and trade_days >= CHASE_TRADE_DAYS
    if not enough:
        verdict = '样本不足'
        text += (f'。窗口只有 {len(w["days"])} 个交易日，满 {WINDOW} 个才下结论' if len(w['days']) < WINDOW
                 else f'。有成交的交易日满 {CHASE_TRADE_DAYS} 个才下结论')
    elif blind_amount * 3 > total if UNJUDGED == D(1)/3 else blind_amount > total * UNJUDGED:
        verdict = '样本不足'; text += '。未能判定的超过三分之一，不下结论'
    else:
        verdict = '缺陷' if pattern and gain < 0 else '特征' if pattern else '未见'
    outcome = '—'
    if hits:
        outcome = '这些成交按当日收盘价计，合计' + (('盈 ' if gain > 0 else '亏 ') + pp(abs(gain)) if gain.quantize(D('.01')) else '持平')
    if verdict == '缺陷' and new_loss >= NEW_LOSS:
        outcome += f'；同期新跌破浮亏 {result["rules"]["loss"][0]}% 警示线的持仓 {new_loss} 只：风险偏好过高，不适应当前市场'
    examples = []
    if verdict in ('缺陷', '特征'):
        for day, trade in sorted(hits, key=lambda item: (-item[1]['amount_rmb'], item[0], item[1]['symbol']))[:3]:
            deviation = trade['deviation']
            close = '与收盘价持平' if not deviation.quantize(D('.01')) else ('高于' if deviation > 0 else '低于') + '收盘 ' + pct(abs(deviation))
            examples.append(f'{day[5:]} {trade["side"]}{trade["name"]}（{trade["behavior"]}）：均价较昨收 {pct(trade["relative"], True)}，{close}')
    facts = dict(trade_days=trade_days, trades=len(verified), chase=chase, kill=kill, opposite=len(reverse),
                 hit_days=hit_days, share=share, unjudged=blind_share, gain=gain, new_loss=new_loss, pattern=pattern,
                 enough=enough, total=total, hit_amount=hit_amount, blind_amount=blind_amount)
    return row('C', verdict, text, outcome, facts=facts, examples=examples)


def row_frequency(result, w):
    n = len(w['days']); gross = ZERO; names = 0; unverified = 0
    for trades in w['trades'].values():
        valid = [t for t in trades if t['verified'] and t['pp'] is not None]
        unverified += len(trades)-len(valid)
        gross += sum((abs(t['pp']) for t in valid), ZERO)
        names += len({t['symbol'] for t in valid})
    average = gross/n; each = D(names)/n
    text = f'日均换手 {pct(average)}，日均买卖 {each:.1f} 只'
    if unverified:
        text += f'；{unverified} 笔待核实，未计'
    busy = average > TURNOVER and each > NAMES
    outcome = '—'
    if busy:
        outcome = f'窗口累计收益 {pct(w["nav"], True)}，累计交易费用占资产 {pct(w["fees"])}'.replace(' 未知', '未知')
    if n < WINDOW:
        verdict = '样本不足'; text += f'。账本只有 {n} 个交易日，满 {WINDOW} 个才下结论'
    elif busy and w['nav'] is None:
        verdict = '待核实'
    else:
        verdict = '缺陷' if busy and w['nav'] <= 0 else '特征' if busy else '未见'
    return row('D', verdict, text, outcome, facts=dict(average=average, names=each, busy=busy, nav=w['nav'], fees=w['fees']))


def page(result, index, bars_dir=None):
    w = window_facts(result, index, bars_dir)
    out = dict(product=result['product'], name=result['name'], head=['行为评估 · '+result['name']],
               table=[], rows=[], examples=[], tail=[], window=w, empty=False, parameters=parameters())
    if w is None:
        out['head'].append(FIRST)
        return out
    out['head'].append(f'窗口：{w["days"][0]} 至 {w["days"][-1]}，{len(w["days"])} 个交易日，有成交 {len(w["trades"])} 日'
                       + (f'；{len(w["pending"])} 日数据待核实' if w['pending'] else ''))
    result_text = ('窗口累计收益 ' + pct(w['nav'], True) if w['nav'] is not None
                   else '窗口累计收益未知（窗口内有分红）' if w['dividend'] else '窗口累计收益未知')
    if w['has_index']:
        result_text += '，同期沪深300 ' + pct(w['index'], True)
        if w['nav'] is not None:
            result_text += '（差 ' + hp.fmt(w['nav']-w['index'], ' 个百分点', signed=True) + '）'
    out['head'].append(result_text)
    out['empty'] = not w['held_days'] and not w['trades']
    if out['empty']:
        out['tail'] = [EMPTY]
        return out
    out['rows'] = [fn(result, w) for fn in (row_drawdown, row_losses, row_chase, row_frequency)]
    out['table'] = [[TITLES[r['key']], r['verdict'], r['behavior'], r['result']] for r in out['rows']]
    out['examples'] = [[TITLES[r['key']], '', text] for r in out['rows'] for text in r['examples']]
    out['tail'] = [f'暂不评价：决策依据（没有买卖理由的记录）；持仓均衡、仓位管理适应市场（需 {LONG_WINDOW} 个估值日，现有 {w["nav_days"]} 个）。口径和门槛见“说明”。']
    return out


def safe_page(result, index, bars_dir=None):
    """Keep calculation failures out of the successfully built homepage."""
    try:
        return page(result, index, bars_dir), None
    except Exception as exc:
        return dict(product=result['product'], name=result['name'], head=[FAILED], table=[], rows=[], examples=[],
                    tail=[], window=None, empty=False, parameters=parameters()), type(exc).__name__ + ': ' + str(exc)


def validate_cache(p, bars_dir):
    if bars_dir is None:
        return
    for day, pairs in (p.get('window') or {}).get('bars', {}).items():
        od.require(all(fetch_bars.read_pair(bars_dir, symbol, day) == pair for symbol, pair in pairs.items()),
                   'behavior bars changed during processing')


def markdown(p):
    lines = ['**' + p['head'][0] + '**', '', *p['head'][1:], '']
    if p['table']:
        lines.extend(['| ' + ' | '.join(HEADER) + ' |', '|---|---|---|---|'])
        lines.extend('| ' + ' | '.join(c.replace('\n', '<br>') for c in cells) + ' |' for cells in p['table'])
        lines.append('')
    if p['examples']:
        lines.extend(['例子', *['- ' + cells[0] + '：' + cells[2] for cells in p['examples']], ''])
    return '\n'.join(lines + p['tail']) + '\n'
