param([switch]$KeepWindowOnError)

$ErrorActionPreference = 'Stop'
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
Push-Location -LiteralPath $PSScriptRoot
try {
    if (-not (Test-Path -LiteralPath $python)) {
        & python.exe -m venv .venv
        if ($LASTEXITCODE -ne 0) { throw 'Python virtual environment creation failed.' }
    }
    & $python -X utf8 launch.py
    if ($LASTEXITCODE -ne 0) { throw 'Viewer startup or shutdown failed. See the details above.' }
} catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    if ($KeepWindowOnError) {
        $null = Read-Host 'Press Enter to close this window'
    }
    exit 1
} finally {
    Pop-Location
}
