# One-shot Windows setup for the phone path (Bark wake-up + Linphone calling this PC).
#
# Run from the folder you want it in, for example:
#   cd "C:\Users\you\Desktop\New folder\AI Shop\SIP"
#   powershell -ExecutionPolicy Bypass -File .\agent-voice-bridge\scripts\setup-phone.ps1
# or, if you haven't cloned yet, download this file there and run it; it clones the repo.
#
# It installs the bridge, generates secrets, asks for your Bark device key, finds this
# PC's Tailscale address, opens the firewall for Tailscale only (if run as admin), and
# writes PHONE-SETUP.txt next to it with exactly what to enter on the iPhone.
param(
    [string]$Branch = "claude/phase0-bridge-mvp",
    [string]$BarkKey = "",
    [string]$SipBind = ""
)
$ErrorActionPreference = "Stop"

$here = (Get-Location).Path
if (Test-Path (Join-Path $PSScriptRoot "..\pyproject.toml")) {
    $repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
} else {
    $repo = Join-Path $here "agent-voice-bridge"
    if (-not (Test-Path $repo)) {
        git clone --branch $Branch https://github.com/b14ck4nge1/agent-voice-bridge.git $repo
        if ($LASTEXITCODE) { throw "git clone failed" }
    }
}
$outDir = Split-Path -Parent $repo

Write-Host "== Installing into $repo"
& powershell -ExecutionPolicy Bypass -File (Join-Path $repo "scripts\install.ps1")
if ($LASTEXITCODE) { throw "install failed" }

$envFile = Join-Path $env:LOCALAPPDATA "AgentVoiceBridge\bridge.env"
$lines = Get-Content $envFile

function Get-Value($name) {
    $line = $lines | Where-Object { $_ -match "^$name=" } | Select-Object -First 1
    if ($line) { return $line.Substring($name.Length + 1) } else { return "" }
}
function Set-Value($name, $value) {
    $found = $false
    $script:lines = $script:lines | ForEach-Object {
        if ($_ -match "^#?\s*$name=") { $found = $true; "$name=$value" } else { $_ }
    }
    if (-not $found) { $script:lines += "$name=$value" }
}

# Tailscale address (recommended): phone and PC reach each other on any network.
if (-not $SipBind) {
    $ts = Get-Command tailscale -ErrorAction SilentlyContinue
    if (-not $ts -and (Test-Path "C:\Program Files\Tailscale\tailscale.exe")) { $ts = "C:\Program Files\Tailscale\tailscale.exe" }
    if ($ts) { $SipBind = (& $ts ip -4 2>$null | Select-Object -First 1) }
}
if (-not $SipBind) {
    Write-Host "Tailscale not found. Install it from https://tailscale.com/download (free), sign in on this PC and the iPhone, then re-run."
    $SipBind = Read-Host "Or type this PC's LAN IP to test on home Wi-Fi only (e.g. 192.168.1.20)"
}
if (-not $BarkKey) {
    $BarkKey = Read-Host "Bark device key (open Bark on the iPhone; it's the part after https://api.day.app/ in the example URL)"
}

Set-Value "AVB_CALL_DISPATCHER" "bark-sip"
Set-Value "AVB_BARK_DEVICE_KEY" $BarkKey.Trim().Trim("/")
Set-Value "AVB_SIP_BIND" $SipBind.Trim()
Set-Content -Path $envFile -Value $lines -Encoding UTF8

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
$fw = @(
    'New-NetFirewallRule -DisplayName "Agent Voice Bridge SIP (UDP)" -Direction Inbound -Protocol UDP -LocalPort 5060,40000-40200 -RemoteAddress 100.64.0.0/10,LocalSubnet -Action Allow',
    'New-NetFirewallRule -DisplayName "Agent Voice Bridge SIP (TCP)" -Direction Inbound -Protocol TCP -LocalPort 5060 -RemoteAddress 100.64.0.0/10,LocalSubnet -Action Allow'
)
if ($isAdmin) {
    if (-not (Get-NetFirewallRule -DisplayName "Agent Voice Bridge SIP (UDP)" -ErrorAction SilentlyContinue)) { $fw | ForEach-Object { Invoke-Expression $_ | Out-Null } }
    Write-Host "Firewall: allowed SIP/RTP from Tailscale and your LAN only."
} else {
    Write-Host "Firewall: not admin. In an Administrator PowerShell run:"
    $fw | ForEach-Object { Write-Host "  $_" }
}

$sipPassword = Get-Value "AVB_SIP_PASSWORD"
$barkCrypto = Get-Value "AVB_BARK_ENCRYPTION_KEY"
$barkIv = -join ((48..57) + (65..90) + (97..122) | Get-Random -Count 12 | ForEach-Object { [char]$_ })
$details = @"
AGENT VOICE BRIDGE - WHAT TO ENTER ON THE IPHONE
(Contains secrets. Keep this file private; delete it once you're set up.)

1) Tailscale app (App Store, free): sign in with the same account as this PC. Keep it connected.

2) Bark app (App Store, free):
   - Allow notifications, and allow Critical Alerts when asked.
   - Settings > Encryption (Encryption Settings):
       Algorithm: AES256
       Mode:      GCM
       Padding:   noPadding
       Key:       $barkCrypto
       IV:        $barkIv
         (any 12 characters; every notification carries its own IV)
   If notifications then show "Decryption Failed", tell Claude, or delete the
   AVB_BARK_ENCRYPTION_KEY line in $envFile to send them unencrypted
   (they only ever contain agent name, project name and risk level).

3) Linphone app (App Store, free) > "Use SIP account" / third-party SIP account
   (NOT a linphone.org account):
       Username:  phone
       Password:  $sipPassword
       Domain:    $($SipBind):5060
       Transport: UDP
   In the account's advanced settings, if present: Push notifications OFF.
   Settings > Calls/Network: Media encryption = None (or SRTP but not mandatory).

4) Test (on this PC):
       cd "$repo"
       .\.venv\Scripts\avb-admin phone-test
   Tap the Bark notification on the iPhone. Linphone calls this PC, you hear a voice,
   press keys and it reads them back; press # to finish.

5) Use it for real:
       .\scripts\start.ps1                                   (keep running)
       .\.venv\Scripts\avb-admin install-hooks --claude --codex
"@
$detailsPath = Join-Path $outDir "PHONE-SETUP.txt"
Set-Content -Path $detailsPath -Value $details -Encoding UTF8
Write-Host ""
Write-Host $details
Write-Host "Saved to $detailsPath"
