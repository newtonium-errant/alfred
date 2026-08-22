"""State persistence — state.json load/save with extraction log and run history."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path

import structlog

from alfred.health.agent_failure import (
    SUSTAINED_FAILURE_STREAK,
    AgentCallOutcomes,
    failure_superseded_by_success,
    next_failure_record,
    read_streak,
)

log = structlog.get_logger()


@dataclass
class FileState:
    md5: str
    last_distilled: str = ""  # ISO timestamp of last extraction run
    learn_records_created: list[str] = field(default_factory=list)  # rel_paths
    # SHA-256 of the body only (frontmatter stripped, trailing whitespace
    # normalized). The skip-distill gate consults this — full-file md5
    # changes on every cosmetic frontmatter write (alfred_tags from
    # surveyor, attribution_audit append from janitor deep_sweep_fix), but
    # body_hash only changes when the source's claim wording shifted,
    # which is what should actually trigger re-extraction.
    body_hash: str = ""


@dataclass
class RunResult:
    run_id: str = ""
    timestamp: str = ""
    candidates_found: int = 0
    candidates_processed: int = 0
    records_created: dict[str, int] = field(default_factory=dict)  # learn_type -> count
    batches: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> RunResult:
        return cls(**data)


@dataclass
class ExtractionLogEntry:
    timestamp: str = ""
    run_id: str = ""
    action: str = ""  # "created"
    learn_type: str = ""  # "assumption", "decision", etc.
    learn_file: str = ""  # rel_path of created learn record
    source_files: list[str] = field(default_factory=list)  # rel_paths of source records
    detail: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> ExtractionLogEntry:
        return cls(**data)


class DistillerState:
    def __init__(self, state_path: str | Path, max_run_history: int = 20) -> None:
        self.state_path = Path(state_path)
        self.max_run_history = max_run_history
        self.version: int = 1
        self.files: dict[str, FileState] = {}  # source rel_path -> state
        self.runs: dict[str, RunResult] = {}  # run_id -> result
        self.extraction_log: list[ExtractionLogEntry] = []  # permanent audit trail
        self.pending_writes: dict[str, str] = {}  # rel_path -> expected_md5
        # ISO timestamp of last deep extraction run. Persisted so daemon
        # restarts do not reset to epoch and trigger a full deep extraction
        # on every boot. Ports upstream e510cbe.
        self.last_deep_extraction: str | None = None
        # ``last_error`` mirrors the brief.state pattern (2026-05-14):
        # parallel state at the DistillerState-level (not per-file).
        # Shape is ``{"ts": iso_string, "message": str}`` when populated;
        # None when no error since last successful deep extraction.
        # Captured by the daemon's outer ``except Exception:`` at
        # daemon.py:749 and surfaced in the BIT
        # ``last-successful-extraction`` probe detail so operators see
        # WHY the extraction stalled, not just that it did. Cleared on
        # the next successful DEEP extraction (``add_run`` only fires
        # inside ``run_extraction`` on a ``deep_due`` tick — light
        # scans don't reset ``last_error``). A failure captured by
        # the daemon's outer except can therefore persist across many
        # successful light-scan ticks until the next deep extraction
        # succeeds; that's intentional, so operators see the failure
        # cause until it's proven fixed by a clean deep run. Compare
        # brief.State.add_run, where every successful brief is a
        # per-tick clear because brief.daemon ticks at one cadence.
        self.last_error: dict | None = None
        # 2026-08-22 — the agent (``claude -p``) failure pair, so the distiller
        # BIT can surface a quota / auth outage. Same shape and same shared
        # arithmetic as curator's and janitor's.
        self.last_agent_failure: dict | None = None
        # A DEDICATED success timestamp, deliberately not reusing ``runs[*]
        # .timestamp`` or ``last_deep_extraction``. ``add_run`` fires at the end
        # of ``run_extraction`` regardless of what the LLM calls inside it did —
        # indeed ``PipelineResult.success`` is set True unconditionally at the
        # end of ``run_pipeline``, so a run in which every stage call failed
        # still records as a run. Feeding either to the recovery predicate would
        # mark every agent failure "superseded by a success" on the next run.
        self.last_agent_success: str = ""

    def load(self) -> None:
        """Load state from disk if it exists."""
        if not self.state_path.exists():
            log.info("state.no_existing_state", path=str(self.state_path))
            return
        with open(self.state_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        self.version = raw.get("version", 1)
        # Tolerate unknown legacy fields (e.g. ``last_scanned`` from an older
        # schema). Filtering on the dataclass __dataclass_fields__ keeps
        # state.load() forward/backward compatible — adding a field never
        # crashes a daemon reading an older state file, and removing a field
        # never crashes one reading a newer file.
        known_fields = set(FileState.__dataclass_fields__.keys())
        for rel, fdata in raw.get("files", {}).items():
            self.files[rel] = FileState(**{k: v for k, v in fdata.items() if k in known_fields})
        for rid, rdata in raw.get("runs", {}).items():
            self.runs[rid] = RunResult.from_dict(rdata)
        self.extraction_log = [
            ExtractionLogEntry.from_dict(e) for e in raw.get("extraction_log", [])
        ]
        self.pending_writes = raw.get("pending_writes", {})
        self.last_deep_extraction = raw.get("last_deep_extraction")
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
        log.info("state.loaded", files=len(self.files), runs=len(self.runs))

    def save(self) -> None:
        """Atomic save: write to .tmp then os.replace."""
        # Trim run history
        if len(self.runs) > self.max_run_history:
            sorted_ids = sorted(
                self.runs.keys(), key=lambda k: self.runs[k].timestamp
            )
            for rid in sorted_ids[: -self.max_run_history]:
                del self.runs[rid]

        data = {
            "version": self.version,
            "files": {
                rel: {
                    "md5": fs.md5,
                    "last_distilled": fs.last_distilled,
                    "learn_records_created": fs.learn_records_created,
                    "body_hash": fs.body_hash,
                }
                for rel, fs in self.files.items()
            },
            "runs": {rid: rr.to_dict() for rid, rr in self.runs.items()},
            "extraction_log": [e.to_dict() for e in self.extraction_log],
            "pending_writes": self.pending_writes,
            "last_deep_extraction": self.last_deep_extraction,
            "last_error": self.last_error,
            "last_agent_failure": self.last_agent_failure,
            "last_agent_success": self.last_agent_success,
        }
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.state_path.with_suffix(".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, self.state_path)

    def should_distill(self, rel_path: str, current_body_hash: str) -> bool:
        """Return True if a file needs distilling (new or body changed).

        Gates on body_hash, not full-file md5: cosmetic frontmatter writes
        (janitor deep_sweep_fix, surveyor alfred_tags) must NOT re-trigger
        extraction. Legacy state with empty ``body_hash`` returns True so
        the next scan re-extracts once and populates the field.
        """
        if rel_path not in self.files:
            return True
        stored = self.files[rel_path].body_hash
        if not stored:
            # Legacy state pre-dating body_hash — treat as unknown,
            # re-extract once to populate the field.
            return True
        return stored != current_body_hash

    def get_distilled_body_hashes(self) -> dict[str, str]:
        """Return {rel_path: body_hash} for files with a recorded body hash.

        Files with empty ``body_hash`` (legacy state) are omitted so the
        scanner treats them as unknown and re-extracts once.
        """
        return {rel: fs.body_hash for rel, fs in self.files.items() if fs.body_hash}

    def get_distilled_last_distilled(self) -> dict[str, str]:
        """Return {rel_path: last_distilled ISO timestamp} as a sidecar to body_hashes.

        Used by ``scan_candidates`` to emit ``candidates.drift_skip`` log
        lines when a file's mtime has bumped since the last distillation
        but its body hash is unchanged — the signature of a cosmetic
        frontmatter rewrite (janitor deep_sweep_fix, surveyor alfred_tags).
        Aggregating this signal over time lets us evaluate whether
        Option 3 (audit-log mutation-source gate) is worth the additional
        80-100 LOC. See ``project_distiller_drift_mitigation.md``.

        Filtered to files with a recorded ``body_hash`` so the keys
        align with ``get_distilled_body_hashes()`` — entries the gate
        would actually check.
        """
        return {
            rel: fs.last_distilled
            for rel, fs in self.files.items()
            if fs.body_hash and fs.last_distilled
        }

    def update_file(
        self,
        rel_path: str,
        md5: str,
        learn_records: list[str] | None = None,
        body_hash: str | None = None,
    ) -> None:
        """Update or create a file entry after distillation.

        ``body_hash`` is optional so legacy callers (e.g.
        ``recompute_source_md5s`` after pipeline writes) can refresh
        the full-file md5 without overwriting a stored body_hash with
        an empty one.
        """
        now = datetime.now(timezone.utc).isoformat()
        if rel_path in self.files:
            self.files[rel_path].md5 = md5
            self.files[rel_path].last_distilled = now
            if learn_records:
                self.files[rel_path].learn_records_created.extend(learn_records)
            if body_hash is not None:
                self.files[rel_path].body_hash = body_hash
        else:
            self.files[rel_path] = FileState(
                md5=md5,
                last_distilled=now,
                learn_records_created=learn_records or [],
                body_hash=body_hash or "",
            )

    def add_run(self, result: RunResult) -> None:
        """Record an extraction run result.

        Also clears ``last_error`` — reaching this call site means a
        deep extraction completed without raising, so the recovery
        semantics treat the deep run as successful and wipe any stale
        failure context the probe would otherwise trail across the BIT
        line. NOTE: this is a per-deep-extraction clear (``add_run`` is
        only called from ``run_extraction`` on a ``deep_due`` tick),
        NOT a per-tick clear; a stored ``last_error`` persists across
        successful light-scan ticks until the next deep extraction
        succeeds. That's the intended recovery semantic — the operator
        wants the deep-extraction failure cause surfaced on BIT until
        a clean deep run proves it's fixed. Compare brief.State, where
        every successful brief tick clears because the brief daemon
        runs at a single cadence.
        """
        self.runs[result.run_id] = result
        self.last_error = None

    def add_log_entry(self, entry: ExtractionLogEntry) -> None:
        """Append to the permanent extraction log."""
        self.extraction_log.append(entry)

    def record_error(self, message: str) -> None:
        """Capture a daemon-level failure into ``state.last_error`` and persist.

        Called from the daemon's outer ``except Exception:`` at
        daemon.py:749 so the BIT ``last-successful-extraction`` probe
        can surface the failure cause (e.g. ``KeyError: 'foo'``) on
        the BIT line rather than forcing the operator to grep
        ``data/distiller.log``.

        Does NOT crash the daemon if persistence itself fails — a
        broken state file shouldn't compound a broken extraction.
        Logs the secondary failure and returns. Mirrors the
        brief.StateManager ``record_error`` pattern from 2026-05-14.
        """
        self.last_error = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "message": message,
        }
        try:
            self.save()
        except OSError as e:
            log.warning("distiller.state.record_error_save_failed", error=str(e))

    # --- agent (``claude -p``) health ---------------------------------------
    # Distiller's half of the cross-tool agent-failure signal (2026-08-22). On
    # 2026-08-22 the box's weekly quota was exhausted and distiller had logged
    # 246 ``pipeline.llm_failed`` lines while its BIT reported ``distiller ok``.
    # ``_call_llm`` already classified every one of them; nothing persisted it.

    def record_agent_failure(self, kind: str, summary: str) -> None:
        """Stamp the most recent agent failure and its STREAK.

        See ``janitor.state.JanitorState.record_agent_failure`` — same contract,
        same shared arithmetic
        (:func:`~alfred.health.agent_failure.next_failure_record`). Applied from
        the daemon/CLI once per agent call, in call order, from the
        :class:`~alfred.health.agent_failure.AgentCallOutcomes` the pipeline
        carried up.
        """
        self.last_agent_failure = next_failure_record(
            prior=self.last_agent_failure,
            last_success_ts=self.last_agent_success,
            kind=kind,
            summary=summary,
        )
        if self.last_agent_failure["consecutive"] == SUSTAINED_FAILURE_STREAK:
            # ILB, once per outage, on the CROSSING — so "when did extraction
            # stop?" has a greppable timestamp rather than one line per failure
            # for the rest of a multi-day outage.
            log.warning(
                "distiller.agent_failure_sustained",
                kind=kind or "other",
                consecutive=self.last_agent_failure["consecutive"],
                since=self.last_agent_failure["since"],
                threshold=SUSTAINED_FAILURE_STREAK,
                summary_tail=(summary or "")[-300:],
            )

    def record_agent_success(self) -> None:
        """Stamp a successful agent call, ending any running failure streak.

        Emits the recovery counterpart BEFORE moving the timestamp, so it fires
        exactly once on the success that ends an outage — see
        ``janitor.state.JanitorState.record_agent_success`` for why the order is
        the whole trick.
        """
        failure = self.last_agent_failure
        if isinstance(failure, dict) and not failure_superseded_by_success(
            failure.get("ts"), self.last_agent_success
        ):
            streak = read_streak(failure)
            if streak >= SUSTAINED_FAILURE_STREAK:
                log.info(
                    "distiller.agent_failure_recovered",
                    kind=failure.get("kind", "other"),
                    consecutive=streak,
                    since=failure.get("since") or failure.get("ts"),
                )
        self.last_agent_success = datetime.now(timezone.utc).isoformat()

    def apply_agent_outcomes(self, outcomes: AgentCallOutcomes) -> None:
        """Fold a run's agent-call outcomes into state, IN ORDER.

        Order matters and is why :class:`AgentCallOutcomes` is a list rather
        than a tally: a success in the middle of a run breaks the streak at that
        point, so replaying (fail, fail, success, fail) must leave a streak of
        1, not 3. Collapsing the run to a single verdict would either erase the
        success or erase the failures.

        A run with no agent calls at all (nothing qualified for extraction)
        applies nothing — it is neither evidence of health nor of failure.
        """
        for ok, kind, summary in outcomes.events:
            if ok:
                self.record_agent_success()
            else:
                self.record_agent_failure(kind=kind, summary=summary)
