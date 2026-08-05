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
# The four sets below partition EVERY IssueCode by remediation owner.
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
# which is the enforcement copy used at dispatch — the two must stay in
# lockstep, and a pin in tests/test_janitor_scanner_falsepos.py holds the
# dispatch copy to exactly these codes).
_AGENT_CODES: frozenset[IssueCode] = frozenset({
    IssueCode.BROKEN_WIKILINK,       # LINK001 — fix when unambiguous
    IssueCode.DUPLICATE_NAME,        # DUP001  — triage task
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

# Vocabulary, not workload (#47 / review-25 O2). SEM005 and SEM006 have NO
# producer: no scanner emits them, so they can never appear in an issue list,
# never be routed, and never be counted. They exist as the two labels the
# agent applies to a judgment it forms on its own while working a record it
# was sent for some OTHER code — which is exactly what the SKILL says
# ("You will never be handed one of these", § SEM005–SEM006).
#
# They were previously listed in the agent-actionable allowlist, which read as
# "the agent is routed these" and was never true. Demoting them here is the
# code catching up to the prompt, not a behaviour change: the dispatch filter
# is an intersection against a list that never contains them, so removing them
# changes nothing that runs. It removes a false claim about what the system
# does.
#
# Kept as a NAMED class rather than deleted from the taxonomy so the
# every-code-is-classified pin still covers them — an unclassified code must
# fail loudly, and "this one is deliberately unroutable" is a classification.
LABEL_ONLY_CODES: frozenset[IssueCode] = frozenset({
    IssueCode.VAGUE_NOTE,          # SEM005
    IssueCode.DUPLICATE_SEMANTIC,  # SEM006
})

# Anything janitor can act on, by either path.
ACTIONABLE_CODES: frozenset[IssueCode] = _AGENT_CODES | AUTOFIX_FIXABLE_CODES

# Everything janitor cannot remediate, for COUNTING purposes. Two different
# reasons, one operator-facing bucket: the scope-blocked codes above, plus the
# label-only vocabulary. If a SEM005 ever did materialise it would be
# unroutable, so "not janitor-fixable" is the honest bucket for it — and
# folding it in keeps ``actionable + not_janitor_fixable == total`` true over
# EVERY code, so no issue can fall out of both buckets and vanish.
UNFIXABLE_CODES: frozenset[IssueCode] = (
    NOT_JANITOR_FIXABLE_CODES | LABEL_ONLY_CODES
)


#: The known-debt cohort marker (#19-B, D2 ruled). A LINK001 whose record
#: already carries a ``LINK001``-prefixed janitor_note has been looked at
#: before: real dangling links, already triaged, deliberately not
#: actionable-today. Counting them inside ``actionable`` let one historical
#: cohort dominate the bucket and hide the handful of FRESH breaks that a
#: person should actually look at this morning.
#:
#: The marker is not invented here — it is the note the agent already writes
#: under the SKILL's LINK001 procedure, and the #30 SKILL guard protects that
#: population from being overwritten *precisely because* it is a cohort marker.
#: This layer keys on the same thing the guard protects, so the two cannot
#: drift apart.
#:
#: BARE PREFIX, deliberately — no dash. Measured across the vault (8,614
#: records, 1,674 with a janitor_note): 1,407 notes start with ``LINK001``,
#: of which 1,372 continue with an EM-dash (``LINK001 —``) and 35 with no
#: dash at all (``LINK001 scanner false positive: …``). An earlier draft of
#: this constant guessed ``"LINK001 --"`` (two hyphens) and would have matched
#: ZERO records — rendering a permanent "cohort: 0" on the very line that
#: exists to stop a number from lying. Matching the code and nothing else is
#: also what the SKILL guard does ("unless that note starts with LINK001"),
#: which is why punctuation drift in the agent's prose cannot break this.
COHORT_NOTE_PREFIX = "LINK001"


def is_known_cohort_issue(issue: Issue, janitor_note: str | None) -> bool:
    """Is this LINK001 part of the known-debt cohort?

    Keyed on the RECORD's existing janitor_note, not on the issue — the issue
    is regenerated from scratch every sweep and carries no history, so the note
    is the only durable cohort evidence. A non-LINK001 issue is never cohort,
    whatever note the record carries: the cohort is a LINK001 backlog, and
    admitting other codes would quietly shrink the actionable bucket for
    reasons nobody asked for.

    Self-limiting in the useful direction: a record whose note reads
    ``LINK001 — resolved: retargeted`` still carries the marker, but once the
    link is genuinely fixed the scanner emits no LINK001 for it, so it
    contributes nothing. The note only ever discriminates among records that
    have a LIVE broken link right now.
    """
    if issue.code is not IssueCode.BROKEN_WIKILINK:
        return False
    return (janitor_note or "").lstrip().startswith(COHORT_NOTE_PREFIX)


def classify_counts(
    issues: list[Issue],
    *,
    cohort_notes: dict[str, str] | None = None,
) -> dict[str, int]:
    """Split issues into the operator-facing buckets.

    Returns ``{"actionable", "not_janitor_fixable", "known_cohort", "total"}``.

    ``not_janitor_fixable`` counts ``UNFIXABLE_CODES`` — the scope-blocked
    codes plus the label-only vocabulary — so the sum covers every member of
    the enum and nothing can fall out of both buckets.

    ``actionable + not_janitor_fixable == total`` STILL holds — the cohort is a
    SUBSET of actionable, reported alongside it rather than carved out of the
    partition. That choice is deliberate: the cohort is genuinely fixable
    (the #44 ``link001_repair`` campaign drains it), so removing it from
    ``actionable`` would understate what the system can still do. It is broken
    out because it is *known debt being drained on a schedule*, not because it
    is unfixable.

    Two views of one number, and they must not read as double-counting:

      * this line is DEBT STANDING — "how much of today's actionable bucket is
        the historical cohort";
      * the drip-drain campaign line is DRAIN PROGRESS — "how fast it is going
        down".

    As the campaign drains, this count falls and the campaign's ``remaining``
    falls in step. They are the same population seen from two angles.

    ``cohort_notes`` maps record path → that record's ``janitor_note``. Absent
    (the default) the cohort count is 0 and the other buckets are unchanged —
    so a caller that hasn't wired the notes gets the pre-#19-B behaviour rather
    than a silently wrong cohort figure.
    """
    actionable = sum(1 for i in issues if i.code in ACTIONABLE_CODES)
    not_fixable = sum(1 for i in issues if i.code in UNFIXABLE_CODES)
    notes = cohort_notes or {}
    cohort = sum(
        1 for i in issues if is_known_cohort_issue(i, notes.get(i.file))
    )
    return {
        "actionable": actionable,
        "not_janitor_fixable": not_fixable,
        "known_cohort": cohort,
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
    #: #19-B — the known-debt LINK001 cohort. A SUBSET of
    #: ``issues_actionable``, not a carve-out from it (see
    #: ``classify_counts``). Falls as the #44 link001_repair campaign
    #: drains it, which is the same population that campaign's
    #: ``remaining`` tracks — debt-standing here, drain-progress there.
    issues_known_cohort: int = 0
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
            "issues_known_cohort": self.issues_known_cohort,
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
            issues_known_cohort=d.get("issues_known_cohort") or 0,
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
