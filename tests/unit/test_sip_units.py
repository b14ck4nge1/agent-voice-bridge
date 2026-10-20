import struct

import pytest

from agent_voice_bridge.sip import g711, sdp
from agent_voice_bridge.sip.digest import DigestAuthenticator, compute_response, parse_auth
from agent_voice_bridge.sip.message import (
    SipParseError,
    frame_length,
    header_params,
    parse,
    response_for,
    tag_of,
    uri_of,
    user_of,
)
from agent_voice_bridge.voice.audio import resample

INVITE = (
    b"INVITE sip:avb@100.64.0.1 SIP/2.0\r\n"
    b"v: SIP/2.0/UDP 10.0.0.2:5060;branch=z9hG4bKabc;rport\r\n"
    b"f: <sip:phone@100.64.0.1>;tag=111\r\n"
    b"t: <sip:avb@100.64.0.1>\r\n"
    b"i: call-1\r\n"
    b"CSeq: 20 INVITE\r\n"
    b"Subject: folded\r\n  continuation\r\n"
    b"l: 4\r\n\r\nbodyEXTRA"
)


def test_parse_request_with_compact_headers_and_folding() -> None:
    m = parse(INVITE)
    assert m.method == "INVITE" and m.uri == "sip:avb@100.64.0.1"
    assert m.call_id == "call-1" and m.cseq == (20, "INVITE")
    assert m.get("Subject") == "folded continuation"
    assert m.body == b"body"  # trimmed to Content-Length
    assert tag_of(m.get("From")) == "111" and tag_of(m.get("To")) is None


def test_response_copies_dialog_headers_and_adds_tag() -> None:
    r = response_for(parse(INVITE), 200, "OK", to_tag="xyz")
    raw = r.to_bytes()
    assert raw.startswith(b"SIP/2.0 200 OK\r\n")
    assert b"To: <sip:avb@100.64.0.1>;tag=xyz" in raw and b"Content-Length: 0" in raw
    assert parse(raw).status == 200


@pytest.mark.parametrize(
    "bad",
    [
        b"",
        b"HELLO\r\n\r\n",
        b"INVITE sip:x SIP/3.0\r\n\r\n",
        b"SIP/2.0 abc OK\r\n\r\n",
        b"INVITE sip:x SIP/2.0\r\nnocolon\r\n\r\n",
        b"X" * 70000,
    ],
)
def test_parse_rejects_garbage(bad: bytes) -> None:
    with pytest.raises(SipParseError):
        parse(bad)


def test_stream_framing() -> None:
    msg = INVITE
    assert frame_length(msg[:30]) is None
    assert frame_length(msg) == msg.index(b"\r\n\r\n") + 4 + 4
    with pytest.raises(SipParseError):
        frame_length(b"INVITE x SIP/2.0\r\nContent-Length: 999999\r\n\r\n")


def test_header_helpers() -> None:
    assert header_params("SIP/2.0/UDP h:1;branch=z9;rport") == {"branch": "z9", "rport": ""}
    assert header_params('"Bob" <sip:b@h;transport=tcp>;tag=9;expires=60') == {
        "tag": "9",
        "expires": "60",
    }
    assert uri_of('"Bob" <sip:b@h;transport=tcp>;tag=9') == "sip:b@h;transport=tcp"
    assert user_of("sip:phone@100.64.0.1:5060") == "phone"


def _auth_header(
    auth: DigestAuthenticator,
    *,
    password: str,
    nc: str = "00000001",
    algorithm: str = "MD5",
    nonce: str | None = None,
    uri: str = "sip:h",
) -> str:
    nonce = nonce or auth.new_nonce()
    resp = compute_response(
        algorithm=algorithm,
        username="phone",
        realm="r",
        password=password,
        method="REGISTER",
        uri=uri,
        nonce=nonce,
        nc=nc,
        cnonce="cn",
        qop="auth",
    )
    return (
        f'Digest username="phone", realm="r", nonce="{nonce}", uri="{uri}", response="{resp}", '
        f'algorithm={algorithm}, qop=auth, nc={nc}, cnonce="cn"'
    )


@pytest.mark.parametrize("algorithm", ["MD5", "SHA-256"])
def test_digest_accepts_correct_password(algorithm: str) -> None:
    auth = DigestAuthenticator("r", "phone", "secret-password-123")
    assert auth.verify(
        _auth_header(auth, password="secret-password-123", algorithm=algorithm), "REGISTER"
    ) == (True, False)


def test_digest_rejects_wrong_password_replay_and_forged_nonce() -> None:
    auth = DigestAuthenticator("r", "phone", "secret-password-123")
    assert auth.verify(_auth_header(auth, password="nope"), "REGISTER") == (False, False)
    nonce = auth.new_nonce()
    header = _auth_header(auth, password="secret-password-123", nonce=nonce)
    assert auth.verify(header, "REGISTER")[0]
    assert auth.verify(header, "REGISTER") == (False, False)  # same nc replayed
    assert auth.verify(
        _auth_header(auth, password="secret-password-123", nonce=nonce, nc="00000002"), "REGISTER"
    )[0]
    forged = _auth_header(auth, password="secret-password-123", nonce="MTIzOmFiYw")
    assert auth.verify(forged, "REGISTER") == (False, False)
    assert auth.verify(None, "REGISTER") == (False, False)
    assert parse_auth("Basic abc") == {}


def test_digest_expired_nonce_is_stale() -> None:
    now = [1000.0]
    auth = DigestAuthenticator("r", "phone", "secret-password-123", clock=lambda: now[0])
    header = _auth_header(auth, password="secret-password-123")
    now[0] += 3600
    assert auth.verify(header, "REGISTER") == (False, True)
    assert all("stale=true" in c for c in auth.challenges(stale=True))


OFFER = (
    "v=0\r\no=- 1 1 IN IP4 10.0.0.2\r\ns=-\r\nc=IN IP4 10.0.0.2\r\nt=0 0\r\n"
    "m=audio 7078 RTP/AVP 96 8 0 101\r\na=rtpmap:96 opus/48000/2\r\na=rtpmap:101 telephone-event/8000\r\n"
    "a=crypto:1 AES_CM_128_HMAC_SHA1_80 inline:abc\r\na=sendrecv\r\n"
)


def test_sdp_offer_and_answer() -> None:
    offer = sdp.parse_offer(OFFER.encode())
    assert (offer.address, offer.port, offer.profile) == ("10.0.0.2", 7078, "RTP/AVP")
    assert offer.telephone_event == 101 and offer.has_crypto
    assert sdp.choose_codec(offer) == 8  # first G.711 in the caller's order
    answer = sdp.build_answer(
        address="100.64.0.1", port=40000, codec=8, telephone_event=101
    ).decode()
    assert "m=audio 40000 RTP/AVP 8 101" in answer and "c=IN IP4 100.64.0.1" in answer
    assert "a=crypto" not in answer


def test_sdp_without_g711_or_audio() -> None:
    assert sdp.choose_codec(sdp.parse_offer(OFFER.replace(" 96 8 0 101", " 96").encode())) is None
    with pytest.raises(sdp.SdpError):
        sdp.parse_offer(b"v=0\r\nc=IN IP4 1.2.3.4\r\nm=video 9 RTP/AVP 96\r\n")


@pytest.mark.parametrize("pt", [0, 8])
def test_g711_round_trip(pt: int) -> None:
    samples = [0, 100, -100, 1000, -1000, 8000, -8000, 30000, -30000, 32767, -32768]
    pcm = struct.pack(f"<{len(samples)}h", *samples)
    encoded = g711.encode(pcm, pt)
    assert len(encoded) == len(samples)
    decoded = struct.unpack(f"<{len(samples)}h", g711.decode(encoded, pt))
    for a, b in zip(samples, decoded, strict=True):
        assert abs(a - b) <= max(16, abs(a) * 0.07), (pt, a, b)


def test_g711_known_codes() -> None:
    assert g711.encode(b"\x00\x00", 0) == b"\xff"  # mu-law silence
    assert g711.encode(b"\x00\x00", 8) == b"\xd5"  # A-law silence


def test_resample_length_and_tone() -> None:
    from tests.support.sip_client import goertzel, tone

    out = resample(tone(440, 1.0, rate=22050), 22050, 8000)
    assert abs(len(out) // 2 - 8000) <= 1
    assert goertzel(out, 440) > 50 * goertzel(out, 1500)
