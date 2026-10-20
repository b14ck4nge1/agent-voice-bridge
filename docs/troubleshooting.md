# Troubleshooting

## The agent's permission request is denied immediately

Run the hook by hand to see its message on stderr:

```powershell
'{"hook_event_name":"PermissionRequest","session_id":"t","cwd":"C:\\tmp","tool_name":"Bash","tool_input":{"command":"echo hi"}}' |
  .\.venv\Scripts\avb-hook --agent claude-code --wait-seconds 20
```

| stderr says | Cause |
|---|---|
| `AVB_AGENT_TOKEN is not set` | Run `avb-admin init`, or set `AVB_HOOK_ENV` to your `hook.env`. |
| `bridge unreachable` | `avb-bridge` isn't running, or is on another port (`AVB_BRIDGE_URL`). |
| `bridge refused the request (401)` | Token mismatch: `hook.env` and `bridge.env` are from different `init` runs. Re-run `avb-admin init --force`. |
| `no decision before the hook deadline` | Nobody answered within `--wait-seconds`. |
| `request ended as FAILED` | Call attempts were exhausted (missed calls). |
| `AVB_BRIDGE_URL must be https://...` | Remote bridges must use HTTPS. |

## Claude Code never calls the hook

* Check `%USERPROFILE%\.claude\settings.json` has `hooks.PermissionRequest` with
  `avb-hook` in `command`. Restart Claude Code after changing it.
* Operations already allowed by your permission rules (or `bypassPermissions` mode)
  never reach a permission prompt, so the hook doesn't run for them.
* The `/hooks` menu in Claude Code shows the hooks it loaded.

## Codex never calls the hook

* Codex requires you to review and trust new hooks. Open Codex and approve it.
* Hooks can be disabled with `[features] hooks = false` in `config.toml`, or by an
  admin's `allow_managed_hooks_only` in `requirements.toml`.
* With `approval_policy = "never"`, Codex never asks, so the hook never runs.

## Bridge refuses to start

* `AVB_AGENT_TOKEN and AVB_VOICE_TOKEN must both be set`: run `avb-admin init`.
* `Refusing to bind to a non-loopback address`: you set `AVB_HOST`. See
  docs/security.md before exposing the bridge.

## Inspecting state

The SQLite database is at `%LOCALAPPDATA%\AgentVoiceBridge\bridge.sqlite3` (or
`AVB_DB_PATH`). Tables: `approval_requests` (redacted input only), `decisions`,
`audit_log`.

## Phone and SIP

See [HW-01](hardware-tests/HW-01-linphone-push.md). Common causes of missed calls
when locked: notifications disabled for Linphone, a Focus mode, Low Power Mode, or
a third-party (non-sip.linphone.org) account, which gets no push.
