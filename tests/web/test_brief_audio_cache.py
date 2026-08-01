"""brief_audio_cache pins (Phase C3a) — the credit-guard store.

Content-keyed cache: a same-day brief regen (changed narration text) re-renders;
an unchanged brief always hits (zero synth). Pins the key derivation + hit/miss +
atomic write + the unset-dir degradation.

Tests run unconditionally per ``feedback_regression_pin_unconditional.md``.
"""

from __future__ import annotations

from pathlib import Path

from alfred.web.brief_audio_cache import (
    audio_cache_key,
    narration_content_hash,
    read_cached_audio,
    write_cached_audio,
)

DATE = "2026-08-01"


def test_content_hash_changes_with_text() -> None:
    a = narration_content_hash("good morning, here's your day")
    b = narration_content_hash("good morning, here's your day.")  # one char differs
    assert a != b
    assert narration_content_hash("x") == narration_content_hash("x")  # stable


def test_key_distinguishes_date_content_speed() -> None:
    h1, h2 = narration_content_hash("one"), narration_content_hash("two")
    base = audio_cache_key(DATE, h1, 1.0)
    assert base.endswith(".mp3")
    assert base != audio_cache_key("2026-08-02", h1, 1.0)   # date
    assert base != audio_cache_key(DATE, h2, 1.0)           # content
    assert base != audio_cache_key(DATE, h1, 1.1)           # speed
    assert audio_cache_key(DATE, h1, None) != audio_cache_key(DATE, h1, 1.0)  # default vs 1.0


def test_write_then_read_roundtrip(tmp_path: Path) -> None:
    key = audio_cache_key(DATE, narration_content_hash("hello"), 1.0)
    assert read_cached_audio(tmp_path, key) is None  # miss before write
    assert write_cached_audio(tmp_path, key, b"MP3DATA") is True
    assert read_cached_audio(tmp_path, key) == b"MP3DATA"  # hit after write


def test_regen_new_content_is_a_miss(tmp_path: Path) -> None:
    """A same-day brief regen with changed narration → new content hash → the
    old cache entry is NOT served (re-render), the new one caches independently."""
    k_old = audio_cache_key(DATE, narration_content_hash("v1 text"), 1.0)
    k_new = audio_cache_key(DATE, narration_content_hash("v2 text"), 1.0)
    write_cached_audio(tmp_path, k_old, b"OLD")
    assert read_cached_audio(tmp_path, k_new) is None  # regen → miss
    write_cached_audio(tmp_path, k_new, b"NEW")
    assert read_cached_audio(tmp_path, k_old) == b"OLD"  # both coexist
    assert read_cached_audio(tmp_path, k_new) == b"NEW"


def test_unset_dir_degrades(tmp_path: Path) -> None:
    key = audio_cache_key(DATE, "abc", 1.0)
    assert read_cached_audio(None, key) is None       # no crash
    assert write_cached_audio(None, key, b"x") is False  # can't cache, no crash


def test_empty_audio_is_a_miss(tmp_path: Path) -> None:
    key = audio_cache_key(DATE, "abc", 1.0)
    write_cached_audio(tmp_path, key, b"")
    assert read_cached_audio(tmp_path, key) is None  # zero-byte → treated as miss


def test_key_is_path_safe() -> None:
    """A malformed date can't escape the cache dir (allowlist sanitize)."""
    key = audio_cache_key("../../etc/passwd", "ab/cd", 1.0)
    assert "/" not in key and ".." not in key.replace(".mp3", "")
