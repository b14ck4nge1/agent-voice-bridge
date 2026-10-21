# Creates .venv, installs Agent Voice Bridge, and generates local secrets.
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

$py = Get-Command py -ErrorAction SilentlyContinue
if ($py) { & py -3 -m venv .venv } else { & python -m venv .venv }
if ($LASTEXITCODE -ne 0) { throw "Could not create .venv (need Python 3.11+)" }

& .\.venv\Scripts\python.exe -m pip install --upgrade pip
& .\.venv\Scripts\python.exe -m pip install -e ".[dev]"
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

$envFile = Join-Path $env:LOCALAPPDATA "AgentVoiceBridge\bridge.env"
if (Test-Path $envFile) {
    Write-Host "Secrets already exist at $envFile (keeping them)."
} else {
    & .\.venv\Scripts\avb-admin.exe init
}
Write-Host ""
Write-Host "Next: scripts\start.ps1, then .\.venv\Scripts\avb-admin install-hooks --claude --codex --dry-run"
