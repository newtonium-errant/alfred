"""Shared vocabulary, arithmetic and BIT probe for ``claude -p`` agent failures.

ONE implementation, consumed by all three CLI-backed tools (curator, janitor,
distiller), across the whole path a failure travels:

  produce   ``classify_agent_failure`` + ``build_failure_summary`` — called by
            each tool's ``backends/cli.py`` on a nonzero exit.
  persist   ``next_failure_record`` (+ ``read_streak``) — called by each tool's
            ``state.py`` to stamp the failure and its streak. ``AgentCallOutcomes``
            carries a run's outcomes up to the state owner when the call site
            cannot reach it (distiller).
  surface   ``agent_failure_check`` — the ``agent-failure-kind`` BIT probe, called
            by each tool's ``health.py`` with its own two state fields.

Mirror-write invariant, no per-tool copies. **The persist and surface halves
were curator-only until 2026-08-22.** Until then the three backends all
classified their failures identically and only curator kept the answer: on that
morning the box's weekly quota was exhausted and the BIT reported ``curator
fail`` with a precise, actionable message, ``janitor ok`` (1,029 dropped
classifications) and ``distiller ok`` (246). The producing half being shared
made that gap invisible — every tool looked wired.

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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from alfred.health.types import CheckResult, Status

# --- Closed set of failure kinds -------------------------------------------
# Callers switch on these; keep the set small and stable. A new kind is a
# deliberate contract change (add here + update the BIT severity mapping in
# curator/health.py + the tests that pin the closed set).
QUOTA_LIMITED = "quota_limited"
AUTH = "auth"
OTHER = "other"

KNOWN_FAILURE_KINDS: frozenset[str] = frozenset({QUOTA_LIMITED, AUTH, OTHER})


# --- Sustained-outage threshold --------------------------------------------
# How many CONSECUTIVE agent failures make an OUTAGE rather than a hiccup.
#
# 2026-08-15 incident: the box's Claude account hit its weekly quota at 00:31Z
# and every ``claude -p`` structuring call failed for days. The signal that
# fired was a curator WARN and an FYI-attention health card — present, but
# addressed to nobody. The operator found the outage by noticing an empty deck.
# Operator ruling: SUSTAINED agent-backend failure is an outage with a daily
# cost and belongs at needs-you strength; a single bad run is not.
#
# THREE, because the streak is bounded at both ends by real events rather than
# by a clock: any success resets it, so a run of 3 means three attempts with no
# success in between — past the one-off network blip and the single malformed
# input, and reached within minutes of a real backend outage instead of after a
# day of waiting. Escalating at 1 would ring for every transient; a time window
# would be a second mechanism saying what the reset already says.
#
# HERE rather than in ``curator/health.py`` because it is a CONTRACT between
# two modules: each tool's ``state.record_agent_failure`` logs the crossing and
# the tool's BIT probe maps it to a severity. Two copies would drift into a
# state file that announces an outage the probe still calls a warning. This
# module is already where the shared agent-failure vocabulary lives (the closed
# ``kind`` set above), and its charter is all three CLI-backed tools.
#
# 2026-08-22: janitor and distiller grew the same counter, as anticipated here,
# and inherited this threshold unchanged. The threshold is deliberately NOT
# per-tool — three attempts with no success in between means the same thing to
# each of them, and a per-tool knob would only be a way for the three to
# disagree about when the shared backend went down.
SUSTAINED_FAILURE_STREAK = 3


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


# --- Recovery: has a success superseded this failure? ----------------------
# ONE definition, because this commit created the second consumer. The BIT
# probe has asked it since 2026-07-29 ("is the last failure still active, or
# did the pipeline recover?"); the consecutive-failure counter in
# ``curator.state.record_agent_failure`` now asks the SAME question to decide
# whether a new failure EXTENDS the current streak or starts a fresh one.
# Two spellings of "recovered" would drift into a probe that calls an outage
# active while the counter has already reset it to 1 — the streak and the
# severity would then disagree about the same state file.


def parse_iso_utc(value: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp to an aware UTC datetime, or ``None``.

    Tolerates the trailing-``Z`` form alongside the explicit-offset form, and
    reads a NAIVE value as UTC — curator stamps UTC everywhere, so treating a
    naive value as UTC is the interpretation that keeps a comparison possible
    instead of raising.
    """
    if not value or not isinstance(value, str):
        return None
    try:
        normalized = value.replace("Z", "+00:00") if value.endswith("Z") else value
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def failure_superseded_by_success(
    failure_ts: str | None, last_success_ts: str | None
) -> bool:
    """Did a success at-or-after ``failure_ts`` retire that failure?

    ``True`` means the pipeline recovered and the failure is history; ``False``
    means it is still active (an outage) — or that we cannot PROVE recovery.

    The unprovable case fails toward ACTIVE on purpose, in both consumers and
    for the same reason: an unparseable/absent timestamp must not launder an
    ongoing outage into a green probe, nor silently reset a failure streak that
    is still running. Surfacing a recovered failure one run too long costs a
    line of noise; swallowing a live one costs the multi-day silence this whole
    mechanism exists to break.
    """
    fail_dt = parse_iso_utc(failure_ts)
    success_dt = parse_iso_utc(last_success_ts)
    if fail_dt is None or success_dt is None:
        return False
    return success_dt >= fail_dt


# --- The recorded failure, and its streak ----------------------------------
# ONE implementation of the streak arithmetic, because 2026-08-22 created the
# second and third writers. Until then curator was the only tool that persisted
# an agent failure, so ``record_agent_failure`` could hold the reset-vs-extend
# rule inline; janitor and distiller now record the same thing, and three
# inline copies is how a state file starts announcing a streak of 1 on the
# third day of an outage.


def read_streak(failure: dict[str, Any] | None) -> int:
    """How many CONSECUTIVE agent failures a recorded ``failure`` evidences.

    Reads ``consecutive``, written by :func:`next_failure_record`. A record
    written before the streak amendment carries no such key and evidences
    exactly ONE failure, so it reads as 1 — that keeps an in-flight outage's
    count monotonic across a deploy instead of restarting it at 0 (which would
    silently un-escalate a live one).

    A non-integer / negative value degrades to 1 rather than raising. The
    streak is an observability signal, not a correctness invariant, and a
    hand-edited count must not take a BIT run or a daemon's failure path down.
    Degrading DOWNWARD is the safe direction: it can only delay an escalation
    by one more failure, never manufacture one.

    ``None`` (no failure recorded at all) reads as 0 — a caller distinguishing
    "never failed" from "failed once" needs that, and no writer ever produces
    a stored record with ``consecutive == 0``.
    """
    if not isinstance(failure, dict):
        return 0
    try:
        streak = int(failure.get("consecutive", 1))
    except (TypeError, ValueError):
        return 1
    return streak if streak >= 1 else 1


def is_sustained(streak: int) -> bool:
    """Is ``streak`` long enough to be an OUTAGE rather than a hiccup?

    One spelling of the comparison, so a probe cannot call an outage a warning
    while the state file's own escalation log already called it an outage.
    """
    return streak >= SUSTAINED_FAILURE_STREAK


def next_failure_record(
    *,
    prior: dict[str, Any] | None,
    last_success_ts: str | None,
    kind: str,
    summary: str,
    now_ts: str | None = None,
) -> dict[str, Any]:
    """Build the new ``last_agent_failure`` record for a just-failed call.

    Pure — no I/O, no logging, no clock beyond the ``now_ts`` default — so the
    reset-vs-extend rule is testable directly and identical in all three tools.

    Reset-vs-extend asks the ONE recovery predicate the BIT probe asks
    (:func:`failure_superseded_by_success`): a success recorded at-or-after the
    stored failure broke the streak, so this failure starts a fresh one at 1;
    otherwise it extends. Sharing the predicate is what keeps the counter and
    the probe's severity from disagreeing about the same state file.

    ``since`` is the FIRST failure of the current streak, carried forward while
    it runs — the probe's operator-facing copy says "failing since X", and the
    most-recent ``ts`` would make a three-day outage read as if it had started
    moments ago. An older record with no ``since`` falls back to its own ``ts``,
    the earliest failure it can evidence.
    """
    now = now_ts or datetime.now(timezone.utc).isoformat()

    streak = 1
    since = now
    if isinstance(prior, dict) and not failure_superseded_by_success(
        prior.get("ts"), last_success_ts
    ):
        streak = read_streak(prior) + 1
        prior_since = prior.get("since") or prior.get("ts")
        if isinstance(prior_since, str) and prior_since:
            since = prior_since

    return {
        "ts": now,
        "kind": kind or OTHER,
        "summary_tail": (summary or "")[-300:],
        "consecutive": streak,
        "since": since,
    }


# --- Threading an agent outcome back to the tool that owns state -----------


@dataclass
class AgentCallOutcomes:
    """Ordered log of what a run's agent calls DID, for a caller that is not
    the one holding the state file.

    Curator does not need this: its daemon holds both the ``BackendResult`` and
    the ``StateManager`` in one frame. Distiller does — its agent call lives in
    ``pipeline._call_llm``, four frames below the daemon, behind functions that
    return a manifest string. Writing the failure from down there would be
    clobbered by the daemon's own ``state.save()`` at the end of the run, so the
    outcomes ride UP instead and the state owner applies them.

    ORDERED, and every call recorded, because the streak means "consecutive
    attempts with no success in between" — a run whose 3rd of 12 stage calls
    succeeded broke the streak at that point, and collapsing the run to a single
    verdict would either erase that success or erase the 11 failures.
    """

    #: ``(success, kind, summary)`` per agent call, in the order they happened.
    events: list[tuple[bool, str, str]] = field(default_factory=list)

    def record_success(self) -> None:
        self.events.append((True, "", ""))

    def record_failure(self, kind: str, summary: str) -> None:
        self.events.append((False, kind or OTHER, summary or ""))

    @property
    def failures(self) -> int:
        return sum(1 for ok, _, _ in self.events if not ok)

    @property
    def successes(self) -> int:
        return sum(1 for ok, _, _ in self.events if ok)


# --- The BIT probe ---------------------------------------------------------
# ONE severity mapping, three callers. The alternative considered and rejected
# was registering a single probe at the aggregation layer that reads all three
# state files: that loses the TOOL attribution the operator needs (which of the
# three is down, and for how long), has to re-derive each tool's
# "is this tool configured on this instance" gate, and cannot participate in the
# per-tool ``Status.worst`` rollup that ``brief.feed_producer.health_feed_items``
# keys its cards on. So the probe is shared as a FUNCTION and called from each
# tool's own ``health.py``, where the config gate and the rollup already live.


def agent_failure_check(
    *,
    failure: dict[str, Any] | None,
    last_success_ts: str | None,
    consequence: str,
    state_path: Path | str,
) -> CheckResult:
    """The ``agent-failure-kind`` probe: is a ``claude -p`` failure still active?

    Closes the 2026-07-29 blind spot: the ``claude-cli-auth`` probe reads
    ``claude auth status`` (login only, zero tokens) and stays GREEN through a
    weekly-QUOTA outage — logged in the whole time, just out of budget — while
    every structuring call fails. This probe derives its signal from REAL
    failing traffic the tool already recorded into its state file, so the
    zero-token BIT invariant stands.

    Severity:
      * no ``failure`` recorded                     → OK   ("no recent agent failures")
      * failure superseded by a later success       → OK   (pipeline recovered)
      * active, ``kind == auth``                    → FAIL (pipeline is DOWN — mirrors
                                                       the claude-cli-auth severity)
      * active and SUSTAINED (streak >= 3)          → FAIL (an outage, not a hiccup)
      * active, ``kind == quota_limited``           → WARN
      * active, ``kind == other`` / unknown         → WARN (surface the CLI tail)

    **Why SUSTAINED is a FAIL and not a louder WARN** (2026-08-15). Severity is
    the health layer's call — the feed producer says so explicitly and refuses
    to make it — and FAIL is the only lever that reaches the operator: the
    brief's health card is an FYI-attention glance item at WARN, while
    ``brief.feed_producer.health_feed_items`` promotes a FAIL to needs-you
    attention, which is what puts it in the needs-you column and rings the push
    doorbell. A WARN with more alarming prose changes nothing an operator would
    notice.

    ``consequence`` is the per-tool half of the card body — what STOPS while
    this tool's agent is down (curator: mail quarantining; janitor: vault issues
    going unfixed; distiller: no learn records). It is the sentence that turns a
    status into an operator decision, and it is the only text in here that
    legitimately differs per tool, so it is a parameter rather than a branch.
    **Write it as a complete sentence ending in a period** — it is embedded
    mid-line ahead of ``Last CLI message:`` in three branches, and each tool
    passes exactly ONE, so a phrase that only reads correctly after "since X —"
    will read wrong after "no success in between:".

    Per ``feedback_intentionally_left_blank.md`` the no-failure case emits an
    explicit OK line so "healthy" is never silent absence.
    """
    payload_base: dict[str, Any] = {"state_path": str(state_path)}

    if not isinstance(failure, dict):
        return CheckResult(
            name="agent-failure-kind",
            status=Status.OK,
            detail="no recent agent failures",
            data=dict(payload_base),
        )

    kind = failure.get("kind", OTHER) or OTHER
    tail = failure.get("summary_tail", "") or ""
    ts_display = failure.get("ts", "unknown")

    # A success AT-OR-AFTER the recorded failure means the pipeline recovered —
    # the stale failure is history, not an active outage. (If either timestamp
    # is unparseable we cannot PROVE recovery, so the shared predicate answers
    # False and we surface the failure rather than swallow it.)
    if failure_superseded_by_success(failure.get("ts"), last_success_ts):
        return CheckResult(
            name="agent-failure-kind",
            status=Status.OK,
            detail=(
                f"no recent agent failures (last failure {ts_display} "
                f"predates last success)"
            ),
            data={
                **payload_base,
                "last_agent_failure": failure,
                "last_agent_success": last_success_ts,
            },
        )

    streak = read_streak(failure)
    # The streak's FIRST failure, not its most recent — "failing since" has to
    # mean since, or a multi-day outage reads as minutes old.
    since_display = failure.get("since") or ts_display
    sustained = is_sustained(streak)

    payload: dict[str, Any] = {
        **payload_base,
        "kind": kind,
        "ts": ts_display,
        "summary_tail": tail,
        "consecutive": streak,
        "since": since_display,
        "sustained": sustained,
    }

    if kind == AUTH:
        return CheckResult(
            name="agent-failure-kind",
            status=Status.FAIL,
            detail=(
                f"claude -p auth failure since {since_display} — {consequence} "
                f"Last CLI message: {tail}"
            ),
            data=payload,
        )
    if sustained:
        # The card body an operator reads at 6am: the failure CLASS on the face
        # (so "quota-limited" is not buried in a tail), the streak, and the
        # CONSEQUENCE. The CLI tail rides along because that is where the reset
        # date lives ("resets Aug 20") — the one fact that decides wait vs act.
        what = "quota-limited" if kind == QUOTA_LIMITED else f"failing ({kind})"
        return CheckResult(
            name="agent-failure-kind",
            status=Status.FAIL,
            detail=(
                f"claude -p {what} since {since_display} — {streak} consecutive agent "
                f"failures, no success in between: {consequence} "
                f"Last CLI message: {tail}"
            ),
            data=payload,
        )
    if kind == QUOTA_LIMITED:
        return CheckResult(
            name="agent-failure-kind",
            status=Status.WARN,
            detail=(
                f"claude -p quota-limited since {since_display} — {consequence} "
                f"Last CLI message: {tail}"
            ),
            data=payload,
        )
    return CheckResult(
        name="agent-failure-kind",
        status=Status.WARN,
        detail=(
            f"claude -p failing ({kind}) since {since_display}; "
            f"last CLI message: {tail}"
        ),
        data=payload,
    )


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
