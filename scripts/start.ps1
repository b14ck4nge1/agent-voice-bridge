# Starts the approval bridge in the foreground (Ctrl+C to stop).
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
& .\.venv\Scripts\avb-bridge.exe @args
