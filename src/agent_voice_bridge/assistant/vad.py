"""Find where you start and stop talking in the call audio (energy-based, no model).

Frames are 20 ms of 8 kHz PCM16. The noise floor adapts to the line; speech is a
few frames clearly above it, and an utterance ends after ``end_silence`` seconds
of quiet. ``on_speech_start`` lets the caller stop its own speech (barge-in).
"""

from __future__ import annotations

import array
import math
from collections.abc import Callable

FRAME = 320  # bytes: 20 ms at 8 kHz, 16-bit


def rms(frame: bytes) -> float:
    samples = array.array("h", frame[: len(frame) - len(frame) % 2])
    if not samples:
        return 0.0
    return math.sqrt(sum(s * s for s in samples) / len(samples))


class Endpointer:
    def __init__(
        self,
        on_utterance: Callable[[bytes], None],
        *,
        on_speech_start: Callable[[], None] | None = None,
        boost: Callable[[], float] | None = None,
        min_level: float = 300.0,
        ratio: float = 3.0,
        start_frames: int = 4,
        end_silence: float = 0.8,
        max_seconds: float = 30.0,
        pre_roll: float = 0.3,
    ) -> None:
        self.on_utterance = on_utterance
        self.on_speech_start = on_speech_start
        # >1 while we are talking: our own voice echoed back by the line must not count.
        self.boost = boost or (lambda: 1.0)
        self.min_level = min_level
        self.ratio = ratio
        self.start_frames = start_frames
        self.end_frames = int(end_silence / 0.02)
        self.max_frames = int(max_seconds / 0.02)
        self.pre_frames = int(pre_roll / 0.02)
        self.floor = min_level / ratio
        self.paused = False  # e.g. while a keypad code is being entered
        self._buf = b""
        self._recent: list[bytes] = []
        self._loud = 0
        self._quiet = 0
        self._speech: list[bytes] | None = None

    def threshold(self) -> float:
        return max(self.min_level, self.floor * self.ratio) * self.boost()

    def feed(self, pcm: bytes) -> None:
        self._buf += pcm
        while len(self._buf) >= FRAME:
            frame, self._buf = self._buf[:FRAME], self._buf[FRAME:]
            self._frame(frame)

    def _frame(self, frame: bytes) -> None:
        level = rms(frame)
        loud = level > self.threshold()
        if self._speech is None:
            if not loud:  # track the noise floor only outside speech
                self.floor = 0.95 * self.floor + 0.05 * level
            self._recent = (self._recent + [frame])[-(self.pre_frames + self.start_frames) :]
            self._loud = self._loud + 1 if loud else 0
            if self._loud >= self.start_frames and not self.paused:
                self._speech = list(self._recent)
                self._quiet = 0
                if self.on_speech_start is not None:
                    self.on_speech_start()
            return
        self._speech.append(frame)
        self._quiet = 0 if loud else self._quiet + 1
        if self._quiet >= self.end_frames or len(self._speech) >= self.max_frames:
            audio = b"".join(self._speech[: len(self._speech) - min(self._quiet, self.end_frames)])
            self._speech, self._loud, self._recent = None, 0, []
            if len(audio) >= FRAME * 10:  # ignore blips shorter than 0.2 s
                self.on_utterance(audio)
