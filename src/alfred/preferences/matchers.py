"""Action-shape preference matcher dispatch — V1 enum (3 rules).

Each Shape A preference carries a ``matcher`` dict with shape:

    matcher:
      domain: <consumer name>
      rule: <rule name from KNOWN_RULES>
      args: <rule-specific args dict>

Consumer modules (curator stage 1, brief upcoming_events) build a
``candidate`` dict from the record being considered (e.g. event
frontmatter or task frontmatter), then call ``evaluate(rule, args,
candidate)``. The function returns a ``MatcherResult`` carrying
``skip`` + ``reason`` so the consumer can log the decision with a
grep-able motivation rather than a silent drop.

V1 rules:
- ``skip_event_if`` — curator stage 1 dispatch. Args: ``title_regex``
  (required, case-insensitive). Candidate keys read: ``name`` /
  ``title`` (first non-empty). Optional ``source_pattern`` reserved
  for V2 (deferred — not implemented; arg passes through but is
  ignored). Caller pattern: curator's event-extract candidate set.
- ``skip_brief_event_if`` — brief upcoming_events dispatch. Args:
  ``title_regex`` (required, case-insensitive). Candidate keys read:
  ``name`` / ``title``. Caller pattern: brief's upcoming-events
  iterator.
- ``skip_brief_task_if`` — same as skip_brief_event_if but applied
  to task records. Kept as a separate rule rather than a polymorphic
  one because the operator's "filter this from my brief" intent is
  type-specific (operator-flagged friction is per-type, not
  per-content).

Prose-LLM fallback is a STUB in ``prose_eval.py``. It raises
``NotImplementedError`` so any path that reaches it surfaces loudly
rather than silently dropping the gate decision.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class MatcherResult:
    """Result of evaluating one matcher against one candidate.

    ``skip`` — True if the candidate matches the rule (consumer
    should drop it). False otherwise (consumer keeps it).
    ``reason`` — operator-grep-able motivation string. When ``skip``
    is True, points at the matching rule + the matched substring
    when applicable; when False, names the reason the rule didn't
    fire (no match, missing field, etc.).
    """

    skip: bool
    reason: str


# Public registry — exposed via ``preferences/__init__.py`` so test
# fixtures and downstream readers (e.g. the future ``alfred prefs
# inspect`` CLI) can enumerate the supported rule names without
# importing private state.
KNOWN_RULES: frozenset[str] = frozenset({
    "skip_event_if",
    "skip_brief_event_if",
    "skip_brief_task_if",
})


def _candidate_title(candidate: dict[str, Any]) -> str:
    """Read the candidate's display title.

    Falls through ``name`` → ``title`` → ``""``. Non-string values
    are coerced via ``str(...)`` defensively (frontmatter parsing can
    produce datetime/list values on malformed records — we don't
    crash, just return an empty title so the regex always evaluates).
    """
    for key in ("name", "title"):
        value = candidate.get(key)
        if value is None:
            continue
        if isinstance(value, str):
            if value.strip():
                return value
        else:
            coerced = str(value)
            if coerced.strip():
                return coerced
    return ""


def _evaluate_title_regex(
    *,
    rule: str,
    args: dict[str, Any],
    candidate: dict[str, Any],
) -> MatcherResult:
    """Shared title-regex implementation for the three V1 rules.

    All three V1 rules dispatch by case-insensitive regex against the
    candidate's title. Differences are domain-only (which consumer
    calls which rule); the matching logic is identical. Centralised
    here so adding a fourth title-regex rule for V1.5 doesn't fork
    the regex compilation path.

    Defensive: missing ``title_regex`` arg returns skip=False with a
    reason naming the missing arg. An unparseable regex returns the
    same — we never crash the consumer, we just decline to gate.
    """
    pattern = args.get("title_regex")
    if not isinstance(pattern, str) or not pattern:
        return MatcherResult(
            skip=False,
            reason=f"{rule}: missing required arg 'title_regex'",
        )
    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        return MatcherResult(
            skip=False,
            reason=f"{rule}: invalid regex {pattern!r} ({exc})",
        )
    title = _candidate_title(candidate)
    if not title:
        return MatcherResult(
            skip=False,
            reason=f"{rule}: candidate has no title — rule does not fire",
        )
    if compiled.search(title):
        return MatcherResult(
            skip=True,
            reason=f"{rule}: title {title!r} matches {pattern!r}",
        )
    return MatcherResult(
        skip=False,
        reason=f"{rule}: title {title!r} does not match {pattern!r}",
    )


def evaluate(
    rule: str,
    args: dict[str, Any] | None,
    candidate: dict[str, Any],
) -> MatcherResult:
    """Evaluate a Shape A preference matcher against one candidate.

    Args:
        rule: rule name (must be in ``KNOWN_RULES``).
        args: rule-specific args dict (from the preference's
            ``matcher.args``). None is tolerated and treated as an
            empty dict (every concrete rule then surfaces its own
            "missing required arg" reason).
        candidate: dict representation of the record being gated.
            Consumer-specific shape — curator passes the event
            manifest entry; brief passes the parsed frontmatter.

    Returns:
        ``MatcherResult`` with ``skip`` + ``reason``. ``skip=True``
        means the consumer should drop this candidate; ``skip=False``
        means keep.

    Unknown rule names return ``skip=False`` with a reason — the
    fail-open default prevents an unrecognised rule from silently
    skipping every candidate. Caller logs the reason; this surfaces
    drift (e.g. a V2 rule landing on a V1 reader) without breaking
    the consumer.
    """
    if rule not in KNOWN_RULES:
        return MatcherResult(
            skip=False,
            reason=f"unknown rule {rule!r} — fail-open, candidate not skipped",
        )
    safe_args = args if isinstance(args, dict) else {}

    if rule in ("skip_event_if", "skip_brief_event_if", "skip_brief_task_if"):
        return _evaluate_title_regex(
            rule=rule, args=safe_args, candidate=candidate,
        )

    # Unreachable — KNOWN_RULES gate above is exhaustive for V1.
    # Kept for future-proofing: a new rule added to KNOWN_RULES
    # without a dispatch branch here falls through to fail-open
    # rather than crashing the consumer.
    return MatcherResult(
        skip=False,
        reason=f"rule {rule!r} known but no dispatch branch — fail-open",
    )
