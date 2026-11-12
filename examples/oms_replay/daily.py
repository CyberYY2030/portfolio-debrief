"""Append an OMS export and its closing review to a local daily ledger."""
import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import unicodedata
import zipfile
from collections import Counter
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from xml.etree import ElementTree as ET

import oms_daily as od
import render

FORMAT = 'daily-ledger-v1'
CODE = ('daily.py', 'oms_daily.py', 'render.py', 'replay.py')
PRODUCT_ERRORS = (ValueError, InvalidOperation, OSError, KeyError, TypeError, ArithmeticError)
RUN_ERRORS = PRODUCT_ERRORS + (zipfile.BadZipFile, ET.ParseError)
LABEL = re.compile(r'[A-Za-z][A-Za-z ]*[A-Za-z](?: [一-鿿]+\.xlsx)?')


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, default=str) + '\n').encode('utf-8')


def read_json(file):
    return json.loads(file.read_text(encoding='utf-8'))


def digest(content):
    return hashlib.sha256(content).hexdigest()


def input_bytes(folder):
    names = list(od.FILES.values())
    names += [n for n in ('SYNTHETIC.txt', 'SIMULATED.txt') if (folder / n).is_file()]
    return {n: (folder / n).read_bytes() for n in names}


def records(root):
    """Index canonical dates and positive integer directories, comparing numerically."""
    found = {}
    if not root.exists():
        return found
    od.require(root.is_dir() and not root.is_symlink(), 'invalid ledger directory')
    for day in root.iterdir():
        if not day.is_dir():
            continue
        try:
            valid = date.fromisoformat(day.name).isoformat() == day.name
        except ValueError:
            valid = False
        if not valid:
            continue
        od.require(not day.is_symlink(), 'invalid ledger directory')
        revisions = {}
        for entry in day.iterdir():
            if entry.is_dir() and re.fullmatch(r'[1-9][0-9]*', entry.name):
                od.require(not entry.is_symlink(), 'invalid ledger directory')
                revisions[int(entry.name)] = entry
        if revisions:
            found[day.name] = revisions
    return found


def inspect_ledger(ledger):
    if ledger.exists():
        od.require(ledger.is_dir(), 'ledger is not a directory')
        if any(ledger.iterdir()):
            marker = ledger / 'LEDGER.json'
            od.require(not marker.is_symlink() and read_json(marker) == {'format': FORMAT}, 'invalid ledger marker')
    archives = records(ledger / 'exports')
    for versions in archives.values():
        for folder in versions.values():
            hashes = read_json(folder / 'SHA256.json')
            members = list(folder.iterdir())
            od.require(all(p.is_file() and not p.is_symlink() for p in members), 'invalid archive contents')
            actual = {p.name: digest(p.read_bytes()) for p in members if p.name != 'SHA256.json'}
            od.require(set(od.FILES.values()) <= set(actual), 'incomplete archived export')
            od.require(actual == hashes, 'archived export hash mismatch')
    return archives, records(ledger / 'days')


def publish(ledger, archive_target, archive_payload, version_target, version_payload):
    """Stage complete directories inside the ledger; roll back this run on failure."""
    created, installed = [], []
    stage = None

    def mkdir(folder):
        if not folder.exists():
            mkdir(folder.parent)
            folder.mkdir()
            created.append(folder)

    try:
        mkdir(ledger)
        stage = Path(tempfile.mkdtemp(prefix='.daily-', dir=ledger))
        batches = []
        if archive_payload is not None:
            batches.append(('export', archive_target, archive_payload))
        batches.append(('version', version_target, version_payload))
        for name, target, payload in batches:
            pending = stage / name
            pending.mkdir()
            for filename, content in payload.items():
                (pending / filename).write_bytes(content)
        marker = ledger / 'LEDGER.json'
        if not marker.exists():
            pending_marker = stage / 'LEDGER.json'
            pending_marker.write_bytes(encoded({'format': FORMAT}))
            os.rename(pending_marker, marker)
            installed.append(marker)
        for name, target, _ in batches:
            mkdir(target.parent)
            od.require(not target.exists(), 'version already exists')
            os.rename(stage / name, target)
            installed.append(target)
    except BaseException:
        for target in reversed(installed):
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
        raise
    finally:
        if stage is not None:
            shutil.rmtree(stage)
        # Only directories made by this run can be removed, and only if empty.
        for folder in reversed(created):
            if not any(folder.iterdir()):
                folder.rmdir()


def events_for(facts):
    events = []
    for product, row in sorted(facts.items()):
        if row.get('fund_alert') is True:
            event = row['fund_event']
            events.append(dict(product=product, scope='fund', kind='fund', state=event['state'],
                               days=event['days'], drawdown_pct=row.get('drawdown_pct')))
        for security, states in sorted(row['alert_states'].items()):
            for kind in ('loss', 'guard'):
                event = states.get(kind) or {}
                if event.get('active') is True:
                    events.append(dict(product=product, scope='holding', security=security,
                                       name=row['names'][security], kind=kind, state=event['state'],
                                       days=event['days'], weight_pct=row['weights'].get(security),
                                       loss_rate_pct=row['loss_rates'].get(security)))
    return events


def followups_for(facts, previous, folder):
    if previous is None:
        return []
    prior = read_json(folder / 'facts.json')
    events = read_json(folder / 'events.json')
    followups = []
    for event in events:
        product = event['product']
        if event['scope'] != 'holding' or product not in facts:
            continue
        security, row = event['security'], facts[product]
        then = Decimal(str(prior[product]['quantities'][security]))
        now = Decimal(str(row['quantities'].get(security, 0)))
        if now == then:
            action = '未变'
        elif security not in row['kinds']:
            action = '变动无成交'
        elif now > then:
            action = '加仓'
        else:
            action = '清仓' if now == 0 else '减仓'
        followups.append(dict(product=product, security=security, name=event['name'], kind=event['kind'],
                              alert_day=previous['day'], state_then=event['state'], action=action,
                              weight_then=prior[product]['weights'][security],
                              weight_now=row['weights'].get(security) if security in row['quantities'] else None))
    return sorted(followups, key=lambda f: (f['product'], f['security'], f['kind'] != 'loss'))


def summary(folder, status):
    manifest = read_json(folder / 'MANIFEST.json')
    events = read_json(folder / 'events.json')
    followups = read_json(folder / 'followups.json')
    return (f"SUMMARY: day={manifest['day']} revision={manifest['revision']} status={status} "
            f"products={len(manifest['products'])} rejected={len(manifest['rejected'])} "
            f"events={len(events)} followups={len(followups)}")


def product_filenames(names, day, *, max_units=254):
    """Allocate portable CSV basenames by product ID. Leave one unit for the .xlsx extension."""
    def clean(text):
        return re.sub(r'[<>:"/\\|?*\[\]\x00-\x1f]', '_', str(text))

    def key(filename):
        return unicodedata.normalize('NFC', filename.upper().casefold())

    def filename(product, *, disambiguate=False, number=1):
        start, end = '基金持仓 ', f' {day}.csv'
        suffix = ''
        if disambiguate:
            serial = '' if number == 1 else f' ({number})'
            # Both arbitrary product IDs and names must fit a bounded path component.
            id_room = max_units - len(start + end + ' ()' + serial) - 2
            od.require(id_room > 0, 'output path too long')
            tag = clean(product).encode('utf-8')[:48].decode('utf-8', errors='ignore')
            tag = tag.encode('utf-16-le')[:id_room * 2].decode('utf-16-le', errors='ignore')
            suffix = f' ({tag}){serial}'
        room = max_units - len((start + suffix + end).encode('utf-16-le')) // 2
        byte_room = 254 - len((start + suffix + end).encode('utf-8'))
        od.require(min(room, byte_room) >= 1, 'output path too long')
        middle = clean(names[product]).encode('utf-8')[:byte_room].decode('utf-8', errors='ignore')
        middle = middle.encode('utf-16-le')[:room * 2].decode('utf-16-le', errors='ignore')
        return start + middle + suffix + end

    original = {p: filename(p) for p in names}
    counts = Counter(key(f) for f in original.values())
    used = set(counts)  # reserve natural names before trying any numbered variants
    result = {}
    for product in sorted(names):
        candidate = original[product]
        if counts[key(candidate)] > 1:
            number = 1
            candidate = filename(product, disambiguate=True)
            while key(candidate) in used:
                number += 1
                candidate = filename(product, disambiguate=True, number=number)
            used.add(key(candidate))
        result[product] = candidate
    return result


def filename_budget(ledger, version):
    if os.name != 'nt':
        return 254
    # Desktop Excel permits 218 UTF-16 units for the full workbook path. Include staging.
    parents = (version, ledger / '.daily-XXXXXXXX' / 'version')
    used = max(len(str(folder.resolve()).encode('utf-16-le')) // 2 for folder in parents)
    return min(254, 218 - used - 2)  # separator and the extra x in .xlsx


def csv_inputs(exports, product, day):
    """Keep raw identities and normalize only the CSV's numeric cells."""
    def numeric(row, fields):
        return {**row, **{field: od.number(row.get(field) or 0) for field in fields}}

    holds = [numeric(row, (
        '持仓数量', '当前成本（全价人民币）', '当前成本（全价本币）', '持仓市值(人民币)',
        '浮动盈亏', '最新价', '当日买入数量', '当日卖出数量', '当日买入均价', '当日卖出均价'))
        for row in od.product_rows(exports[-1], 'hold', product)]
    assets = {}
    for export in exports:
        for row in od.product_rows(export, 'asset', product):
            if not od.is_total(row) and row['日期'] <= day:
                if row['日期'] == day and export is exports[-1]:
                    od.require(row.get('净资产') not in (None, ''), 'missing current net assets')
                assets[row['日期']] = numeric(row, (
                    '单位净值', '单位分红金额', '现金资产', '回购资产', '期初投入', '总资产', '净资产'))
    current = assets[day]
    current['产品份额'] = od.number(current['产品份额'])
    return holds, assets


def run(source, ledger):
    od.output_guard([source], ledger)
    archives, days = inspect_ledger(ledger)
    for filename in od.FILES.values():
        od.require((source / filename).is_file(), 'missing ' + filename)
    raw = input_bytes(source)
    today = od.load_input(source)
    day = today['day']
    hashes = {name: digest(content) for name, content in raw.items()}
    latest_n = max(archives.get(day, {}), default=0)
    reuse = latest_n > 0 and read_json(archives[day][latest_n] / 'SHA256.json') == hashes
    n = latest_n if reuse else latest_n + 1
    selected = [{'day': d, 'n': max(archives[d])} for d in sorted(archives) if d < day]
    exports = [od.load_input(archives[e['day']][e['n']]) for e in selected]
    od.require(all(e['day'] == entry['day'] for e, entry in zip(exports, selected)), 'archive date mismatch')
    exports.append(today)
    selected.append({'day': day, 'n': n})
    earlier = [d for d in days if d < day]
    previous = None
    prior_folder = None
    if earlier:
        prior_day = max(earlier)
        prior_revision = max(days[prior_day])
        previous = {'day': prior_day, 'revision': prior_revision}
        prior_folder = days[prior_day][prior_revision]
    code = {name: digest((Path(__file__).parent / name).read_bytes()) for name in CODE}
    identity = dict(exports=selected, code=code, previous=previous)
    latest_r = max(days.get(day, {}), default=0)
    reason = []
    if latest_r:
        latest = days[day][latest_r]
        manifest = read_json(latest / 'MANIFEST.json')
        reason = [label for field, label in (('exports', 'export'), ('code', 'code'), ('previous', 'previous'))
                  if manifest[field] != identity[field]]
        if not reason:
            return summary(latest, 'unchanged')
    revision = latest_r + 1
    products = sorted({r.get('产品序号') for r in today['tables']['asset'][2]
                       if not od.is_total(r) and r.get('产品序号')})
    title = f'# {day} 收盘复盘（第 {revision} 版）'
    if any(e['simulated'] for e in exports):
        title += '（模拟数据）'
    parts, facts, rejected, csv_payload = [title, *od.NOTES], {}, {}, {}
    names = {p: max((r for r in od.product_rows(today, 'asset', p) if not od.is_total(r)),
                    key=lambda r: (r.get('日期') == day, r.get('日期') or ''))['产品'] for p in products}
    version = ledger / 'days' / day / str(revision)
    filenames = product_filenames(names, day, max_units=filename_budget(ledger, version))
    for product in products:
        name, filename = names[product], filenames[product]
        try:
            name, rows = od.build_product(exports, product)
            od.require(rows and rows[-1]['date'] == day, 'no row for the day')
            page = render.render_day(rows[-1])
        except PRODUCT_ERRORS as err:
            rejected[product] = str(err)
            parts.extend([f'## 产品 {product}', f'今日没有页面：数据未通过校验（{err}）。'])
            csv_payload[filename] = render.product_csv(name, day, rejected=rejected[product])
            continue
        facts[product] = rows[-1]
        parts.extend([f'## {name}', page])
        try:
            holds, assets = csv_inputs(exports, product, day)
            csv_payload[filename] = render.product_csv(name, day, page, rows[-1], holds, assets)
        except PRODUCT_ERRORS as err:
            message = str(err)
            label = message if type(err) is ValueError and LABEL.fullmatch(message) else type(err).__name__
            csv_payload[filename] = render.product_csv(name, day, rejected=label)
    od.require(facts, 'all products rejected')
    events = events_for(facts)
    followups = followups_for(facts, previous, prior_folder)
    manifest = dict(format=FORMAT, day=day, revision=revision, reason=reason,
                    generated_at=datetime.now().astimezone().isoformat(timespec='seconds'),
                    products=sorted(facts), rejected=rejected, **identity)
    payload = {name: encoded(value) for name, value in (
        ('MANIFEST.json', manifest), ('facts.json', facts), ('events.json', events), ('followups.json', followups))}
    payload['PAGES.md'] = ('\n\n'.join(parts) + '\n').encode('utf-8')
    payload.update(csv_payload)
    # A producer replacing the live export mid-run must not mix facts and archived bytes.
    od.require(input_bytes(source) == raw, 'input changed during processing')
    archive_payload = None if reuse else {**raw, 'SHA256.json': encoded(hashes)}
    publish(ledger, ledger / 'exports' / day / str(n), archive_payload, version, payload)
    return summary(version, 'new')


class Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's default diagnostic can echo caller-supplied private values.
        raise ValueError('invalid command arguments')


def main():
    parser = Parser(description=__doc__)
    parser.add_argument('--input', required=True, type=Path)
    parser.add_argument('--ledger', required=True, type=Path)
    try:
        args = parser.parse_args()
        result = run(args.input, args.ledger)
    except RUN_ERRORS as err:
        # Labels written by require() name the cause; other errors may carry paths, keys or cell values, so only their class is shown.
        message = str(err)
        label = message if type(err) is ValueError and LABEL.fullmatch(message) else type(err).__name__
        print(f'REJECTED: {label}', file=sys.stderr)
        print('SUMMARY: status=rejected')
        return 1
    print(result)
    return 0


if __name__ == '__main__':
    sys.exit(main())
