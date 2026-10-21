"""G.711 mu-law (PCMU) and A-law (PCMA) codecs, pure Python with lookup tables.

``audioop`` was removed in Python 3.13, so this avoids a native dependency. Audio is
16-bit signed little-endian mono PCM at 8 kHz.
"""

from __future__ import annotations

import array
import sys

_BIAS = 0x84
_CLIP = 32635


def _ulaw_encode_sample(sample: int) -> int:
    sign = 0x80 if sample < 0 else 0
    if sample < 0:
        sample = -sample
    sample = min(sample, _CLIP) + _BIAS
    exponent = 7
    mask = 0x4000
    while exponent > 0 and not (sample & mask):
        exponent -= 1
        mask >>= 1
    mantissa = (sample >> (exponent + 3)) & 0x0F
    return ~(sign | (exponent << 4) | mantissa) & 0xFF


def _ulaw_decode_byte(byte: int) -> int:
    byte = ~byte & 0xFF
    sign = byte & 0x80
    exponent = (byte >> 4) & 0x07
    mantissa = byte & 0x0F
    sample = (((mantissa << 3) + _BIAS) << exponent) - _BIAS
    return -sample if sign else sample


def _alaw_encode_sample(sample: int) -> int:
    sign = 0x80 if sample >= 0 else 0
    if sample < 0:
        sample = -sample - 1 if sample > -32768 else 32767
    sample = min(sample, 32767)
    if sample >= 256:
        exponent = 7
        mask = 0x4000
        while exponent > 1 and not (sample & mask):
            exponent -= 1
            mask >>= 1
        mantissa = (sample >> (exponent + 3)) & 0x0F
        value = (exponent << 4) | mantissa
    else:
        value = sample >> 4
    return (sign | value) ^ 0x55


def _alaw_decode_byte(byte: int) -> int:
    byte ^= 0x55
    sign = byte & 0x80
    exponent = (byte >> 4) & 0x07
    mantissa = byte & 0x0F
    if exponent == 0:
        sample = (mantissa << 4) + 8
    else:
        sample = ((mantissa << 4) + 0x108) << (exponent - 1)
    return sample if sign else -sample


# Encode tables indexed by (sample >> 2) + 8192 keep them small (14-bit precision is
# below G.711's own resolution); decode tables cover all 256 codes.
_ULAW_DEC = [_ulaw_decode_byte(b) for b in range(256)]
_ALAW_DEC = [_alaw_decode_byte(b) for b in range(256)]
_ULAW_ENC = bytes(_ulaw_encode_sample((i - 8192) << 2) for i in range(16384))
_ALAW_ENC = bytes(_alaw_encode_sample((i - 8192) << 2) for i in range(16384))


def _samples(pcm: bytes) -> array.array[int]:
    a = array.array("h")
    a.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    if sys.byteorder == "big":
        a.byteswap()
    return a


def _to_pcm(samples: list[int]) -> bytes:
    a = array.array("h", samples)
    if sys.byteorder == "big":
        a.byteswap()
    return a.tobytes()


def encode(pcm: bytes, payload_type: int) -> bytes:
    table = _ULAW_ENC if payload_type == 0 else _ALAW_ENC
    return bytes(table[(s >> 2) + 8192] for s in _samples(pcm))


def decode(data: bytes, payload_type: int) -> bytes:
    table = _ULAW_DEC if payload_type == 0 else _ALAW_DEC
    return _to_pcm([table[b] for b in data])
