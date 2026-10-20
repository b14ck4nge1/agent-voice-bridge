"""``avb-admin``: generate secrets and install agent hooks."""

from __future__ import annotations

import argparse
import asyncio
import os
import secrets
import string
import sys
from pathlib import Path

from agent_voice_bridge.agents import install
from agent_voice_bridge.config import default_data_dir, load_env_file
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
    sip_password = secrets.token_urlsafe(18)
    alphabet = string.ascii_letters + string.digits
    bark_key = "".join(secrets.choice(alphabet) for _ in range(32))
    _write_private(
        bridge_env,
        "# Agent Voice Bridge server secrets. Keep private.\n"
        f"AVB_AGENT_TOKEN={agent_token}\nAVB_VOICE_TOKEN={voice_token}\nAVB_TOTP_SECRET={totp_secret}\n"
        "\n# Phone path (docs/phone-setup.md). Fill in the two blanks, then set the dispatcher.\n"
        "# AVB_CALL_DISPATCHER=bark-sip\n"
        "AVB_BARK_DEVICE_KEY=\n"
        f"AVB_BARK_ENCRYPTION_KEY={bark_key}\n"
        "AVB_SIP_BIND=\n"
        f"AVB_SIP_PASSWORD={sip_password}\n",
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
    print("Phone setup values (SIP password, Bark key) are in bridge.env; see docs/phone-setup.md.")
    return 0


def _load_phone_env() -> None:
    load_env_file(default_data_dir() / "bridge.env")


def cmd_phone_info(_: argparse.Namespace) -> int:
    _load_phone_env()
    bind = os.environ.get("AVB_SIP_BIND", "<this PC's Tailscale IP>")
    port = os.environ.get("AVB_SIP_PORT", "5060")
    print("Linphone (iPhone) -> Use SIP account (third-party):")
    print(f"  Username:  {os.environ.get('AVB_SIP_USERNAME', 'phone')}")
    print("  Password:  (AVB_SIP_PASSWORD in bridge.env)")
    print(f"  Domain:    {os.environ.get('AVB_SIP_PUBLIC_HOST', bind)}:{port}")
    print(f"  Transport: {os.environ.get('AVB_SIP_TRANSPORT', 'udp').upper()}")
    print(
        "  Then in the account's advanced settings: media encryption None (or leave SRTP optional),"
    )
    print("  push notifications off (they only work for Linphone's own accounts).")
    print(
        "Bark -> Settings -> Encryption: AES256, GCM, key = AVB_BARK_ENCRYPTION_KEY from bridge.env"
    )
    return 0


def cmd_phone_test(args: argparse.Namespace) -> int:
    _load_phone_env()
    from agent_voice_bridge.phone_test import run_phone_test

    return asyncio.run(
        run_phone_test(seconds=args.seconds, send_bark=not args.no_bark, direct=args.direct)
    )


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


def cmd_test_approval(args: argparse.Namespace) -> int:
    """Send a harmless approval request through the real path and print the outcome."""
    from agent_voice_bridge.agents.hook import DEFAULT_URL, BridgeClient, BridgeError, decide

    load_env_file(Path(os.environ.get("AVB_HOOK_ENV", default_data_dir() / "hook.env")))
    url = os.environ.get("AVB_BRIDGE_URL", DEFAULT_URL)
    token = os.environ.get("AVB_AGENT_TOKEN", "")
    if not token:
        print(
            "AVB_AGENT_TOKEN is not set (run setup-pc-remote.ps1 or avb-admin init)",
            file=sys.stderr,
        )
        return 2
    hook_input = {
        "hook_event_name": "PermissionRequest",
        "session_id": "avb-test-" + secrets.token_hex(3),
        "cwd": os.getcwd(),
        "tool_name": "Bash",
        "tool_input": {
            "command": "echo hello from Agent Voice Bridge",
            "description": "Test request",
        },
    }
    print(
        f"Sending a test request to {url}; your phone should ring (Bark). Waiting up to {args.seconds}s..."
    )
    try:
        outcome = decide(
            hook_input,
            agent="claude-code",
            client=BridgeClient(url, token),
            wait_seconds=args.seconds,
        )
    except BridgeError as exc:
        print(f"No decision: {exc}")
        return 1
    print(f"Decision: {outcome.behavior}" + (f" ({outcome.message})" if outcome.message else ""))
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

    p = sub.add_parser("phone-info", help="Show what to enter in Linphone and Bark")
    p.set_defaults(func=cmd_phone_info)

    p = sub.add_parser(
        "phone-test", help="Send a Bark alert and answer one test call (keypad echo)"
    )
    p.add_argument("--seconds", type=int, default=180, help="How long to wait for the call")
    p.add_argument("--no-bark", action="store_true", help="Don't send a notification; just wait")
    p.add_argument(
        "--direct",
        action="store_true",
        help="No Bark: wait for Linphone to sign in, then ring it from this server",
    )
    p.set_defaults(func=cmd_phone_test)

    p = sub.add_parser(
        "test-approval", help="Send a harmless test approval request and wait for it"
    )
    p.add_argument("--seconds", type=int, default=180)
    p.set_defaults(func=cmd_test_approval)

    args = parser.parse_args(argv)
    if getattr(args, "wait_seconds", 0) >= install.HOOK_TIMEOUT_SECONDS:
        parser.error(
            f"--wait-seconds must be below the hook timeout ({install.HOOK_TIMEOUT_SECONDS}s)"
        )
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
