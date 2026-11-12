@echo off
setlocal DisableDelayedExpansion
"%SystemRoot%\System32\chcp.com" 65001 >nul
title Portfolio Debrief（组合复盘）
set "TDL_ARCHIVE=python-3.13.17-embed-amd64.zip"
set "TDL_SHA=9e35319bffedab8b36ad44d8f377cb4f1733bcec257a0b6d04d94f01486ddb38"
if exist "runtime\python.exe" if exist "runtime\.ready-%TDL_SHA%" goto launch
echo 首次打开正在校验并准备本机运行环境，请稍候。
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NoLogo -NonInteractive -Command ^
 "$ErrorActionPreference='Stop'; $root=(Get-Location).ProviderPath;" ^
 "$runtime=[IO.Path]::Combine($root,'runtime'); $zip=[IO.Path]::Combine($root,'app',$env:TDL_ARCHIVE);" ^
 "$stage=[IO.Path]::Combine($root,'.runtime-'+[Guid]::NewGuid().ToString('N')); $marker='.ready-'+$env:TDL_SHA;" ^
 "try { $lock=[IO.File]::OpenRead($zip); try {" ^
 "if ((Get-FileHash -InputStream $lock -Algorithm SHA256).Hash -ne $env:TDL_SHA) {exit 2};" ^
 "Add-Type -AssemblyName System.IO.Compression.FileSystem; [IO.Compression.ZipFile]::ExtractToDirectory($zip,$stage);" ^
 "foreach ($file in 'python.exe','python313.dll','python313.zip','python313._pth') {if (-not [IO.File]::Exists([IO.Path]::Combine($stage,$file))) {throw 'incomplete'}};" ^
 "[IO.File]::WriteAllText([IO.Path]::Combine($stage,$marker),$env:TDL_SHA);" ^
 "try {[IO.Directory]::Move($stage,$runtime)} catch [IO.IOException] {if (-not ([IO.File]::Exists([IO.Path]::Combine($runtime,$marker)) -and [IO.File]::Exists([IO.Path]::Combine($runtime,'python.exe')))) {throw}};" ^
 "} finally {$lock.Dispose()};" ^
 "} catch {exit 1} finally {if ([IO.Directory]::Exists($stage)) {[IO.Directory]::Delete($stage,$true)}}; exit 0"
set "prepared=%errorlevel%"
if "%prepared%"=="2" goto checksum_failed
if not "%prepared%"=="0" goto prepare_failed

:launch
echo 正在生成持仓表。首次使用或更改目录时，请在弹出窗口选择输入和输出位置。
"runtime\python.exe" -I -B "app\examples\oms_replay\manager.py" %1
if not "%errorlevel%"=="0" goto run_failed
exit /b 0

:checksum_failed
echo 运行环境校验失败。请重新获取完整便携包并解压到新的本机目录。
exit /b 2

:prepare_failed
echo 无法准备运行环境。请将完整 ZIP 解压到新的、可写的本机短路径，然后再次双击。
echo 如果公司终端限制运行 PowerShell 或便携程序，请联系 IT。
exit /b 1

:run_failed
echo 持仓表未生成。请查看弹窗或上方提示，确认三份导出和输出目录后重试。
exit /b 1
