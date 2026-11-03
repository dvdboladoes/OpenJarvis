# Builds Jarvis.exe (the double-click launcher) into %LOCALAPPDATA%\OpenJarvis
# and creates the «Jarvis» shortcuts on the Desktop and in the Start Menu.
#
#   powershell -ExecutionPolicy Bypass -File packaging\windows\build_exe.ps1

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = (Resolve-Path (Join-Path $here "..\..")).Path
$appDir = Split-Path $root -Parent
$work = Join-Path $env:TEMP "jarvis-build"

Set-Location $root
uv run python -c "from pathlib import Path; from openjarvis.hud.tray import save_ico; save_ico(Path(r'$appDir') / 'jarvis.ico')"

uvx --from "pyinstaller==6.16.0" pyinstaller --noconfirm --onefile --noconsole `
    --name Jarvis --icon (Join-Path $appDir "jarvis.ico") `
    --distpath $appDir --workpath $work --specpath $work `
    (Join-Path $here "jarvis_launcher.py")

uv run jarvis hud --instalar
Write-Host "Listo: $appDir\Jarvis.exe"
