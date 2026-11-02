$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
$Python = Join-Path (Get-Location) ".venv\Scripts\python.exe"
if (!(Test-Path $Python)) {
    py -3.13 scripts/setup.py
    if ($LASTEXITCODE -ne 0) { throw "Setup failed. Install Python 3.13 and retry." }
}
& $Python run_studio.py @args
exit $LASTEXITCODE
