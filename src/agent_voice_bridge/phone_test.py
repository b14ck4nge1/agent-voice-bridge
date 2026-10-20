"""``avb-admin phone-test``: prove the phone path without any agent or approval.

Sends a Bark alert whose tap dials this PC, answers the call, speaks a greeting, reads
back every key you press, and hangs up on ``#``. It reports what happened, so you can
check notification, network, audio in both directions and DTMF in one go.

``--direct`` skips Bark: it waits for Linphone to register, then rings it from the
server. On iOS that only rings while Linphone is open or was just in use, because
without push iOS suspends the app and closes its connection.
"""

from __future__ import annotations

import asyncio
import time

from agent_voice_bridge.notify.bark import BarkError, BarkNotifier
from agent_voice_bridge.phone import make_tts
from agent_voice_bridge.phone_config import PhoneSettings
from agent_voice_bridge.sip.server import DialError, SipCall, SipServer
from agent_voice_bridge.voice.audio import silence

DIGIT_NAMES = {"*": "star", "#": "hash"}


async def run_phone_test(
    *, seconds: int = 180, send_bark: bool = True, direct: bool = False
) -> int:
    try:
        settings = PhoneSettings.from_env()
    except ValueError as exc:
        print(f"Configuration problem: {exc}")
        return 2
    tts = make_tts(settings)
    done = asyncio.Event()
    report: dict[str, object] = {"call": False, "digits": [], "packets_in": 0}

    async def on_call(call: SipCall) -> None:
        report["call"] = True
        print(f"Call answered from {call.peer.addr[0]} over {call.peer.kind.upper()}")

        async def say(text: str) -> None:
            pcm, _ = await tts.synthesize(text)
            call.rtp.play(pcm + silence(0.2))

        await say(
            "This is the Agent Voice Bridge test call. Press any key and I will read it back. "
            "Press hash to finish."
        )
        while not call.ended.is_set():
            try:
                digit = await asyncio.wait_for(call.dtmf.get(), 60)
            except TimeoutError:
                await say("I didn't hear any keys. Goodbye.")
                break
            report["digits"].append(digit)  # type: ignore[attr-defined]
            print(f"  key {digit}")
            call.rtp.stop_playback()
            if digit == "#":
                await say("Test complete. Goodbye.")
                break
            await say(f"You pressed {DIGIT_NAMES.get(digit, digit)}.")
        if not call.ended.is_set():
            await asyncio.wait_for(call.rtp.wait_played(), 15)
        report["packets_in"] = call.rtp.packets_in
        done.set()

    server = SipServer(settings.sip, on_call)
    await server.start()
    print(f"SIP server listening on {settings.sip.bind_host}:{settings.sip.port}")
    print(f"Dial link: {settings.dial_url}")
    try:
        if direct:
            print("Open Linphone on the phone (keep it open) so it signs in to this server...")
            deadline = time.monotonic() + seconds
            while not server.phone_registered and time.monotonic() < deadline:
                await asyncio.sleep(1)
            if not server.phone_registered:
                print(f"Linphone did not sign in within {seconds}s.")
                return 1
            print("Linphone signed in; calling it now.")
            try:
                await server.dial(ring_seconds=45)
            except DialError as exc:
                print(f"Call failed: {exc}")
                return 1
        elif send_bark:
            try:
                await BarkNotifier(settings.bark).send(
                    "Agent Voice Bridge test", "Tap to call your PC.", settings.dial_url
                )
                print("Bark notification sent; tap it on your iPhone.")
            except BarkError as exc:
                print(f"Bark failed: {exc}")
                return 1
        started = time.monotonic()
        try:
            await asyncio.wait_for(done.wait(), seconds)
        except TimeoutError:
            print(f"No completed call within {seconds}s.")
        registered = "yes" if server.phone_registered else "no"
        print(
            f"Result: phone registered: {registered}; call answered: {report['call']}; "
            f"keys: {''.join(report['digits']) or '-'}; "  # type: ignore[arg-type]
            f"audio packets from phone: {report['packets_in']}; "
            f"time: {time.monotonic() - started:.0f}s"
        )
        return 0 if report["call"] else 1
    finally:
        await server.stop()
