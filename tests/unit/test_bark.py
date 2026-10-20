import json

import httpx
import pytest

from agent_voice_bridge.notify.bark import (
    BarkConfig,
    BarkError,
    BarkNotifier,
    decrypt_gcm,
    encrypt_gcm,
)

KEY = "k" * 16 + "0123456789abcdef"  # 32 ASCII chars


def test_plain_payload() -> None:
    n = BarkNotifier(BarkConfig(device_key="dev123"))
    p = n.build_payload("Approval needed", "Claude Code · frontend", "linphone-sip:avb@100.64.0.1")
    assert p == {
        "device_key": "dev123",
        "title": "Approval needed",
        "body": "Claude Code · frontend",
        "group": "agent-voice-bridge",
        "level": "critical",
        "volume": "8",
        "call": "1",
        "url": "linphone-sip:avb@100.64.0.1",
    }


def test_encrypted_payload_hides_everything() -> None:
    n = BarkNotifier(BarkConfig(device_key="dev123", encryption_key=KEY))
    p = n.build_payload("Approval needed", "Codex · api", "linphone-sip:avb@100.64.0.1")
    assert set(p) == {"device_key", "ciphertext", "iv"}
    assert len(p["iv"]) == 12 and p["iv"].isascii()
    inner = json.loads(decrypt_gcm(KEY, p["ciphertext"], p["iv"]))
    assert inner["url"] == "linphone-sip:avb@100.64.0.1" and inner["call"] == "1"
    assert "linphone" not in json.dumps(p)


def test_gcm_known_layout() -> None:
    # Combined mode: ciphertext followed by the 16-byte tag, as CryptoSwift's GCM(.combined).
    ct, iv = encrypt_gcm(KEY, "{}", iv="abcdefghijkl")
    import base64

    assert len(base64.b64decode(ct)) == 2 + 16
    assert decrypt_gcm(KEY, ct, iv) == "{}"


@pytest.mark.parametrize(
    "cfg",
    [
        BarkConfig(device_key=""),
        BarkConfig(device_key="a/b"),
        BarkConfig(device_key="x", server="http://api.day.app"),
        BarkConfig(device_key="x", encryption_key="short"),
        BarkConfig(device_key="x", level="loud"),
    ],
)
def test_invalid_config(cfg: BarkConfig) -> None:
    with pytest.raises(BarkError):
        cfg.validate()


async def test_send_posts_json_and_reports_errors() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if json.loads(request.content)["device_key"] == "bad":
            return httpx.Response(400, json={"code": 400, "message": "failed to get device token"})
        return httpx.Response(200, json={"code": 200, "message": "success"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await BarkNotifier(BarkConfig(device_key="good"), client=client).send("t", "b")
    assert str(seen[0].url) == "https://api.day.app/push"
    with pytest.raises(BarkError, match="device token"):
        await BarkNotifier(BarkConfig(device_key="bad"), client=client).send("t", "b")
