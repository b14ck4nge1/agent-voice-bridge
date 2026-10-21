"""``avb-worker``: the PC helper that carries out what you ask for on the phone.

It connects out to the bridge server (no ports opened on the PC), waits for jobs from
the phone assistant, and runs them with Claude Code in headless mode:

* ``start_task``     ``claude -p`` in a new session (the prompt goes in on stdin)
* ``continue_task``  ``claude -p --resume <session>`` with a follow-up
* ``list_tasks``     tasks it started, plus your most recent Claude Code sessions
* ``task_report``    status and the latest output of a task or session

Tasks still go through Claude Code's permissions: the Agent Voice Bridge hook asks you
by phone before anything that needs approval. When a task ends, the helper tells the
server, which can call you with the result.

Standard library only, like the hook.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from agent_voice_bridge.agents.hook import DEFAULT_URL, _check_url
from agent_voice_bridge.config import default_data_dir, load_env_file

log = logging.getLogger("avb-worker")

SESSION_ID = re.compile(r"^[0-9a-fA-F-]{8,64}$")
MAX_PROMPT = 8000
TASK_TIMEOUT = 2 * 3600


@dataclass
class Task:
    id: str
    title: str
    cwd: str
    status: str = "running"  # running | done | failed
    session_id: str | None = None
    started: float = field(default_factory=time.time)
    finished: float | None = None
    result: str | None = None
    cost_usd: float | None = None
    turns: list[str] = field(default_factory=list)  # prompts sent, oldest first


class TaskStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.Lock()
        self.tasks: dict[str, Task] = {}
        try:
            for raw in json.loads(path.read_text(encoding="utf-8")):
                task = Task(**raw)
                if task.status == "running":  # the helper was restarted mid-task
                    task.status = "failed"
                    task.result = task.result or "Interrupted: the PC helper restarted."
                self.tasks[task.id] = task
        except (OSError, ValueError, TypeError):
            pass

    def save(self) -> None:
        with self.lock:
            data = [asdict(t) for t in sorted(self.tasks.values(), key=lambda t: t.started)][-50:]
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
            tmp.replace(self.path)

    def latest(self) -> Task | None:
        return max(self.tasks.values(), key=lambda t: t.started, default=None)


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def recent_sessions(root: Path, limit: int = 5) -> list[dict[str, Any]]:
    """Best effort: Claude Code's transcript format is internal and may change."""
    files = sorted(root.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    out = []
    for path in files[:limit]:
        info = read_session(path, max_messages=0)
        info["session_id"] = path.stem
        out.append(info)
    return out


def read_session(path: Path, max_messages: int = 6) -> dict[str, Any]:
    first_prompt, cwd, messages = None, None, []
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(rec, dict):
                    continue
                cwd = cwd or rec.get("cwd")
                kind = rec.get("type")
                msg = rec.get("message")
                if kind not in ("user", "assistant") or not isinstance(msg, dict):
                    continue
                text = _text_of(msg.get("content")).strip()
                if not text:
                    continue
                if kind == "user" and first_prompt is None:
                    first_prompt = text
                messages.append(f"{kind}: {text}")
    except OSError:
        pass
    return {
        "first_prompt": (first_prompt or "")[:300],
        "folder": cwd,
        "last_active": time.strftime("%Y-%m-%d %H:%M", time.localtime(path.stat().st_mtime)),
        "latest_messages": [m[:1500] for m in messages[-max_messages:]] if max_messages else [],
    }


def find_transcript(root: Path, session_id: str) -> Path | None:
    return next(iter(root.glob(f"*/{session_id}.jsonl")), None)


class Worker:
    def __init__(
        self,
        url: str,
        token: str,
        *,
        work_dir: Path,
        claude: str | None = None,
        sessions_root: Path | None = None,
        state: Path | None = None,
    ) -> None:
        self.base = _check_url(url)
        self.token = token
        self.work_dir = work_dir
        self.claude = claude or shutil.which("claude") or "claude"
        self.sessions_root = sessions_root or Path.home() / ".claude" / "projects"
        self.store = TaskStore(state or default_data_dir() / "worker-tasks.json")
        self.stop = threading.Event()

    # -- server ------------------------------------------------------------------

    def _post(self, path: str, body: dict[str, Any] | None, timeout: float) -> dict[str, Any]:
        req = urllib.request.Request(  # noqa: S310 - scheme validated by _check_url
            self.base + path,
            data=json.dumps(body or {}).encode(),
            method="POST",
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return json.loads(resp.read() or b"{}")  # type: ignore[no-any-return]

    def serve(self) -> None:
        log.info("connected to %s; tasks run in %s", self.base, self.work_dir)
        backoff = 2.0
        while not self.stop.is_set():
            try:
                jobs = self._post("/v1/worker/poll?wait=25", None, timeout=40).get("jobs", [])
                backoff = 2.0
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
                log.warning("server unreachable (%s); retrying in %.0fs", exc, backoff)
                self.stop.wait(backoff)
                backoff = min(backoff * 2, 60)
                continue
            for job in jobs:
                threading.Thread(target=self._run_job, args=(job,), daemon=True).start()

    def _run_job(self, job: dict[str, Any]) -> None:
        try:
            result = self.handle(str(job.get("kind")), dict(job.get("args") or {}))
        except Exception as exc:  # report, never crash the helper
            log.exception("job %s failed", job.get("kind"))
            result = {"error": f"{type(exc).__name__}: {exc}"}
        try:
            self._post(f"/v1/worker/jobs/{job['id']}/result", {"result": result}, timeout=15)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            log.warning("could not send a job result: %s", exc)

    # -- jobs -------------------------------------------------------------------

    def handle(self, kind: str, args: dict[str, Any]) -> dict[str, Any]:
        if kind == "start_task":
            return self.start_task(str(args.get("prompt", "")), args.get("folder"))
        if kind == "continue_task":
            return self.continue_task(str(args.get("task_id", "last")), str(args.get("prompt", "")))
        if kind == "list_tasks":
            return self.list_tasks()
        if kind == "task_report":
            return self.task_report(str(args.get("task_id", "last")))
        return {"error": f"unknown job {kind}"}

    def _folder(self, folder: Any) -> Path:
        """Tasks run inside the work folder; a named subfolder is created there."""
        base = self.work_dir
        if isinstance(folder, str) and folder.strip():
            name = re.sub(r"[^\w .-]", "", folder.strip())[:60].strip(" .") or "task"
            base = self.work_dir / name
        base.mkdir(parents=True, exist_ok=True)
        return base

    def start_task(self, prompt: str, folder: Any = None) -> dict[str, Any]:
        prompt = prompt.strip()[:MAX_PROMPT]
        if not prompt:
            return {"error": "empty task"}
        cwd = self._folder(folder)
        task = Task(id=f"t{secrets.token_hex(3)}", title=prompt[:120], cwd=str(cwd), turns=[prompt])
        self.store.tasks[task.id] = task
        self.store.save()
        threading.Thread(target=self._run_claude, args=(task, prompt, None), daemon=True).start()
        return {"task_id": task.id, "status": "started", "folder": str(cwd)}

    def continue_task(self, task_id: str, prompt: str) -> dict[str, Any]:
        task = self.store.latest() if task_id in ("", "last") else self.store.tasks.get(task_id)
        if task is None:
            return {"error": "no such task"}
        if task.status == "running":
            return {"error": "that task is still running"}
        if not task.session_id or not SESSION_ID.match(task.session_id):
            return {"error": "that task has no Claude Code session to continue"}
        prompt = prompt.strip()[:MAX_PROMPT]
        if not prompt:
            return {"error": "empty message"}
        task.status, task.finished = "running", None
        task.turns.append(prompt)
        self.store.save()
        threading.Thread(
            target=self._run_claude, args=(task, prompt, task.session_id), daemon=True
        ).start()
        return {"task_id": task.id, "status": "continuing"}

    def _run_claude(self, task: Task, prompt: str, resume: str | None) -> None:
        # The prompt goes in on stdin, never on the command line: on Windows, claude is
        # often a .cmd script and cmd.exe would interpret characters in the text.
        cmd = [self.claude, "-p", "--output-format", "json"]
        if resume:
            cmd += ["--resume", resume]
        log.info("task %s: running Claude Code in %s", task.id, task.cwd)
        try:
            proc = subprocess.run(  # noqa: S603 - fixed arguments, prompt on stdin
                cmd,
                input=prompt,
                cwd=task.cwd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=TASK_TIMEOUT,
            )
            out = proc.stdout.strip()
            data: dict[str, Any] = {}
            try:
                data = json.loads(out.splitlines()[-1]) if out else {}
            except (ValueError, IndexError):
                data = {}
            task.session_id = str(data.get("session_id") or task.session_id or "") or None
            failed = proc.returncode != 0 or bool(data.get("is_error"))
            text = data.get("result") or " ".join(data.get("errors") or []) or proc.stderr.strip()
            task.result = (str(text) or "(no output)")[:8000]
            task.cost_usd = data.get("total_cost_usd")
            task.status = "failed" if failed else "done"
        except FileNotFoundError:
            task.status, task.result = "failed", f"Claude Code not found ({self.claude})"
        except subprocess.TimeoutExpired:
            task.status, task.result = "failed", "Timed out after two hours."
        task.finished = time.time()
        self.store.save()
        log.info("task %s %s", task.id, task.status)
        try:
            self._post(
                "/v1/worker/events",
                {
                    "task_id": task.id,
                    "status": task.status,
                    "title": task.title[:500],
                    "summary": (task.result or "")[:8000],
                },
                timeout=15,
            )
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            log.warning("could not report the finished task: %s", exc)

    def list_tasks(self) -> dict[str, Any]:
        mine = sorted(self.store.tasks.values(), key=lambda t: t.started, reverse=True)[:8]
        return {
            "tasks_started_by_phone": [
                {
                    "task_id": t.id,
                    "title": t.title,
                    "status": t.status,
                    "started": time.strftime("%Y-%m-%d %H:%M", time.localtime(t.started)),
                }
                for t in mine
            ],
            "recent_claude_code_sessions": recent_sessions(self.sessions_root)
            if self.sessions_root.is_dir()
            else [],
        }

    def task_report(self, task_id: str) -> dict[str, Any]:
        task = self.store.latest() if task_id in ("", "last") else self.store.tasks.get(task_id)
        if task is None and SESSION_ID.match(task_id):  # a Claude Code session id
            path = find_transcript(self.sessions_root, task_id)
            return read_session(path) if path else {"error": "no such task or session"}
        if task is None and task_id in ("", "last") and self.sessions_root.is_dir():
            recent = recent_sessions(self.sessions_root, limit=1)
            if recent:
                path = find_transcript(self.sessions_root, recent[0]["session_id"])
                return {
                    "source": "latest Claude Code session",
                    **(read_session(path) if path else {}),
                }
        if task is None:
            return {"error": "no such task"}
        report: dict[str, Any] = {
            "task_id": task.id,
            "title": task.title,
            "status": task.status,
            "folder": task.cwd,
            "result": task.result,
            "running_for_minutes": round(((task.finished or time.time()) - task.started) / 60, 1),
        }
        if task.session_id:
            path = find_transcript(self.sessions_root, task.session_id)
            if path:
                report["latest_messages"] = read_session(path)["latest_messages"]
        return report


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="avb-worker", description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--dir",
        default=os.environ.get("AVB_WORKER_DIR") or str(Path.home() / "AgentVoiceBridge-Tasks"),
        help="Folder where phone tasks run (each named task gets a subfolder)",
    )
    parser.add_argument("--claude", default=os.environ.get("AVB_CLAUDE_BIN"))
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_env_file(Path(os.environ.get("AVB_HOOK_ENV", default_data_dir() / "hook.env")))
    token = os.environ.get("AVB_AGENT_TOKEN", "")
    if not token:
        raise SystemExit("AVB_AGENT_TOKEN is not set (run setup-pc-remote.ps1 first)")
    work = Path(args.dir)
    work.mkdir(parents=True, exist_ok=True)
    worker = Worker(
        os.environ.get("AVB_BRIDGE_URL", DEFAULT_URL), token, work_dir=work, claude=args.claude
    )
    try:
        worker.serve()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
