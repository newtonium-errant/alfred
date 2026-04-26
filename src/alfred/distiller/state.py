"""State persistence — state.json load/save with extraction log and run history."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path

import structlog

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
        """Record an extraction run result."""
        self.runs[result.run_id] = result

    def add_log_entry(self, entry: ExtractionLogEntry) -> None:
        """Append to the permanent extraction log."""
        self.extraction_log.append(entry)
