from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from agent_voice_bridge.worker.cli import Worker

FAKE_CLAUDE = r"""
import json, os, sys
prompt = sys.stdin.read()
resume = sys.argv[sys.argv.index("--resume") + 1] if "--resume" in sys.argv else None
open("calls.log", "a").write(json.dumps({"argv": sys.argv[1:], "prompt": prompt, "cwd": os.getcwd()}) + "\n")
print(json.dumps({"type": "result", "session_id": resume or "11111111-2222-3333-4444-555555555555",
                  "is_error": False, "result": "Answer to: " + prompt, "total_cost_usd": 0.01}))
"""


@pytest.fixture
def worker(tmp_path: Path) -> Worker:
    script = tmp_path / "fake_claude.py"
    script.write_text(FAKE_CLAUDE)
    launcher = tmp_path / "claude"
    launcher.write_text(f'#!/bin/sh\nexec {sys.executable} {script} "$@"\n')
    launcher.chmod(0o755)
    sessions = tmp_path / "projects" / "C--work"
    sessions.mkdir(parents=True)
    (sessions / "aaaaaaaa-0000-0000-0000-000000000000.jsonl").write_text(
        json.dumps(
            {
                "type": "user",
                "cwd": "C:\\work",
                "message": {"role": "user", "content": "fix the login bug"},
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Fixed it in auth.py"}],
                },
            }
        )
        + "\n"
    )
    w = Worker(
        "http://127.0.0.1:9",
        "token",
        work_dir=tmp_path / "tasks",
        claude=str(launcher),
        sessions_root=tmp_path / "projects",
        state=tmp_path / "state.json",
    )
    w.events: list[dict[str, Any]] = []  # type: ignore[misc]

    def post(path: str, body: dict[str, Any] | None, timeout: float) -> dict[str, Any]:
        w.events.append({"path": path, **(body or {})})  # type: ignore[attr-defined]
        return {}

    w._post = post  # type: ignore[method-assign]
    return w


def _wait_done(w: Worker, task_id: str) -> None:
    """Until the task has ended and its end was reported (which happens just after)."""
    for _ in range(250):
        reported = [
            e
            for e in w.events  # type: ignore[attr-defined]
            if e["path"] == "/v1/worker/events" and e["task_id"] == task_id
        ]
        if w.store.tasks[task_id].status != "running" and len(reported) == len(
            w.store.tasks[task_id].turns
        ):
            return
        time.sleep(0.02)
    raise AssertionError("task did not finish")


@pytest.mark.skipif(sys.platform == "win32", reason="uses a shell-script stand-in for claude")
def test_start_report_continue(worker: Worker) -> None:
    started = worker.handle(
        "start_task", {"prompt": "how do refunds work & why?", "folder": "stripe"}
    )
    tid = started["task_id"]
    _wait_done(worker, tid)
    task = worker.store.tasks[tid]
    assert task.status == "done" and task.result == "Answer to: how do refunds work & why?"
    assert Path(task.cwd).name == "stripe"
    calls = [json.loads(line) for line in (Path(task.cwd) / "calls.log").read_text().splitlines()]
    assert calls[0]["argv"] == ["-p", "--output-format", "json"]  # prompt only on stdin
    assert worker.events[-1]["path"] == "/v1/worker/events"  # type: ignore[attr-defined]
    assert worker.events[-1]["status"] == "done"  # type: ignore[attr-defined]

    report = worker.handle("task_report", {"task_id": "last"})
    assert report["status"] == "done" and report["result"].startswith("Answer to")

    worker.handle("continue_task", {"task_id": tid, "prompt": "and disputes?"})
    _wait_done(worker, tid)
    calls = [json.loads(line) for line in (Path(task.cwd) / "calls.log").read_text().splitlines()]
    assert calls[1]["argv"][-2:] == ["--resume", "11111111-2222-3333-4444-555555555555"]
    assert worker.store.tasks[tid].turns == ["how do refunds work & why?", "and disputes?"]


def test_list_and_read_claude_sessions(worker: Worker) -> None:
    listing = worker.handle("list_tasks", {})
    session = listing["recent_claude_code_sessions"][0]
    assert session["first_prompt"] == "fix the login bug" and session["folder"] == "C:\\work"
    report = worker.handle("task_report", {"task_id": "aaaaaaaa-0000-0000-0000-000000000000"})
    assert report["latest_messages"][-1] == "assistant: Fixed it in auth.py"
    # With no phone tasks yet, "last" means the latest Claude Code chat.
    assert worker.handle("task_report", {"task_id": "last"})["first_prompt"] == "fix the login bug"


def test_bad_jobs(worker: Worker) -> None:
    assert "error" in worker.handle("start_task", {"prompt": "  "})
    assert "error" in worker.handle("continue_task", {"task_id": "nope", "prompt": "x"})
    assert "error" in worker.handle("explode", {})
    assert worker._folder("../../etc").parent == worker.work_dir  # stays inside
