"""Shared classification + summary for ``claude -p`` agent failures.

ONE implementation, consumed by all three CLI backends (curator, janitor,
distiller) so a failed structuring call produces the SAME diagnostic summary
and the SAME closed-set failure ``kind`` everywhere — mirror-write invariant,
no per-tool copies.

Why this exists (2026-07-29 incident): the box's Claude account hit its
WEEKLY usage limit. Every ``claude -p`` structuring call failed ``exit 1``
from Jul 27 onward (234 emails quarantined). Diagnosis needed manual SSH
forensics because of two gaps this module closes:

  1. **Empty failure summaries.** The backends built ``summary=f"Exit code
     {code}: {stderr[:500]}"`` — stderr ONLY. ``claude -p`` prints the quota
     message ("You've hit your weekly limit · resets 4am (UTC)") on
     **stdout**, so three days of ``daemon.agent_failed`` events logged
     ``summary='Exit code 1: '`` — content-free. :func:`build_failure_summary`
     folds tails of BOTH streams into a bounded, single-line, ANSI-stripped
     summary that is never empty.

  2. **Quota is invisible to the login probe.** ``claude auth status`` stays
     green while quota-limited (logged in, just out of budget). So the backend
     tags the already-failed result with a ``kind`` (:func:`classify_agent_failure`)
     derived from the REAL failing traffic — zero marginal tokens, since
     classification only ever runs on a call that already ran and failed —
     and the curator BIT surfaces ``kind == quota_limited`` as a WARN.

The ``kind`` is a small CLOSED set so downstream (log fields, state, BIT
severity mapping) can switch on it deterministically. When the evidence is
weak, classification returns :data:`OTHER` — never a false-confident
``quota_limited`` / ``auth`` on a message it doesn't recognize.
"""

from __future__ import annotations

import re

# --- Closed set of failure kinds -------------------------------------------
# Callers switch on these; keep the set small and stable. A new kind is a
# deliberate contract change (add here + update the BIT severity mapping in
# curator/health.py + the tests that pin the closed set).
QUOTA_LIMITED = "quota_limited"
AUTH = "auth"
OTHER = "other"

KNOWN_FAILURE_KINDS: frozenset[str] = frozenset({QUOTA_LIMITED, AUTH, OTHER})


# Bounding for the human summary. Tails of both streams, so the total stays
# grep-friendly in a log line without dumping a whole subprocess transcript.
_STREAM_TAIL_CHARS = 130
_SUMMARY_MAX_CHARS = 300

# CSI + simple two-char escapes. ``claude -p`` colorizes its error banner;
# raw escape bytes in a structured log field are noise, so strip them before
# both summarising AND classifying.
_ANSI_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def _normalize(text: str | None) -> str:
    """ANSI-strip + collapse all whitespace (incl. newlines) to single spaces.

    ``" ".join(s.split())`` both collapses internal runs and trims the ends,
    giving a single-line rendering safe to embed in a structured log field.
    """
    if not text:
        return ""
    return " ".join(_ANSI_RE.sub("", text).split())


def build_failure_summary(
    returncode: int,
    stdout: str | None,
    stderr: str | None,
    *,
    max_len: int = _SUMMARY_MAX_CHARS,
    stream_tail: int = _STREAM_TAIL_CHARS,
) -> str:
    """Bounded, single-line, never-empty summary of a failed subprocess.

    Folds the tails of BOTH ``stdout`` and ``stderr`` (labelled) into one
    line. Falls back to ``"(no output)"`` when both streams are empty — the
    "no diagnostic output at all" signature stays explicit rather than
    trailing with a bare colon. Result is capped at ``max_len`` chars.
    """
    out = _normalize(stdout)
    err = _normalize(stderr)
    parts: list[str] = []
    if out:
        parts.append(f"stdout: {out[-stream_tail:]}")
    if err:
        parts.append(f"stderr: {err[-stream_tail:]}")
    body = " | ".join(parts) if parts else "(no output)"
    return f"Exit code {returncode}: {body}"[:max_len]


def _looks_quota(text: str) -> bool:
    """True if ``text`` (already normalized + lowercased) reads as quota/rate.

    Deliberately redundant: the 2026-07 message matches several ways so a
    minor wording drift ("weekly" → "monthly", punctuation change) still
    classifies. The compound branch guards the weaker single tokens —
    ``resets`` / ``upgrade`` only count toward quota when ``limit`` is also
    present, so an unrelated "resets the cache" line can't false-fire.
    """
    for token in (
        "usage limit",
        "rate limit",
        "rate-limit",
        "rate limited",
        "weekly limit",
        "monthly limit",
        "daily limit",
        "quota",
        "too many requests",
    ):
        if token in text:
            return True
    if "limit" in text and any(
        k in text
        for k in ("hit your", "hit the", "reached your", "reached the", "resets", "reset at", "reset in", "upgrade")
    ):
        return True
    return False


def _looks_auth(text: str) -> bool:
    """True if ``text`` (already normalized + lowercased) reads as a login/auth failure."""
    for token in (
        "not logged in",
        "please run /login",
        "run /login",
        "/login",
        "please log in",
        "log in to",
        "login required",
        "invalid api key",
        "authentication_error",
        "not authenticated",
        "unauthorized",
    ):
        if token in text:
            return True
    return False


def classify_agent_failure(stdout: str | None, stderr: str | None) -> str:
    """Classify a failed ``claude -p`` call into the closed :data:`KNOWN_FAILURE_KINDS` set.

    Scans the FULL text of both streams (not just the summary tail), so a
    front-loaded quota banner in a long transcript still classifies even
    when the bounded summary would clip it. Case-insensitive. Quota is
    checked before auth — a quota banner that also mentions ``/login``
    ("upgrade at …") is a quota event, not an auth one. Unrecognized →
    :data:`OTHER` (never a false-confident specific kind on weak evidence).
    """
    blob = _normalize(f"{stdout or ''} {stderr or ''}").lower()
    if not blob:
        return OTHER
    if _looks_quota(blob):
        return QUOTA_LIMITED
    if _looks_auth(blob):
        return AUTH
    return OTHER
