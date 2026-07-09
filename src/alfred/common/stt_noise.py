"""Deterministic STT hallucination denylist — a clinical-safety control.

Cloud STT (Whisper, Deepgram) emit YouTube-caption-style hallucinations on
silence / near-silence / non-speech audio: "Thank you for watching!", "Please
subscribe", "Thanks for watching everyone." In a CLINICAL capture these are NOT
cosmetic — a hallucinated line that drives a live turn or lands in a structured
action-item list ("send a prescription") is a SAFETY event, not a typo. This
module drops such LINES deterministically (no LLM) at the STT seams BEFORE the
text can drive a live turn (``voice_stt`` / the Telegram voice seam) and again,
defensively, before structuring (``capture_batch``), and reports what it dropped
(``discarded_noise``) as a transparency + self-correcting-glossary surface.

Matching is LINE-LEVEL and EXACT after normalization (NEVER substring): a real
"thank you" inside a clinical sentence SURVIVES — only a line that IS a caption
artifact is dropped. The frozen default set below is ALWAYS active (caption
artifacts are universal); ``talker.stt.hallucination_denylist`` supplies
per-instance EXTRAS that UNION onto it (never replace — so a per-instance list
can't silently disable the universal defaults). Both the Telegram and web seams
read the same list.

Pure — no LLM, no I/O, no config import (config threads the list in).
"""

from __future__ import annotations

# Edge chars stripped when normalizing a line for match. Internal
# apostrophes/hyphens are KEPT ("don't" stays "don't", "twenty-one" intact) so a
# real contraction is never mangled — only leading/trailing punctuation goes.
_EDGE = " \t\r\n.,!?;:\"'“”‘’…-—–"

# FROZEN default caption-artifact set (normalized forms). Conservative by design:
# every entry is an unambiguous non-speech caption hallucination a clinician
# would never utter as a STANDALONE line. Bare "thank you" is DELIBERATELY
# excluded (a real closing) — only the "...for watching / subscribe" caption
# forms. Extend per-instance via config, never by loosening to substrings.
_DEFAULT_STT_HALLUCINATION_DENYLIST: frozenset[str] = frozenset({
    "thank you for watching",
    "thanks for watching",
    "thank you for watching this video",
    "thanks for watching everyone",
    "thank you so much for watching",
    "please subscribe",
    "don't forget to subscribe",
    "like and subscribe",
    "please like and subscribe",
    "subscribe to my channel",
    "see you in the next video",
    "see you in the next one",
    "see you next time",
    "thanks for listening",
    "thank you for listening",
    "i'll see you in the next video",
})


def _normalize_line(line: str) -> str:
    """Lowercase, collapse internal whitespace, strip edge punctuation.

    ``"Thank you for watching!"`` → ``"thank you for watching"``;
    ``"  Please   subscribe.  "`` → ``"please subscribe"``.
    """
    s = " ".join((line or "").lower().split())   # collapse ws + trim ends
    s = s.strip(_EDGE)                            # strip edge punctuation
    return " ".join(s.split())                    # re-collapse if strip exposed ws


def normalized_denylist(extra: "list[str] | None" = None) -> set[str]:
    """Normalized match set = the frozen default UNION any per-instance ``extra``.

    The default is always present; ``extra`` (the per-instance config list) only
    ADDS terms — it can never disable a universal caption artifact."""
    out: set[str] = set()
    for entry in _DEFAULT_STT_HALLUCINATION_DENYLIST:
        n = _normalize_line(entry)
        if n:
            out.add(n)
    for entry in (extra or []):
        n = _normalize_line(entry)
        if n:
            out.add(n)
    return out


def filter_stt_noise(
    text: str, extra: "list[str] | None" = None,
) -> "tuple[str, list[str]]":
    """Drop caption-artifact LINES from ``text``. Returns ``(kept, dropped)``.

    LINE-LEVEL, EXACT match after normalization — a line is dropped only when its
    normalized form EXACTLY equals a denylist entry (the frozen default UNION the
    per-instance ``extra``). This is the load-bearing safety property: a real
    "thank you" (or any denylist phrase) embedded inside a longer clinical
    sentence is a DIFFERENT normalized line, so it SURVIVES (no substring-nuke).
    ``dropped`` holds the ORIGINAL (unnormalized) dropped lines in order — the
    ``discarded_noise`` transparency surface.

    When nothing matches, returns the input ``text`` unchanged (identity) — the
    clean path is byte-identical. When every line is dropped, ``kept`` is ``""``
    (a fully-hallucinated utterance → empty → no live turn fires)."""
    if not text:
        return text, []
    norm_deny = normalized_denylist(extra)
    kept: list[str] = []
    dropped: list[str] = []
    for line in text.split("\n"):
        if _normalize_line(line) in norm_deny:
            dropped.append(line)
        else:
            kept.append(line)
    if not dropped:
        return text, []                 # identity — no allocation of a new string
    return "\n".join(kept).strip(), dropped


__all__ = [
    "filter_stt_noise",
    "normalized_denylist",
    "_DEFAULT_STT_HALLUCINATION_DENYLIST",
]
