"""The "linphone" path end to end: avb-hook -> bridge -> the bridge signs in to a fake
sip.linphone.org and calls the phone through it -> spoken prompts -> keypad -> decision.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from agent_voice_bridge.linphone import LinphoneSettings, LinphoneStack
from agent_voice_bridge.sip.uac import AccountConfig
from tests.conftest import run_bridge
from tests.end_to_end.test_hook_e2e import claude_input, decision, finish, run_hook
from tests.end_to_end.test_phone_e2e import ToneTTS, _wait_for
from tests.support.fake_provider import FakeProvider
from tests.support.sip_client import make_self_signed


async def test_linphone_call_then_keypad_approval(settings: Any, tmp_path: Path) -> None:
    cert, key = make_self_signed(tmp_path)
    provider = FakeProvider(cert, key)
    await provider.start()
    stack = LinphoneStack(
        LinphoneSettings(
            account=AccountConfig(
                username="bot",
                password=provider.password,
                domain="localhost",
                proxy_host="127.0.0.1",
                proxy_port=provider.port,
                bind_host="127.0.0.1",
                media_ip="127.0.0.1",
                ca_file=cert,
            ),
            target="phone",
            ring_seconds=5,
        ),
        tts=ToneTTS(),
    )
    tts: ToneTTS = stack.tts  # type: ignore[assignment]
    # The bridge signs in while starting; keep this loop (the provider's) free meanwhile.
    bridge = run_bridge(settings, stack.dispatcher, stack.run)
    url = await asyncio.to_thread(bridge.__enter__)
    try:
        if True:
            proc = await run_hook(url, claude_input("sess-lin", "npm test"), tmp=tmp_path)
            await _wait_for(lambda: provider.answered.is_set())
            await _wait_for(lambda: any("npm test" in s for s in tts.spoken))
            await asyncio.sleep(0.2)
            await provider.press("1")  # approve
            await _wait_for(
                lambda: any(s.startswith("Please confirm: approve") for s in tts.spoken)
            )
            await provider.press("1")  # yes
            out, _ = await finish(proc)
            assert decision(out) == {"behavior": "allow"}
            await _wait_for(lambda: "BYE" in provider.methods())  # the bridge hangs up
    finally:
        await asyncio.to_thread(bridge.__exit__, None, None, None)
        await provider.stop()


def test_settings_need_a_second_account(monkeypatch: Any) -> None:
    import pytest

    monkeypatch.setenv("AVB_LINPHONE_USERNAME", "alice")
    monkeypatch.setenv("AVB_LINPHONE_PASSWORD", "pw")
    monkeypatch.setenv("AVB_LINPHONE_CALL", "alice")
    with pytest.raises(ValueError, match="second"):
        LinphoneSettings.from_env()
    monkeypatch.setenv("AVB_LINPHONE_CALL", "kian")
    monkeypatch.setenv("AVB_LINPHONE_PROXY", "proxy.example:5223")
    s = LinphoneSettings.from_env()
    assert s.account.domain == "sip.linphone.org" and s.account.proxy_port == 5223
    assert s.account.target_uri(s.target) == "sip:kian@sip.linphone.org"
