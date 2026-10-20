import pytest

from agent_voice_bridge.voice.conversation.intents import Intent, extract_code, parse


@pytest.mark.parametrize(
    ("text", "intent"),
    [
        ("Approve it.", Intent.APPROVE),
        ("go ahead", Intent.APPROVE),
        ("Don't approve that", Intent.REJECT),
        ("do not allow it", Intent.REJECT),
        ("Reject", Intent.REJECT),
        ("deny it, tell it to use pnpm instead", Intent.REJECT),
        ("Tell it to use pnpm instead", Intent.REJECT),
        ("What command?", Intent.DETAILS),
        ("Will that delete anything?", Intent.RISK),
        ("Is it safe?", Intent.RISK),
        ("Which project is this?", Intent.CONTEXT),
        ("say that again", Intent.REPEAT),
        ("um", Intent.UNKNOWN),
        ("", Intent.UNKNOWN),
    ],
)
def test_discussion_intents(text: str, intent: Intent) -> None:
    assert parse(text).intent == intent


@pytest.mark.parametrize(
    ("text", "intent"),
    [
        ("Yes.", Intent.YES),
        ("yeah", Intent.YES),
        ("correct", Intent.YES),
        ("no", Intent.NO),
        ("wait", Intent.NO),
        ("hmm", Intent.UNKNOWN),
        ("yes no", Intent.NO),
    ],
)
def test_confirmation_intents(text: str, intent: Intent) -> None:
    assert parse(text, confirming=True).intent == intent


def test_reject_feedback_is_kept() -> None:
    parsed = parse("Reject it and tell it to use pnpm instead.")
    assert parsed.intent == Intent.REJECT
    assert parsed.message == "tell it to use pnpm instead"


def test_extract_code() -> None:
    assert extract_code("one two three four five six") == "123456"
    assert extract_code("my code is 4 8 1 5 1 6") == "481516"
    assert extract_code("oh seven 3 nine nine 2") == "073992"
    assert extract_code("12345") is None
    assert extract_code("1234567") is None
