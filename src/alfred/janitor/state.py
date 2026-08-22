"""State persistence — state.json load/save with open issues and fix log."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import structlog

from alfred.health.agent_failure import (
    SUSTAINED_FAILURE_STREAK,
    failure_superseded_by_success,
    next_failure_record,
    read_streak,
)

from .issues import FixLogEntry, SweepResult

log = structlog.get_logger()


@dataclass
class FileState:
    md5: str
    last_scanned: str = ""
    open_issues: list[str] = field(default_factory=list)  # issue codes
    # Upstream #15: Stage 3 stub-enrichment staleness tracking. After
    # max_enrichment_attempts failures on the same content hash, we stop
    # retrying until the file changes (hash reset clears the counter).
    enrichment_attempts: int = 0
    last_enrichment_attempt: str = ""
    enrichment_stale: bool = False


class JanitorState:
    def __init__(self, state_path: str | Path, max_sweep_history: int = 20) -> None:
        self.state_path = Path(state_path)
        self.max_sweep_history = max_sweep_history
        self.version: int = 1
        self.files: dict[str, FileState] = {}  # rel_path -> FileState
        self.sweeps: dict[str, SweepResult] = {}  # sweep_id -> SweepResult
        self.fix_log: list[FixLogEntry] = []  # permanent audit trail
        self.ignored: dict[str, str] = {}  # rel_path -> reason
        self.pending_writes: dict[str, str] = {}  # rel_path -> expected_md5
        # ISO timestamp of last deep (fix-mode) sweep. Persisted so daemon
        # restarts do not reset to epoch and trigger a full sweep on every
        # boot. Upstream observed 21 restarts in 3 days -> 968 wasted LLM
        # calls before adding this persistence.
        self.last_deep_sweep: str | None = None
        # Upstream #15: snapshot of the last deep-sweep's issue set. Used
        # for event-driven deep sweeps — on the next tick we only invoke
        # the expensive fix pipeline if the current issue set contains
        # codes not present in the previous snapshot. Shape: rel_path ->
        # list of issue code strings.
        self.previous_sweep_issues: dict[str, list[str]] = {}
        # Layer 3 triage queue: deterministic IDs of dedup/orphan/etc.
        # candidate sets for which a triage task has already been surfaced.
        # Prevents the agent from re-creating the same triage task across
        # successive sweeps. Persisted as a JSON list; loaded as a set.
        self.triage_ids_seen: set[str] = set()
        # ``last_error`` mirrors the brief.state pattern (2026-05-14):
        # parallel state at the JanitorState-level (not per-file). Shape
        # is ``{"ts": iso_string, "message": str}`` when populated; None
        # when no error since last successful sweep. Captured by the
        # daemon's outer ``except Exception:`` at daemon.py:639 and
        # surfaced in the BIT ``last-successful-sweep`` probe detail so
        # operators see WHY the sweep stalled, not just that it did.
        # Cleared on each successful ``save_sweep_issues`` call.
        self.last_error: dict | None = None
        # 2026-08-22 — the agent (``claude -p``) failure pair, so the janitor
        # BIT can surface a quota / auth outage. Same shape and same shared
        # arithmetic as curator's: {"ts", "kind", "summary_tail", "consecutive",
        # "since"}, ``None`` until the first agent failure.
        self.last_agent_failure: dict | None = None
        # And a DEDICATED success timestamp, deliberately not reusing any sweep
        # timestamp. ``sweeps[*].timestamp`` and ``last_deep_sweep`` are stamped
        # whether or not the agent call inside the sweep succeeded — ``add_sweep``
        # runs unconditionally at the end of ``run_sweep``, right past the
        # ``sweep.agent_failed`` log. Feeding one of those to the recovery
        # predicate would mark every agent failure "superseded by a success"
        # on the very next sweep, which is exactly the self-certifying-recovery
        # bug curator's ``bump_last_run=False`` exists to prevent. Empty string
        # = the agent has never succeeded here, which the predicate reads as
        # "cannot prove recovery" (fails toward ACTIVE — the safe direction).
        self.last_agent_success: str = ""

    def load(self) -> None:
        """Load state from disk if it exists."""
        if not self.state_path.exists():
            log.info("state.no_existing_state", path=str(self.state_path))
            return
        with open(self.state_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        self.version = raw.get("version", 1)
        # Tolerate unknown legacy fields. Mirrors the ``distiller/state.py``
        # and ``surveyor/state.py`` forward-compat pattern: filtering on
        # ``__dataclass_fields__`` keeps load() compatible with older or
        # newer schemas, and protects against accidental cross-tool path
        # collisions (e.g. surveyor state ever landing here via a shared
        # default path).
        file_known = set(FileState.__dataclass_fields__.keys())
        for rel, fdata in raw.get("files", {}).items():
            self.files[rel] = FileState(**{k: v for k, v in fdata.items() if k in file_known})
        for sid, sdata in raw.get("sweeps", {}).items():
            self.sweeps[sid] = SweepResult.from_dict(sdata)
        self.fix_log = [FixLogEntry.from_dict(e) for e in raw.get("fix_log", [])]
        self.ignored = raw.get("ignored", {})
        self.pending_writes = raw.get("pending_writes", {})
        self.last_deep_sweep = raw.get("last_deep_sweep")
        self.previous_sweep_issues = raw.get("previous_sweep_issues", {})
        self.triage_ids_seen = set(raw.get("triage_ids_seen", []))
        # Schema tolerance: older state files (pre 2026-05-14) won't
        # have last_error — defaults to None. A corrupt non-dict value
        # also degrades to None so a malformed state file can't poison
        # the probe-side _read_last_error helper.
        last_error_raw = raw.get("last_error")
        self.last_error = last_error_raw if isinstance(last_error_raw, dict) else None
        # Schema tolerance: state files written before 2026-08-22 carry neither
        # agent field. A malformed value degrades to the empty default rather
        # than crashing the loader OR poisoning the probe.
        agent_failure_raw = raw.get("last_agent_failure")
        self.last_agent_failure = (
            agent_failure_raw if isinstance(agent_failure_raw, dict) else None
        )
        agent_success_raw = raw.get("last_agent_success")
        self.last_agent_success = (
            agent_success_raw if isinstance(agent_success_raw, str) else ""
        )
        log.info(
            "state.loaded",
            files=len(self.files),
            sweeps=len(self.sweeps),
            triage_ids_seen=len(self.triage_ids_seen),
        )

    def save(self) -> None:
        """Atomic save: write to .tmp then os.replace."""
        # Trim sweep history
        if len(self.sweeps) > self.max_sweep_history:
            sorted_ids = sorted(self.sweeps.keys(), key=lambda k: self.sweeps[k].timestamp)
            for sid in sorted_ids[:-self.max_sweep_history]:
                del self.sweeps[sid]

        data = {
            "version": self.version,
            "files": {
                rel: {
                    "md5": fs.md5,
                    "last_scanned": fs.last_scanned,
                    "open_issues": fs.open_issues,
                    "enrichment_attempts": fs.enrichment_attempts,
                    "last_enrichment_attempt": fs.last_enrichment_attempt,
                    "enrichment_stale": fs.enrichment_stale,
                }
                for rel, fs in self.files.items()
            },
            "sweeps": {sid: sr.to_dict() for sid, sr in self.sweeps.items()},
            "fix_log": [e.to_dict() for e in self.fix_log],
            "ignored": self.ignored,
            "pending_writes": self.pending_writes,
            "last_deep_sweep": self.last_deep_sweep,
            "previous_sweep_issues": self.previous_sweep_issues,
            "triage_ids_seen": sorted(self.triage_ids_seen),
            "last_error": self.last_error,
            "last_agent_failure": self.last_agent_failure,
            "last_agent_success": self.last_agent_success,
        }
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.state_path.with_suffix(".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, self.state_path)

    def should_scan(self, rel_path: str, current_md5: str) -> bool:
        """Return True if a file needs scanning (changed or has open issues)."""
        if rel_path in self.ignored:
            return False
        if rel_path not in self.files:
            return True
        fs = self.files[rel_path]
        if fs.md5 != current_md5:
            return True
        if fs.open_issues:
            return True
        return False

    def update_file(self, rel_path: str, md5: str, issue_codes: list[str] | None = None) -> None:
        """Update or create a file entry after scanning."""
        now = datetime.now(timezone.utc).isoformat()
        if rel_path in self.files:
            self.files[rel_path].md5 = md5
            self.files[rel_path].last_scanned = now
            self.files[rel_path].open_issues = issue_codes or []
        else:
            self.files[rel_path] = FileState(
                md5=md5,
                last_scanned=now,
                open_issues=issue_codes or [],
            )

    def remove_file(self, rel_path: str) -> None:
        """Remove a file from state."""
        self.files.pop(rel_path, None)
        self.pending_writes.pop(rel_path, None)

    def add_sweep(self, result: SweepResult) -> None:
        """Record a sweep result."""
        self.sweeps[result.sweep_id] = result

    def add_fix_log(self, entry: FixLogEntry) -> None:
        """Append to the permanent fix log."""
        self.fix_log.append(entry)

    def ignore_file(self, rel_path: str, reason: str = "") -> None:
        """Add a file to the ignore list."""
        self.ignored[rel_path] = reason

    def has_seen_triage(self, triage_id: str) -> bool:
        """Return True if the given triage id has already been surfaced."""
        return triage_id in self.triage_ids_seen

    def mark_triage_seen(self, triage_id: str) -> None:
        """Record that a triage task has been surfaced for this id."""
        self.triage_ids_seen.add(triage_id)

    # --- Upstream #15: Stage 3 enrichment staleness helpers ---

    def record_enrichment_attempt(self, rel_path: str, max_attempts: int = 3) -> None:
        """Increment the enrichment attempt counter for ``rel_path``.

        Marks the file as ``enrichment_stale`` once the counter reaches
        ``max_attempts`` so the next sweep's Stage 3 will skip it. The
        counter is reset on a content-hash change via
        :meth:`reset_enrichment_staleness`.
        """
        if rel_path not in self.files:
            return
        fs = self.files[rel_path]
        fs.enrichment_attempts += 1
        fs.last_enrichment_attempt = datetime.now(timezone.utc).isoformat()
        if fs.enrichment_attempts >= max_attempts:
            fs.enrichment_stale = True

    def reset_enrichment_staleness(self, rel_path: str) -> None:
        """Clear enrichment staleness when the file's content has changed."""
        if rel_path not in self.files:
            return
        fs = self.files[rel_path]
        fs.enrichment_attempts = 0
        fs.last_enrichment_attempt = ""
        fs.enrichment_stale = False

    def is_enrichment_stale(self, rel_path: str) -> bool:
        """Return True if Stage 3 has exhausted attempts on this file."""
        if rel_path not in self.files:
            return False
        return self.files[rel_path].enrichment_stale

    # --- Upstream #15: event-driven deep sweep helpers ---

    def get_new_issues(
        self, current_issues: dict[str, list[str]],
    ) -> dict[str, list[str]]:
        """Return files whose current issue codes include ones NOT seen last sweep.

        Used by run_watch to skip the expensive fix pipeline entirely when
        no new issues surfaced since the last deep sweep. Compares per-file
        issue-code sets; if a file has any code not in the previous snapshot,
        that file's new codes are included in the result.
        """
        new: dict[str, list[str]] = {}
        for path, codes in current_issues.items():
            prev_codes = set(self.previous_sweep_issues.get(path, []))
            novel = [c for c in codes if c not in prev_codes]
            if novel:
                new[path] = novel
        return new

    def save_sweep_issues(self, issues: dict[str, list[str]]) -> None:
        """Persist the current sweep's issue snapshot for the next comparison.

        Also clears ``last_error`` — reaching this call site means the
        outer ``except Exception:`` in daemon.run_watch did NOT fire on
        this tick, so the recovery semantics treat the sweep as
        successful and wipe any stale failure context the probe would
        otherwise trail across the BIT line. Mirrors the brief.State
        ``add_run(success=True)`` clear-on-success pattern from
        2026-05-14.
        """
        self.previous_sweep_issues = issues
        self.last_error = None

    def record_error(self, message: str) -> None:
        """Capture a daemon-level failure into ``state.last_error`` and persist.

        Called from the daemon's outer ``except Exception:`` at
        daemon.py:639 so the BIT ``last-successful-sweep`` probe can
        surface the failure cause (e.g. ``KeyError: 'foo'``) on the
        BIT line rather than forcing the operator to grep
        ``data/janitor.log``.

        Does NOT crash the daemon if persistence itself fails — a
        broken state file shouldn't compound a broken sweep. Logs the
        secondary failure and returns. Mirrors the brief.StateManager
        ``record_error`` pattern from 2026-05-14.
        """
        self.last_error = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "message": message,
        }
        try:
            self.save()
        except OSError as e:
            log.warning("janitor.state.record_error_save_failed", error=str(e))

    # --- agent (``claude -p``) health ---------------------------------------
    # Janitor's half of the cross-tool agent-failure signal (2026-08-22). On
    # 2026-08-22 the box's weekly quota was exhausted and janitor had logged
    # 1,029 ``sweep.agent_failed`` lines while its BIT reported ``janitor ok``:
    # the backend already classified every one of them into a ``kind``, and the
    # daemon logged that kind and dropped it. Nothing persisted it, so no probe
    # could read it. These two methods are the missing consumer.

    def record_agent_failure(self, kind: str, summary: str) -> None:
        """Stamp the most recent agent failure and its STREAK.

        Does NOT touch ``last_agent_success`` — that stays the last time an
        agent call actually returned success, so the BIT probe can compare the
        two to tell an active outage from a recovered one.

        The streak is the STRUCTURAL outage signal: ``kind`` carries what
        failed, ``consecutive`` carries how long it has been failing, and only
        the second separates one bad sweep from the multi-day backend outage
        that leaves the vault's issues unfixed. Arithmetic is shared with
        curator and distiller via
        :func:`~alfred.health.agent_failure.next_failure_record`.
        """
        self.last_agent_failure = next_failure_record(
            prior=self.last_agent_failure,
            last_success_ts=self.last_agent_success,
            kind=kind,
            summary=summary,
        )
        if self.last_agent_failure["consecutive"] == SUSTAINED_FAILURE_STREAK:
            # ILB, and once per outage: logged on the CROSSING (``==``, not
            # ``>=``) so the log carries the moment the backend went from flaky
            # to down rather than one line per failure for the rest of a
            # multi-day outage. The probe re-derives ``sustained`` from the
            # streak every sweep, so nothing depends on this line being seen —
            # it exists so "when did fixes stop?" has a greppable timestamp.
            log.warning(
                "janitor.agent_failure_sustained",
                kind=kind or "other",
                consecutive=self.last_agent_failure["consecutive"],
                since=self.last_agent_failure["since"],
                threshold=SUSTAINED_FAILURE_STREAK,
                summary_tail=(summary or "")[-300:],
            )

    def record_agent_success(self) -> None:
        """Stamp a successful agent call, ending any running failure streak.

        Emits the recovery counterpart of ``janitor.agent_failure_sustained``
        BEFORE moving the timestamp — the order is the whole trick. Once
        ``last_agent_success`` moves past the failure,
        :func:`~alfred.health.agent_failure.failure_superseded_by_success`
        answers True and the outage is no longer active, so this fires exactly
        once, on the first success that ends it. No "already logged" flag to
        persist.

        Silent on ordinary successes (nothing was down) and on recovery from a
        SHORT streak (a hiccup that resolved is not news). Without the pair, the
        log shows an outage beginning and never ending — which reads identically
        to an outage that never ended.
        """
        failure = self.last_agent_failure
        if isinstance(failure, dict) and not failure_superseded_by_success(
            failure.get("ts"), self.last_agent_success
        ):
            streak = read_streak(failure)
            if streak >= SUSTAINED_FAILURE_STREAK:
                log.info(
                    "janitor.agent_failure_recovered",
                    kind=failure.get("kind", "other"),
                    consecutive=streak,
                    since=failure.get("since") or failure.get("ts"),
                )
        self.last_agent_success = datetime.now(timezone.utc).isoformat()
