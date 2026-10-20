# Lint, type-check and run the test suite.
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
& .\.venv\Scripts\ruff.exe check src tests;          if ($LASTEXITCODE) { exit $LASTEXITCODE }
& .\.venv\Scripts\ruff.exe format --check src tests; if ($LASTEXITCODE) { exit $LASTEXITCODE }
& .\.venv\Scripts\mypy.exe;                          if ($LASTEXITCODE) { exit $LASTEXITCODE }
& .\.venv\Scripts\pytest.exe @args;                  exit $LASTEXITCODE
