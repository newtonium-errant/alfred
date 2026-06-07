"""Action-shape preference matcher dispatch — V1 enum (4 rules).

Each Shape A preference carries a ``matcher`` dict with shape:

    matcher:
      domain: <consumer name>
      rule: <rule name from KNOWN_RULES>
      args: <rule-specific args dict>

Consumer modules (curator stage 1, curator daemon inbox-stage, brief
upcoming_events) build a ``candidate`` dict from the record being
considered (e.g. event frontmatter or inbox sender metadata), then
call ``evaluate(rule, args, candidate)``. The function returns a
``MatcherResult`` carrying ``skip`` + ``reason`` so the consumer can
log the decision with a grep-able motivation rather than a silent
drop.

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
- ``skip_inbox_if_sender_matches`` — curator inbox-stage dispatch
  (P10 / Ship 3 — 2026-06-07). Args: ``sender_patterns`` (required;
  list of glob-style patterns matched via :func:`fnmatch.fnmatchcase`
  on the lowercased sender email vs. lowercased pattern). Candidate
  key read: ``sender``. Caller pattern: curator daemon's
  ``_process_file`` BEFORE any backend prompt is built — gates the
  whole inbox file, not individual extracted entities. First-match
  wins on the pattern list; first-skip wins on the preference list at
  the caller layer. Operator motivation: Salem inbox is ~99%
  empty-body promotional with ~29% Substack-platform-routed; dropping
  those at the inbox stage avoids LLM cost + manifest churn entirely.

Prose-LLM fallback is a STUB in ``prose_eval.py``. It raises
``NotImplementedError`` so any path that reaches it surfaces loudly
rather than silently dropping the gate decision.
"""
from __future__ import annotations

import fnmatch
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
    # P10 / Ship 3 — inbox-stage curator dispatch. See module docstring
    # + ``_evaluate_sender_glob`` for rationale.
    "skip_inbox_if_sender_matches",
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


def _candidate_sender(candidate: dict[str, Any]) -> str:
    """Read the candidate's sender email.

    Returns ``""`` for missing / None / empty-after-coerce sender so
    the caller (the sender-glob evaluator) can take the "no sender"
    branch and decline to gate. The caller-side curator daemon has
    already done sender extraction via :func:`extract_sender_email`;
    a missing sender at THIS layer means the inbox file had no
    ``**From:**`` line (e.g. a non-email file dropped into ``inbox/``).
    """
    value = candidate.get("sender")
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _evaluate_sender_glob(
    *,
    rule: str,
    args: dict[str, Any],
    candidate: dict[str, Any],
) -> MatcherResult:
    """Sender-glob implementation for ``skip_inbox_if_sender_matches``.

    Matches the candidate's ``sender`` field against each entry in
    ``args.sender_patterns`` via :func:`fnmatch.fnmatchcase`,
    lowercased on both sides. First match wins. Returns a
    ``MatcherResult`` with the matching pattern in the reason so the
    operator can grep "which pattern dropped this sender."

    Defensive failures (all fail-open with a reason — never crash the
    consumer):
        * Missing or non-list ``sender_patterns`` arg
        * Empty ``sender_patterns`` list
        * Empty / missing sender on the candidate

    Glob over regex: per project_empty_body_email_arc.md and
    operator decision 2026-06-07, sender filtering is intent-aligned
    with a domain-shaped pattern language (``*@substack.com`` /
    ``*@*.substack.com``); regex would be overkill and harder for the
    operator to author via the talker. The ``fnmatchcase`` (vs.
    ``fnmatch``) variant defeats platform-dependent case-folding on
    Windows; we lowercase both sides ourselves so the matching stays
    case-insensitive across all platforms.
    """
    patterns = args.get("sender_patterns")
    if not isinstance(patterns, list):
        return MatcherResult(
            skip=False,
            reason=(
                f"{rule}: missing or non-list 'sender_patterns' arg "
                f"(got {type(patterns).__name__!r}) — rule does not fire"
            ),
        )
    if not patterns:
        return MatcherResult(
            skip=False,
            reason=f"{rule}: empty sender_patterns list — rule does not fire",
        )
    sender = _candidate_sender(candidate)
    if not sender:
        return MatcherResult(
            skip=False,
            reason=f"{rule}: candidate has no sender — rule does not fire",
        )

    sender_lower = sender.lower()
    for pattern in patterns:
        # Defensive: non-string patterns silently skipped. An operator
        # who authored a yaml list with a stray int / dict entry
        # shouldn't break gate dispatch for the rest of the list.
        if not isinstance(pattern, str) or not pattern:
            continue
        if fnmatch.fnmatchcase(sender_lower, pattern.lower()):
            return MatcherResult(
                skip=True,
                reason=(
                    f"{rule}: sender {sender!r} matches pattern {pattern!r}"
                ),
            )

    return MatcherResult(
        skip=False,
        reason=(
            f"{rule}: sender {sender!r} does not match any of "
            f"{len(patterns)} pattern(s)"
        ),
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

    if rule == "skip_inbox_if_sender_matches":
        return _evaluate_sender_glob(
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
