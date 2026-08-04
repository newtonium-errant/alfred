"""Issue, SweepResult, and FixLogEntry dataclasses."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class Severity(str, Enum):
    CRITICAL = "CRITICAL"
    WARNING = "WARNING"
    INFO = "INFO"


# --- Structural issue codes ---

class IssueCode(str, Enum):
    # Frontmatter
    MISSING_REQUIRED_FIELD = "FM001"
    INVALID_TYPE_VALUE = "FM002"
    INVALID_STATUS_VALUE = "FM003"
    INVALID_FIELD_TYPE = "FM004"
    # Directory
    WRONG_DIRECTORY = "DIR001"
    # Links
    BROKEN_WIKILINK = "LINK001"
    UNLINKED_BODY_ENTITY = "LINK002"
    # Orphan
    ORPHANED_RECORD = "ORPHAN001"
    # Stub
    STUB_RECORD = "STUB001"
    # Duplicate
    DUPLICATE_NAME = "DUP001"
    # Semantic drift (deterministic date/link checks)
    STALE_ACTIVE_PROJECT = "SEM001"
    STALE_TODO_TASK = "SEM002"
    STALE_ACTIVE_CONVERSATION = "SEM003"
    STALE_ACTIVE_PERSON = "SEM004"
    # Semantic (agent-detected, reserved)
    VAGUE_NOTE = "SEM005"
    DUPLICATE_SEMANTIC = "SEM006"


SEVERITY_MAP: dict[IssueCode, Severity] = {
    IssueCode.MISSING_REQUIRED_FIELD: Severity.CRITICAL,
    IssueCode.INVALID_TYPE_VALUE: Severity.CRITICAL,
    IssueCode.INVALID_STATUS_VALUE: Severity.WARNING,
    IssueCode.INVALID_FIELD_TYPE: Severity.WARNING,
    IssueCode.WRONG_DIRECTORY: Severity.WARNING,
    IssueCode.BROKEN_WIKILINK: Severity.CRITICAL,
    IssueCode.UNLINKED_BODY_ENTITY: Severity.WARNING,
    IssueCode.ORPHANED_RECORD: Severity.WARNING,
    IssueCode.STUB_RECORD: Severity.INFO,
    IssueCode.DUPLICATE_NAME: Severity.INFO,
    IssueCode.STALE_ACTIVE_PROJECT: Severity.WARNING,
    IssueCode.STALE_TODO_TASK: Severity.INFO,
    IssueCode.STALE_ACTIVE_CONVERSATION: Severity.WARNING,
    IssueCode.STALE_ACTIVE_PERSON: Severity.INFO,
    IssueCode.VAGUE_NOTE: Severity.INFO,
    IssueCode.DUPLICATE_SEMANTIC: Severity.INFO,
}


# --- Remediation classes — who, if anyone, can actually fix this ------
#
# The three sets below partition EVERY IssueCode by remediation owner.
# This is the segregation taxonomy behind the brief's split counts: the
# raw "N open issues" number conflates work janitor is failing to do with
# work janitor structurally CANNOT do, which makes the figure unreadable
# as a health signal.
#
# NOT a suppression mechanism. Every issue in every class is still
# detected, still carried in ``SweepResult.issues``, still reported. The
# split governs how the count is PRESENTED, nothing else — a
# scope-blocked issue is real and open, it is simply not a janitor
# backlog item.

# Routed to the LLM agent (see ``backends.AGENT_ACTIONABLE_CODES``,
# which is the enforcement copy used at dispatch).
_AGENT_CODES: frozenset[IssueCode] = frozenset({
    IssueCode.BROKEN_WIKILINK,       # LINK001 — fix when unambiguous
    IssueCode.DUPLICATE_NAME,        # DUP001  — triage task
    IssueCode.VAGUE_NOTE,            # SEM005  — agent judgment
    IssueCode.DUPLICATE_SEMANTIC,    # SEM006  — agent judgment
})

# Repaired deterministically by ``autofix._apply_fix``. These genuinely
# mutate the record (frontmatter repair / body-entity promotion).
AUTOFIX_FIXABLE_CODES: frozenset[IssueCode] = frozenset({
    IssueCode.MISSING_REQUIRED_FIELD,   # FM001
    IssueCode.INVALID_TYPE_VALUE,       # FM002
    IssueCode.INVALID_STATUS_VALUE,     # FM003
    IssueCode.INVALID_FIELD_TYPE,       # FM004
    IssueCode.UNLINKED_BODY_ENTITY,     # LINK002
})

# Detected correctly, but NO janitor path can remediate them:
#   * DIR001    — the fix is a move; janitor scope sets ``move: False``.
#   * STUB001   — enrichment belongs to the ``janitor_enrich`` scope.
#   * ORPHAN001 — flag-only in ``_apply_fix``; resolving it is an
#                 editorial decision (link it, or accept it as a leaf).
#   * SEM001-4  — flag-only; status changes are human-only per the SKILL.
# Real issues, correctly reported, not janitor's backlog.
NOT_JANITOR_FIXABLE_CODES: frozenset[IssueCode] = frozenset({
    IssueCode.WRONG_DIRECTORY,             # DIR001
    IssueCode.STUB_RECORD,                 # STUB001
    IssueCode.ORPHANED_RECORD,             # ORPHAN001
    IssueCode.STALE_ACTIVE_PROJECT,        # SEM001
    IssueCode.STALE_TODO_TASK,             # SEM002
    IssueCode.STALE_ACTIVE_CONVERSATION,   # SEM003
    IssueCode.STALE_ACTIVE_PERSON,         # SEM004
})

# Anything janitor can act on, by either path.
ACTIONABLE_CODES: frozenset[IssueCode] = _AGENT_CODES | AUTOFIX_FIXABLE_CODES


def classify_counts(issues: list[Issue]) -> dict[str, int]:
    """Split issues into actionable vs not-janitor-fixable counts.

    Returns ``{"actionable": N, "not_janitor_fixable": M, "total": N+M}``.
    Because the three sets partition the enum exhaustively (pinned in
    tests), ``actionable + not_janitor_fixable == total`` always holds —
    no issue is dropped on the floor by the split.
    """
    actionable = sum(1 for i in issues if i.code in ACTIONABLE_CODES)
    not_fixable = sum(1 for i in issues if i.code in NOT_JANITOR_FIXABLE_CODES)
    return {
        "actionable": actionable,
        "not_janitor_fixable": not_fixable,
        "total": len(issues),
    }


@dataclass
class Issue:
    code: IssueCode
    severity: Severity
    file: str  # relative path
    message: str
    detail: str = ""
    suggested_fix: str = ""

    def to_dict(self) -> dict:
        return {
            "code": self.code.value,
            "severity": self.severity.value,
            "file": self.file,
            "message": self.message,
            "detail": self.detail,
            "suggested_fix": self.suggested_fix,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Issue:
        return cls(
            code=IssueCode(d["code"]),
            severity=Severity(d["severity"]),
            file=d["file"],
            message=d["message"],
            detail=d.get("detail", ""),
            suggested_fix=d.get("suggested_fix", ""),
        )


@dataclass
class SweepResult:
    sweep_id: str
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    files_scanned: int = 0
    files_skipped: int = 0
    issues_found: int = 0
    issues_by_severity: dict[str, int] = field(default_factory=dict)
    # Segregation counts (see the remediation-class sets above). Both
    # default 0 so a state file written by an older version loads clean
    # under the schema-tolerance contract.
    issues_actionable: int = 0
    issues_not_janitor_fixable: int = 0
    files_fixed: int = 0
    files_deleted: int = 0
    agent_invoked: bool = False
    structural_only: bool = False
    issues: list[Issue] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "sweep_id": self.sweep_id,
            "timestamp": self.timestamp,
            "files_scanned": self.files_scanned,
            "files_skipped": self.files_skipped,
            "issues_found": self.issues_found,
            "issues_by_severity": self.issues_by_severity,
            "issues_actionable": self.issues_actionable,
            "issues_not_janitor_fixable": self.issues_not_janitor_fixable,
            "files_fixed": self.files_fixed,
            "files_deleted": self.files_deleted,
            "agent_invoked": self.agent_invoked,
            "structural_only": self.structural_only,
            "issues": [i.to_dict() for i in self.issues],
        }

    @classmethod
    def from_dict(cls, d: dict) -> SweepResult:
        issues = [Issue.from_dict(i) for i in d.get("issues", [])]
        # Coerce counter fields with ``value or 0`` rather than plain
        # ``.get(key, 0)``. ``.get`` only applies the default when the
        # key is MISSING; if the key exists with a ``None`` value (e.g.
        # from a half-written earlier state file) the None would
        # propagate and later crash status/history formatting that
        # assumes an int. Defense in depth — the daemon always writes
        # ints, but state loaded from disk should never surface None.
        return cls(
            sweep_id=d["sweep_id"],
            timestamp=d.get("timestamp", ""),
            files_scanned=d.get("files_scanned") or 0,
            files_skipped=d.get("files_skipped") or 0,
            issues_found=d.get("issues_found") or 0,
            issues_by_severity=d.get("issues_by_severity") or {},
            issues_actionable=d.get("issues_actionable") or 0,
            issues_not_janitor_fixable=d.get("issues_not_janitor_fixable") or 0,
            files_fixed=d.get("files_fixed") or 0,
            files_deleted=d.get("files_deleted") or 0,
            agent_invoked=bool(d.get("agent_invoked", False)),
            structural_only=bool(d.get("structural_only", False)),
            issues=issues,
        )


@dataclass
class FixLogEntry:
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    sweep_id: str = ""
    action: str = ""  # "fixed", "deleted", "flagged", "skipped"
    file: str = ""
    issue_code: str = ""
    detail: str = ""

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "sweep_id": self.sweep_id,
            "action": self.action,
            "file": self.file,
            "issue_code": self.issue_code,
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, d: dict) -> FixLogEntry:
        return cls(**d)
