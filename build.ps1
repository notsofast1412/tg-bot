#requires -Version 5.1
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
if ($env:PAINTWATCH_BUILD_PYTHON) {
    & $env:PAINTWATCH_BUILD_PYTHON -m venv .venv
} else {
    & py -3.12 -m venv .venv
}
if ($LASTEXITCODE -ne 0) { throw 'Python 3.12 is required.' }
$pythonExe = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
& $pythonExe -m pip install --require-hashes -r requirements-dev.lock
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
& $pythonExe -m unittest discover -s tests -v
if ($LASTEXITCODE -ne 0) { throw 'Tests failed.' }
& .\.venv\Scripts\ruff.exe check .
if ($LASTEXITCODE -ne 0) { throw 'Lint failed.' }
& $pythonExe -m PyInstaller --noconfirm --clean --windowed --onedir --name PaintWatch --collect-all playwright --hidden-import pystray._win32 --exclude-module pystray._darwin --exclude-module pystray._xorg --exclude-module pystray._gtk --exclude-module pystray._appindicator run_app.py
if ($LASTEXITCODE -ne 0) { throw 'Windows build failed.' }
Copy-Item README.md dist\PaintWatch\README.md -Force
Copy-Item docs\windows.md dist\PaintWatch\WINDOWS.md -Force
Copy-Item extension dist\PaintWatch\extension -Recurse -Force
Write-Host 'Built dist\PaintWatch\PaintWatch.exe. Distribute the entire PaintWatch folder.'
