"""Brief-audio cache — the credit-guard for the interruptible player (C3a).

ElevenLabs synthesis costs credits; replay and scrubbing must cost ZERO. Audio
is rendered on-demand the first time the player opens a given brief, then cached
on disk keyed by ``(brief_date, narration_content_hash, speed)``:

  * ``brief_date`` — the day.
  * ``narration_content_hash`` — a hash of the narration's spoken text, so a
    same-day brief REGEN with changed content re-renders (new hash → cache miss)
    while an unchanged brief always hits (zero synth). Content-keyed, not just
    date-keyed, so the cache never serves stale audio for a regenerated brief.
  * ``speed`` — the operator's /speed pref (each speed is a distinct render).

A cache hit returns bytes and the route serves them WITHOUT calling ElevenLabs —
the mutation-pinned credit guard. The store is a flat dir of ``.mp3`` files under
``<data_dir>/brief_audio/``; filenames are built from an allowlisted key (never
interpolating raw untrusted input into a path).
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

from .utils import get_logger

log = get_logger(__name__)

_CACHE_SUBDIR = "brief_audio"
# Speed is a float; render it as a stable 2-decimal token for the key.
_SPEED_FMT = "{:.2f}"
# Defensive: a key SEGMENT must be filename-safe (allowlist) — brief_date is an
# ISO date and the hash is hex, but we sanitize so a malformed date can never
# escape the cache dir. Dots are stripped too (no segment legitimately needs one
# — the speed token's dot becomes ``_``, e.g. 1.00 → 1_00), so a ``..`` can
# never survive into a filename; the literal ``.mp3`` suffix is appended OUTSIDE
# the sanitizer in :func:`audio_cache_key`.
_SAFE = re.compile(r"[^A-Za-z0-9_-]")


def narration_content_hash(text: str) -> str:
    """Stable short hash of the narration's spoken text — the content key. A
    changed brief (regen) yields a different hash → cache miss → re-render."""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


def _safe(seg: str) -> str:
    return _SAFE.sub("_", seg or "")


def audio_cache_key(brief_date: str, content_hash: str, speed: float | None) -> str:
    """The cache filename (no dir) for a render. Speed ``None`` → provider
    default, keyed as ``default``."""
    speed_tok = _SPEED_FMT.format(speed) if speed is not None else "default"
    return f"{_safe(brief_date)}__{_safe(content_hash)}__{_safe(speed_tok)}.mp3"


def _cache_path(cache_dir: str | os.PathLike | None, key: str) -> Path | None:
    if not cache_dir:
        return None
    return Path(cache_dir) / _CACHE_SUBDIR / key


def read_cached_audio(cache_dir: str | os.PathLike | None, key: str) -> bytes | None:
    """Return cached mp3 bytes for ``key``, or ``None`` on a miss / unset dir /
    read error. A HIT is the credit guard — the caller MUST NOT synth."""
    path = _cache_path(cache_dir, key)
    if path is None or not path.is_file():
        return None
    try:
        data = path.read_bytes()
    except OSError as exc:
        log.warning("web.brief_audio.cache_read_failed", key=key, error=str(exc))
        return None
    if not data:
        return None
    log.info("web.brief_audio.cache_hit", key=key, bytes=len(data))
    return data


def write_cached_audio(
    cache_dir: str | os.PathLike | None, key: str, audio: bytes,
) -> bool:
    """Persist ``audio`` under ``key`` (atomic ``.tmp`` → ``os.replace``).
    Returns True on write, False when no cache_dir is threaded (the route still
    streams the freshly-synthesized bytes; it just isn't cached). Best-effort —
    a write failure never fails the request (logged)."""
    path = _cache_path(cache_dir, key)
    if path is None:
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(audio)
        os.replace(tmp, path)
    except OSError as exc:
        log.warning("web.brief_audio.cache_write_failed", key=key, error=str(exc))
        return False
    log.info("web.brief_audio.cache_write", key=key, bytes=len(audio))
    return True


__all__ = [
    "audio_cache_key",
    "narration_content_hash",
    "read_cached_audio",
    "write_cached_audio",
]
