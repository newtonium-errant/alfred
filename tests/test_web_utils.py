"""Unit tests for ``alfred.web.utils`` pure helpers."""

from __future__ import annotations

import array
import math

from alfred.web.utils import pcm_rms


def test_pcm_rms_constant() -> None:
    data = array.array("h", [8000] * 1000).tobytes()
    assert pcm_rms(data) == 8000.0


def test_pcm_rms_sine_is_amp_over_root2() -> None:
    amp = 10000
    sine = array.array("h", [
        int(amp * math.sin(2 * math.pi * 440 * i / 16000)) for i in range(1600)
    ]).tobytes()
    assert abs(pcm_rms(sine) - amp / math.sqrt(2)) < 50   # ≈ 7071


def test_pcm_rms_silence_and_empty() -> None:
    assert pcm_rms(b"\x00\x00" * 100) == 0.0
    assert pcm_rms(b"") == 0.0


def test_pcm_rms_odd_trailing_byte_dropped() -> None:
    # A half-sample tail is dropped rather than crashing frombytes().
    data = array.array("h", [5000] * 10).tobytes() + b"\x01"
    assert pcm_rms(data) == 5000.0
