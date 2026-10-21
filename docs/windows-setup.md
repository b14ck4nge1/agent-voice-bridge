# Windows setup

The test suite passes in GitHub Actions on `windows-latest` (Python 3.11 and 3.13)
as well as Ubuntu. The scripts below have **not
yet been run on a personal Windows 10/11 machine**; task WIN-02 tracks that.

## Requirements

* Windows 10 or 11
* Python 3.11 or newer from python.org (tick *Add python.exe to PATH*), or
  `winget install Python.Python.3.12`
* Git
* Claude Code and/or Codex, if you want to hook them up

No WSL2 or Docker is needed for the bridge.

## Install

```powershell
git clone https://github.com/b14ck4nge1/agent-voice-bridge.git
cd agent-voice-bridge
powershell -ExecutionPolicy Bypass -File scripts\install.ps1
```

`install.ps1` creates `.venv`, installs the package with dev tools, and runs
`avb-admin init`, which writes two private files to
`%LOCALAPPDATA%\AgentVoiceBridge\`:

* `bridge.env`: agent token, voice token, TOTP secret (read by `avb-bridge` and
  `avb-voice-sim`)
* `hook.env`: agent token and bridge URL (read by `avb-hook`)

It also prints an `otpauth://` URI. Add it to your phone's authenticator app.

## Run

```powershell
scripts\start.ps1                 # starts the bridge on http://127.0.0.1:8765
.\.venv\Scripts\avb-voice-sim     # in a second terminal: text stand-in for the phone call
```

The API docs are at <http://127.0.0.1:8765/docs> while the bridge runs.

## Hook up the agents

```powershell
.\.venv\Scripts\avb-admin install-hooks --claude --codex --dry-run   # preview
.\.venv\Scripts\avb-admin install-hooks --claude --codex
```

* Claude Code: adds a `PermissionRequest` hook to `%USERPROFILE%\.claude\settings.json`
  (a backup is written next to it). It uses the exec form, so it doesn't matter
  whether Claude Code runs hooks via Git Bash or PowerShell. Restart Claude Code.
* Codex: adds `%USERPROFILE%\.codex\hooks.json`. Codex asks you to **review and
  trust** the new hook once.
* To remove: `avb-admin install-hooks --claude --codex --remove`.

From then on, whenever the agent would show a permission prompt, the hook asks the
bridge and waits up to 300 s. With the simulator running you'll see "RING RING" and
can type answers such as `what command?`, `approve`, `yes`. If the bridge isn't
running, the hook **denies** the request (or shows the normal prompt if you
installed it with `--on-failure ask`).

## Test

```powershell
scripts\test.ps1     # ruff, mypy, pytest
```
