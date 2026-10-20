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


def test_cloud_hook_install_and_remove(tmp_path: Path) -> None:
    import json
    import subprocess
    import sys

    from agent_voice_bridge.agents.install import CLOUD_HOOK_FILE, install_cloud_hook

    repo = tmp_path / "repo"
    (repo / ".claude").mkdir(parents=True)
    (repo / ".claude" / "settings.json").write_text(
        json.dumps({"permissions": {"allow": ["Bash(ls)"]}})
    )
    install_cloud_hook(repo)
    settings = json.loads((repo / ".claude" / "settings.json").read_text())
    assert settings["permissions"] == {"allow": ["Bash(ls)"]}  # kept
    cmd = settings["hooks"]["PermissionRequest"][0]["hooks"][0]["command"]
    assert "--cloud" in cmd and cmd.startswith('test -n "$AVB_AGENT_TOKEN"')
    assert not list((repo / ".claude").glob("*.avb-backup-*"))  # git keeps history

    # The copied file runs on its own, and without a token it decides nothing.
    hook = repo / CLOUD_HOOK_FILE
    out = subprocess.run(
        [sys.executable, "-I", str(hook), "--cloud", "--agent", "claude-code"],
        input="{}",
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "SYSTEMROOT": "C:\\Windows"},
        timeout=30,
    )
    assert out.returncode == 0 and out.stdout == ""

    install_cloud_hook(repo, remove=True)
    assert not hook.exists()
    assert (
        "hooks" not in json.loads((repo / ".claude" / "settings.json").read_text())
        or not (json.loads((repo / ".claude" / "settings.json").read_text())["hooks"])
    )


def test_repo_cloud_hook_copy_is_current() -> None:
    """This repository's own cloud hook is a copy of the hook; keep them identical."""
    from agent_voice_bridge.agents import hook

    root = Path(__file__).resolve().parents[2]
    copy = root / ".claude" / "hooks" / "avb-hook.py"
    def text(path: Path) -> str:  # line endings may differ after a Windows checkout
        return path.read_text(encoding="utf-8").replace("\r\n", "\n")

    assert text(copy) == text(Path(hook.__file__)), (
        "run: avb-admin install-cloud-hook --repo ."
    )
