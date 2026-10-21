"""Heuristic risk classification of a requested operation.

This only decides how much friction an approval needs (for example a TOTP code). It
never approves anything on its own, and a LOW rating is not a safety claim.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from agent_voice_bridge.models import RiskLevel

_ORDER = [RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.CRITICAL]

_COMMAND_RULES: list[tuple[re.Pattern[str], RiskLevel, str]] = [
    (
        re.compile(r"\brm\s+(-[a-zA-Z]*[rRf][a-zA-Z]*\s+)+(/|~|\*|\$HOME|\.\s*$|\.\.)"),
        RiskLevel.CRITICAL,
        "recursive delete of a broad path",
    ),
    (
        re.compile(r"\b(rm|rmdir|del|erase|Remove-Item)\b.*(-r|-R|/s|-Recurse|--recursive)", re.I),
        RiskLevel.HIGH,
        "recursive delete",
    ),
    (
        re.compile(r"\b(format|mkfs|diskpart|dd\s+if=)", re.I),
        RiskLevel.CRITICAL,
        "disk-level operation",
    ),
    (
        re.compile(r"\bgit\s+push\b.*(--force|-f\b|--mirror|--delete|\s:)"),
        RiskLevel.HIGH,
        "force push or remote branch deletion",
    ),
    (
        re.compile(r"\bgit\s+(reset\s+--hard|clean\s+-[a-z]*f|checkout\s+--\s|branch\s+-D)"),
        RiskLevel.HIGH,
        "discards local git changes",
    ),
    (re.compile(r"\bgit\s+push\b"), RiskLevel.MEDIUM, "pushes to a remote"),
    (
        re.compile(r"\b(sudo|runas|Start-Process\b.*-Verb\s+RunAs)", re.I),
        RiskLevel.HIGH,
        "elevated privileges",
    ),
    (
        re.compile(
            r"(curl|wget|iwr|Invoke-WebRequest|irm|Invoke-RestMethod)\b[^|]*\|\s*(sh|bash|iex|python|pwsh)",
            re.I,
        ),
        RiskLevel.CRITICAL,
        "pipes a download into a shell",
    ),
    (
        re.compile(
            r"\b(npm|pnpm|yarn)\s+publish|\btwine\s+upload|\bcargo\s+publish|\bgh\s+release"
        ),
        RiskLevel.HIGH,
        "publishes a package or release",
    ),
    (
        re.compile(
            r"\b(terraform\s+(apply|destroy)|kubectl\s+(delete|apply)|helm\s+(install|upgrade|uninstall))"
        ),
        RiskLevel.HIGH,
        "changes infrastructure",
    ),
    (
        re.compile(r"\b(chmod\s+-R|chown\s+-R|icacls|takeown|Set-ExecutionPolicy)", re.I),
        RiskLevel.HIGH,
        "changes permissions",
    ),
    (
        re.compile(r"\b(reg\s+(add|delete)|Set-ItemProperty\s+.*HK(LM|CU))", re.I),
        RiskLevel.HIGH,
        "modifies the Windows registry",
    ),
    (
        re.compile(r"\b(DROP\s+(TABLE|DATABASE)|TRUNCATE\s+TABLE|DELETE\s+FROM)\b", re.I),
        RiskLevel.HIGH,
        "destructive database statement",
    ),
    (re.compile(r"\b(ssh|scp|rsync)\b"), RiskLevel.MEDIUM, "remote host access"),
    (
        re.compile(r"\b(curl|wget|Invoke-WebRequest|iwr)\b", re.I),
        RiskLevel.MEDIUM,
        "network access",
    ),
    (
        re.compile(r"\b(npm|pnpm|yarn|pip|uv|poetry|cargo|go)\s+(install|add|i)\b"),
        RiskLevel.MEDIUM,
        "installs packages (may run lifecycle scripts)",
    ),
]

_SENSITIVE_PATHS = re.compile(
    r"(\.env\b|\.ssh[\\/]|id_rsa|id_ed25519|\.aws[\\/]|\.git[\\/]config|\.npmrc|\.pypirc|"
    r"settings\.json$|\.claude[\\/]|\.codex[\\/]|[\\/]etc[\\/]|Windows[\\/]System32)",
    re.I,
)


@dataclass
class RiskAssessment:
    level: RiskLevel = RiskLevel.LOW
    reasons: list[str] = field(default_factory=list)

    def bump(self, level: RiskLevel, reason: str) -> None:
        if _ORDER.index(level) > _ORDER.index(self.level):
            self.level = level
        if reason not in self.reasons:
            self.reasons.append(reason)


def _is_outside(path: str, workspace: str) -> bool:
    flavour = PureWindowsPath if (":" in workspace[:3] or "\\" in workspace) else PurePosixPath
    try:
        p, w = flavour(path), flavour(workspace)
    except (TypeError, ValueError):
        return True
    if not p.is_absolute():
        return ".." in p.parts
    try:
        p.relative_to(w)
    except ValueError:
        return True
    return ".." in p.parts


def classify(tool_name: str, tool_input: dict[str, Any], workspace: str) -> RiskAssessment:
    risk = RiskAssessment()
    name = tool_name.lower()
    command = tool_input.get("command")
    if isinstance(command, list):
        command = " ".join(str(c) for c in command)

    if isinstance(command, str) and command:
        risk.bump(RiskLevel.MEDIUM, "runs a shell command")
        for pattern, level, reason in _COMMAND_RULES:
            if pattern.search(command):
                risk.bump(level, reason)
        if _SENSITIVE_PATHS.search(command):
            risk.bump(RiskLevel.HIGH, "touches a sensitive file")

    paths = [
        v
        for k in ("file_path", "path", "notebook_path", "grant_root")
        if isinstance(v := tool_input.get(k), str) and v
    ]
    if name in {"write", "edit", "multiedit", "notebookedit", "applypatch", "apply_patch"} or paths:
        if name not in {"read", "glob", "grep"}:
            risk.bump(RiskLevel.MEDIUM, "modifies files")
    for path in paths:
        if _SENSITIVE_PATHS.search(path):
            risk.bump(RiskLevel.HIGH, "touches a sensitive file")
        if _is_outside(path, workspace):
            risk.bump(RiskLevel.HIGH, "path is outside the workspace")

    if name in {"webfetch", "websearch"}:
        risk.bump(RiskLevel.LOW, "network read")
    if name.startswith("mcp__"):
        risk.bump(RiskLevel.MEDIUM, "calls an MCP tool")
    if not risk.reasons:
        risk.bump(RiskLevel.MEDIUM, "unrecognised operation")
    return risk
