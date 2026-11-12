"""Offline homepage v2 overlay: replay archived exports without changing v1."""
import argparse
import copy
import csv
import io
import json
import re
import tempfile
from collections import defaultdict
from decimal import Decimal as D
from pathlib import Path

import daily
import oms_daily as od
import render
import replay

RULES = {'fund': [D(5),D(10),D(15),D(20)], 'loss': [D(10),D(15),D(20),D(30)],
         'fall': [D(10)], 'guard': [D(10)]}
LABELS = {'fund':'回撤','loss':'浮亏','fall':'近 5 日跌幅','guard':'盈利保护（较最高收盘回撤）'}
UPGRADES = ('首次','升档','再次')
ZERO = D(0)


def episode_step(prior, metric, thresholds, day, *, first=False, baseline=None, closed=False):
    """Apply the specification's nine rows in order; never mutate prior state."""
    p=prior or {}
    active=p.get('active',False)
    row=dict(date=day,metric=metric,tier=None,highest=p.get('highest',0) if active else 0,
             status='未入档',start=p.get('start') if active else None,
             baseline=p.get('baseline') if active else None,inside=p.get('inside',0) if active else 0,
             last_inside=p.get('last_inside',False) if active else False,
             active=active,days=p.get('days',0)+1 if active else 0,
             origin_unknown=p.get('origin_unknown',False) if active else False)
    if closed:
        row.update(status='段结束',active=False,inside=0)
        return row
    if metric is None:
        row.update(status='无法判定',inside=0)
        return row
    tier=sum(metric>t for t in thresholds)
    row['tier']=tier
    if not active:
        if tier:
            row.update(status='已在档内' if first else '首次',active=True,start=day,
                       baseline=baseline or day,highest=tier,days=1,origin_unknown=first)
        return row
    if tier>row['highest']:
        row.update(status='升档',highest=tier)
    elif tier and row['last_inside']:
        row['status']='再次'
    elif tier:
        row['status']='持续'
    else:
        row['inside']+=1
        row['status']=f'观察第 {row["inside"]} 日' if row['inside']<5 else '解除'
        if row['inside']>=5:
            row['active']=False
    if tier:
        row['inside']=0
    row['last_inside']=tier==0
    return row


def episode_series(values, thresholds, *, closed=()):
    rows=[]; prior=None
    for i,value in enumerate(values):
        prior=episode_step(prior,None if value is None else D(value),thresholds,str(i),first=i==0,closed=i in closed)
        rows.append(prior)
    return rows


def selected_exports(ledger, as_of):
    archives,_=daily.inspect_ledger(ledger)
    selected=[(day,archives[day][max(archives[day])]) for day in sorted(archives) if day<=as_of]
    od.require(selected and selected[-1][0]==as_of, 'missing as of export')
    exports=[]; receipts=[]
    for day,folder in selected:
        export=od.load_input(folder)
        od.require(export['day']==day,'archive date mismatch')
        exports.append(export)
        receipts.append(dict(day=day,revision=int(folder.name),files=daily.read_json(folder/'SHA256.json')))
    return exports,receipts


def raw_day(export, fid, prior):
    holds=od.positions(od.numeric_rows([r for r in od.product_rows(export,'hold',fid) if r['资产类别']=='股票资产'],'hold'))
    fills=od.numeric_rows([r for r in od.product_rows(export,'fill',fid) if r['证券类别']=='股票'],'fill')
    trades=defaultdict(list)
    for t in fills:
        trades[od.security(t)].append(t)
    verified={}
    for s in set(holds)|set(trades)|set(prior or {}):
        h=holds.get(s);ts=trades.get(s,[])
        buy=sum((t['成交数量'] for t in ts if t['委托方向']=='买入'),ZERO)
        sell=sum((t['成交数量'] for t in ts if t['委托方向']=='卖出'),ZERO)
        if h:
            okay=h['期初数量']+buy-sell==h['持仓数量'] and buy==h['当日买入数量'] and sell==h['当日卖出数量']
            if prior is not None:
                okay &= h['期初数量']==prior.get(s,{}).get('持仓数量',ZERO)
        else:
            opening=prior.get(s,{}).get('持仓数量',ZERO) if prior is not None else sell-buy
            okay=opening>0 and opening+buy-sell==0
        verified[s]=bool(okay)
    return holds,dict(trades),verified


def compounded(values):
    if not values or any(v is None for v in values):
        return None
    result=D(1)
    for value in values:
        result*=1+value/100
    return (result-1)*100


def replay_ledger(ledger, as_of, fid, rules=None):
    exports,receipts=selected_exports(ledger,as_of)
    return replay_exports(exports,receipts,as_of,fid,rules)


def replay_exports(exports, receipts, as_of, fid, rules=None):
    rules=copy.deepcopy(rules or RULES)
    name,old_rows=od.build_product(exports,fid)
    facts={r['date']:r for r in old_rows}
    accounts=od.asset_history(exports,fid,as_of)
    dates=[d for d in sorted(accounts) if exports[0]['day']<=d<=as_of]
    source={e['day']:e for e in exports}
    days={};timeline=[];states={};price_windows={};prior=None
    last_dividend=None
    for day in dates:
        account=accounts[day]
        if account['单位分红金额']:
            last_dividend=day
        nav_history=[(d,a['单位净值']) for d,a in sorted(accounts.items()) if d<=day and (last_dividend is None or d>=last_dividend)]
        fund=replay.fund_state(nav_history)
        old=states.get(('fund','fund'))
        metric=fund['drawdown_pct']
        if account['单位分红金额'] or (old and old['active'] and last_dividend and old['baseline']<last_dividend):
            metric=None
        state=episode_step(old,metric,rules['fund'],day,first=day==dates[0],baseline=fund['peak_date'])
        state.update(object='fund',rule='fund')
        states['fund','fund']=state;timeline.append(state)
        export=source.get(day)
        if export:
            held,trades,verified=raw_day(export,fid,prior)
        else:
            held,trades,verified=None,None,{}
        data=dict(date=day,account=account,facts=facts.get(day),held=held,trades=trades,verified=verified,
                  fund=fund,coverage={'valuation':True,'holdings':held is not None,'trades':trades is not None,'quotes':held is not None},
                  states={},five_day={},streak={})
        securities=set(held or {})|set(price_windows)|{s for s,r in states if s!='fund' and states[s,r]['active']}
        for s in sorted(securities):
            h=(held or {}).get(s);f=facts.get(day)
            closed=held is not None and h is None and verified.get(s,False)
            if closed:
                price_windows.pop(s,None)
            elif h:
                if s not in price_windows or h['期初数量']==0:
                    price_windows[s]=[]
                price_windows[s].append(h['当日涨跌幅（%）'])
            else:
                price_windows.setdefault(s,[]).append(None)
            values=price_windows.get(s,[])
            five=compounded(values[-5:]) if len(values)>=5 else None
            streak=0
            for v in reversed(values):
                if v is None:
                    streak=None;break
                if v>=0:
                    break
                streak+=1
            data['five_day'][s]=five;data['streak'][s]=streak
            rate=f['loss_rates'].get(s) if f and h else None
            protection=f['protection'].get(s,{}) if f and h else {}
            guard=protection.get('drawdown_pct') if protection.get('armed') is True else ZERO if protection.get('armed') is False else None
            metrics={'loss':-rate if rate is not None else None,'fall':-five if five is not None else None,'guard':guard}
            for rule,metric in metrics.items():
                state=episode_step(states.get((s,rule)),metric,rules[rule],day,first=day==dates[0],closed=closed)
                state.update(object=s,rule=rule)
                states[s,rule]=state;timeline.append(state);data['states'].setdefault(s,{})[rule]=state
        days[day]=data
        if held is not None:
            prior=held
    return dict(product=fid,name=name,as_of=as_of,rules=rules,exports=exports,receipts=receipts,
                accounts=accounts,days=days,timeline=timeline)


def write_timeline(result, target):
    fields=['date','object','rule','metric','tier','highest','status','start','baseline','inside']
    buffer=io.StringIO(newline=''); writer=csv.DictWriter(buffer,fieldnames=fields,extrasaction='ignore')
    writer.writeheader();writer.writerows(result['timeline'])
    target.write_text(buffer.getvalue(),encoding='utf-8')


def known_position(data, symbol):
    if data is None or data['held'] is None:
        return False
    if symbol in data['held']:
        return True
    # Absence is zero only after a confirmed close or with no unresolved episode.
    return data['verified'].get(symbol, not any(s['active'] for s in data['states'].get(symbol,{}).values()))


def cumulative(result, baseline, symbol=None):
    current=result['days'][result['as_of']]
    dates=[d for d in sorted(result['accounts']) if baseline<=d<=result['as_of']]
    baseline_day=result['as_of']==baseline
    def weight(data):
        if not data or data['held'] is None:
            return None
        if symbol is None:
            return od.pct(data['account']['股票资产'],od.denominator_assets(data['account']))
        h=data['held'].get(symbol)
        return od.pct(h['持仓市值(人民币)'],od.denominator_assets(data['account'])) if h else ZERO if known_position(data,symbol) else None
    start_weight=weight(result['days'].get(baseline));end_weight=weight(current)
    prices=[]; contributions=[]; flows=[];buy_days=sell_days=0;any_trade=False;trade_known=True
    for day in dates[1:]:
        data=result['days'].get(day)
        if data is None or data['held'] is None:
            prices.append(None);contributions.append(None);flows.append(None);trade_known=False
            continue
        held=data['held'];f=data['facts']
        symbols=list(held) if symbol is None else [symbol]
        verified=all(data['verified'].values()) if symbol is None else known_position(data,symbol) and data['verified'].get(symbol,True)
        if symbol is not None:
            prices.append(held[symbol]['当日涨跌幅（%）'] if symbol in held else None)
        pieces=[f['contribution_pct'].get(s) for s in symbols if s in held] if f else [None]
        contributions.append(sum(pieces,ZERO) if verified and all(v is not None for v in pieces) else None)
        trades=data['trades']
        ts=[t for group in trades.values() for t in group] if symbol is None else trades.get(symbol,[])
        okay=all(data['verified'].values()) if symbol is None else known_position(data,symbol) and data['verified'].get(symbol,True)
        okay &= all(t['成交金额']==0 or t['成交金额(RMB)']>0 for t in ts)
        if not okay:
            flows.append(None);trade_known=False
        else:
            buy=sum((t['成交金额(RMB)'] for t in ts if t['委托方向']=='买入'),ZERO)
            sell=sum((t['成交金额(RMB)'] for t in ts if t['委托方向']=='卖出'),ZERO)
            flows.append(od.pct(buy-sell,od.denominator_assets(data['account'])))
            buy_days+=buy>sell;sell_days+=sell>buy
            any_trade |= any(t['成交数量']>0 for t in ts)
    def total(values):
        return sum(values,ZERO) if values and all(v is not None for v in values) else None
    net=total(flows);delta=end_weight-start_weight if start_weight is not None and end_weight is not None else None
    direction='待核实' if not trade_known else '、'.join(text for count,text in ((buy_days,f'净买入 {buy_days} 日'),(sell_days,f'净卖出 {sell_days} 日')) if count) if any_trade else '期间无已核实成交'
    return dict(baseline_day=baseline_day,visible_days=len(dates),price_pct=compounded(prices),
                contribution_pp=total(contributions),net_buy_pp=net,weight_start=start_weight,weight_end=end_weight,
                weight_delta=None if baseline_day else delta,remaining_pp=delta-net if delta is not None and net is not None else None,
                trade_direction='—' if baseline_day else direction)


def fmt(value, unit='%', *, signed=False, baseline=False):
    if baseline:
        return '—'
    if value is None:
        return '未知'
    rounded=value.quantize(D('.01'))
    return ('0.00' if rounded==0 else f'{value:+.2f}' if signed else f'{value:.2f}')+unit


def state_text(state, thresholds=None):
    status=state['status']; rule=state['rule']; label=LABELS[rule]
    thresholds=thresholds or RULES[rule]
    if status=='段结束':return ''
    if status=='解除':return label+'提醒解除'
    if not state['active']:
        return label+'待核实' if state['metric'] is None else '未超 '+fmt(thresholds[0],unit='%',signed=False).replace('.00','')
    def threshold(tier):return format(thresholds[tier-1],'f').rstrip('0').rstrip('.')+'%' if '.' in format(thresholds[tier-1],'f') else str(thresholds[tier-1])+'%'
    days=f"提醒第 {state['days']} 日"
    if status=='无法判定':return label+'待核实，'+days
    if status.startswith('观察第 '):return label+f"回到 {threshold(1)} 以内第 {state['inside']} 日，"+days
    line=threshold(state['tier'])
    if status=='已在档内':return label+'超 '+line+'，有记录以来'
    if status=='首次':return label+'首次超 '+line
    verb={'升档':'升至超 ','再次':'再次超 '}.get(status,'超 ')
    text=label+verb+line+'，'+days
    if status=='持续' and state['tier']<state['highest']:text+='（本段最高超 '+threshold(state['highest'])+'）'
    return text


def holding_state_text(states, rules, separator='；'):
    """Show observation fragments only when every active rule is observing."""
    active=[s for s in states if s['active']]
    observing=bool(active) and all(s['status'].startswith('观察第 ') for s in active)
    return separator.join(state_text(s,rules[s['rule']]) for s in states
                          if (s['active'] or s['status']=='解除')
                          and (observing or not s['status'].startswith('观察第 ')))


def next_distance(state, thresholds):
    if state['metric'] is None or state['tier'] is None or state['tier']>=len(thresholds):
        return None
    return thresholds[state['tier']]-state['metric']


def pnl_text(data):
    f=data['facts'];value=f['holdings_pnl_pct']
    difference=value-f['daily_return_pct'] if value is not None and f['daily_return_pct'] is not None else None
    prefix=(f"持仓合计贡献 {fmt(value,' 个百分点',signed=True)}；与净值涨跌差额 {fmt(difference,' 个百分点',signed=True)}；" if difference is not None and abs(difference)>D('.05') else '')
    if value is None:
        return prefix+'缺前一估值日持仓，个股当日盈亏未知'
    if not all(data['verified'].values()):
        return prefix+'个股涨跌分布待核实'
    contributions=f['contribution_pct']
    if abs(value)<D('.30') and not any(abs(v)>=D('.30') for v in contributions.values()):
        return prefix+'平'
    direction=-1 if value<=0 else 1
    same={s:v for s,v in contributions.items() if v*direction>0}
    total_weight=sum(f['weights'].values(),ZERO)
    same_weight=sum((f['weights'][s] for s in same),ZERO)
    ranked=sorted(same,key=lambda s:(-abs(same[s]),s))
    same_contribution=sum((abs(v) for v in same.values()),ZERO)
    if abs(value)<D('.30'):
        shape='分化'
    elif total_weight and same_weight/total_weight>=D('.70'):
        shape='普跌' if direction<0 else '普涨'
    elif same_contribution and sum((abs(same[s]) for s in ranked[:3]),ZERO)/same_contribution>=D('.60'):
        shape='集中'
    else:
        shape='分化'
    if shape=='分化' or direction<0:
        ranked=sorted((s for s,v in contributions.items() if v<0),key=lambda s:(contributions[s],s))[:3]
        title='前三大拖累'
    else:
        ranked=sorted((s for s,v in contributions.items() if v>0),key=lambda s:(-contributions[s],s))[:3]
        title='前三大贡献'
    return prefix+shape+('；'+title+'：'+'、'.join(f"{f['names'][s]} {fmt(contributions[s],' 个百分点',signed=True)}" for s in ranked) if ranked else '')


def read_index(directory, as_of):
    from datetime import date
    from zipfile import BadZipFile
    from xml.etree.ElementTree import ParseError
    result=dict(status='missing',values={},files={})
    if directory is None:return result
    paths=[Path(directory)/('沪深300'+ext) for ext in ('.csv','.xlsx')]
    files=[p for p in paths if p.is_file()]
    if not files:return result
    result['files']={p.name:daily.digest(p.read_bytes()) for p in files}
    result['status']='invalid'
    if len(files)!=1:return result
    try:
        file=files[0]
        if file.suffix=='.csv':
            with file.open(encoding='utf-8-sig',newline='') as source:
                reader=csv.DictReader(source);header=reader.fieldnames;rows=list(reader)
        else:
            _,header,rows=od.read_workbook(file,{'日期','收盘'})
        od.require(header is not None and all(h in header for h in ('日期','收盘')), 'missing index columns')
        values={}
        for row in rows:
            day=date.fromisoformat(str(row['日期'])).isoformat()
            if day>as_of:continue
            close=od.number(row['收盘']);od.require(close>0,'nonpositive index close')
            od.require(day not in values or values[day]==close,'conflicting index date')
            values[day]=close
        result.update(status='valid',values=values)
    except (ValueError,KeyError,TypeError,OSError,UnicodeError,BadZipFile,ParseError,csv.Error):
        pass
    return result


def index_return(index, start, end):
    values=index['values']
    if index['status']!='valid' or start not in values or end not in values:return None
    return (values[end]/values[start]-1)*100


# Trial wording thresholds. Risk lines and episode transitions remain in RULES.
CONCLUSION_HIGH = D(80)
CONCLUSION_VERY_HIGH = D(90)
CONCLUSION_FLOW = D(2)
CONCLUSION_STREAK = 3
CONCLUSION_MANY = 3
CONCLUSION_HALF = D('.5')
NEAR_LINE_PP = D(1)
TRADE_DEVIATION_PCT = D(3)
FOCUS_REASONS = ('今日升级','今日成交','临近下一条线','拖累最大','首日已在线外','持续提醒')


def previous_alert(result, day, symbol):
    """Only the preceding valuation close can establish an already alerted trade."""
    before=[d for d in result['accounts'] if d<day]
    previous=result['days'].get(max(before)) if before else None
    if not previous or previous['held'] is None:return None,previous
    states=previous['states'].get(symbol,{})
    if any(s['active'] for s in states.values()):return True,previous
    if symbol not in previous['held']:return False,previous
    unknown=[s for s in states.values() if s['metric'] is None and (s['rule']!='guard' or not (previous.get('facts') or {}).get('protection',{}).get(symbol,{}).get('cold_start'))]
    if not states or unknown:return None,previous
    return False,previous


def operation_facts(result, start, symbol=None):
    """Count buys and sells separately; retain total flow when prior state is missing."""
    out=dict(status='known',net=ZERO,alert_net=ZERO,by_symbol={},buy_days=0,sell_days=0)
    if start==result['as_of']:
        out.update(status='baseline',net=None,alert_net=None);return out
    for day in sorted(d for d in result['accounts'] if start<d<=result['as_of']):
        data=result['days'].get(day)
        if not data or data['held'] is None or data['trades'] is None:
            out['status']='missing';out['net']=out['alert_net']=None;continue
        symbols=set(data['verified']) if symbol is None else {symbol}
        if any(not data['verified'].get(s,known_position(data,s)) for s in symbols):
            if out['status']!='missing':out['status']='unverified'
            out['net']=out['alert_net']=None
        buys=sells=ZERO
        for s,ts in data['trades'].items():
            if symbol is not None and s!=symbol:continue
            if any(t['成交金额'] and t['成交金额(RMB)']<=0 for t in ts):
                if out['status']!='missing':out['status']='unverified'
                out['net']=out['alert_net']=None;continue
            buy=sum((t['成交金额(RMB)'] for t in ts if t['委托方向']=='买入'),ZERO)
            sell=sum((t['成交金额(RMB)'] for t in ts if t['委托方向']=='卖出'),ZERO)
            buys+=buy;sells+=sell
            net=od.pct(buy-sell,od.denominator_assets(data['account']))
            if net is None:out.update(status='unverified',net=None,alert_net=None);continue
            out['by_symbol'][s]=out['by_symbol'].get(s,ZERO)+net
            if out['net'] is not None:out['net']+=net
            alerted,_=previous_alert(result,day,s)
            if alerted is None:out['alert_net']=None
            elif alerted and out['alert_net'] is not None:out['alert_net']+=net
        out['buy_days']+=buys>0;out['sell_days']+=sells>0
    if out['status']!='known':out['by_symbol']={}
    return out


def trade_facts(result):
    day=result['as_of'];data=result['days'][day];out=[]
    for s,ts in sorted((data['trades'] or {}).items()):
        if not ts:continue
        alerted,previous=previous_alert(result,day,s)
        prior=(previous or {}).get('facts') or {};current=data['held'].get(s)
        before=prior.get('weights',{}).get(s,ZERO) if previous and previous['held'] is not None else None
        after=data['facts']['weights'].get(s,ZERO) if known_position(data,s) else None
        change=after-before if before is not None and after is not None else None
        okay=data['verified'].get(s,False) and all(not t['成交金额'] or t['成交金额(RMB)']>0 for t in ts)
        state_list=list((previous or {}).get('states',{}).get(s,{}).values())
        for side in ('买入','卖出'):
            fills=[t for t in ts if t['委托方向']==side and t['成交数量']>0]
            if not fills:continue
            amount=sum((t['成交金额'] for t in fills),ZERO);qty=sum((t['成交数量'] for t in fills),ZERO)
            deviation=(amount/qty/current['最新价']-1)*100 if current and current['最新价']>0 else None
            out.append(dict(symbol=s,name=ts[0]['证券名称'],side=side,closed=current is None and okay,
                verified=okay,alerted=alerted,previous=previous['date'] if previous else None,
                previous_states=state_list,before=before,after=after,change=change,
                loss=data['facts']['loss_rates'].get(s) if current else prior.get('loss_rates',{}).get(s),
                pp=od.pct(sum((t['成交金额(RMB)'] for t in fills),ZERO),od.denominator_assets(data['account']))*(1 if side=='买入' else -1),
                deviation=deviation if okay else None))
    return sorted(out,key=lambda t:(t['change'] is None,-abs(t['change'] or ZERO),t['symbol'],t['side']))


def upgrade_key(item):
    states=[s for s in item['states'] if s['status'] in UPGRADES]
    first=min(states,key=lambda s:('loss','fall','guard').index(s['rule']))
    return (('loss','fall','guard').index(first['rule']),-(first['tier'] or 0) if first['rule']=='loss' else 0,
            -(item['weight'] or ZERO),item['symbol'])


def conclusion(f):
    """Pure rendering of already computed facts; no ledger, I/O or model."""
    od.require(f and 'fund' in f,'missing conclusion facts')
    fund=f['fund'];status=fund['status'];line=f['fund_line'];streak=f['streak']
    continuous=streak is not None and streak>=CONCLUSION_STREAK
    if status=='无法判定':nav='净值回撤待核实'
    elif status in UPGRADES:nav=(f'净值连续回撤，今日升至超 {line}%' if continuous else f'净值回撤升至超 {line}%')
    elif status.startswith('观察'):nav=f"净值回到 {f.get('fund_first_line',D(5))}% 线内第 {fund['inside']} 日，提醒未解除"
    elif status=='解除':nav='净值回撤提醒解除'
    elif status=='已在档内':nav=f'净值回撤超 {line}%'
    elif fund['active']:
        nav=f'净值连续回撤，已超 {line}%' if continuous else f'净值今日反弹，回撤仍超 {line}%' if f['daily_return'] is not None and f['daily_return']>0 else f'净值回撤仍超 {line}%'
    else:nav=('净值连续下跌，' if continuous else '净值')+f"回撤未超 {f.get('fund_first_line',D(5))}%"
    losses=f['losses'];ups=f['upgrades'];budget=2;used=set()
    def take(group):
        nonlocal budget
        chosen=[]
        for i in group:
            if i['symbol'] not in used and budget:
                chosen.append(i);used.add(i['symbol']);budget-=1
        return chosen
    def event(i):
        verb={'首次':'首次超','升档':'升至超','再次':'再次超'}[i['status']]
        label={'loss':'浮亏','fall':'近 5 日跌幅','guard':'盈利保护回撤'}[i['rule']]
        return label+verb+f" {i['line']}%"
    change=''
    if not f['first']:
        if len(ups)>=CONCLUSION_MANY:
            i=take(ups[:1])[0];change=f"今日多只升级，最重的是{i['name']}（{event(i)}）"
        elif ups:change='今日'+'、'.join(i['name']+' '+event(i) for i in take(ups))
        else:change='今日无升级'
    state=''
    if len(losses)>=CONCLUSION_MANY:
        state=('过半持仓' if f['total'] and D(len(losses))/f['total']>=CONCLUSION_HALF else '多只持仓')+f"浮亏超 {f.get('loss_first_line',D(10))}%"
        deepest=max(i['line'] for i in losses)
        if deepest>f.get('loss_first_line',D(10)):state+=f'（最深超 {deepest}%）'
    elif losses:
        remaining=[i for i in losses if i['symbol'] not in used];chosen=take(remaining)
        lines=defaultdict(list)
        for i in chosen:lines[i['line']].append(i['name'])
        state='、'.join('、'.join(lines[line])+f' 浮亏超 {line}%' for line in sorted(lines,reverse=True))
        unspoken=[i for i in remaining if i not in chosen]
        if unspoken:state+=('，' if state else '')+f"另有持仓浮亏超 {max(i['line'] for i in unspoken)}%"
    elif not ups:state=f"无持仓浮亏超 {f.get('loss_first_line',D(10))}%"
    if f['total']==0:state='无股票持仓'
    parts=[x for x in (state,change) if x]
    if not f['first'] and not ups:
        for group,word in ((f['near'],'临近下一条线'),(f['cleared'],'提醒解除')):
            if not group:continue
            if len(group)>=CONCLUSION_MANY:parts.append('多只'+word)
            else:
                already=[i for i in group if i['symbol'] in used]
                remaining=[i for i in group if i['symbol'] not in used]
                chosen=take(remaining)
                if already:parts.append(('该持仓' if len(already)==1 else '上述持仓')+word)
                if chosen:parts.append('、'.join(i['name'] for i in chosen)+' '+word)
                if any(i not in chosen for i in remaining):parts.append('另有持仓'+word)
    if f.get('holding_unknown'):parts.append('部分持仓状态待核实')
    holding='无股票持仓' if f['total']==0 else ('账本首日，' if f['first'] else '')+'，'.join(parts)
    operations=[];budget=2;used=set()
    active=fund['active'];flow=f['net'];op_status=f['operation_status']
    if active or f['buys'] or f['sells'] or f['closes'] or f['trade_unknown']:
        weight=f['weight']
        if weight is None:operations.append('仓位待核实')
        elif weight>=CONCLUSION_VERY_HIGH:operations.append('仓位高')
        elif weight>=CONCLUSION_HIGH:operations.append('仓位较高')
        if active and op_status!='baseline':
            operations.append('回撤提醒后的操作待核实' if flow is None else '回撤提醒后不降反升' if flow>=CONCLUSION_FLOW else '回撤提醒后已减仓' if flow<=-CONCLUSION_FLOW else '回撤提醒后未减仓')
        both={i['symbol'] for i in f['buys']} & {i['symbol'] for i in f['sells']+f['closes']}
        groups=[([i for i in f['buys'] if i['symbol'] in both],'双向'),
                ([i for i in f['buys'] if i['symbol'] not in both],'仍在买入'),
                ([i for i in f['sells'] if i['symbol'] not in both],'减持'),
                ([i for i in f['closes'] if i['symbol'] not in both],'清仓')]
        for group,verb in groups:
            if verb=='双向' and group:
                if len(group)>budget or len(group)>=CONCLUSION_MANY:operations.append('今日在多只已提醒的持仓上有买有卖')
                else:operations.append('今日在已提醒的'+'、'.join(i['name'] for i in take(group))+' 上有买有卖')
                continue
            if not group:continue
            already=[i for i in group if i['symbol'] in used]
            group=[i for i in group if i['symbol'] not in used]
            if already:operations.append('今日也'+verb+'上述已提醒持仓')
            if not group:continue
            if len(group)>budget or len(group)>=CONCLUSION_MANY:
                operations.append('今日'+verb+'多只已提醒的持仓')
            else:
                chosen=take(group)
                if chosen:operations.append('今日'+verb+'已提醒的'+'、'.join(i['name'] for i in chosen))
                if len(chosen)<len(group):operations.append('今日'+verb+'多只已提醒的持仓')
        if f['trade_unknown'] and not f['first']:operations.append('今日成交提醒状态待核实')
    tail='注意风险。' if active and not status.startswith('观察') and f['weight'] is not None and f['weight']>=CONCLUSION_HIGH and (flow is None or flow>-CONCLUSION_FLOW) else ''
    return '；'.join(x for x in (nav,holding,'，'.join(operations)) if x)+'。'+tail


def page_facts(result):
    od.require(result and result.get('days'),'missing v2 facts')
    day=result['as_of'];data=result['days'][day];f=data['facts']
    od.require(f is not None and data['held'] is not None,'missing current holdings')
    fund=next(s for s in reversed(result['timeline']) if s['object']=='fund')
    base=fund['baseline'] or data['fund']['peak_date'];fs=cumulative(result,base)
    comparable=not any(a['单位分红金额'] for d,a in result['accounts'].items() if base<d<=day)
    nav_return=(data['account']['单位净值']/result['accounts'][base]['单位净值']-1)*100 if comparable else None
    trades=trade_facts(result);items=[]
    for symbol,states in sorted(data['states'].items()):
        active=[s for s in states.values() if s['active']]
        if not active:continue
        start=min(s['start'] for s in active);weight=f['weights'].get(symbol)
        metrics=[s['metric'] for s in active if s['metric'] is not None]
        item=dict(symbol=symbol,name=f['names'].get(symbol,symbol),weight=weight,loss_rate=f['loss_rates'].get(symbol),
            states=active,baseline=start,stats=cumulative(result,start,symbol),operations=operation_facts(result,start,symbol),
            score=weight*max(metrics) if weight is not None and metrics else None,
            five_day=data['five_day'].get(symbol),streak=data['streak'].get(symbol),
            distances={rule:next_distance(states[rule],result['rules'][rule]) if rule=='loss' else None for rule in states})
        items.append(item)
    upgrades=sorted((i for i in items if any(s['status'] in UPGRADES for s in i['states'])),key=upgrade_key)
    near=sorted((i for i in items if i['distances']['loss'] is not None and ZERO<=i['distances']['loss']<=NEAR_LINE_PP),key=lambda i:(i['distances']['loss'],i['symbol']))
    traded={t['symbol']:t for t in trades if t['verified'] and t['alerted']}
    trading=sorted((i for i in items if i['symbol'] in traded),key=lambda i:(traded[i['symbol']]['change'] is None,-abs(traded[i['symbol']]['change'] or ZERO),i['symbol']))
    selected=[];seen=set()
    for reason,group in zip(FOCUS_REASONS[:3],(upgrades,trading,near)):
        for item in group:
            if item['symbol'] in seen:continue
            item['reason']=reason;selected.append(item);seen.add(item['symbol'])
    rest=[i for i in items if i['symbol'] not in seen]
    pending=[i for i in rest if i['stats']['contribution_pp'] is None and not i['stats']['baseline_day']]
    dragging=sorted((i for i in rest if i['stats']['contribution_pp'] is not None and i['stats']['contribution_pp']<0),key=lambda i:(i['stats']['contribution_pp'],i['symbol']))
    dragging+=sorted((i for i in rest if i['stats']['baseline_day']),key=lambda i:(i['score'] is None,-(i['score'] or ZERO),i['symbol']))
    for item in dragging[:3]:
        item['reason']='首日已在线外' if day==min(result['days']) else '拖累最大'
        selected.append(item);seen.add(item['symbol'])
    folded=[i for i in items if i['symbol'] not in seen]
    if len(folded)<=3:
        for item in folded:item['reason']='持续提醒';selected.append(item)
        folded=[]
    if day==min(result['days']):
        for item in selected:item['reason']='首日已在线外'
    op=operation_facts(result,fund['start']) if fund['active'] else None
    def simple(i,upgrade=False):
        s=next(s for rule in ('loss','fall','guard') for s in i['states'] if s['rule']==rule and (s['status'] in UPGRADES if upgrade else rule=='loss'))
        return dict(symbol=i['symbol'],name=i['name'],line=result['rules'][s['rule']][s['tier']-1],rule=s['rule'],status=s['status'])
    losses=[simple(i) for i in items if any(s['rule']=='loss' and s['tier'] for s in i['states'])]
    cleared=[dict(symbol=s,name=f['names'].get(s,s)) for s,states in sorted(data['states'].items()) if any(t['status']=='解除' for t in states.values()) and not any(t['active'] for t in states.values())]
    cf=dict(fund=fund,fund_line=result['rules']['fund'][(fund['tier'] or 1)-1],fund_first_line=result['rules']['fund'][0],loss_first_line=result['rules']['loss'][0],
        streak=data['fund']['streak'],daily_return=f['daily_return_pct'],first=day==min(result['days']),total=len(data['held']),losses=losses,
        upgrades=[simple(i,True) for i in upgrades],near=near,cleared=cleared,weight=f['stock_weight_pct'],net=op['net'] if op else None,
        operation_status=op['status'] if op else 'baseline',
        buys=[t for t in trades if t['verified'] and t['alerted'] and t['side']=='买入'],
        sells=[t for t in trades if t['verified'] and t['alerted'] and t['side']=='卖出' and not t['closed']],
        closes=[t for t in trades if t['verified'] and t['alerted'] and t['closed']],
        trade_unknown=any(not t['verified'] or t['alerted'] is None for t in trades),
        holding_unknown=any(s['metric'] is None for i in items for s in i['states']))
    return dict(date=day,name=result['name'],fund=dict(state=fund,stats=fs,nav_return_pct=nav_return,operations=op),items=items,
        upgrades=upgrades,upgrade_folded=[],continuing=dragging[:3],folded=folded,pending=pending,selected=selected,near=near,trades=trades,conclusion_facts=cf)


def operation_text(op):
    if op['status']=='baseline':return '—'
    if op['status']=='missing':return '未知'
    if op['status']=='unverified':return '待核实'
    if not op['buy_days'] and not op['sell_days']:return '无成交'
    return fmt(op['net'],'',signed=True)+'｜'+'、'.join(f'{side} {op[key]} 日' for side,key in [('买','buy_days'),('卖','sell_days')] if op[key])


def remaining_text(items):
    groups=defaultdict(list)
    for item in items:
        states=item['states'];loss=next((s for s in states if s['rule']=='loss' and s['tier']),None)
        if loss:label=f"浮亏超 {RULES['loss'][loss['tier']-1]}%"
        elif any(s['rule']=='fall' and s['tier'] for s in states):label='近 5 日跌幅超 10%'
        elif any(s['rule']=='guard' and s['tier'] for s in states):label='盈利保护'
        elif all(s['status'].startswith('观察') for s in states):label='回到线内观察中'
        else:label='待核实'
        groups[label].append(item['name'])
    labels=[f'浮亏超 {n}%' for n in reversed(RULES['loss'])]+['近 5 日跌幅超 10%','盈利保护','回到线内观察中','待核实']
    weight=sum((i['weight'] for i in items),ZERO) if all(i['weight'] is not None for i in items) else None
    return '合计仓位 '+fmt(weight)+'；'+'；'.join(label+'：'+'、'.join(groups[label]) for label in labels if groups[label])


def today_trades_text(view,result):
    selected={i['symbol'] for i in view['selected']};groups=defaultdict(list);briefs=defaultdict(list);counts=defaultdict(int)
    for t in view['trades']:
        if not t['verified']:
            groups['待核实'].append(t['name']+' 成交待核实');briefs['待核实'].append(t['name']);continue
        label='清仓' if t['closed'] and t['side']=='卖出' else t['side']
        deviation=''
        if t['deviation'] is not None and abs(t['deviation'])>=TRADE_DEVIATION_PCT:
            deviation=t['side']+'均价'+('高于' if t['deviation']>0 else '低于')+f"收盘 {abs(t['deviation']):.1f}%"
        if t['symbol'] in selected:
            text=t['name']+'（见重点持仓'+('，'+deviation if deviation else '')+'）'
        else:
            if t['before'] is None:
                text=t['name']+' 今日'+t['side']+' '+fmt(t['pp'],' 个百分点',signed=True)+('，收盘仓位 '+fmt(t['after']) if not t['closed'] else '')
            else:
                text=t['name']+' '+('清仓前仓位 '+fmt(t['before']) if t['closed'] else '仓位 '+fmt(t['before'])+' → '+fmt(t['after']))
            if not t['closed'] or t['before'] is not None:
                text+='，'+('前一日浮盈亏 ' if t['closed'] else '收盘浮盈亏 ')+fmt(t['loss'],signed=True)
            if deviation:text+='，'+deviation
            if t['closed'] and t['before'] is not None:
                status='提醒状态未知' if t['alerted'] is None else '未在提醒段内'
                if t['alerted']:
                    active=[s for s in t['previous_states'] if s['active']]
                    fragments=[]
                    for state in active:
                        fragment=state_text(state,result['rules'][state['rule']])
                        if '提醒第 ' not in fragment:fragment+=f"，提醒第 {state['days']} 日"
                        fragments.append(fragment)
                    status='，'.join(fragments)
                text+='，前一估值日'+status
        if t['symbol'] in selected or counts[label]<6:
            groups[label].append(text)
            if t['symbol'] not in selected:counts[label]+=1
        else:briefs[label].append(t['name']+('（'+deviation+'）' if deviation else ''))
    return '；'.join(label+'：'+'；'.join(values)+('；其余：'+'、'.join(briefs[label]) if briefs[label] else '') for label in ('买入','卖出','清仓','待核实') if (values:=groups[label]))


def build_view(result,index=None):
    view=page_facts(result);day=result['as_of'];data=result['days'][day];f=data['facts'];fund=view['fund']['state']
    index=index or read_index(None,day);has_index=index['status']=='valid';calendar=sorted(result['accounts']);base=fund['baseline'] or data['fund']['peak_date']
    peak=data['fund']['peak_date'];fs=view['fund']['stats'];early=base<min(result['days'])
    label=f'有记录以来高点（{peak}）' if len(calendar)<60 else f'{peak} 高点'
    fund_text=f'距{label}回撤 {fmt(fund["metric"])}；'+state_text(fund,result['rules']['fund'])
    if fund['active']:
        distance=next_distance(fund,result['rules']['fund'])
        if distance is not None:fund_text+=f"；距 {result['rules']['fund'][fund['tier']]}% 线 "+fmt(distance,' 个百分点')
        if base!=peak:fund_text+=f"；本段高点 {base} 以来净值 "+fmt(view['fund']['nav_return_pct'],signed=True,baseline=base==day)
        fund_text+=f"；高点以来第 {fs['visible_days']} 个估值日"
        if data['fund']['streak']!=0:fund_text+='；连跌 '+str(data['fund']['streak'] if data['fund']['streak'] is not None else '未知')+' 日'
        if base<calendar[-60:][0]:fund_text+='；本段高点已移出 60 日窗口，累计基准不变'
        if has_index:fund_text+='；同期沪深300 '+fmt(index_return(index,base,day),signed=True,baseline=base==day)
    view['fund']['text']=fund_text
    headline='今日有升级事项' if view['upgrades'] or fund['status'] in UPGRADES else '今日有提醒解除' if view['conclusion_facts']['cleared'] or fund['status']=='解除' else '今日无升级事项'
    if not all(data['coverage'].values()):headline='数据不全，'+headline
    rows=[['产品概览',f'{day} 收盘｜'+headline+('（模拟数据）' if any(e['simulated'] for e in result['exports']) else '')]]
    if f.get('cash_excluded'):
        rows.extend(['数据提示',p.split('：',1)[1]] for p in render.render_day(f).split('\n\n') if p.startswith('数据提示：'))
    if render.share_note(f):rows.append(['申赎说明',render.share_note(f)])
    _,assets=daily.csv_inputs(result['exports'],result['product'],day);nav=render.csv_nav(assets);dates=sorted(assets)[-5:]
    def prior(d):return max((x for x in calendar if x<d),default='')
    five_base=prior(dates[0]) if len(dates)==5 else ''
    five=(assets[day]['单位净值']/assets[five_base]['单位净值']-1)*100 if five_base in assets and not any(a['单位分红金额'] for d,a in assets.items() if five_base<d<=day) else None
    nav[0].append('5 日累计');nav[1].append('—');nav[2].append(fmt(five,signed=True))
    nav.append(['','股票仓位',*[fmt(od.pct(result['accounts'][d]['股票资产'],od.denominator_assets(result['accounts'][d]))) for d in dates],'—'])
    if has_index:nav.append(['','沪深300当日涨跌',*[fmt(index_return(index,prior(d),d),signed=True) for d in dates],fmt(index_return(index,five_base,day),signed=True)])
    rows.extend(nav);rows.extend([['今日结论',conclusion(view['conclusion_facts'])],['净值回撤',fund_text]])
    names={s:h['证券名称'] for d in result['days'].values() for s,h in (d['held'] or {}).items()}
    chosen={i['symbol']:i for i in view['selected']}
    if fund['active']:
        start=fund['start'];op=view['fund']['operations'];weights={}
        for when,title in [(base,'高点日'),(start,'首次提醒日'),(day,'今日')]:
            weights.setdefault(when,[]).append(title)
        comparisons=[]
        for d,titles in weights.items():
            known=result['days'].get(d)
            weight=od.pct(known['account']['股票资产'],od.denominator_assets(known['account'])) if known and known['held'] is not None else None
            comparisons.append(fmt(weight)+'（'+'、'.join(titles)+(' '+d if '今日' not in titles else '')+'）')
        text='股票仓位 '+' → '.join(comparisons)
        if early:text+='；高点日早于持仓记录，期间仓位变化与成交未知'
        if start==day:text+='；今日首次提醒'
        else:
            value='未知' if op['status']=='missing' else '待核实' if op['status']=='unverified' else fmt(op['net'],' 个百分点',signed=True)
            text+='；首次提醒以来净买入占比累计 '+value+'，其中已提醒持仓 '+(fmt(op['alert_net'],' 个百分点',signed=True) if op['alert_net'] is not None else '待核实')
            for sign,title in ((1,'净买入最多'),(-1,'净卖出最多')):
                top=sorted(((s,v) for s,v in op['by_symbol'].items() if v*sign>0),key=lambda x:(-abs(x[1]),x[0]))[:3]
                if top:text+='；'+title+'：'+'、'.join(names.get(s,s)+('（见重点持仓）' if s in chosen and chosen[s]['baseline']==start else ' '+fmt(v,' 个百分点',signed=True)) for s,v in top)
        if day==min(result['days']) and fund['status']=='已在档内':text='账本首日净值已在回撤提醒中，此前的仓位变化和成交未知'
        rows.append(['回撤期间操作',text])
        if not early:
            symbols={s for d in result['days'].values() if d['date']>=base for s in (d['held'] or {})}
            drag=[(s,cumulative(result,base,s)['contribution_pp']) for s in symbols]
            known=sorted(((s,v) for s,v in drag if v is not None and v<0),key=lambda x:(x[1],x[0]))[:3]
            text='；'.join(names[s]+('（提醒段内）' if any(i['symbol']==s for i in view['items']) else '')+('（见重点持仓）' if s in chosen and chosen[s]['baseline']==base else ' '+fmt(v,' 个百分点',signed=True)) for s,v in known)
            if any(v is None for _,v in drag):text+=('；' if text else '')+'日贡献累计待核实'
            rows.append(['回撤期间拖累',('高点以来日贡献累计最负：'+text) if known else text or '高点以来无负日贡献累计'])
    if data['held']:rows.append(['今日盈亏',pnl_text(data)])
    if view['selected']:
        rows.append(['重点持仓',''])
        header=['入选原因','持仓','状态','','','今日仓位（%）','浮盈亏（%）','提醒开始日','提醒日仓位（%）','提醒以来股价（%）','日贡献累计（百分点）','提醒以来操作（净买入占比累计，百分点）','近 5 日股价（%）','连跌日数','距下一浮亏线（百分点）']
        if has_index:header.insert(10,'同期沪深300（%）')
        rows.append(header)
        for item in view['selected']:
            st=item['stats'];same=st['baseline_day'];optext=operation_text(item['operations'])
            today=[t for t in view['trades'] if t['symbol']==item['symbol']]
            for t in today:optext+='；今日'+t['side']+' '+(fmt(t['pp'],'',signed=True) if t['verified'] else '待核实') if t['verified'] else '；今日成交待核实'
            row=[item['reason'],item['name'],holding_state_text(item['states'],result['rules']),'','',fmt(item['weight'],''),fmt(item['loss_rate'],'',signed=True),
                '有记录以来' if any(s['origin_unknown'] and s['start']==item['baseline'] for s in item['states']) else item['baseline'],
                fmt(st['weight_start'],'',baseline=same),fmt(st['price_pct'],'',signed=True,baseline=same),fmt(st['contribution_pp'],'',signed=True,baseline=same),optext,
                fmt(item['five_day'],'',signed=True),str(item['streak']) if item['streak'] is not None else '未知',fmt(item['distances']['loss'],'') if item['distances']['loss'] is not None else '—']
            if has_index:row.insert(10,fmt(index_return(index,item['baseline'],day),'',signed=True,baseline=same))
            rows.append(row)
    if view['folded']:rows.append(['其余提醒中',remaining_text(view['folded'])])
    if view['trades']:rows.append(['今日成交',today_trades_text(view,result)])
    gaps=[]
    first=day==min(result['days'])
    if first:gaps.append('账本首日：持仓历史从 '+day+' 起，此前的成交、个股仓位和各项累计未知')
    labels={row[0] for row in rows}
    affected=[label for label in ('回撤期间操作','回撤期间拖累','今日盈亏','重点持仓','今日成交') if label in labels]
    missing_exports=[d['date'] for d in result['days'].values() if d['held'] is None]
    if missing_exports:gaps.append('、'.join(missing_exports)+' 缺持仓、成交导出；受影响：'+'、'.join(affected))
    if not prior(dates[0]):gaps.append('近 5 日表首日（'+dates[0]+'）缺前一日净值')
    elif not five_base:gaps.append('截至 '+day+' 净值不足六个估值日；受影响：近 5 日表累计')
    dividends=[d for d,a in assets.items() if a['单位分红金额'] and (not five_base or five_base<d<=day)]
    if dividends:gaps.append('、'.join(dividends)+' 有分红，前后净值不可比；受影响：近 5 日表累计、净值回撤')
    unverified=[d['date'] for d in result['days'].values() if any(not okay for okay in d['verified'].values())]
    if unverified:gaps.append('、'.join(unverified)+' 成交与持仓未核对；受影响：'+'、'.join(affected))
    if index['status']=='invalid':gaps.append('指数文件有误，未使用；受影响：沪深300各项')
    if has_index:
        required=set(dates)|{prior(d) for d in dates}|{five_base}
        if fund['active']:required.add(base)
        required.update(i['baseline'] for i in view['selected'])
        missing=sorted(d for d in required if d and d not in index['values'])
        if missing:gaps.append('沪深300缺日期 '+ '、'.join(missing)+'；受影响：近 5 日表、净值回撤、重点持仓的同期指数')
    if not first:
        short=[i['name'] for i in view['selected'] if i['five_day'] is None]
        if short and not missing_exports:gaps.append(day+' 持仓价格不足五个估值日：'+'、'.join(short)+'；受影响：重点持仓的近 5 日股价')
        if early and fund['active']:gaps.append(base+' 高点日早于持仓记录起始日 '+min(result['days'])+'；受影响：回撤期间操作、回撤期间拖累')
    grouped_gaps=[];affected_groups={}
    for gap in dict.fromkeys(gaps):
        reason,separator,affected_names=gap.partition('；受影响：')
        if separator and affected_names in affected_groups:
            n=affected_groups[affected_names]
            earlier=grouped_gaps[n].split('；受影响：',1)[0]
            grouped_gaps[n]=earlier+'，'+reason+'；受影响：'+affected_names
        else:
            if separator:affected_groups[affected_names]=len(grouped_gaps)
            grouped_gaps.append(gap)
    if grouped_gaps:rows.append(['数据覆盖','；'.join(grouped_gaps)])
    weights=f['weights'];ranked=sorted((s for s,w in weights.items() if w is not None and w>0),key=lambda s:(-weights[s],s))
    concentration=('第一大 '+f['names'][ranked[0]]+' '+fmt(weights[ranked[0]])+'；前三 '+fmt(sum((weights[s] for s in ranked[:3]),ZERO))+'；前五 '+fmt(sum((weights[s] for s in ranked[:5]),ZERO))) if ranked else '无股票持仓'
    view['footer']=[['总仓位',f"股票 {fmt(f['stock_weight_pct'])}；现金 {fmt(f['cash_weight_pct'])}"],['个股仓位',concentration],['总盈亏','见产品概览“净值回撤”'],['个股盈亏','见产品概览“重点持仓”，逐只状态见风控提示列' if data['held'] else '无股票持仓']]
    view['overview']=[[re.sub(r'[+-]0\.00(?!\d)','0.00',cell) for cell in row] for row in rows]
    view['index']=index
    view['claims']=presentation_claims(result,view)
    return view


def presentation_claims(result,view):
    """Semantic statement identities plus independently inspectable scalar locations."""
    day=result['as_of'];product=result['product'];claims=[]
    def identity(obj,metric,start=day,end=day,basis='percent'):
        return [product,obj,metric,start,end,basis]
    by_name={i['name']:i for i in view['items']};dates=[];header=[]
    def member(claim,obj,metric,cell,pattern,start=day,end=day,basis='percent',kind='single'):
        claim['members'].append(dict(identity=identity(obj,metric,start,end,basis),cell=cell,pattern=pattern,kind=kind))
    for source in ('overview','footer'):
        for n,row in enumerate(view[source]):
            label=row[0];text=' | '.join(row[1:]);kind='single';obj='fund';metric=label
            start=end=day
            if label in ('产品概览','今日结论','重点持仓','入选原因','数据覆盖','持仓历史','总盈亏','个股盈亏'):continue
            if label=='近5日':dates=row[2:-1];continue
            if label=='':
                metric=row[1];kind='series';start=dates[0]
            elif label in FOCUS_REASONS and len(row)>2:
                obj=by_name[row[1]]['symbol'];metric='holding_review';start=by_name[row[1]]['baseline'];kind='comparison'
            elif label=='回撤期间操作':kind='comparison';start=view['fund']['state']['baseline']
            claim=dict(identity=identity(obj,metric,start,end,'statement'),kind=kind,text=text,members=[])
            claims.append(claim)
            cell=[source,n,1]
            if label=='净值回撤':
                peak=result['days'][day]['fund']['peak_date']
                member(claim,'fund','drawdown',cell,r'回撤 ([0-9.]+)%',peak)
            elif label=='总仓位':
                member(claim,'fund','stock_weight',cell,r'股票 ([0-9.]+)%')
                member(claim,'fund','cash_weight',cell,r'现金 ([0-9.]+)%')
            elif label=='个股仓位':
                ranks=sorted((s for s,w in result['days'][day]['facts']['weights'].items() if w is not None and w>0),key=lambda s:(-result['days'][day]['facts']['weights'][s],s))
                if ranks:member(claim,ranks[0],'weight',cell,r'第一大 .*? ([0-9.]+)%',kind='comparison')
                for title,num in [('前三',3),('前五',5)]:member(claim,'fund','top_'+str(num)+'_weight',cell,title+r' ([0-9.]+)%')
            elif label=='':
                for j,d in enumerate(dates,2):
                    metric={'股票仓位':'stock_weight','单位净值':'nav','当日涨跌':'daily_return','沪深300当日涨跌':'index_return'}[row[1]]
                    if row[j] not in ('未知','—'):
                        member(claim,'index' if metric=='index_return' else 'fund',metric,[source,n,j],r'^([+\-]?[0-9.]+)%?$',start=d,end=d,basis='nav' if metric=='nav' else 'percent',kind='series')
            elif label=='回撤期间操作':
                state=view['fund']['state'];weights={}
                for d,title in [(state['baseline'],'高点日'),(state['start'],'首次提醒日'),(day,'今日')]:weights.setdefault(d,[]).append(title)
                for d,titles in weights.items():
                    title='、'.join(titles)+(' '+d if '今日' not in titles else '')
                    pattern=r'([0-9.]+)%（'+re.escape(title)+'）'
                    if re.search(pattern,row[1]):member(claim,'fund','stock_weight',cell,pattern,start=d,end=d,kind='comparison')
                if state['start']<day:
                    for word,metric in [('净买入占比累计','net_flow'),('其中已提醒持仓','alert_net_flow')]:
                        pattern=word+r' ([+\-]?[0-9.]+) 个百分点'
                        if re.search(pattern,row[1]):member(claim,'fund',metric,cell,pattern,start=state['start'],basis='pp')
            elif label in FOCUS_REASONS and len(row)>2:
                item=by_name[row[1]];s=item['symbol'];start=item['baseline'];offset=int(view['index']['status']=='valid')
                fields=[(5,'weight',day,'percent'),(6,'pnl_rate',day,'percent'),(8,'weight',start,'percent'),(9,'price_return',start,'percent'),(10+offset,'contribution',start,'pp'),(12+offset,'five_day_price',day,'percent'),(14+offset,'next_loss_distance',day,'pp')]
                for col,metric,d,basis in fields:
                    if re.fullmatch(r'[+\-]?[0-9.]+',row[col]):member(claim,s,metric,[source,n,col],r'^([+\-]?[0-9.]+)$',start=d,end=d if metric=='weight' else day,basis=basis,kind='comparison' if metric=='weight' else 'single')
                if re.match(r'[+\-]?[0-9.]+｜',row[11+offset]):member(claim,s,'net_flow',[source,n,11+offset],r'^([+\-]?[0-9.]+)｜',start=start,basis='pp')
    return claims

def render_view(view):
    od.require(view and view.get('overview'), 'missing overview')
    return '# '+view['name']+'\n\n'+'\n\n'.join(' | '.join(row) for row in view['overview'])+'\n'


def product_csv(result, view):
    f=result['days'][result['as_of']]['facts'];day=result['as_of']
    holds,assets=daily.csv_inputs(result['exports'],result['product'],day)
    rows=[[render.csv_text(result['name'])],['基金持仓'],['日期',day,'基金经理：']]
    for row in view['overview']:
        # NAV rows are already generated numeric/date cells, like v1 csv_nav().
        if row[0] in FOCUS_REASONS and len(row)>2:
            rows.append([row[0],render.csv_text(row[1]),*row[2:]])
        else:
            rows.append(row if row[1] in ('日期','单位净值','当日涨跌','股票仓位','沪深300当日涨跌') else [render.csv_text(cell) for cell in row])
    rows.extend([[],render.CSV_HEADER])
    body,total=render.csv_holdings(holds)
    tail,ending=render.csv_tail(assets[day],total,len(rows)+len(body)+1)
    for n,row in enumerate(body,len(rows)+1):
        symbol=row[3]+'|'+row[1]
        states=result['days'][day]['states'].get(symbol,{})
        hint=holding_state_text(list(states.values()),result['rules'])
        row[9]=render.csv_asset_ratio(f'I{n}',ending);row.append(render.csv_text(hint))
        row[1:4]=[render.csv_text(cell) for cell in row[1:4]]
    rows.extend(body)
    for row,(_,text) in zip(tail[-4:],view['footer']):row.append(text)
    rows.extend(tail)
    out=io.StringIO(newline='');writer=csv.writer(out,lineterminator='\r\n')
    writer.writerows(row+['']*(len(render.CSV_HEADER)-len(row)) for row in rows)
    return re.sub(r'[+-]0\.00(?!\d)', '0.00',out.getvalue().removesuffix('\r\n')).encode('utf-8')


def fact_sheet(result, view):
    """Export only presentation facts; never raw balances, quantities or trades."""
    facts={};day=result['as_of'];header=None;nav_dates=[]
    names=result['days'][day]['facts']['names'];objects={v:k for k,v in names.items()}
    objects.update({'账户':'fund','沪深300':'index'})
    scalar={tuple(m['cell']):m['identity'] for c in view['claims'] for m in c['members']}
    def add(obj,item,value,date=day,identity=None):
        key='F-'+daily.digest((obj+'|'+item+'|'+date).encode())[:16]
        fact=dict(id=key,object=obj,date=date,item=item,value=str(value),identity=identity or [result['product'],objects.get(obj,obj),item,date,date,'statement'])
        od.require(key not in facts or facts[key]==fact,'conflicting fact identifier')
        facts[key]=fact
    for n,row in enumerate(view['overview']):
        section=row[0]
        if section=='入选原因':header=row;continue
        if section=='近5日' and row[1]=='日期':nav_dates=row[2:-1];continue
        if not section and row[1] in ('单位净值','当日涨跌','股票仓位','沪深300当日涨跌'):
            for col,(date,value) in enumerate(zip(nav_dates,row[2:]),2):add('沪深300' if row[1].startswith('沪深300') else '账户',row[1],value,date,scalar.get(('overview',n,col)))
            if row[1] in ('当日涨跌','沪深300当日涨跌'):add('沪深300' if row[1].startswith('沪深300') else '账户',row[1]+'五日累计',row[-1])
        elif section in FOCUS_REASONS and len(row)>2:
            for col,(label,value) in enumerate(zip(header,row)):
                if not label:continue
                unit='%' if label.endswith('（%）') else ' 个百分点' if label.endswith('（百分点）') else ' 日' if label=='连跌日数' else ''
                add(row[1],label,value+unit if value not in ('—','未知','待核实') else value,identity=scalar.get(('overview',n,col)))
        else:
            # Distinguish the known folded row from the unknown folded row.
            item=section+('（待核实）' if section=='其余持续中' and row[1].startswith('待核实') else '')
            add('账户',item,row[1])
    for label,value in view['footer']:add('账户',label,value)
    for n,claim in enumerate(view['claims']):
        key='C-'+daily.digest(daily.encoded(claim['identity']))[:16]
        facts[key]=dict(id=key,object=names.get(claim['identity'][1],{'fund':'账户','index':'沪深300'}.get(claim['identity'][1],claim['identity'][1])),date=day,item=claim['identity'][2],value=claim['text'],identity=claim['identity'],kind=claim['kind'],members=claim['members'])
    states=[view['fund']['state']]+[state for item in view['items'] for state in item['states']]
    names=result['days'][day]['facts']['names']
    for state in states:
        if not state['active']:continue
        obj='账户' if state['object']=='fund' else names.get(state['object'],state['object'])
        thresholds=result['rules'][state['rule']];tier=state['tier']
        prefix=LABELS[state['rule']]
        add(obj,prefix+'当前指标',fmt(state['metric']))
        add(obj,prefix+'下一门槛',fmt(thresholds[tier]) if tier is not None and tier<len(thresholds) else '未知' if tier is None else '无更高门槛')
        add(obj,prefix+'距下一门槛',fmt(next_distance(state,thresholds),' 个百分点'))
    return list(facts.values())


def fact_text(facts):
    return '事实底稿｜只据以下事实写至多3条观察；不得推断原因、预测或给出操作建议。\n'+'\n'.join(f"[{f['id']}] 对象：{f['object']}；日期：{f['date']}；项名：{f['item']}；展示值：{f['value']}" for f in facts)+'\n'


FORBIDDEN_POINTS=re.compile(r'应|建议|宜|需要|公告|消息|同行|行业|预计|将会|有望|因为|由于|受.{0,40}影响|跑输|跑赢|超额|(?<!净)(?:买入|卖出)|加仓|减仓|止损|止盈|调仓|增持|减持|(?:明日|明天|未来|后市|接下来).{0,20}(?:涨|跌|反弹|回升|走高|走低)|(?:[0-9一二三四五六七八九十]+\s*日后|以后|随后).{0,20}(?:解除|升档|回落|涨|跌)|(?:可能|或将|料将|大概率).{0,15}(?:上涨|下跌|反弹)')
POINT_NUMBER=re.compile(r'[+−＋－-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?(?:\s*(?:个百分点|%|％|日|万元|亿元|元|股(?!价)|只|手|倍|万|亿|天|bps?))*|[零一二三四五六七八九十百千万两]+(?:个百分点|%|日|元|股|只|手)')
POINT_DATE=re.compile(r'\d{4}-\d{2}-\d{2}')


def validate_points(payload, facts):
    od.require(isinstance(payload,dict) and isinstance(payload.get('points'),list),'invalid points file')
    source={f['id']:f for f in facts};accepted=[];decisions=[]
    for n,point in enumerate(payload['points']):
        reason='';text=point.get('text') if isinstance(point,dict) else None
        ids=point.get('facts') if isinstance(point,dict) else None
        if n>=3:reason='超过前三条'
        elif not isinstance(text,str) or not text.strip():reason='缺少句子'
        elif len(text)>70:reason='超过70个字'
        elif '\n' in text or len(re.findall(r'[。！？!?]',text.rstrip('。！？!?')))>0:reason='必须为一句'
        elif not isinstance(ids,list) or not ids or any(not isinstance(i,str) or i not in source for i in ids):reason='事实编号不存在或为空'
        elif FORBIDDEN_POINTS.search(text):reason='命中禁用内容'
        else:
            cited=[source[i] for i in ids]
            allowed_dates={f['date'] for f in cited}|{d for f in cited for d in POINT_DATE.findall(f['value'])}
            if any(d not in allowed_dates for d in POINT_DATE.findall(text)):reason='日期无所引事实支持'
            def numbers(value):
                value=POINT_DATE.sub('',value)
                for name in sorted({f['object'] for f in cited},key=len,reverse=True):value=value.replace(name,'')
                return [re.sub(r'\s+','',v) for v in POINT_NUMBER.findall(value)]
            supported={v for f in cited for v in numbers(f['value'])}
            if any(v not in supported for v in numbers(text)):reason='数字、正负号或单位无所引事实支持'
        if not reason:accepted.append({'text':text,'facts':ids})
        decisions.append(dict(index=n+1,text=text,facts=ids,kept=not bool(reason),reason=reason or '保留'))
    return dict(accepted=accepted,decisions=decisions)


def workbook_options(content, index):
    rows=list(csv.reader(io.StringIO(content.decode())))
    nav_start=next(i for i,row in enumerate(rows,1) if row[0]=='近5日' and row[1]=='日期')
    table_rows=[i for i,row in enumerate(rows,1) if row[0] in ('入选原因',*FOCUS_REASONS) and row[2]]
    return dict(nav_start=nav_start,table_rows=table_rows,unsigned_zero=True,state_span=True,
                nav_count=5 if index['status']=='valid' else 4,nav_cumulative=True,explanation=True)


def archive_receipts(ledger, as_of):
    archives,_=daily.inspect_ledger(ledger)
    return [dict(day=day,revision=max(archives[day]),files=daily.read_json(archives[day][max(archives[day])]/'SHA256.json'))
            for day in sorted(archives) if day<=as_of]


def batch_pages(ledger, as_of, *, index_dir=None):
    """Parse each selected export once. Only product calculation errors are isolated."""
    ledger=Path(ledger);exports,receipts=selected_exports(ledger,as_of);index=read_index(index_dir,as_of)
    products=sorted({r.get('产品序号') for r in exports[-1]['tables']['asset'][2] if not od.is_total(r) and r.get('产品序号')})
    od.require(products,'no products')
    names={p:max((r for r in od.product_rows(exports[-1],'asset',p) if not od.is_total(r)),key=lambda r:(r.get('日期')==as_of,r.get('日期') or ''))['产品'] for p in products}
    # Split rows once instead of repeatedly scanning the other products' histories.
    grouped={p:[] for p in products}
    for export in exports:
        tables={p:{} for p in products}
        for kind,(sheet,header,rows) in export['tables'].items():
            by_product={p:[] for p in products}
            for row in rows:
                if row.get('产品序号') in by_product:by_product[row['产品序号']].append(row)
            for p in products:tables[p][kind]=(sheet,header,by_product[p])
        for p in products:grouped[p].append(dict(export,tables=tables[p]))
    pages={};errors={}
    for fid in products:
        try:
            result=replay_exports(grouped[fid],receipts,as_of,fid)
            view=build_view(result,index);content=product_csv(result,view)
            pages[fid]=dict(result=result,view=view,csv=content,options=workbook_options(content,index))
        except Exception as err:
            # This block only computes in memory; archive reads and writes are outside.
            errors[fid]=type(err).__name__+': '+str(err)
    od.require(archive_receipts(ledger,as_of)==receipts,'input changed during processing')
    od.require(read_index(index_dir,as_of)['files']==index['files'],'index changed during processing')
    return dict(pages=pages,errors=errors,names=names,index=index,receipts=receipts)


def validate_sources(ledger, as_of, receipts, index_dir, index):
    od.require(archive_receipts(Path(ledger),as_of)==receipts,'input changed during processing')
    od.require(read_index(index_dir,as_of)['files']==index['files'],'index changed during processing')


def run_all(ledger, as_of, out, *, index_dir=None):
    import os
    ledger=Path(ledger);out=Path(out)
    od.require(out.resolve()!=ledger.resolve() and ledger.resolve() not in out.resolve().parents,'output inside ledger')
    batch=batch_pages(ledger,as_of,index_dir=index_dir);outputs={}
    out.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.batch-',dir=out) as temp:
        staged={fid:write_page(page['result'],page['view'],Path(temp)/fid) for fid,page in batch['pages'].items()}
        validate_sources(ledger,as_of,batch['receipts'],index_dir,batch['index'])
        for fid,folder in staged.items():
            target=out/fid/folder.name;target.parent.mkdir(exist_ok=True)
            if target.exists():
                od.require({p.name:p.read_bytes() for p in target.iterdir()}=={p.name:p.read_bytes() for p in folder.iterdir()},'existing v2 output differs')
            else:os.rename(folder,target)
            outputs[fid]=target
    return dict(outputs=outputs,errors=batch['errors'])


def run(ledger, as_of, fid, out, rules=None, *, index_dir=None, points_file=None):
    ledger=Path(ledger);out=Path(out)
    od.require(out.resolve()!=ledger.resolve() and ledger.resolve() not in out.resolve().parents, 'output inside ledger')
    result=replay_ledger(ledger,as_of,fid,rules);index=read_index(index_dir,as_of);view=build_view(result,index)
    od.require(archive_receipts(ledger,as_of)==result['receipts'],'input changed during processing')
    od.require(read_index(index_dir,as_of)['files']==index['files'],'index changed during processing')
    return write_page(result,view,out,points_file=points_file,validate=lambda:validate_sources(ledger,as_of,result['receipts'],index_dir,index))


def write_page(result, view, out, *, points_file=None, validate=None):
    import os
    import zipfile
    import manager_workbook as mw
    out=Path(out);index=view['index'];as_of=result['as_of'];fid=result['product']
    facts=fact_sheet(result,view);points_bytes=Path(points_file).read_bytes() if points_file is not None else None
    validation=None
    if points_bytes is not None:
        validation=validate_points(json.loads(points_bytes),facts)
    code={name:daily.digest((Path(__file__).parent/name).read_bytes()) for name in (*daily.CODE,'homepage_v2.py','manager_workbook.py')}
    identity=dict(format='homepage-v2-round4',as_of=as_of,product=fid,inputs=result['receipts'],rules=result['rules'],code=code,index=index['files'],points=daily.digest(points_bytes) if points_bytes is not None else None)
    identity_bytes=daily.encoded(identity);key=daily.digest(identity_bytes)
    target=out/key
    payload={'MANIFEST.json':identity_bytes,'facts.json':daily.encoded(view),'PAGES.md':render_view(view).encode(),
             'holdings.csv':product_csv(result,view),'AI_FACTS.json':daily.encoded(facts),'AI_FACTS.txt':fact_text(facts).encode()}
    if validation is not None:payload['AI_VALIDATION.json']=daily.encoded(validation)
    with tempfile.TemporaryDirectory() as temp:
        temp=Path(temp);source=temp/'holdings.csv';source.write_bytes(payload['holdings.csv'])
        mw.write_workbook(source,temp/'holdings.xlsx',**workbook_options(payload['holdings.csv'],index))
        # Package timestamps must not change an otherwise identical v2 artifact.
        buffer=io.BytesIO()
        with zipfile.ZipFile(temp/'holdings.xlsx') as source_zip, zipfile.ZipFile(buffer,'w',zipfile.ZIP_DEFLATED) as dest:
            for name in source_zip.namelist():
                info=zipfile.ZipInfo(name,date_time=(2000,1,1,0,0,0));info.compress_type=zipfile.ZIP_DEFLATED
                dest.writestr(info,source_zip.read(name))
        payload['holdings.xlsx']=buffer.getvalue()
    od.require(points_bytes is None or Path(points_file).read_bytes()==points_bytes,'points changed during processing')
    if validate:validate()
    if target.exists():
        od.require({p.name for p in target.iterdir()}==set(payload) and all((target/n).read_bytes()==b for n,b in payload.items()), 'existing v2 output differs')
        return target
    out.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.homepage-',dir=out) as temp:
        pending=Path(temp)/'version';pending.mkdir()
        for name,content in payload.items():(pending/name).write_bytes(content)
        if validate:validate()
        os.rename(pending,target)
    return target


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ledger',type=Path,required=True);parser.add_argument('--as-of',required=True)
    parser.add_argument('--product',required=True,help='product ID or all');parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--fund-thresholds',help='comma-separated strictly increasing trial percentages')
    parser.add_argument('--index-dir',type=Path)
    parser.add_argument('--points-file',type=Path)
    args=parser.parse_args();rules=copy.deepcopy(RULES)
    if args.fund_thresholds:
        values=[od.number(s) for s in args.fund_thresholds.split(',')]
        od.require(values and values==sorted(set(values)) and values[0]>0,'invalid thresholds');rules['fund']=values
    if args.product=='all':
        od.require(not args.points_file and not args.fund_thresholds,'batch uses standard rules without points')
        od.require(args.out.resolve()!=args.ledger.resolve() and args.ledger.resolve() not in args.out.resolve().parents,'output inside ledger')
        batch=run_all(args.ledger,args.as_of,args.out,index_dir=args.index_dir)
        print(f"V2 BATCH SUMMARY: pages={len(batch['outputs'])} failed={len(batch['errors'])}")
        for product,reason in batch['errors'].items():print(product+': '+reason)
        return
    folder=run(args.ledger,args.as_of,args.product,args.out,rules,index_dir=args.index_dir,points_file=args.points_file)
    print('V2 OUTPUT: '+str(folder))


if __name__=='__main__':
    main()
