# build_nuitka.ps1
# Build Office Radio Overlay via Nuitka (standalone mode)

Write-Host "=== Cleaning previous build ===" -ForegroundColor Yellow
if (Test-Path "main.dist") { Remove-Item "main.dist" -Recurse -Force }
if (Test-Path "main.build") { Remove-Item "main.build" -Recurse -Force }

Write-Host "=== Running Nuitka compilation ===" -ForegroundColor Cyan

python -m nuitka --standalone --assume-yes-for-downloads --include-module=sounddevice --include-package=pjsua2 --include-package-data=pjsua2 --include-data-dir=templates=templates --include-data-dir=static=static --include-data-dir=assets=assets --include-data-file=cacert.pem=cacert.pem --windows-console-mode=force main.py

if ($LASTEXITCODE -eq 0) {
    Write-Host ""
    Write-Host "=== BUILD SUCCESSFUL ===" -ForegroundColor Green
    Write-Host "Executable: main.dist\main.exe" -ForegroundColor Green
} else {
    Write-Host ""
    Write-Host "=== BUILD FAILED - see output above ===" -ForegroundColor Red
}