"""Rule-based intent detection for the approval conversation (English).

Deliberately conservative: anything ambiguous is UNKNOWN, and UNKNOWN never leads
to a decision. A future LLM layer may answer free-form questions, but decisions are
always taken from these explicit intents plus a confirmation step.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class Intent(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    YES = "yes"
    NO = "no"
    DETAILS = "details"
    RISK = "risk"
    CONTEXT = "context"
    REPEAT = "repeat"
    HELP = "help"
    SKIP = "skip"
    UNKNOWN = "unknown"


@dataclass
class Parsed:
    intent: Intent
    message: str | None = None  # feedback to pass to the agent on reject


def _norm(text: str) -> str:
    text = text.lower().replace("’", "'")
    text = re.sub(r"[^a-z0-9' ]+", " ", text)
    return " ".join(text.split())


_YES = re.compile(
    r"^(yes|yeah|yep|yup|correct|confirm(ed)?|that's (right|correct)|affirmative|i confirm|sure|"
    r"yes please|yes confirm|yes i do)\b"
)
# Anywhere in the utterance: "yes, no wait" must not count as a yes.
_NO = re.compile(r"\b(no|nope|nah|negative|not|wait|hold on|cancel|stop|don't|do not)\b")
_NEGATED_APPROVE = re.compile(
    r"\b(don't|do not|not|never|no)\s+(approve|allow|accept|permit|go ahead)"
)
_REJECT = re.compile(r"\b(reject|deny|decline|refuse|block|disallow|veto)\b")
_APPROVE = re.compile(r"\b(approve|approved|allow|accept|permit|grant|go ahead)\b")
_CHANGE = re.compile(r"\b(tell it to|ask it to|instead|rather|use)\b")
_DETAILS = re.compile(
    r"\b(what|which|exact|exactly|full|details?|read|arguments?|command|file|path|show)\b"
)
_RISK = re.compile(
    r"\b(risk|risky|danger|dangerous|safe|harm|harmful|delete|deleting|destroy|break|damage)\b"
)
_CONTEXT = re.compile(r"\b(who|which agent|which project|what project|where|why|reason|session)\b")
_REPEAT = re.compile(r"\b(repeat|again|pardon|didn't (catch|hear)|say that)\b")
_HELP = re.compile(r"\b(help|options|what can i say)\b")
_SKIP = re.compile(r"\b(skip|next( one)?|later)\b")

_FEEDBACK_SPLIT = re.compile(
    r"\b(?:reject|deny|decline|refuse|block)\b(?:\s+(?:it|this|that))?(?:\s*(?:and|but|,))?\s*(.*)$"
)


def _feedback(original: str) -> str | None:
    match = _FEEDBACK_SPLIT.search(original.lower())
    if not match:
        return None
    rest = match.group(1).strip(" ,.")
    if not rest:
        return None
    start = len(original) - len(match.group(1))
    return original[start:].strip(" ,.") or None


def parse(text: str, *, confirming: bool = False) -> Parsed:
    t = _norm(text)
    if not t:
        return Parsed(Intent.UNKNOWN)
    if confirming:
        if _YES.search(t) and not _NO.search(t):
            return Parsed(Intent.YES)
        if _NO.search(t):
            return Parsed(Intent.NO)
    if _NEGATED_APPROVE.search(t) or _REJECT.search(t):
        return Parsed(Intent.REJECT, _feedback(text))
    if _APPROVE.search(t):
        return Parsed(Intent.APPROVE)
    if _CHANGE.search(t) and re.search(r"\b(tell it to|ask it to|instead)\b", t):
        # "Tell it to use pnpm instead" is a request for changes: reject with feedback.
        return Parsed(Intent.REJECT, text.strip())
    if _REPEAT.search(t):
        return Parsed(Intent.REPEAT)
    if _HELP.search(t):
        return Parsed(Intent.HELP)
    if _RISK.search(t):
        return Parsed(Intent.RISK)
    if _CONTEXT.search(t):
        return Parsed(Intent.CONTEXT)
    if _DETAILS.search(t):
        return Parsed(Intent.DETAILS)
    if _SKIP.search(t):
        return Parsed(Intent.SKIP)
    return Parsed(Intent.UNKNOWN)


_WORD_DIGITS = {
    "zero": "0",
    "oh": "0",
    "o": "0",
    "one": "1",
    "two": "2",
    "to": "2",
    "too": "2",
    "three": "3",
    "four": "4",
    "for": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
}


def extract_code(text: str, digits: int = 6) -> str | None:
    """Pull a spoken one-time code out of an utterance ('one two three 4 5 6')."""
    out = []
    for token in re.findall(r"[a-z]+|\d", text.lower()):
        if token.isdigit():
            out.append(token)
        elif token in _WORD_DIGITS:
            out.append(_WORD_DIGITS[token])
    code = "".join(out)
    return code if len(code) == digits else None
