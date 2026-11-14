$ErrorActionPreference = 'Stop'
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
Push-Location -LiteralPath $PSScriptRoot
try {
    if (-not (Test-Path -LiteralPath $python)) {
        & python.exe -m venv .venv
        if ($LASTEXITCODE -ne 0) { throw 'Python virtual environment creation failed.' }
    }
    & $python -X utf8 launch.py
    if ($LASTEXITCODE -ne 0) { throw 'Viewer startup failed.' }
} finally {
    Pop-Location
}
