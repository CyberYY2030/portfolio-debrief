"""Windows manager entry point. The local CLI remains available for verification."""
import argparse
import csv
import ctypes
from contextlib import contextmanager
from ctypes import wintypes
from hashlib import sha256
import json
import os
import re
from pathlib import Path
import shutil
import sys
import tempfile
from datetime import date, timedelta

# Embedded Windows Python isolates its search path from the script directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import daily
import fetch_index
import fetch_bars
import homepage_v2 as hp
from manager_workbook import write_workbook


class UserError(Exception):
    pass


def validate(source, output):
    source, output = source.resolve(), output.resolve()
    if any(ch in str(output) for ch in '[]'):
        raise UserError('Excel 双击打开不支持含方括号的输出路径，请选择另一个本机文件夹。')
    if not source.is_dir():
        raise UserError('找不到原始导出文件夹。请使用「更改目录」选择当前目录。')
    if output == source or output in source.parents or source in output.parents:
        raise UserError('输入与输出必须是分开的文件夹，不能相同或互相包含。')
    missing = [name for name in daily.od.FILES.values() if not (source / name).is_file()]
    if missing:
        raise UserError('原始导出文件夹缺少：' + '、'.join(missing))
    if output.exists() and not output.is_dir():
        raise UserError('输出位置必须是文件夹。')


@contextmanager
def output_lock(output):
    """The OS releases the lock if the application exits unexpectedly."""
    output.mkdir(parents=True, exist_ok=True)
    with (output / '.holdings.lock').open('a+b') as lock:
        if os.name == 'nt':
            import msvcrt
            lock.seek(0)
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as err:
                raise UserError('这个输出目录正在生成持仓表，请等待前一个窗口完成。') from err
        else:
            import fcntl
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as err:
                raise UserError('这个输出目录正在生成持仓表，请等待前一个窗口完成。') from err
        yield


def generate(source, output, *, index_dir=None, index_fetcher=None, clock=None, index_messages=None,
             bars_fetcher=None, monotonic=None, bars_messages=None):
    validate(source, output)
    ledger = output / '每日账本'
    index_dir = Path(index_dir) if index_dir is not None else output / '指数'
    bars_dir = output / '行情'
    with output_lock(output):
        summary = daily.run(source, ledger)
        fields = dict(item.split('=', 1) for item in summary.split()[1:])
        day, revision = fields['day'], fields['revision']
        version = ledger / 'days' / day / revision
        sheets = sorted(version.glob('*.csv'))
        if not sheets or len(sheets) != int(fields['products']) + int(fields['rejected']):
            raise UserError('产品数量与实际持仓表数量不符，已停止生成。')
        # Index enrichment is optional and never changes the ledger or blocks generation.
        if index_fetcher is not None:
            try:
                if not (index_dir / '沪深300.xlsx').exists():
                    existing = hp.read_index(index_dir, day)
                    if existing['status'] != 'valid' or day not in existing['values']:
                        first = min(receipt['day'] for receipt in hp.archive_receipts(ledger, day))
                        start = (date.fromisoformat(first) - timedelta(days=14)).isoformat()
                        index_fetcher(index_dir, start, now=(clock or fetch_index.east8_now)())
                        refreshed = hp.read_index(index_dir, day)
                        if refreshed['status'] != 'valid' or day not in refreshed['values']:
                            raise ValueError('缺少估值日 ' + day + ' 的已收盘数据')
            except Exception as err:
                message = fetch_index.failure_message(err)
                print(message)
                if index_messages is not None:
                    index_messages.append(message)
        # Archive integrity and parsing happen outside the per-product fallback boundary.
        batch = hp.batch_pages(ledger, day, index_dir=index_dir, bars_dir=bars_dir,
                               bars_fetcher=bars_fetcher, monotonic=monotonic, clock=clock)
        bars_message = fetch_bars.failure_message(batch['bar_errors'])
        if bars_message:
            print(bars_message)
            if bars_messages is not None:
                bars_messages.append(bars_message)
        filenames = daily.product_filenames(batch['names'], day, max_units=daily.filename_budget(ledger, version))
        if set(filenames.values()) != {f.name for f in sheets}:
            raise UserError('产品与账本持仓表不一致，已停止生成。')
        payload = {}; warnings = {}; rejected = 0
        for fid, name in filenames.items():
            old = (version / name).read_bytes()
            rows = list(csv.reader(old.decode('utf-8').splitlines()))
            is_rejected = rows[3][1].startswith('今日没有持仓表')
            rejected += is_rejected
            if is_rejected:
                payload[name] = (old, {})
            elif fid in batch['pages']:
                page = batch['pages'][fid]
                payload[name] = (page['csv'], page['options'])
            else:
                warnings[fid] = batch['errors'][fid]
                rows[0][0] += '｜新版首页生成失败，本页为旧版'
                import io
                buffer = io.StringIO(newline=''); csv.writer(buffer, lineterminator='\r\n').writerows(rows)
                payload[name] = (buffer.getvalue().encode('utf-8'), {'explanation': True})
        import behavior_review as br
        behavior_pages={fid:page['behavior'] for fid,page in batch['pages'].items() if not list(csv.reader(page['csv'].decode('utf-8').splitlines()))[3][1].startswith('今日没有持仓表')}
        receipt = {'source': {f.name: sha256(f.read_bytes()).hexdigest() for f in sheets},
                   'v2': {name: sha256(content).hexdigest() for name, (content, _) in payload.items()},
                   'index': batch['index']['files'], 'bars': batch['bars'], 'failures': warnings,
                   'behavior': {fid: sha256(daily.encoded(page)).hexdigest() for fid,page in behavior_pages.items()},
                   'behavior_failures': batch['behavior_errors'],
                   'code': {name: sha256((Path(__file__).parent / name).read_bytes()).hexdigest()
                            for name in ('homepage_v2.py', 'manager_workbook.py', 'manager.py', 'fetch_index.py', 'fetch_bars.py', 'behavior_review.py')}}
        parent = output / '持仓表' / day
        editions = sorted((p for p in parent.glob('第*版') if p.is_dir() and re.fullmatch(r'第[1-9][0-9]*版', p.name)),
                          key=lambda p: int(p.name[1:-1]))
        marker = '.source.json'; target = None
        for edition in reversed(editions):
            if not (edition / marker).is_file():
                raise UserError('目标版本缺少来源记录。请保留原目录，使用另一个输出目录。')
            if daily.read_json(edition / marker) == receipt:
                target = edition
                if any(not (target / Path(name).with_suffix('.xlsx')).is_file() for name in payload):
                    raise UserError('已生成的版本缺少工作簿。请先备份并移走该版本文件夹，然后重试。')
                break
        if target is None:
            number = int(editions[-1].name[1:-1]) + 1 if editions else 1
            target = parent / f'第{number}版'
            if os.name == 'nt' and any(len(str((target / Path(name).with_suffix('.xlsx')).resolve()).encode('utf-16-le')) // 2 > 218 for name in payload):
                raise UserError('Excel 的完整文件路径过长，请通过「更改目录」选择较短的本机输出路径。')
            parent.mkdir(parents=True, exist_ok=True)
            stage = Path(tempfile.mkdtemp(prefix='.w-', dir=parent))
            try:
                # Write errors deliberately propagate; publish only the complete edition.
                with tempfile.TemporaryDirectory() as temp:
                    for name, (content, options) in payload.items():
                        source_csv = Path(temp) / name; source_csv.write_bytes(content)
                        write_workbook(source_csv, stage / Path(name).with_suffix('.xlsx'), **options)
                behavior_folder=stage/'行为评估';behavior_folder.mkdir()
                for fid,page in behavior_pages.items():
                    (behavior_folder/(br.storage_name(fid)+'.json')).write_bytes(daily.encoded(page))
                    (behavior_folder/(br.storage_name(fid)+'.md')).write_text(br.markdown(page),encoding='utf-8')
                if batch['behavior_errors']:(stage/'BEHAVIOR_FAILURES.json').write_bytes(daily.encoded(batch['behavior_errors']))
                (stage / marker).write_bytes(daily.encoded(receipt))
                if warnings: (stage / 'V2_FAILURES.json').write_bytes(daily.encoded(warnings))
                hp.validate_sources(ledger, day, batch['receipts'], index_dir, batch['index'],
                                    bars_dir=bars_dir, bars=batch['bars'])
                for page in behavior_pages.values():br.validate_cache(page,bars_dir)
                stage.rename(target)
            finally:
                if stage.exists(): shutil.rmtree(stage)
        print(f'MANAGER V2 SUMMARY: products={len(sheets)} v2={len(sheets)-rejected-len(warnings)} fallback={len(warnings)} rejected={rejected}')
        for fid, reason in warnings.items(): print(batch['names'][fid] + '（' + fid + '）：' + reason)
        for fid,reason in batch['behavior_errors'].items():print(batch['names'][fid]+'（'+fid+'）：行为评估未能生成：'+reason)
        return target, len(sheets), rejected


def choose_folder(title):
    # Use the Windows shell picker, available without Tk in the portable runtime.
    shell, ole = ctypes.windll.shell32, ctypes.windll.ole32

    class BrowseInfo(ctypes.Structure):
        _fields_ = [('owner', wintypes.HWND), ('root', ctypes.c_void_p),
                    ('display', wintypes.LPWSTR), ('title', wintypes.LPCWSTR),
                    ('flags', wintypes.UINT), ('callback', ctypes.c_void_p),
                    ('param', wintypes.LPARAM), ('image', ctypes.c_int)]

    shell.SHBrowseForFolderW.argtypes = [ctypes.POINTER(BrowseInfo)]
    shell.SHBrowseForFolderW.restype = ctypes.c_void_p
    shell.SHGetPathFromIDListW.argtypes = [ctypes.c_void_p, wintypes.LPWSTR]
    ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    display = ctypes.create_unicode_buffer(260)
    info = BrowseInfo(None, None, ctypes.cast(display, wintypes.LPWSTR), title, 0x41, None, 0, 0)
    initialized = ole.CoInitialize(None) in (0, 1)
    try:
        item = shell.SHBrowseForFolderW(ctypes.byref(info))
        if not item:
            return None
        try:
            value = ctypes.create_unicode_buffer(260)
            if not shell.SHGetPathFromIDListW(item, value):
                raise UserError('请选择本机上的文件夹。')
            return Path(value.value)
        finally:
            ole.CoTaskMemFree(item)
    finally:
        if initialized:
            ole.CoUninitialize()


def notice(text, flags=0):
    return ctypes.windll.user32.MessageBoxW(None, text, 'Portfolio Debrief（组合复盘）', flags | 0x10000)


def desktop(config, setup=False):
    if setup or not config.exists():
        source = choose_folder('第 1 步：选择放有三份 OMS 导出的文件夹')
        if source is None:
            return
        output = choose_folder('第 2 步：选择本机输出文件夹（与输入目录分开）')
        if output is None:
            return
        validate(source, output)
        if notice(f'原始导出：{source}\n\n输出位置：{output}\n\n保存这两个目录，并生成持仓表？', 1) != 1:
            return
        config.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=config.parent, delete=False) as tmp:
            tmp.write(daily.encoded({'input': str(source), 'output': str(output)}))
        os.replace(tmp.name, config)
    else:
        try:
            settings = daily.read_json(config)
            source, output = Path(settings['input']), Path(settings['output'])
            if not source.is_absolute() or not output.is_absolute():
                raise ValueError('invalid settings')
        except (KeyError, TypeError, ValueError) as err:
            raise UserError('保存的目录设置无效，请双击「更改目录」重新选择。') from err
    index_messages = []
    bars_messages = []
    target, count, rejected = generate(source, output, index_fetcher=fetch_index.run,
                                       clock=fetch_index.east8_now, index_messages=index_messages,
                                       bars_fetcher=fetch_bars.tencent_fetch, bars_messages=bars_messages)
    message = f'{target.parent.name} · {target.name}\n共 {count} 份 Excel 工作簿。'
    if rejected:
        message += f'其中 {rejected} 份只含数据校验说明，请检查对应导出。'
    if (target / 'V2_FAILURES.json').is_file():
        failures = daily.read_json(target / 'V2_FAILURES.json')
        message += '\n新版首页失败，以下产品已用旧版：\n' + '\n'.join(fid + '：' + reason for fid, reason in failures.items())
    if (target / 'BEHAVIOR_FAILURES.json').is_file():
        failures = daily.read_json(target / 'BEHAVIOR_FAILURES.json')
        message += '\n行为评估未能生成，首页不受影响：\n' + '\n'.join(fid + '：' + reason for fid, reason in failures.items())
    if index_messages:
        message += '\n' + '\n'.join(index_messages)
    if bars_messages:
        message += '\n' + '\n'.join(bars_messages)
    notice(message + '\n\n点「确定」打开本次结果文件夹。')
    os.startfile(target)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--setup', action='store_true')
    args = parser.parse_args()
    try:
        desktop(Path(os.environ['LOCALAPPDATA']) / 'TradingDecisionLedger' / 'settings.json', args.setup)
    except Exception as err:
        known = {'all products rejected': '全部产品数据未通过校验。',
                 'invalid ledger marker': '每日账本目录不是本工具建立的有效账本。',
                 'archived export hash mismatch': '历史导出校验失败，请检查或恢复账本备份。',
                 'holding and fill dates differ': '持仓与成交的估值日不一致。',
                 'missing required columns in 资产查询.xlsx': '资产查询表缺少必需列。',
                 'output path too long': '输出路径过长，请使用较短的本机目录。'}
        detail = str(err) if isinstance(err, UserError) else known.get(str(err))
        if not detail:
            detail = f'请检查三份 OMS 原始导出是否齐全、格式正确，并确认输出目录可写。（{type(err).__name__}）'
        notice(detail, 0x10)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
