"""Ops notables — the DELTA half of the Operations section (Feed Phase A).

The Operations section renders a full status snapshot every morning. Projecting
that snapshot into the feed verbatim would card steady state, and this codebase
has already paid for that mistake once: KAL-LE's seven permanently-skipped BIT
tools produced seven undismissable cards every day. Steady-BAD is no more
notable than steady-good — a metric that has been bad for a month was news the
day it turned, not today.

So a notable is a CHANGE IN THE CONCERNING DIRECTION SINCE THE PREVIOUS RENDER
(operator-approved predicate, 2026-08-11):

  * a count that went UP when up is bad (quarantine);
  * an error count that is nonzero AND *newly* nonzero (it was zero, or unseen,
    last render);
  * an error KIND seen for the first time ever.

Answering "since the previous render" requires remembering the previous render,
which is what :class:`OpsBaseline` is for. Note the distinction from the
Campaigns section's deliberate read-only stance ("a brief render that could
trigger real work would make the morning brief a side effect"): this writes
BOOKKEEPING — an observation of what the metrics already were. It advances no
campaign, mutates no vault record, and performs no work the operator could
otherwise have to undo.

**First render emits nothing.** With no prior observation, every nonzero metric
would look "new" and the operator would get a flood describing history rather
than news. The baseline is recorded and the ILB line says so.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from alfred.feed import FeedItem

from .utils import get_logger

log = get_logger(__name__)

_SOURCE_REF = {"producer": "brief", "section": "operations"}

#: Stable keys, one per ANOMALY SOURCE (not per render, never an ordinal). A
#: source that stays notable across renders keeps its key, so the feed dedups it
#: to one card rather than accreting a new one each morning.
KEY_QUARANTINE_RISE = "quarantine_rise"
KEY_JANITOR_CRITICAL = "janitor_critical"
KEY_CURATOR_FAILURES = "curator_failed_attempts"
KEY_AGENT_FAILURE_KIND = "agent_failure_kind"


@dataclass
class OpsBaseline:
    """What the metrics were at the previous render.

    ``seen_agent_failure_kinds`` is cumulative-forever by design: "first-seen"
    means first-seen EVER, so a kind that recurs months later is not new. The
    other fields are last-render values.
    """

    quarantine_week: int | None = None
    janitor_critical: int | None = None
    curator_failed_files: int | None = None
    seen_agent_failure_kinds: list[str] = field(default_factory=list)
    #: Distinguishes "no baseline file yet" (first render) from "a baseline of
    #: all zeros" (a genuinely quiet instance). Without it the two are identical
    #: on disk and the first render would card every nonzero metric.
    recorded: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "OpsBaseline":
        """Schema-tolerant load (the house contract): unknown fields from a
        newer writer are dropped rather than crashing an older reader."""
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        kinds = known.get("seen_agent_failure_kinds")
        known["seen_agent_failure_kinds"] = (
            [str(k) for k in kinds] if isinstance(kinds, list) else []
        )
        return cls(**known)

    def to_dict(self) -> dict[str, Any]:
        return {
            "quarantine_week": self.quarantine_week,
            "janitor_critical": self.janitor_critical,
            "curator_failed_files": self.curator_failed_files,
            "seen_agent_failure_kinds": sorted(set(self.seen_agent_failure_kinds)),
            "recorded": True,
        }


def load_baseline(path: str | Path) -> OpsBaseline:
    """Read the baseline. A missing/unreadable/malformed file yields an
    UNRECORDED baseline, which suppresses every card this render — the same
    conservative direction as the first run, because a baseline we cannot read
    is a baseline we do not have."""
    p = Path(path)
    if not p.is_file():
        return OpsBaseline()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        log.warning("ops_notable.baseline_unreadable", path=str(p), error=str(exc))
        return OpsBaseline()
    if not isinstance(data, dict):
        return OpsBaseline()
    return OpsBaseline.from_dict(data)


def save_baseline(path: str | Path, baseline: OpsBaseline) -> None:
    """Atomically rewrite the baseline (``.tmp`` → ``os.replace``, house pattern)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(
        json.dumps(baseline.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8",
    )
    os.replace(tmp, p)


# --- metric readers (each degrades to None = "couldn't measure") -------------


def _quarantine_week_count(vault_path: Path, quarantine_dir_name: str) -> int | None:
    """Rolling 7-day quarantine count, re-derived from the same directory the
    Operations render counts. Returns ``None`` on read failure so an unreadable
    directory is not mistaken for a drop to zero."""
    from datetime import datetime, timedelta

    root = vault_path / quarantine_dir_name / "spam"
    if not root.exists():
        return 0
    cutoff = datetime.now() - timedelta(days=7)
    count = 0
    try:
        for md_file in root.rglob("*.md"):
            try:
                if datetime.fromtimestamp(md_file.stat().st_mtime) >= cutoff:
                    count += 1
            except OSError:
                continue
    except OSError:
        return None
    return count


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _janitor_critical_count(janitor_state: dict[str, Any]) -> int:
    """CRITICAL issues in the LATEST sweep (a snapshot, not a cumulative sum —
    mirroring ``_janitor_summary``, which reports the latest sweep too)."""
    sweeps = janitor_state.get("sweeps")
    if not isinstance(sweeps, dict) or not sweeps:
        return 0
    try:
        latest = max(
            (v for v in sweeps.values() if isinstance(v, dict)),
            key=lambda v: str(v.get("timestamp", "")),
        )
    except ValueError:
        return 0
    by_sev = latest.get("issues_by_severity")
    if not isinstance(by_sev, dict):
        return 0
    try:
        return int(by_sev.get("CRITICAL", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _curator_failed_file_count(curator_state: dict[str, Any]) -> int:
    """How many inbox files are currently carrying retry failures."""
    failed = curator_state.get("failed_attempts")
    if not isinstance(failed, dict):
        return 0
    count = 0
    for value in failed.values():
        try:
            if int(value) > 0:
                count += 1
        except (TypeError, ValueError):
            continue
    return count


def _active_agent_failure_kind(curator_state: dict[str, Any]) -> str | None:
    """The kind of the most recent agent failure, when it has NOT been
    superseded by a later success. Mirrors the BIT ``agent-failure-kind``
    recovery rule: a failure at-or-before the last success is history."""
    failure = curator_state.get("last_agent_failure")
    if not isinstance(failure, dict):
        return None
    fail_ts = str(failure.get("ts", "") or "")
    last_run = str(curator_state.get("last_run", "") or "")
    if fail_ts and last_run and last_run >= fail_ts:
        return None  # recovered
    kind = failure.get("kind") or "other"
    return str(kind)


# --- the producer -----------------------------------------------------------


def ops_notable_feed_items(
    data_dir: str | Path,
    vault_path: str | Path,
    baseline_path: str | Path,
    *,
    instance: str,
    quarantine_dir_name: str = "quarantine",
) -> list[FeedItem] | None:
    """One ``ops_notable`` item per metric that moved the wrong way since the
    previous render. ``[]`` is a genuine "ran, nothing notable"; ``None`` is a
    read failure (caller must not reconcile).

    The baseline is advanced on every successful run, INCLUDING runs that emit
    nothing — that is what makes a return to steady state stick rather than
    re-firing forever.
    """
    data = Path(data_dir)
    vault = Path(vault_path)

    quarantine_week = _quarantine_week_count(vault, quarantine_dir_name)
    if quarantine_week is None:
        log.warning("ops_notable.quarantine_unreadable", vault=str(vault))
        return None

    curator_state = _read_json(data / "curator_state.json")
    janitor_state = _read_json(data / "janitor_state.json")
    janitor_critical = _janitor_critical_count(janitor_state)
    curator_failed_files = _curator_failed_file_count(curator_state)
    active_kind = _active_agent_failure_kind(curator_state)

    prior = load_baseline(baseline_path)
    seen_kinds = set(prior.seen_agent_failure_kinds)

    items: list[FeedItem] = []

    def _add(key: str, title: str, evidence: dict[str, Any]) -> None:
        items.append(FeedItem.create(
            kind="ops_notable",
            stable_key=key,
            instance=instance,
            title=title,
            evidence=evidence,
            source_ref=dict(_SOURCE_REF),
        ))

    if prior.recorded:
        # (a) a count that rose when rising is bad
        if prior.quarantine_week is not None and quarantine_week > prior.quarantine_week:
            delta = quarantine_week - prior.quarantine_week
            _add(
                KEY_QUARANTINE_RISE,
                f"Spam quarantine up {delta} (now {quarantine_week} this week)",
                {"metric": "quarantine_week", "previous": prior.quarantine_week,
                 "current": quarantine_week, "delta": delta},
            )
        # (b) error counts that are nonzero AND newly so
        if janitor_critical > 0 and not prior.janitor_critical:
            _add(
                KEY_JANITOR_CRITICAL,
                f"Janitor: {janitor_critical} CRITICAL issue"
                f"{'s' if janitor_critical != 1 else ''} (was none)",
                {"metric": "janitor_critical", "previous": prior.janitor_critical or 0,
                 "current": janitor_critical},
            )
        if curator_failed_files > 0 and not prior.curator_failed_files:
            _add(
                KEY_CURATOR_FAILURES,
                f"Curator: {curator_failed_files} file"
                f"{'s' if curator_failed_files != 1 else ''} failing to process (was none)",
                {"metric": "curator_failed_files",
                 "previous": prior.curator_failed_files or 0,
                 "current": curator_failed_files},
            )
        # (c) an error kind never seen before
        if active_kind is not None and active_kind not in seen_kinds:
            _add(
                f"{KEY_AGENT_FAILURE_KIND}|{active_kind}",
                f"New agent-failure kind: {active_kind}",
                {"metric": "agent_failure_kind", "kind": active_kind, "first_seen": True},
            )
    else:
        # ILB: the first render is deliberately silent, and says so. Without
        # this line an operator seeing no ops cards on day one cannot tell
        # "nothing happened" from "this never ran".
        log.info(
            "ops_notable.baseline_recorded",
            instance=instance,
            path=str(baseline_path),
            detail="first render (or unreadable baseline) — recording the baseline "
                   "and emitting nothing; deltas start from the next render",
        )

    if active_kind is not None:
        seen_kinds.add(active_kind)

    try:
        save_baseline(baseline_path, OpsBaseline(
            quarantine_week=quarantine_week,
            janitor_critical=janitor_critical,
            curator_failed_files=curator_failed_files,
            seen_agent_failure_kinds=sorted(seen_kinds),
            recorded=True,
        ))
    except OSError as exc:
        # A baseline we failed to write means the NEXT render compares against
        # stale numbers and may re-card what we just carded. Noisy, not silent —
        # the correct direction — but it must be visible.
        log.warning(
            "ops_notable.baseline_write_failed",
            path=str(baseline_path), error=str(exc),
            detail="next render will compare against a stale baseline",
        )

    if prior.recorded and not items:
        # ILB: "ran, nothing notable". The ruling puts this in the LOG rather
        # than the feed — a card saying "nothing to report" is the noise the
        # delta predicate exists to avoid.
        log.info(
            "ops_notable.nothing_notable",
            instance=instance,
            quarantine_week=quarantine_week,
            janitor_critical=janitor_critical,
            curator_failed_files=curator_failed_files,
            detail="metrics steady or improving since the previous render",
        )
    return items
