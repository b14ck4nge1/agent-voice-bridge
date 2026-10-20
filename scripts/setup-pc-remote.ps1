# Connect this Windows PC's Claude Code / Codex to an Agent Voice Bridge server.
#   powershell -ExecutionPolicy Bypass -File .\agent-voice-bridge\scripts\setup-pc-remote.ps1 -Url https://voice.example.com -Token <AVB_AGENT_TOKEN>
# The URL and token are printed by deploy/install.sh on the server (SETUP-DETAILS.txt).
param(
    [Parameter(Mandatory = $true)][string]$Url,
    [Parameter(Mandatory = $true)][string]$Token,
    [switch]$NoHooks
)
$ErrorActionPreference = "Stop"
if (-not $Url.StartsWith("https://")) { throw "Url must start with https://" }
$repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $repo

$py = Get-Command py -ErrorAction SilentlyContinue
if (-not (Test-Path .venv)) { if ($py) { & py -3 -m venv .venv } else { & python -m venv .venv } }
& .\.venv\Scripts\python.exe -m pip install --quiet --upgrade pip
& .\.venv\Scripts\python.exe -m pip install --quiet -e .
if ($LASTEXITCODE) { throw "pip install failed" }

$dir = Join-Path $env:LOCALAPPDATA "AgentVoiceBridge"
New-Item -ItemType Directory -Force -Path $dir | Out-Null
$hookEnv = Join-Path $dir "hook.env"
Set-Content -Path $hookEnv -Encoding UTF8 -Value @(
    "# Read by avb-hook. Points the PC's agents at the bridge server.",
    "AVB_BRIDGE_URL=$($Url.TrimEnd('/'))",
    "AVB_AGENT_TOKEN=$Token"
)
# Only this Windows user may read the token.
icacls $hookEnv /inheritance:r /grant:r "$($env:USERNAME):(R,W)" | Out-Null

$health = Invoke-RestMethod -Uri "$($Url.TrimEnd('/'))/healthz" -TimeoutSec 15
Write-Host "Server reachable: $($health.status) (version $($health.version))"

if (-not $NoHooks) {
    & .\.venv\Scripts\avb-admin.exe install-hooks --claude --codex
}
Write-Host ""
Write-Host "Done. Restart Claude Code (and approve the new hook once in Codex)."
Write-Host "Test the whole path (your iPhone will ring via Bark):"
Write-Host "  .\.venv\Scripts\avb-admin test-approval"
