"""``avb-admin``: generate secrets and install agent hooks."""

from __future__ import annotations

import argparse
import os
import secrets
import sys
from pathlib import Path

from agent_voice_bridge.agents import install
from agent_voice_bridge.config import default_data_dir
from agent_voice_bridge.security import totp


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)


def cmd_init(args: argparse.Namespace) -> int:
    out = Path(args.dir)
    bridge_env, hook_env = out / "bridge.env", out / "hook.env"
    if (bridge_env.exists() or hook_env.exists()) and not args.force:
        print(f"{bridge_env} already exists; use --force to regenerate (old tokens stop working)")
        return 1
    agent_token = secrets.token_urlsafe(32)
    voice_token = secrets.token_urlsafe(32)
    totp_secret = totp.generate_secret()
    _write_private(
        bridge_env,
        "# Agent Voice Bridge server secrets. Keep private.\n"
        f"AVB_AGENT_TOKEN={agent_token}\nAVB_VOICE_TOKEN={voice_token}\nAVB_TOTP_SECRET={totp_secret}\n",
    )
    _write_private(
        hook_env,
        "# Read by avb-hook when the variables are not set in the environment.\n"
        f"AVB_AGENT_TOKEN={agent_token}\nAVB_BRIDGE_URL=http://127.0.0.1:8765\n",
    )
    print(f"Wrote {bridge_env} and {hook_env}")
    print(
        "Add this TOTP secret to an authenticator app on your phone (needed for high-risk approvals):"
    )
    print(f"  {totp.provisioning_uri(totp_secret)}")
    return 0


def cmd_install_hooks(args: argparse.Namespace) -> int:
    exe = Path(args.hook_path) if args.hook_path else install.hook_executable()
    if not exe.exists() and not args.dry_run:
        print(
            f"avb-hook not found at {exe}; install the package first (pip install -e .)",
            file=sys.stderr,
        )
        return 1
    targets = []
    if args.claude:
        entry = None if args.remove else install.claude_entry(exe, args.wait_seconds)
        targets.append((install.claude_settings_path(), entry))
    if args.codex:
        entry = None if args.remove else install.codex_entry(exe, args.wait_seconds)
        targets.append((install.codex_hooks_path(), entry))
    if not targets:
        print("Choose --claude and/or --codex", file=sys.stderr)
        return 2
    for path, entry in targets:
        text = install.update_config(path, entry, dry_run=args.dry_run)
        action = (
            "Would write" if args.dry_run else ("Removed hook from" if args.remove else "Updated")
        )
        print(f"{action} {path}")
        if args.dry_run:
            print(text)
    if args.codex and not args.remove:
        print(
            "Codex asks you to review and trust new hooks before they run; open Codex and approve it."
        )
    return 0


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="avb-admin")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="Generate tokens and a TOTP secret")
    p.add_argument("--dir", default=str(default_data_dir()))
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("install-hooks", help="Add avb-hook to Claude Code and/or Codex config")
    p.add_argument("--claude", action="store_true")
    p.add_argument("--codex", action="store_true")
    p.add_argument("--remove", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--wait-seconds", type=int, default=300)
    p.add_argument("--hook-path", help="Override the avb-hook executable path")
    p.set_defaults(func=cmd_install_hooks)

    args = parser.parse_args(argv)
    if getattr(args, "wait_seconds", 0) >= install.HOOK_TIMEOUT_SECONDS:
        parser.error(
            f"--wait-seconds must be below the hook timeout ({install.HOOK_TIMEOUT_SECONDS}s)"
        )
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
