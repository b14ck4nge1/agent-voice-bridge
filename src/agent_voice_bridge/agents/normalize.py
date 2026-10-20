"""Turn a raw tool request into a short, speakable description.

Input must already be redacted. The text produced here is what the assistant reads
to the user, so it quotes the operation rather than paraphrasing it.
"""

from __future__ import annotations

from typing import Any

MAX_QUOTE = 300


def _clip(text: str, limit: int = MAX_QUOTE) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def describe_operation(tool_name: str, tool_input: dict[str, Any]) -> str:
    name = tool_name
    lower = name.lower()
    command = tool_input.get("command")
    if isinstance(command, list):
        command = " ".join(str(c) for c in command)
    path = tool_input.get("file_path") or tool_input.get("path") or tool_input.get("notebook_path")

    if isinstance(command, str) and command:
        text = f"run the command: {_clip(command)}"
        desc = tool_input.get("description")
        if isinstance(desc, str) and desc:
            text += f". The agent describes it as: {_clip(desc, 200)}"
        cwd = tool_input.get("cwd")
        if isinstance(cwd, str) and cwd:
            text += f". Working directory: {cwd}"
        return text
    if lower == "write" and isinstance(path, str):
        content = tool_input.get("content")
        size = f" ({len(content)} characters)" if isinstance(content, str) else ""
        return f"create or overwrite the file {path}{size}"
    if lower in {"edit", "multiedit"} and isinstance(path, str):
        edits = tool_input.get("edits")
        count = f" with {len(edits)} edits" if isinstance(edits, list) else ""
        return f"edit the file {path}{count}"
    if lower == "notebookedit" and isinstance(path, str):
        return f"edit the notebook {path}"
    if lower == "webfetch" and isinstance(tool_input.get("url"), str):
        return f"fetch the web page {_clip(tool_input['url'], 200)}"
    if lower == "websearch" and isinstance(tool_input.get("query"), str):
        return f"search the web for: {_clip(tool_input['query'], 200)}"
    if lower.startswith("mcp__"):
        parts = name.split("__")
        if len(parts) >= 3:
            return f"call the {parts[2]} tool from the {parts[1]} MCP server"
    changes = tool_input.get("changes")
    if isinstance(changes, list):
        paths = [str(c.get("path")) for c in changes if isinstance(c, dict) and c.get("path")]
        listed = ", ".join(paths[:5]) + (f" and {len(paths) - 5} more" if len(paths) > 5 else "")
        return f"change {len(paths)} file{'s' if len(paths) != 1 else ''}: {listed}"
    if isinstance(path, str):
        return f"use the {name} tool on {path}"
    return f"use the {name} tool"
