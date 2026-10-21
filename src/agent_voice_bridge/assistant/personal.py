"""Personal-assistant features: scheduled calls (reminders) and things to remember.

Both are stored in a small JSON file next to the bridge's database, so they survive
restarts. The scheduler checks every few seconds and, when a reminder is due, gets the
phone to ring with the reminder as the reason for the call.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import secrets
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from agent_voice_bridge.assistant.brain import Tool

log = logging.getLogger(__name__)

MAX_MEMORIES = 100
MAX_REMINDERS = 50


def data_file() -> Path:
    from agent_voice_bridge.config import default_data_dir

    db = os.environ.get("AVB_DB_PATH")
    base = Path(db).parent if db else default_data_dir()
    return base / "personal.json"


def user_zone() -> ZoneInfo:
    name = (os.environ.get("AVB_TIMEZONE") or "UTC").strip()
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        log.warning("unknown AVB_TIMEZONE %r; using UTC", name)
        return ZoneInfo("UTC")


class PersonalStore:
    def __init__(
        self,
        path: Path | None = None,
        *,
        zone: ZoneInfo | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.path = path or data_file()
        self.zone = zone or user_zone()
        self.clock = clock or (lambda: datetime.now(UTC))
        self._lock = threading.Lock()
        self.reminders: list[dict[str, Any]] = []
        self.memories: list[str] = []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.reminders = list(data.get("reminders", []))
            self.memories = [str(m) for m in data.get("memories", [])]
        except (OSError, ValueError, AttributeError):
            pass

    def _save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps({"reminders": self.reminders, "memories": self.memories}, indent=1),
                encoding="utf-8",
            )
            tmp.replace(self.path)

    def now_local(self) -> datetime:
        return self.clock().astimezone(self.zone)

    def local(self, iso_utc: str) -> str:
        return datetime.fromisoformat(iso_utc).astimezone(self.zone).strftime("%A %d %B, %H:%M")

    # -- reminders ----------------------------------------------------------------------

    def add_reminder(self, due: datetime, text: str) -> dict[str, Any]:
        if len(self.reminders) >= MAX_REMINDERS:
            raise ValueError("too many reminders; cancel some first")
        item = {
            "id": f"r{secrets.token_hex(2)}",
            "due": due.astimezone(UTC).isoformat(),
            "text": text.strip()[:500] or "You asked me to call you.",
        }
        self.reminders.append(item)
        self.reminders.sort(key=lambda r: r["due"])
        self._save()
        return item

    def cancel(self, reminder_id: str) -> bool:
        before = len(self.reminders)
        self.reminders = [r for r in self.reminders if r["id"] != reminder_id]
        if len(self.reminders) != before:
            self._save()
            return True
        return False

    def take_due(self) -> list[dict[str, Any]]:
        now = self.clock().astimezone(UTC).isoformat()
        due = [r for r in self.reminders if r["due"] <= now]
        if due:
            self.reminders = [r for r in self.reminders if r["due"] > now]
            self._save()
        return due

    # -- memories ---------------------------------------------------------------------------

    def remember(self, fact: str) -> None:
        fact = fact.strip()[:500]
        if fact and fact not in self.memories:
            self.memories = (self.memories + [fact])[-MAX_MEMORIES:]
            self._save()

    def forget(self, text: str) -> int:
        """Forget every memory containing ``text``; returns how many."""
        needle = text.strip().lower()
        keep = [m for m in self.memories if needle not in m.lower()] if needle else self.memories
        removed = len(self.memories) - len(keep)
        if removed:
            self.memories = keep
            self._save()
        return removed

    # -- for the brain -------------------------------------------------------------------------

    def context(self) -> str:
        now = self.now_local()
        lines = [
            f"Now it is {now.strftime('%A %d %B %Y, %H:%M')} in the user's time zone "
            f"({self.zone.key})."
        ]
        if self.memories:
            lines.append("Things the user asked you to remember: " + " | ".join(self.memories))
        if self.reminders:
            lines.append(
                "Scheduled calls: "
                + " | ".join(f"{self.local(r['due'])}: {r['text']}" for r in self.reminders[:10])
            )
        return "\n".join(lines)

    def parse_when(self, args: dict[str, Any]) -> datetime:
        minutes = args.get("in_minutes")
        if minutes not in (None, ""):
            value = float(minutes)
            if not 0 < value <= 60 * 24 * 60:
                raise ValueError("in_minutes must be between 0 and 86400")
            return self.clock() + timedelta(minutes=value)
        at = str(args.get("at") or "").strip()
        if not at:
            raise ValueError("give in_minutes or at")
        when = datetime.fromisoformat(at.replace(" ", "T"))
        if when.tzinfo is None:
            when = when.replace(tzinfo=self.zone)
        if when <= self.clock():
            raise ValueError(f"{at} is in the past (now: {self.now_local():%Y-%m-%d %H:%M})")
        return when

    def tools(self) -> list[Tool]:
        def obj(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
            return {
                "type": "object",
                "properties": props,
                "required": required,
                "additionalProperties": False,
            }

        async def schedule(args: dict[str, Any]) -> str:
            try:
                when = self.parse_when(args)
                item = self.add_reminder(when, str(args.get("message") or ""))
            except ValueError as exc:
                return f"error: {exc}"
            return f"Scheduled ({item['id']}) for {self.local(item['due'])}."

        async def list_reminders(_: dict[str, Any]) -> str:
            if not self.reminders:
                return "No calls are scheduled."
            return " ".join(
                f"{r['id']}: {self.local(r['due'])}, {r['text']}." for r in self.reminders
            )

        async def cancel(args: dict[str, Any]) -> str:
            return "Cancelled." if self.cancel(str(args.get("id"))) else "error: no such id"

        async def remember(args: dict[str, Any]) -> str:
            self.remember(str(args.get("fact") or ""))
            return "Remembered."

        async def forget(args: dict[str, Any]) -> str:
            n = self.forget(str(args.get("text") or ""))
            return f"Forgot {n} thing(s)." if n else "Nothing matched."

        return [
            Tool(
                "schedule_call",
                "Call the user later: a reminder, a wake-up call, 'call me in 10 minutes'. Give "
                "in_minutes, or at as local time 'YYYY-MM-DD HH:MM'. message is what to tell "
                "them when the call comes.",
                obj(
                    {
                        "in_minutes": {"type": "number"},
                        "at": {"type": "string"},
                        "message": {"type": "string"},
                    },
                    ["message"],
                ),
                schedule,
            ),
            Tool("list_scheduled_calls", "Calls scheduled for later.", obj({}, []), list_reminders),
            Tool(
                "cancel_scheduled_call",
                "Cancel a scheduled call by its id.",
                obj({"id": {"type": "string"}}, ["id"]),
                cancel,
            ),
            Tool(
                "remember",
                "Keep a fact about the user for future calls (name, preferences, people, "
                "projects). Only what they want remembered.",
                obj({"fact": {"type": "string"}}, ["fact"]),
                remember,
            ),
            Tool(
                "forget",
                "Forget remembered facts containing this text.",
                obj({"text": {"type": "string"}}, ["text"]),
                forget,
            ),
        ]


class Scheduler:
    """Rings the phone when a scheduled call is due."""

    def __init__(
        self,
        store: PersonalStore,
        on_due: Callable[[dict[str, Any]], None],
        *,
        interval: float = 5.0,
    ) -> None:
        self.store = store
        self.on_due = on_due
        self.interval = interval
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._run())

    async def _run(self) -> None:
        while True:
            for item in self.store.take_due():
                log.info("scheduled call due: %s", item["text"])
                try:
                    self.on_due(item)
                except Exception:
                    log.exception("scheduled call failed")
            await asyncio.sleep(self.interval)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
