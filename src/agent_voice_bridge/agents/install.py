"""Install or remove the ``avb-hook`` PermissionRequest hook in agent config files.

* Claude Code: ``~/.claude/settings.json`` (``hooks.PermissionRequest``). Uses the
  exec form (``command`` + ``args``) so no shell is involved, which avoids Git Bash /
  PowerShell quoting differences on Windows.
* Codex: ``~/.codex/hooks.json``. Codex runs hook commands through ``cmd.exe /C`` on
  Windows and ``sh -c`` elsewhere, so the executable path is quoted.

Existing files are backed up next to the original before being rewritten. Entries
are recognised by the ``avb-hook`` executable name, so re-running replaces rather
than duplicates them, and unrelated hooks are left alone.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any

MARKER = "avb-hook"
HOOK_TIMEOUT_SECONDS = 330  # must exceed avb-hook --wait-seconds (default 300)


def hook_executable() -> Path:
    scripts = Path(sys.executable).parent
    name = "avb-hook.exe" if os.name == "nt" else "avb-hook"
    candidate = scripts / name
    found = shutil.which("avb-hook")
    if candidate.exists():
        return candidate
    if found:
        return Path(found)
    return candidate


def _portable(path: Path) -> str:
    # Forward slashes work for CreateProcess, cmd.exe and Git Bash alike.
    return str(path).replace("\\", "/")


def _is_ours(handler: Any) -> bool:
    return isinstance(handler, dict) and MARKER in str(handler.get("command", ""))


def _strip_ours(groups: list[Any]) -> list[Any]:
    kept = []
    for group in groups:
        if not isinstance(group, dict):
            kept.append(group)
            continue
        hooks = [h for h in group.get("hooks", []) if not _is_ours(h)]
        if hooks:
            kept.append({**group, "hooks": hooks})
    return kept


def claude_entry(exe: Path, wait_seconds: int) -> dict[str, Any]:
    return {
        "matcher": "*",
        "hooks": [
            {
                "type": "command",
                "command": _portable(exe),
                "args": ["--agent", "claude-code", "--wait-seconds", str(wait_seconds)],
                "timeout": HOOK_TIMEOUT_SECONDS,
            }
        ],
    }


def codex_entry(exe: Path, wait_seconds: int) -> dict[str, Any]:
    return {
        "matcher": "*",
        "hooks": [
            {
                "type": "command",
                "command": f'"{_portable(exe)}" --agent codex --wait-seconds {wait_seconds}',
                "timeout": HOOK_TIMEOUT_SECONDS,
                "statusMessage": "Waiting for phone approval (Agent Voice Bridge)",
            }
        ],
    }


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8") or "{}")
    if not isinstance(data, dict):
        raise ValueError(f"{path} does not contain a JSON object")
    return data


def _write(path: Path, data: dict[str, Any], *, dry_run: bool, backup: bool = True) -> str:
    text = json.dumps(data, indent=2) + "\n"
    if dry_run:
        return text
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and backup:
        copy = path.with_name(f"{path.name}.avb-backup-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(path, copy)
    tmp = path.with_name(path.name + ".avb-tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    return text


def update_config(
    path: Path, entry: dict[str, Any] | None, *, dry_run: bool = False, backup: bool = True
) -> str:
    """Add ``entry`` (or just remove ours when None) under hooks.PermissionRequest."""
    data = _load(path)
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError(f"{path}: 'hooks' is not an object")
    groups = _strip_ours(list(hooks.get("PermissionRequest", [])))
    if entry is not None:
        groups.append(entry)
    if groups:
        hooks["PermissionRequest"] = groups
    else:
        hooks.pop("PermissionRequest", None)
    return _write(path, data, dry_run=dry_run, backup=backup)


CLOUD_HOOK_FILE = ".claude/hooks/avb-hook.py"


def cloud_entry(wait_seconds: int) -> dict[str, Any]:
    """Runs in cloud sessions (Linux); does nothing where AVB_AGENT_TOKEN isn't set."""
    return {
        "matcher": "*",
        "hooks": [
            {
                "type": "command",
                # The shell test keeps it silent on your PC (no token in the environment;
                # Windows may not even have python3), where your own hook decides.
                "command": f'test -n "$AVB_AGENT_TOKEN" && python3 '
                f'"$CLAUDE_PROJECT_DIR/{CLOUD_HOOK_FILE}" --cloud --agent claude-code '
                f"--wait-seconds {wait_seconds} || true",
                "timeout": HOOK_TIMEOUT_SECONDS,
            }
        ],
    }


def install_cloud_hook(repo: Path, *, wait_seconds: int = 300, remove: bool = False) -> list[Path]:
    """Copy the hook into ``repo`` and add it to the repo's ``.claude/settings.json``."""
    from agent_voice_bridge.agents import hook

    target = repo / CLOUD_HOOK_FILE
    settings = repo / ".claude" / "settings.json"
    if remove:
        target.unlink(missing_ok=True)
        update_config(settings, None, backup=False)
        return [settings]
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(hook.__file__, target)
    update_config(settings, cloud_entry(wait_seconds), backup=False)
    return [target, settings]


def claude_settings_path() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")) / "settings.json"


def codex_hooks_path() -> Path:
    return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "hooks.json"
