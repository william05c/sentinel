# Build a single-file executable.
#
#   powershell -ExecutionPolicy Bypass -File scripts/build_exe.ps1
#
# Output: dist\sentinel.exe
#
# This bundles duckdb, pandas and lxml, so expect 150-250 MB. The browser extra is NOT
# bundled: crawl4ai needs Playwright's browser binaries on disk, which do not fit in a
# single file. Use the HTTP deployment if you need rendering.

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

Write-Host "installing build tooling"
& .\.venv\Scripts\python.exe -m pip install --quiet pyinstaller

Write-Host "building"
& .\.venv\Scripts\python.exe -m PyInstaller `
  --onefile `
  --name sentinel `
  --distpath dist `
  --workpath build `
  --specpath build `
  --collect-all duckdb `
  --collect-all lxml `
  --collect-submodules uvicorn `
  --hidden-import sentinel.web.app `
  --add-data "src/sentinel/warehouse/schema.sql;sentinel/warehouse" `
  --add-data "src/sentinel/reports/templates;sentinel/reports/templates" `
  --add-data "config;config" `
  scripts/sentinel_entry.py

Write-Host "built dist\sentinel.exe"
Write-Host "the executable expects config/ and a writable data/ directory beside it"
