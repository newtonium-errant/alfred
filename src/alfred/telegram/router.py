"""Opening-cue router — classify the first message of a new session.

When the bot gets a message and there's no active session, it calls
:func:`classify_opening_cue` with the message text and a compact summary of
recent closed sessions. The router runs one Sonnet API call with a pinned
instruction prompt and returns a :class:`RouterDecision`: session type,
model to use, and (optionally) the record path of a previous session to
continue.

Design notes:

- Pinned constant router model (:data:`alfred.telegram.session_types.ROUTER_MODEL`)
  per plan open question #7. Promote to config in wk3 if we need to swap
  without a code change.
- Prompt is inline here (plan open question #6) so the router is one file.
- JSON-only output — the model is instructed to emit nothing else. We
  parse with ``json.loads`` and fall back to ``note`` / default model /
  no continuation on any parse or network error. Graceful degradation is
  the whole point: a router failure should feel like wk1, not a crash.
- Article-continuation with no prior match stays on ``article`` / Opus
  (plan open question #8) — intent trumps absence of a prior. The
  continuation link is simply ``None``.
- Router returns no extra context, only a decision. The caller owns
  everything else (opening the session, pre-seeding transcript, logging).

The router prompt is deliberately short — the model doesn't need to know
anything about Alfred beyond "which of these five buckets does this
opening message fall into?".
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .session_types import (
    ROUTER_MODEL,
    SessionTypeDefaults,
    defaults_for,
    known_types,
)
from .utils import get_logger

log = get_logger(__name__)


# --- Data types -----------------------------------------------------------


@dataclass(frozen=True)
class RouterDecision:
    """The router's classification of one opening message.

    Attributes:
        session_type: Canonical type name (``note|task|journal|article|brainstorm``).
        model: Anthropic model id to start the session on. Usually the
            type's default, but the router may override (e.g. "quick article
            note" → Sonnet even for article type).
        continues_from: Record path of a previous session to resume, or
            ``None``. Populated when the opening cue implies continuation
            AND a matching record was found in recent state.
        reasoning: One-line rationale the model emitted — purely for logs.
            Safe to empty.
    """

    session_type: str
    model: str
    continues_from: str | None
    reasoning: str = ""


# --- Prompt ---------------------------------------------------------------

# Kept short and explicit. The "only JSON" line is load-bearing — without it
# the model occasionally wraps the object in prose, which we'd then have to
# regex-extract. Failing closed to ``note`` is fine for one-off bad JSON,
# but regular JSON noise would mean the router never routes anything.
_ROUTER_PROMPT = """\
You classify the opening message of a Telegram voice/text session with \
Alfred (a personal assistant). Pick ONE session type and (optionally) \
flag continuation of a prior session.

Types:
- note: quick capture, one-off reminders, short observations.
- task: "create a task", "remind me to", "add a task". Assistant will \
act, not converse.
- journal: introspective / reflective ("I want to think through X", \
"how am I feeling about Y").
- article: long-form drafting or editing ("let's continue the article", \
"work on the draft").
- brainstorm: divergent idea generation ("brainstorm names for X", \
"ideas for Y").
- capture: silent brainstorm-capture ("let me brainstorm", "thinking \
out loud", "I want to ramble", "just let me talk for a while"). The \
user wants to dump thoughts without interruption; the assistant stays \
silent mid-session and a structured summary is produced at /end.

Continuation: if the user says "continue the last journal", "pick up the \
article we were writing", "same brainstorm as yesterday", etc., AND the \
recent sessions list below contains a matching session, set \
"continues_from" to that session's record_path. Otherwise null.

Recent sessions (most recent first):
{recent_summary}

Opening message:
{opening}

Respond with ONLY a JSON object, no prose, no markdown fences:
{{"session_type": "<one of: note, task, journal, article, brainstorm, capture>",
  "continues_from": "<record_path or null>",
  "reasoning": "<one short sentence>"}}
"""


# --- Deterministic prefix detection ---------------------------------------

# The ``capture:`` prefix forces capture-session dispatch without an LLM
# call. Matched case-insensitively against the leading token of the
# opening message. The prefix is load-bearing: if the user explicitly
# signals "this is a capture session" we should never round-trip to a
# classifier and risk a mis-route on a borderline phrasing.
_CAPTURE_PREFIX: str = "capture:"


def _detect_capture_prefix(message: str) -> bool:
    """Return True iff ``message`` starts with a ``capture:`` prefix.

    Case-insensitive, leading-whitespace-tolerant. Deliberately narrow —
    we check only the leading literal ``capture:`` token, not "let's
    capture" or "capturing thoughts now" — those borderline cases are
    the LLM classifier's job.
    """
    if not message:
        return False
    return message.lstrip().lower().startswith(_CAPTURE_PREFIX)


# --- Helpers --------------------------------------------------------------


def _format_recent_summary(recent: list[dict[str, Any]]) -> str:
    """Render the recent-sessions list as a compact multi-line summary.

    Shape per entry (from ``state.closed_sessions``):
        {"record_path": str, "session_type": str|None,
         "started_at": str, "ended_at": str, ...}

    We only expose the three fields the router needs; anything else would
    just waste tokens. Limits to 10 lines to keep the prompt bounded.
    """
    if not recent:
        return "(none — first session)"
    lines: list[str] = []
    for entry in recent[:10]:
        path = entry.get("record_path", "")
        stype = entry.get("session_type") or "note"
        started = entry.get("started_at", "")
        lines.append(f"- {stype} @ {started[:10]} → {path}")
    return "\n".join(lines)


def _extract_text(response: Any) -> str:
    """Pull concatenated text from an Anthropic response's content list."""
    content = getattr(response, "content", None) or []
    parts: list[str] = []
    for block in content:
        if getattr(block, "type", None) == "text":
            text = getattr(block, "text", "")
            if text:
                parts.append(text)
    return "\n".join(parts).strip()


def _parse_decision(raw: str) -> dict[str, Any] | None:
    """Best-effort parse of the router's JSON-only response.

    Returns ``None`` on any parse failure — the caller then falls back to
    the default decision. We deliberately don't regex-extract a JSON
    substring: if the model ignored the "only JSON" instruction, we want
    the fallback to fire so the bug is visible in logs.
    """
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def _fallback_decision(reason: str) -> RouterDecision:
    """Return the safe-default decision used whenever the router errors."""
    defaults = defaults_for("note")
    return RouterDecision(
        session_type=defaults.session_type,
        model=defaults.model,
        continues_from=None,
        reasoning=reason,
    )


def _decision_from_parsed(
    parsed: dict[str, Any],
    recent: list[dict[str, Any]],
) -> RouterDecision:
    """Build a :class:`RouterDecision` from a parsed JSON dict.

    Applies defaults from :mod:`session_types`, validates the session type
    (unknown → ``note``), and validates ``continues_from`` against the
    recent-sessions list (if the model hallucinated a record path, we
    refuse it rather than feed a phantom into the opener).
    """
    session_type = parsed.get("session_type") or "note"
    if session_type not in known_types():
        log.info(
            "talker.router.unknown_type_coerced_to_note",
            session_type=session_type,
        )
        session_type = "note"

    defaults: SessionTypeDefaults = defaults_for(session_type)
    model = defaults.model

    # Continuation handling. Only trust ``continues_from`` if (a) the type
    # supports continuation and (b) the record path appears in our recent
    # state. This defends against model hallucination — the router can
    # invent a plausible-looking path, but state is the source of truth.
    raw_cont = parsed.get("continues_from")
    continues_from: str | None = None
    if (
        defaults.supports_continuation
        and isinstance(raw_cont, str)
        and raw_cont
        and raw_cont != "null"
    ):
        known_paths = {e.get("record_path") for e in recent}
        if raw_cont in known_paths:
            continues_from = raw_cont
        else:
            log.info(
                "talker.router.unknown_continuation_dropped",
                raw=raw_cont[:80],
            )

    reasoning = parsed.get("reasoning") or ""
    if not isinstance(reasoning, str):
        reasoning = str(reasoning)

    return RouterDecision(
        session_type=session_type,
        model=model,
        continues_from=continues_from,
        reasoning=reasoning[:200],  # trim for log friendliness
    )


# --- Public entry point ---------------------------------------------------


async def classify_opening_cue(
    client: Any,
    first_message: str,
    recent_sessions: list[dict[str, Any]],
) -> RouterDecision:
    """Classify one opening message; return a :class:`RouterDecision`.

    Args:
        client: An Anthropic ``AsyncAnthropic`` client (or any object with
            a ``messages.create`` async method).
        first_message: The text of the user's opening message.
        recent_sessions: Most-recent-first list of closed-session summaries
            from ``state.closed_sessions``.

    Returns:
        A :class:`RouterDecision`. Any error (network, bad JSON, unknown
        type, hallucinated continuation) degrades to a ``note`` / Sonnet
        / no-continuation decision. That keeps the user-visible behaviour
        identical to wk1 whenever the router is unreliable.
    """
    if not first_message:
        return _fallback_decision("empty_message")

    # Deterministic capture-prefix short-circuit. Runs BEFORE the LLM
    # call: an explicit ``capture:`` prefix is a user-asserted
    # classification and we must never round-trip it to the classifier.
    # Continuation is disabled for capture (``supports_continuation=False``
    # on the session-type defaults), so ``continues_from`` is always None
    # for this branch.
    if _detect_capture_prefix(first_message):
        capture_defaults = defaults_for("capture")
        log.info(
            "talker.router.capture_prefix",
            session_type="capture",
            model=capture_defaults.model,
        )
        return RouterDecision(
            session_type=capture_defaults.session_type,
            model=capture_defaults.model,
            continues_from=None,
            reasoning="capture: prefix (deterministic)",
        )

    prompt = _ROUTER_PROMPT.format(
        recent_summary=_format_recent_summary(recent_sessions),
        opening=first_message.strip(),
    )

    try:
        response = await client.messages.create(
            model=ROUTER_MODEL,
            max_tokens=256,
            # Low temperature — classification, not creative writing. A
            # non-zero value lets the model escape obvious local-minima
            # ("always classify as note") without the noise of ``1.0``.
            temperature=0.2,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:  # noqa: BLE001 — network / SDK failures mustn't crash the bot
        log.warning("talker.router.api_error", error=str(exc))
        return _fallback_decision("api_error")

    raw = _extract_text(response)
    parsed = _parse_decision(raw)
    if parsed is None:
        log.warning("talker.router.parse_failed", raw_head=raw[:200])
        return _fallback_decision("parse_failed")

    decision = _decision_from_parsed(parsed, recent_sessions)
    log.info(
        "talker.router.decided",
        session_type=decision.session_type,
        model=decision.model,
        continues=decision.continues_from is not None,
    )
    return decision
