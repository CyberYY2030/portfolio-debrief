"""Build the offline Windows x64 bundle without opening or executing its nested Python archive."""
import argparse
import hashlib
from pathlib import Path
import sys
import tempfile
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile

ROOT = Path(__file__).resolve().parents[1]
WINDOWS = ROOT / 'windows'
REPLAY = ROOT / 'examples' / 'oms_replay'
RUNTIME = 'python-3.13.17-embed-amd64.zip'
SHA256 = '9e35319bffedab8b36ad44d8f377cb4f1733bcec257a0b6d04d94f01486ddb38'
SOURCES = ('fetch_bars.py', 'fetch_index.py', 'homepage_v2.py', 'manager.py', 'manager_workbook.py', 'daily.py', 'oms_daily.py', 'render.py', 'replay.py')
DEMO = ('各产品持仓.xlsx', '成交查询.xlsx', '资产查询.xlsx', 'SYNTHETIC.txt')


def windows_text(text, *, bom=False):
    return text.replace('\r\n', '\n').replace('\n', '\r\n').encode('utf-8-sig' if bom else 'utf-8')


def launcher(setup):
    # Only a fixed literal is passed to CALL: cmd's second expansion never sees the user's path.
    argument = ' --setup' if setup else ''
    return windows_text(f'''@echo off
setlocal DisableDelayedExpansion
"%SystemRoot%\\System32\\chcp.com" 65001 >nul
pushd "%~dp0"
if errorlevel 1 goto bad_directory
call app\\bootstrap.cmd{argument}
set "result=%errorlevel%"
popd
if not "%result%"=="0" pause
exit /b %result%
:bad_directory
echo 请将完整便携包解压到可写的本机目录后重试。
pause
exit /b 1
''')


def build(runtime, output):
    archive = runtime.read_bytes()
    if hashlib.sha256(archive).hexdigest() != SHA256:
        raise ValueError('Python runtime SHA-256 mismatch')
    bootstrap = (WINDOWS / 'bootstrap.cmd').read_text(encoding='utf-8')
    if RUNTIME not in bootstrap or SHA256 not in bootstrap:
        raise ValueError('Bootstrap and build do not pin the same runtime')
    sys.path.insert(0, str(REPLAY))
    import oms_fixture

    with tempfile.TemporaryDirectory() as tmp:
        samples = Path(tmp) / 'fixture'
        oms_fixture.build(samples)
        # Exclusive creation protects a previously reviewed delivery. Delete only our incomplete output.
        with output.open('xb') as stream:
            try:
                with ZipFile(stream, 'w', compression=ZIP_DEFLATED) as bundle:
                    bundle.writestr('生成持仓表.cmd', launcher(False))
                    bundle.writestr('更改目录.cmd', launcher(True))
                    readme = (WINDOWS / 'README.md').read_text(encoding='utf-8')
                    bundle.writestr('使用说明.txt', windows_text(readme, bom=True))
                    bundle.writestr('app/bootstrap.cmd', windows_text(bootstrap))
                    bundle.writestr('app/' + RUNTIME, archive, compress_type=ZIP_STORED)
                    for name in SOURCES:
                        bundle.write(REPLAY / name, 'app/examples/oms_replay/' + name)
                    for name in DEMO:
                        bundle.write(samples / 'd1' / name, '试用数据/' + name)
            except BaseException:
                stream.close()
                output.unlink()
                raise


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    build(args.runtime, args.output)
    print('Created:', args.output)
