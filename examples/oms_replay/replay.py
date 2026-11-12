"""Replay synthetic daily OMS exports against an independent cash and position ledger."""
import argparse
import csv
import hashlib
import json
import sys
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal as D, InvalidOperation
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from render import render_report

TZ = timezone(timedelta(hours=8))
ZERO = D(0)
FORMAT = 'synthetic-oms-replay-v1'
HEADERS = {
    'calendar': 'date', 'accounts': 'date,cash,assets,units,nav',
    'positions': 'date,symbol,qty,avg_cost,mv,unrealized', 'marks': 'date,symbol,close',
    'fills': 'id,timestamp,symbol,side,qty,price,fee',
    'markets': 'symbol,market',
}

def require(ok, message):
    if not ok:
        raise ValueError(message)

def number(value):
    result = D(value)
    require(result.is_finite(), 'nonfinite decimal')
    return result

def equal(observed, expected, label):
    require(observed == expected, f'{label}: observed {observed}, expected {expected}')

def pct(part, base):
    return part * 100 / base

def csv_rows(folder, key):
    with (folder / (key + '.csv')).open(encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f)
        equal(reader.fieldnames, HEADERS[key].split(','), key + ' columns')
        return list(reader)

def load_exports(folder, as_of):
    days = [r['date'] for r in csv_rows(folder, 'calendar') if not as_of or r['date'] <= as_of]
    require(days and (as_of is None or days[-1] == as_of), 'empty calendar or cutoff outside calendar')
    prior = date.fromisoformat(days[0])
    require(prior.weekday() < 5, 'opening valuation date must be a weekday')
    for day in days[1:]:
        prior += timedelta(days=1)
        while prior.weekday() >= 5:
            prior += timedelta(days=1)
        equal(day, prior.isoformat(), 'valuation calendar gap or duplicate')
    cutoff = days[-1]
    known = set(days)
    tables = {}
    for key in ('marks', 'positions', 'accounts'):
        by_day = defaultdict(dict)
        for row in csv_rows(folder, key):
            day = row['date']
            if day > cutoff:
                continue
            require(day in known, f'{key}: unexpected date {day}')
            name = row.get('symbol', 'account')
            require(name and name not in by_day[day], f'duplicate or empty {key} key {day} {name}')
            by_day[day][name] = row
        tables[key] = by_day
    trades = defaultdict(list)
    ids = set()
    end = datetime.combine(date.fromisoformat(cutoff), time(16), TZ)
    for row in csv_rows(folder, 'fills'):
        if row['timestamp'][:10] > cutoff:
            continue
        dt = datetime.fromisoformat(row['timestamp'])
        require(dt.utcoffset() == TZ.utcoffset(None), 'invalid fill timezone')
        if dt > end:
            continue
        day = dt.date().isoformat()
        require(day in known and dt.hour * 3600 + dt.minute * 60 + dt.second <= 57600
                and (dt.hour < 16 or dt.microsecond == 0), 'fill outside valuation session')
        require(row['id'] and row['id'] not in ids, 'duplicate or empty fill ID')
        ids.add(row['id'])
        trades[day].append((dt, row))
    # Stable input order resolves ties in the synthetic execution log.
    fills = {day:[row for dt,row in sorted(group, key=lambda pair:pair[0])] for day,group in trades.items()}
    opening = json.loads((folder / 'opening.json').read_text())
    require(tables['accounts'] or tables['positions'] or fills, 'empty OMS observations through the cutoff')
    return days, {k:number(opening[k]) for k in ('cash','units','liabilities')}, tables, fills

def fund_state(history):
    window = history[-60:]
    good = [(day,v) for day,v in window if v is not None]
    current = window[-1][1]
    peak, peak_date = max(((v,d) for d,v in good), default=(None,None))
    dd = pct(peak-current,peak) if current is not None and peak else None
    streak = 0
    for (_,prev),(_,now) in zip(reversed(history[:-1]),reversed(history[1:])):
        if prev is None or now is None:
            streak = None
            break
        if now >= prev:
            break
        streak += 1
    if len(history)==1 or current is None:
        streak = None
    valid = len(good)==60 and len(window)==60
    recent = list(zip(history[:-1], history[1:]))[-3:]
    nonfall = any(a is not None and b is not None and b >= a for (_,a),(_,b) in recent)
    falling = len(recent)==3 and all(a is not None and b is not None and b<a for (_,a),(_,b) in recent)
    if current is None or len(history)==1:
        alert = None
    elif nonfall:
        alert = False
    elif falling and dd>10:
        alert = True
    elif valid and dd<=10:
        alert = False
    else:
        alert = None
    record_peak = max((v for _,v in history if v is not None), default=None)
    record_dd = pct(record_peak-current,record_peak) if current is not None and record_peak else None
    return {'peak_nav':peak, 'peak_date':peak_date, 'drawdown_pct':dd, 'streak':streak, 'fund_alert':alert,
            'window_complete':valid, 'record_peak_nav':record_peak, 'record_drawdown_pct':record_dd}


def alert_event(prior, active, metric):
    """Advance a three-state observation; unknown must never emit a clearance."""
    prior = prior or {}
    was_active = prior.get('active')
    if active is None:
        return {'active':None, 'state':'unknown', 'days':0, 'shown':None}
    if not active:
        return {'active':False, 'state':'cleared' if was_active is True else 'off', 'days':0, 'shown':None}
    days = prior.get('days',0)+1 if was_active is True else 1
    shown = prior.get('shown')
    if was_active is not True:
        event, shown = ('resumed' if prior and was_active is None else 'first'), metric
    elif metric is not None and shown is not None and metric-shown>3:
        event, shown = 'worse', metric
    else:
        event = 'persist'
    return {'active':True, 'state':event, 'days':days, 'shown':shown}

def protection_step(seg, close, gain):
    """Advance the visible high and profit guard for one holding segment."""
    if close is None:
        seg['complete'] = False
    else:
        seg['peak'] = max(seg['peak'] or close,close)
        if gain is not None and gain>30:
            seg['armed'] = True
    dd = pct(seg['peak']-close,seg['peak']) if close is not None and seg['peak'] else None
    alert = None
    if close is not None:
        if seg['armed'] and dd>10:
            alert = True
        elif seg['complete']:
            alert = False
    return {'armed':True if seg['armed'] else False if seg['complete'] else None,
            'alert':alert, 'drawdown_pct':dd, 'missing_close':close is None}

def pick_headline(previous_assets, buys, sells, reconciled, day_ret, fund_alert, drawdown_pct):
    """Choose the page lead from reconciled flows and observable return facts."""
    large, small = max(buys,sells), min(buys,sells)
    # Only today's reconciled facts and the previous closing assets authorize a trade lead.
    if previous_assets and large>previous_assets/10 and reconciled:
        return 'rebalancing' if small*2>=large else 'net_buying' if buys>sells else 'net_selling'
    elif day_ret is None:
        return 'partial'
    elif fund_alert:
        return 'deep_decline'
    elif drawdown_pct is not None and drawdown_pct>10 and day_ret and day_ret>0:
        return 'rebound_deep'
    else:
        return 'normal'

class Ledger:
    def __init__(self, opening):
        self.cash, self.units = opening['cash'], opening['units']
        require(self.cash>0 and self.units>0 and opening['liabilities']==0, 'invalid opening cash, units or liabilities')
        self.opening_cash = self.cash
        self.qty, self.cost, self.realized = defaultdict(D), defaultdict(D), defaultdict(D)
        self.segments = {}

    def apply(self, rows):
        net_sales = fees = ZERO
        sales = {}
        self.opened = set()
        for row in rows:
            name, side = row['symbol'], row['side']
            q, price, fee = (number(row[k]) for k in ('qty','price','fee'))
            require(name and side in ('BUY','SELL') and q>0 and q==q.to_integral_value()
                    and price>0 and fee>=0, 'invalid fill ' + row['id'])
            amount = q * price
            before = self.qty[name]
            if side == 'BUY':
                self.cost[name] += amount
                self.qty[name] += q
                self.cash -= amount + fee
                net_sales -= amount
                self.realized[name] -= fee
                if before==0:
                    self.segments[name] = {'peak':None,'armed':False,'complete':True}
                    self.opened.add(name)
            else:
                require(q<=before, 'oversell ' + name)
                basis = self.cost[name] / before * q
                earned = amount - basis - fee
                self.realized[name] += earned
                self.cost[name] -= basis
                self.qty[name] -= q
                self.cash += amount - fee
                net_sales += amount
                item = sales.setdefault(name,{'symbol':name,'qty':ZERO,'amount':ZERO,'net_realized':ZERO})
                item['qty'] += q
                item['amount'] += amount
                item['net_realized'] += earned
                if self.qty[name] == 0:
                    del self.segments[name]
            require(self.cash>=0, 'negative cash after ' + row['id'])
            fees += fee
        for item in sales.values():
            item['average_price'] = item.pop('amount') / item['qty']
        return fees, net_sales, list(sales.values())

    def protection(self, name, close, gain):
        seg = self.segments[name]
        return protection_step(seg, close, gain)

def replay(folder, as_of=None):
    days, opening, tables, fills = load_exports(folder,as_of)
    book = Ledger(opening)
    markets = {}
    if (folder/'markets.csv').exists():
        for row in csv_rows(folder,'markets'):
            require(row['symbol'] and row['market'] and row['symbol'] not in markets, 'invalid or duplicate market mapping')
            markets[row['symbol']] = row['market']
    previous_quotes, previous_weights = {}, {}
    previous_assets, previous_nav, previous_cash_weight = book.cash, book.cash/book.units, D(100)
    history, result = [], []
    previous_unreal = ZERO
    previous_rates, previous_alerts, previous_fund_event = {}, {}, None
    previous_position_snapshot = {}
    previous_stock_weight = D(0)
    for day in days:
        before = dict(book.qty)
        before_cost = dict(book.cost)
        trades = fills.get(day,[])
        fees, net_sales, sales = book.apply(trades)
        qty = {s:q for s,q in book.qty.items() if q}
        quotes = {s:number(r['close']) for s,r in tables['marks'][day].items()}
        require(all(v>0 for v in quotes.values()), 'invalid closing price')
        pos = tables['positions'][day]
        acc = tables['accounts'][day].get('account')
        gaps = []
        if acc is None:
            gaps.append('缺当日 OMS 账户估值')
        pos_present = bool(pos) or not qty
        if pos_present or acc:
            equal(set(pos),set(qty),day+' position set')
        else:
            gaps.append('缺当日持仓快照')
        mv, floating, rates, protect = {}, {}, {}, {}
        for s,q in qty.items():
            p = pos.get(s)
            if p:
                equal(number(p['qty']),q,day+' position qty '+s)
                equal(number(p['avg_cost']),book.cost[s]/q,day+' average cost '+s)
            close = quotes.get(s)
            if close is not None:
                mv[s] = q*close
            elif p:
                mv[s] = number(p['mv'])
            if s in mv:
                floating[s] = mv[s] - book.cost[s]
                rates[s] = pct(floating[s],book.cost[s])
                if p:
                    equal(number(p['mv']),mv[s],day+' market value '+s)
                    equal(number(p['unrealized']),floating[s],day+' unrealized '+s)
            protect[s] = book.protection(s,close,rates.get(s))
        if any(s not in quotes for s in set(qty)|{t['symbol'] for t in trades}):
            gaps.append('缺部分收盘行情')
        assets = book.cash + sum(mv.values()) if len(mv)==len(qty) else None
        nav = None
        if acc:
            equal(number(acc['cash']),book.cash,day+' cash')
            equal(number(acc['units']),book.units,day+' units')
            stated_assets,nav = number(acc['assets']),number(acc['nav'])
            require(stated_assets>0 and nav>0, day+' invalid equity')
            equal(nav,stated_assets/book.units,day+' NAV')
            if assets is not None:
                equal(stated_assets,assets,day+' assets')
            assets = stated_assets
        history.append((day,nav))
        state = fund_state(history)
        contribution = defaultdict(D)
        market = execution = ZERO
        enough = all(s in previous_quotes and s in quotes for s,q in before.items() if q) and all(t['symbol'] in quotes for t in trades)
        if enough:
            for s,q in before.items():
                if q:
                    change = q*(quotes[s]-previous_quotes[s])
                    market += change
                    contribution[s] += change
            for t in trades:
                sign = 1 if t['side']=='BUY' else -1
                gap_pnl = sign*number(t['qty'])*(quotes[t['symbol']]-number(t['price']))
                execution += gap_pnl
                contribution[t['symbol']] += gap_pnl - number(t['fee'])
        daily = assets-previous_assets if acc and previous_assets is not None else None
        residual = daily-sum(contribution.values()) if enough and daily is not None else None
        if residual is not None:
            equal(residual,0,day+' daily contribution residual')
        realized = sum(book.realized.values(),ZERO)
        unreal = sum(floating.values(),ZERO) if len(floating)==len(qty) else None
        if assets is not None and unreal is not None:
            equal(assets-book.opening_cash,realized+unreal,day+' cumulative P&L')
        weights = {s:pct(v,assets) for s,v in mv.items()} if assets else {}
        cash_weight = pct(book.cash,assets) if assets else None
        day_ret = pct(nav-previous_nav,previous_nav) if nav is not None and previous_nav else None
        if state['fund_alert'] and not state['window_complete']:
            gaps.append('近期净值窗口缺估值日，实际高点可能更高')
        elif not gaps and state['fund_alert'] is None:
            gaps.append('近期净值窗口不足 60 日' if len(history)<60 else '近期窗口缺估值日，连续回撤规则待定')
        if daily is None or not enough:
            gaps.append('当日归因覆盖不足')
        reconciled = bool(acc and pos_present and residual==0 and all(s in quotes for s in qty))
        buys = sum((number(t['qty'])*number(t['price']) for t in trades if t['side']=='BUY'),ZERO)
        sells = sum((number(t['qty'])*number(t['price']) for t in trades if t['side']=='SELL'),ZERO)
        headline = pick_headline(previous_assets, buys, sells, reconciled, day_ret,
                                 state['fund_alert'], state['drawdown_pct'])
        kinds, changes_verified = {}, {}
        for s in sorted({t['symbol'] for t in trades}):
            old, now = before.get(s,ZERO), qty.get(s,ZERO)
            kinds[s] = '新开' if not old else '清仓' if not now else '加仓' if now>old else '减仓' if now<old else '调整'
            net_qty = sum((number(t['qty'])*(1 if t['side']=='BUY' else -1) for t in trades if t['symbol']==s),ZERO)
            changes_verified[s] = bool(pos_present and previous_position_snapshot is not None
                and number(pos[s]['qty'])-previous_position_snapshot.get(s,ZERO)==net_qty) if s in pos else bool(
                pos_present and previous_position_snapshot is not None and -previous_position_snapshot.get(s,ZERO)==net_qty)
        alerts = {}
        for s in qty:
            previous = {} if s in book.opened else previous_alerts.get(s,{})
            rate = rates.get(s)
            alerts[s] = {
                'loss':alert_event(previous.get('loss'), rate < -10 if rate is not None else None, -rate if rate is not None else None),
                'guard':alert_event(previous.get('guard'), protect[s]['alert'], protect[s]['drawdown_pct']),
            }
        fund_event = alert_event(previous_fund_event,state['fund_alert'],state['drawdown_pct'])
        contribution_pct = {s:pct(v,previous_assets) for s,v in contribution.items()} if enough and day_ret is not None and previous_assets else {}
        result.append(dict(date=day,headline=headline,assets=assets,nav=nav,cash=book.cash,
                           daily_pnl=daily,daily_return_pct=day_ret,market_pnl=market if enough else None,
                           trading_pnl_before_fees=execution if enough else None,fees=fees,net_sales=net_sales,
                           contribution=dict(contribution) if enough else None,reconciliation_residual=residual,
                           realized_net_total=realized,realized_by_symbol=dict(book.realized),unrealized_total=unreal,
                           prior_unrealized_total=previous_unreal,
                           quantities=qty,floating=floating,loss_rates=rates,loss_names=sorted(s for s,v in rates.items() if v < -10),
                           weights=weights,prior_weights=previous_weights,cash_weight_pct=cash_weight,
                           prior_cash_weight_pct=previous_cash_weight,stock_weight_pct=100-cash_weight if assets else None,
                           protection=protect,sales=sales,closed=[s['symbol'] for s in sales if s['symbol'] not in qty],
                           prior_rates=previous_rates, alert_states=alerts, fund_event=fund_event,
                           kinds=kinds, changes_verified=changes_verified,
                           same_close={s:s in quotes and s in previous_quotes and quotes[s]==previous_quotes[s] for s in qty},
                           cost_diluted={s:book.cost[s]/qty[s]<before_cost[s]/before[s] for s in qty if before.get(s)},
                           prior_stock_weight_pct=previous_stock_weight, contribution_pct=contribution_pct, markets=markets,
                           reconciled=reconciled,
                           coverage='；'.join(gaps) or '完整',**state))
        previous_assets = assets if acc else None
        previous_unreal = unreal
        previous_nav,previous_quotes,previous_weights,previous_cash_weight = nav,quotes,weights,cash_weight
        previous_rates, previous_alerts, previous_fund_event = rates, alerts, fund_event
        previous_position_snapshot = {s:number(p['qty']) for s,p in pos.items()} if pos_present else None
        previous_stock_weight = result[-1]['stock_weight_pct']
    return result

def digest(data):
    return hashlib.sha256(data).hexdigest()

def owned(out):
    try:
        if out.is_symlink() or {p.name for p in out.iterdir()} != {'daily.json','REPORT.md','receipt.json'}:
            return False
        r = json.loads((out/'receipt.json').read_text())
        return r['format']==FORMAT and all(not (out/k).is_symlink() and digest((out/k).read_bytes())==r['sha256'][k]
                                           for k in ('daily.json','REPORT.md'))
    except (OSError,ValueError,KeyError,TypeError):
        return False

def archive(out):
    if owned(out):
        target = out.with_name(out.name+'-prior-'+uuid4().hex[:12])
        out.rename(target)
        print(f'Previous successful report preserved at {target}',file=sys.stderr)

def publish(out, rows):
    payload = {'daily.json':(json.dumps(rows,ensure_ascii=False,indent=2,default=str)+'\n').encode(),
               'REPORT.md':render_report(rows).encode()}
    receipt = {'format':FORMAT,'as_of':rows[-1]['date'],'sha256':{k:digest(v) for k,v in payload.items()}}
    payload['receipt.json'] = (json.dumps(receipt,sort_keys=True,indent=2)+'\n').encode()
    if out.exists():
        require(owned(out) or (out.is_dir() and not out.is_symlink() and not any(out.iterdir())), 'refusing unrecognized output directory')
        if owned(out) and all((out/k).read_bytes()==v for k,v in payload.items()):
            return
    out.parent.mkdir(parents=True,exist_ok=True)
    with TemporaryDirectory(dir=out.parent) as tmp:
        stage = Path(tmp)/'stage'
        stage.mkdir()
        for key,data in payload.items():
            (stage/key).write_bytes(data)
        archive(out)
        stage.replace(out)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',required=True,type=Path)
    parser.add_argument('--out',required=True,type=Path)
    parser.add_argument('--as-of')
    args = parser.parse_args()
    try:
        require(not args.out.is_symlink() and args.input.resolve()!=args.out.resolve(),'separate input and output required')
        rows = replay(args.input,args.as_of)
        publish(args.out,rows)
    except (ValueError,InvalidOperation,OSError,KeyError,TypeError) as err:
        archive(args.out)
        print(f'REJECTED: {err}',file=sys.stderr)
        return 1
    print(f'SUMMARY: days={len(rows)}; reconciled={sum(r["reconciled"] for r in rows)}; last={rows[-1]["date"]}; published={args.out}')
    return 0

if __name__ == '__main__':
    sys.exit(main())
