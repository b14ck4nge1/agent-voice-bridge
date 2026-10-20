import json
from pathlib import Path

from agent_voice_bridge.agents import install
from agent_voice_bridge.agents.hook import Outcome, render

EXE = Path("C:\\Users\\dev\\avb\\.venv\\Scripts\\avb-hook.exe")


def test_claude_install_preserves_other_hooks_and_is_idempotent(tmp_path: Path) -> None:
    cfg = tmp_path / "settings.json"
    other = {"matcher": "Bash", "hooks": [{"type": "command", "command": "my-linter"}]}
    cfg.write_text(
        json.dumps({"model": "x", "hooks": {"PermissionRequest": [other], "Stop": [other]}})
    )
    for _ in range(2):
        install.update_config(cfg, install.claude_entry(EXE, 300))
    data = json.loads(cfg.read_text())
    groups = data["hooks"]["PermissionRequest"]
    assert data["model"] == "x" and data["hooks"]["Stop"] == [other]
    assert groups[0] == other and len(groups) == 2
    ours = groups[1]["hooks"][0]
    assert ours["command"] == "C:/Users/dev/avb/.venv/Scripts/avb-hook.exe"
    assert ours["args"][:2] == ["--agent", "claude-code"] and ours["timeout"] > 300
    assert list(tmp_path.glob("settings.json.avb-backup-*"))

    install.update_config(cfg, None)
    assert json.loads(cfg.read_text())["hooks"]["PermissionRequest"] == [other]


def test_codex_entry_and_removal(tmp_path: Path) -> None:
    cfg = tmp_path / "hooks.json"
    install.update_config(cfg, install.codex_entry(EXE, 300))
    data = json.loads(cfg.read_text())
    assert set(data) == {"hooks"}  # Codex rejects unknown top-level keys
    cmd = data["hooks"]["PermissionRequest"][0]["hooks"][0]["command"]
    assert cmd == '"C:/Users/dev/avb/.venv/Scripts/avb-hook.exe" --agent codex --wait-seconds 300'
    install.update_config(cfg, None)
    assert json.loads(cfg.read_text()) == {"hooks": {}}


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    cfg = tmp_path / "settings.json"
    text = install.update_config(cfg, install.claude_entry(EXE, 300), dry_run=True)
    assert "avb-hook" in text and not cfg.exists()


def test_render_never_widens_permissions() -> None:
    allow = json.loads(render(Outcome("allow")) or "")
    assert allow == {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {"behavior": "allow"},
        }
    }
    deny = json.loads(render(Outcome("deny", "no")) or "")
    assert deny["hookSpecificOutput"]["decision"] == {"behavior": "deny", "message": "no"}
    assert render(Outcome(None)) is None
