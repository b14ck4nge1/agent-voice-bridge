"""Small PCM helpers: WAV decoding and resampling to the 8 kHz telephone rate."""

from __future__ import annotations

import array
import io
import sys
import wave

TELEPHONE_RATE = 8000


def _samples(pcm: bytes) -> array.array[int]:
    a = array.array("h")
    a.frombytes(pcm[: len(pcm) - len(pcm) % 2])
    if sys.byteorder == "big":
        a.byteswap()
    return a


def _bytes(samples: array.array[int]) -> bytes:
    if sys.byteorder == "big":
        samples.byteswap()
    return samples.tobytes()


def wav_to_pcm16(data: bytes) -> tuple[bytes, int]:
    """Decode a PCM WAV file to mono 16-bit samples. Returns (pcm, sample_rate)."""
    with wave.open(io.BytesIO(data)) as w:
        if w.getsampwidth() != 2:
            raise ValueError("only 16-bit WAV is supported")
        rate, channels = w.getframerate(), w.getnchannels()
        frames = w.readframes(w.getnframes())
    samples = _samples(frames)
    if channels > 1:
        samples = array.array(
            "h",
            (sum(samples[i : i + channels]) // channels for i in range(0, len(samples), channels)),
        )
    return _bytes(samples), rate


def resample(pcm: bytes, src_rate: int, dst_rate: int = TELEPHONE_RATE) -> bytes:
    """Linear-interpolation resampler with a moving-average pre-filter when downsampling.

    Good enough for speech prompts over G.711; not a high-fidelity converter.
    """
    if src_rate == dst_rate:
        return pcm
    s = _samples(pcm)
    if not s:
        return b""
    if dst_rate < src_rate:
        width = max(1, round(src_rate / dst_rate))
        acc, filtered = 0, array.array("h")
        window: list[int] = []
        for v in s:
            window.append(v)
            acc += v
            if len(window) > width:
                acc -= window.pop(0)
            filtered.append(int(acc / len(window)))
        s = filtered
    ratio = src_rate / dst_rate
    n = int(len(s) / ratio)
    out = array.array("h", bytes(2 * n))
    last = len(s) - 1
    for i in range(n):
        pos = i * ratio
        j = int(pos)
        frac = pos - j
        a = s[j]
        b = s[j + 1] if j < last else a
        out[i] = int(a + (b - a) * frac)
    return _bytes(out)


def silence(seconds: float, rate: int = TELEPHONE_RATE) -> bytes:
    return b"\x00\x00" * int(seconds * rate)
