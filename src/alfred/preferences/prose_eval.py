"""Prose-LLM fallback for Shape A preferences — STUB (V2).

When a Shape A preference is shipped without a structured matcher
(matcher == None) OR when the structured matcher's args don't
capture the operator's intent, a prose-LLM fallback would let the
record's ``## Policy`` body drive the gate decision directly:
ask the model "given this candidate and this policy, should the
candidate be skipped?"

V1 ship: stub only. The decision was deferred per
``project_operator_preferences_v1.md`` Hard Contract #10 — implement
when the first miss surfaces in operator workflow. Until then, any
consumer path that reaches this fallback raises ``NotImplementedError``
loudly so the gap shows up in logs rather than silently dropping the
gate decision.

When V2 lands:
- Wire to the same OpenRouter/Anthropic backend the talker uses for
  voice-eval-style judgement calls (probably claude-haiku for cost).
- Cache by preference SHA + candidate hash so repeat hits don't
  re-call the API per-candidate.
- Surface the LLM's reason string in the consumer's log so operators
  can audit decisions ("preferences.prose_skip" + reason).
"""
from __future__ import annotations

from typing import Any


def evaluate_prose(
    *,
    preference_body: str,
    candidate: dict[str, Any],
) -> "MatcherResult":
    """Stub: prose-LLM fallback for Shape A preferences (V2).

    Raises ``NotImplementedError`` per
    ``project_operator_preferences_v1.md`` Hard Contract #10. The
    intentional crash surfaces the fact that a consumer reached the
    fallback path — every caller should be using structured matchers
    in V1; the prose-LLM path is reserved for a deferred V2 ship
    triggered by operator-flagged friction (a preference that
    structured matchers can't express).

    See ``project_next_session.md`` for the V2 trigger criteria.
    """
    raise NotImplementedError(
        "prose-LLM fallback for Shape A preferences deferred to V2 — "
        "see project_operator_preferences_v1.md Hard Contract #10 and "
        "project_next_session.md for the trigger criteria. V1 ship: "
        "structured matchers only (see matchers.KNOWN_RULES)."
    )
