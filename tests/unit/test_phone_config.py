import pytest

from agent_voice_bridge.phone_config import PhoneSettings

BASE = {
    "AVB_BARK_DEVICE_KEY": "dev",
    "AVB_SIP_PASSWORD": "a-long-sip-password-123",
}


def _set(monkeypatch: pytest.MonkeyPatch, **env: str) -> None:
    for k, v in {**BASE, **env}.items():
        monkeypatch.setenv(k, v)


def test_server_mode_with_tls(monkeypatch: pytest.MonkeyPatch, tmp_path: object) -> None:
    _set(
        monkeypatch,
        AVB_SIP_BIND="0.0.0.0",
        AVB_SIP_TLS_CERT="/c.crt",
        AVB_SIP_TLS_KEY="/c.key",
        AVB_SIP_PUBLIC_HOST="voice.example.com",
        AVB_SIP_MEDIA_IP="203.0.113.7",
    )
    s = PhoneSettings.from_env()
    assert s.transport == "tls" and s.sip.require_srtp and not s.sip.udp and not s.sip.tcp
    assert s.sip.media_ip == "203.0.113.7"
    assert s.sip.realm == "voice.example.com"
    assert s.dial_url == "linphone-sip:avb@voice.example.com;transport=tls"


def test_public_bind_without_tls_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _set(monkeypatch, AVB_SIP_BIND="0.0.0.0")
    with pytest.raises(ValueError, match="TLS only"):
        PhoneSettings.from_env()


def test_public_bind_with_plain_listeners_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _set(
        monkeypatch,
        AVB_SIP_BIND="0.0.0.0",
        AVB_SIP_TLS_CERT="/c",
        AVB_SIP_TLS_KEY="/k",
        AVB_SIP_PLAIN="1",
    )
    with pytest.raises(ValueError):
        PhoneSettings.from_env()


def test_tailscale_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    _set(monkeypatch, AVB_SIP_BIND="100.64.1.2")
    s = PhoneSettings.from_env()
    assert s.transport == "udp" and not s.sip.require_srtp
    assert s.dial_url == "linphone-sip:avb@100.64.1.2"


def test_missing_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AVB_BARK_DEVICE_KEY", raising=False)
    monkeypatch.setenv("AVB_SIP_BIND", "100.64.1.2")
    with pytest.raises(ValueError, match="AVB_BARK_DEVICE_KEY"):
        PhoneSettings.from_env()
