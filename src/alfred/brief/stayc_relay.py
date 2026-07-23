"""Brief integration for the STAY-C bug-report relay spool.

STAY-C uses NO Telegram (standing operator rule 2026-07-16) and its clinical
sandbox cannot egress, so a bug report filed on the STAY-C PWA is a silent
sink unless something OUTSIDE the clinical unit surfaces it. The
``stayc_bug_watcher`` box component (task #4) does that surfacing: on every
bug-dir change it regenerates a **Salem-readable relay spool file** — a
whole-file snapshot of the currently-unresolved reports. THIS module is the
downstream half: Salem's Morning Brief reads that spool and renders one
PHI-free status line.

**The only thing that may cross into the brief is the COUNT.** The brief
transits Telegram; the no-Telegram rule means bug bodies / summaries / ids
must NEVER appear in it. The spool file can be in ``full`` mode (bodies
present in the file — the all-synthetic era) or ``locked`` mode (count + ids
only), but this reader parses ONLY the ``unresolved:`` and ``generated_at:``
header fields and never opens the body. Even a full-mode spool yields nothing
but the count. That is the load-bearing property of this module.

Intentionally-left-blank: a dead watcher must be VISIBLE, not silent. When
the spool file is absent, unreadable, or STALE (``generated_at`` older than
``staleness_hours``), the section renders an explicit "no data / stale" line
rather than omitting itself — so a watcher that stopped writing shows up in
the brief instead of the count just quietly vanishing.

The spool header format (a cross-component contract owned by
``stayc_bug_watcher.build_snapshot`` — parse defensively, do not re-derive)::

    # STAY-C bug reports — relay snapshot
    generated_at: 2026-07-18T03:20:10Z
    mode: locked
    unresolved: 3
    new_since_last: 1
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .utils import SectionReadStatus, get_logger, safe_read_section_file

log = get_logger(__name__)

SECTION_HEADER = "STAY-C Bug Relay"

# The spool's generated_at timestamp format (UTC, as written by
# ``stayc_bug_watcher._now_iso``). Parsed to compute staleness.
_TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def _parse_spool_header(text: str) -> tuple[int | None, datetime | None]:
    """Extract ``(unresolved_count, generated_at)`` from the spool header.

    Reads ONLY the two header fields the brief needs — never the report
    bodies / ids / summaries below the header (a full-mode spool carries
    bodies; they must not reach the brief). Defensive line scan rather than
    a YAML/frontmatter parse: the file is Markdown, not frontmatter, and the
    watcher owns the exact format. Returns ``None`` for either field that is
    absent or unparseable.
    """
    count: int | None = None
    generated_at: datetime | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if count is None and stripped.startswith("unresolved:"):
            raw = stripped[len("unresolved:"):].strip()
            try:
                count = int(raw)
            except ValueError:
                count = None
        elif generated_at is None and stripped.startswith("generated_at:"):
            raw = stripped[len("generated_at:"):].strip()
            try:
                generated_at = datetime.strptime(raw, _TS_FORMAT).replace(
                    tzinfo=timezone.utc,
                )
            except ValueError:
                generated_at = None
        # Stop once we have both — the fields we need are in the header, and
        # we must NOT descend into the body (full-mode bodies live below).
        if count is not None and generated_at is not None:
            break
    return count, generated_at


def render_stayc_bug_relay_section(config, now_utc: datetime) -> str:
    """Render the STAY-C bug-relay status line, or ``""`` when disabled.

    Args:
        config: A ``StaycBugRelayConfig`` (``enabled`` / ``spool_path`` /
            ``staleness_hours``).
        now_utc: Current time (UTC) — passed in for deterministic staleness
            tests, mirroring the tier section's ``now`` injection.

    Returns:
        Markdown for the section body, or ``""`` when the feature is disabled
        (the daemon omits the section header entirely in that case — the one
        permitted silence, matching Watch Items / Peer Digests). When
        enabled, ALWAYS returns a non-empty line (intentionally-left-blank):
        the count when fresh, or an explicit no-data / stale line otherwise.
    """
    if not config.enabled:
        return ""

    spool_path = (config.spool_path or "").strip()
    if not spool_path:
        # Enabled but no path configured — a config mistake the operator must
        # SEE, not a silent omission (the spool path is deployment-specific,
        # so there is deliberately no baked-in default).
        log.warning("brief.stayc_relay", state="unconfigured")
        return (
            "**STAY-C bug relay: not configured** — set "
            "`brief.stayc_bug_relay.spool_path` to the watcher's relay file."
        )

    path = Path(spool_path).expanduser()
    # Defensive read via the shared helper — it catches FileNotFoundError,
    # other OSError, AND UnicodeDecodeError (which subclasses ValueError, not
    # OSError; an escaping decode error on a corrupted spool would kill the
    # whole brief, since the daemon calls this render bare). Each outcome maps
    # to its own operator-facing no-data line.
    read = safe_read_section_file(path)
    if read.status is SectionReadStatus.NOT_FOUND:
        # Watcher never wrote / wrong path — visible, not silent.
        log.info("brief.stayc_relay", state="absent", spool_path=spool_path)
        return (
            "**STAY-C bug relay: no data** — spool file not found "
            f"(`{spool_path}`). The box watcher may not be running."
        )
    if read.status is SectionReadStatus.OS_ERROR:
        log.warning(
            "brief.stayc_relay", state="unreadable",
            spool_path=spool_path, error=read.detail,
        )
        return (
            "**STAY-C bug relay: no data** — spool file unreadable "
            f"(`{spool_path}`)."
        )
    if read.status is SectionReadStatus.DECODE_ERROR:
        # A corrupted / non-UTF-8 spool.
        log.warning(
            "brief.stayc_relay", state="unreadable",
            spool_path=spool_path, error=read.detail,
        )
        return (
            "**STAY-C bug relay: no data** — spool file unreadable "
            "(not UTF-8)."
        )

    count, generated_at = _parse_spool_header(read.text)

    if count is None or generated_at is None:
        # Present but the header we depend on didn't parse — treat as no data
        # (never trust a body we can't verify a fresh header on).
        log.warning(
            "brief.stayc_relay", state="unreadable", spool_path=spool_path,
            detail="header missing unresolved/generated_at",
        )
        return (
            "**STAY-C bug relay: no data** — spool present but its header "
            "could not be parsed."
        )

    age_hours = (now_utc - generated_at).total_seconds() / 3600.0
    if age_hours > config.staleness_hours:
        # A watcher that stopped writing must be visible — a stale count is
        # worse than no count because it looks live.
        log.warning(
            "brief.stayc_relay", state="stale", count=count,
            age_hours=round(age_hours, 1), spool_path=spool_path,
        )
        return (
            f"**STAY-C bug relay: stale** — last update "
            f"{generated_at.strftime(_TS_FORMAT)} "
            f"({int(age_hours)}h ago, threshold {config.staleness_hours}h). "
            "The box watcher may have stopped."
        )

    # Fresh header → render the PHI-free count (and nothing else).
    log.info("brief.stayc_relay", state="fresh", count=count)
    if count == 0:
        return "STAY-C: no unresolved bug reports."
    plural = "report" if count == 1 else "reports"
    return f"STAY-C: {count} unresolved bug {plural}."


# --- STAY-C Retention Review Relay (task #13 §4 / C3) --------------------------

RETENTION_SECTION_HEADER = "STAY-C Retention Review"


def _parse_retention_spool_header(text: str) -> tuple[int | None, str, datetime | None, bool]:
    """``(review_due, oldest_encounter_id, generated_at, surfaced)`` from the retention review spool
    header — the PHI-free fields the sweep's ``_write_review_spool`` writes. ``review_due`` /
    ``generated_at`` are ``None`` when absent/unparseable; ``oldest_encounter_id`` is an OPAQUE
    salted-HMAC id (safe to render); ``surfaced`` (default False when absent — fail-safe: an older
    spool without the field is treated as did-not-evaluate, never a false all-clear). Defensive line
    scan (the sweep owns the exact format), never a body descent."""
    review_due: int | None = None
    oldest = ""
    generated_at: datetime | None = None
    surfaced = False
    for line in text.splitlines():
        s = line.strip()
        if review_due is None and s.startswith("review_due:"):
            try:
                review_due = int(s[len("review_due:"):].strip())
            except ValueError:
                review_due = None
        elif s.startswith("surfaced:"):
            surfaced = s[len("surfaced:"):].strip().lower() == "true"
        elif not oldest and s.startswith("oldest_encounter_id:"):
            oldest = s[len("oldest_encounter_id:"):].strip()
        elif generated_at is None and s.startswith("generated_at:"):
            raw = s[len("generated_at:"):].strip()
            try:
                generated_at = datetime.strptime(raw, _TS_FORMAT).replace(tzinfo=timezone.utc)
            except ValueError:
                generated_at = None
    return review_due, oldest, generated_at, surfaced


def render_stayc_retention_relay_section(config, now_utc: datetime) -> str:
    """Render the STAY-C retention-review status line (§4 morning-review surface, C3), or ``""`` when
    disabled. PHI-free: ONLY the ``review_due`` count + the OPAQUE oldest encounter_id cross into the
    (Telegram-transiting) brief — never encounter labels/bodies. ILB: enabled ALWAYS returns a line
    (the count, or an explicit no-data / stale signal so a dead box sweep is visible)."""
    if not config.enabled:
        return ""

    spool_path = (config.spool_path or "").strip()
    if not spool_path:
        log.warning("brief.stayc_retention_relay", state="unconfigured")
        return (
            "**STAY-C retention relay: not configured** — set "
            "`brief.stayc_retention_relay.spool_path` to the sweep's review spool."
        )

    path = Path(spool_path).expanduser()
    # E6 merge-fold: defensive read via the shared helper (arrived from master's #25 arc) — it catches
    # FileNotFoundError, other OSError, AND UnicodeDecodeError (which subclasses ValueError, not
    # OSError; an escaping decode error on a corrupted spool would kill the whole brief since the
    # daemon calls this render bare). Each outcome maps to its own operator-facing no-data line.
    read = safe_read_section_file(path)
    if read.status is SectionReadStatus.NOT_FOUND:
        log.info("brief.stayc_retention_relay", state="absent", spool_path=spool_path)
        return (
            "**STAY-C retention relay: no data** — review spool not found "
            f"(`{spool_path}`). The box sweep may not be running / synced."
        )
    if read.status is SectionReadStatus.OS_ERROR:
        log.warning("brief.stayc_retention_relay", state="unreadable",
                    spool_path=spool_path, error=read.detail)
        return f"**STAY-C retention relay: no data** — review spool unreadable (`{spool_path}`)."
    if read.status is SectionReadStatus.DECODE_ERROR:
        log.warning("brief.stayc_retention_relay", state="unreadable", spool_path=spool_path)
        return "**STAY-C retention relay: no data** — review spool unreadable (not UTF-8)."

    review_due, oldest, generated_at, surfaced = _parse_retention_spool_header(read.text)
    if review_due is None or generated_at is None:
        log.warning("brief.stayc_retention_relay", state="unreadable", spool_path=spool_path,
                    detail="header missing review_due/generated_at")
        return (
            "**STAY-C retention relay: no data** — review spool present but its header "
            "could not be parsed."
        )

    age_hours = (now_utc - generated_at).total_seconds() / 3600.0
    if age_hours > config.staleness_hours:
        log.warning("brief.stayc_retention_relay", state="stale", review_due=review_due,
                    age_hours=round(age_hours, 1), spool_path=spool_path)
        return (
            f"**STAY-C retention relay: stale** — last update "
            f"{generated_at.strftime(_TS_FORMAT)} "
            f"({int(age_hours)}h ago, threshold {config.staleness_hours}h). "
            "The box sweep may have stopped."
        )

    # E3/R5: a FRESH spool whose surfacing did NOT run this sweep must NOT read as an all-clear —
    # review_due is UNKNOWN, not zero. The spool carries only surfaced=false; it does NOT record WHICH
    # of the sweep's THREE not-surfaced causes fired (no/corrupt schedule → no_schedule_published /
    # schedule_load_failed; an unenumerable sealed-blob store → review_enumeration_failed; an unreadable
    # clinical chain, the over-window AGE BASIS → review_basis_unavailable). So the render names ALL
    # THREE + their DISTINCT remediations — "publish the schedule" is correct for ONLY the first and
    # would mis-point the operator for the other two — and points at the daemon-log latch that DOES name
    # the exact one. (If the spool is ever extended to thread the specific cause, collapse this to the
    # named cause + its single remediation.)
    if not surfaced:
        log.info("brief.stayc_retention_relay", state="not_surfaced")
        return (
            "**STAY-C retention: review not evaluated** — the box sweep is alive but over-window "
            "surfacing did not run, so review_due is UNKNOWN (NOT an all-clear). One of three causes: "
            "(1) no s.50 schedule is published, or the published one is corrupt — publish/repair it via "
            "`alfred scribe retention schedule publish`; (2) the sealed-blob store could not be "
            "enumerated — check the retained/blob dir's readability + permissions; (3) the clinical "
            "chain could not be read to date the blobs — check the clinical event store's health + "
            "permissions. The daemon log's latched `scribe.retention.sweep.*` signal names which one."
        )

    log.info("brief.stayc_retention_relay", state="fresh", review_due=review_due)
    if review_due == 0:
        return "STAY-C retention: no encounters over the s.50 review window."
    plural = "encounter" if review_due == 1 else "encounters"
    tail = f" (oldest: {oldest})" if oldest else ""
    return (
        f"STAY-C retention: {review_due} {plural} over the s.50 review window{tail} — "
        "review + run the destroy playbook (§5) as warranted."
    )


# --- STAY-C Negation-Paraphrase Review Relay (#26 Phase 3) --------------------

NEGATION_SECTION_HEADER = "STAY-C Negation Review"


def _parse_negation_spool_header(text: str) -> tuple[int | None, datetime | None]:
    """``(pending, generated_at)`` from the #26 negation-review spool header — the PHI-free fields the
    sweep's ``_write_negation_review_spool`` writes (a bare COUNT + generated_at, NEVER a concept-set).
    ``None`` for either field absent/unparseable. Defensive line scan (the sweep owns the format)."""
    pending: int | None = None
    generated_at: datetime | None = None
    for line in text.splitlines():
        s = line.strip()
        if pending is None and s.startswith("pending:"):
            try:
                pending = int(s[len("pending:"):].strip())
            except ValueError:
                pending = None
        elif generated_at is None and s.startswith("generated_at:"):
            raw = s[len("generated_at:"):].strip()
            try:
                generated_at = datetime.strptime(raw, _TS_FORMAT).replace(tzinfo=timezone.utc)
            except ValueError:
                generated_at = None
        if pending is not None and generated_at is not None:
            break
    return pending, generated_at


def render_stayc_negation_relay_section(config, now_utc: datetime) -> str:
    """Render the STAY-C negation-paraphrase review status line (#26 Phase-3 morning-review surface),
    or ``""`` when disabled. PHI-FREE: ONLY the pending COUNT crosses into the (Telegram-transiting)
    brief — never a concept-set (the PHI pairs stay on-box in ``alfred scribe negation-candidates``).
    ILB: enabled ALWAYS returns a line (the count — INCLUDING an explicit '0 awaiting review' — or a
    no-data / stale signal so a dead box sweep is visible; idle ≠ broken)."""
    if not config.enabled:
        return ""

    spool_path = (config.spool_path or "").strip()
    if not spool_path:
        log.warning("brief.stayc_negation_relay", state="unconfigured")
        return (
            "**STAY-C negation relay: not configured** — set "
            "`brief.stayc_negation_relay.spool_path` to the sweep's negation-review spool."
        )

    path = Path(spool_path).expanduser()
    read = safe_read_section_file(path)
    if read.status is SectionReadStatus.NOT_FOUND:
        log.info("brief.stayc_negation_relay", state="absent", spool_path=spool_path)
        return (
            "**STAY-C negation relay: no data** — negation-review spool not found "
            f"(`{spool_path}`). The box sweep may not be running / synced."
        )
    if read.status is SectionReadStatus.OS_ERROR:
        log.warning("brief.stayc_negation_relay", state="unreadable",
                    spool_path=spool_path, error=read.detail)
        return f"**STAY-C negation relay: no data** — negation-review spool unreadable (`{spool_path}`)."
    if read.status is SectionReadStatus.DECODE_ERROR:
        log.warning("brief.stayc_negation_relay", state="unreadable", spool_path=spool_path)
        return "**STAY-C negation relay: no data** — negation-review spool unreadable (not UTF-8)."

    pending, generated_at = _parse_negation_spool_header(read.text)
    if pending is None or generated_at is None:
        log.warning("brief.stayc_negation_relay", state="unreadable", spool_path=spool_path,
                    detail="header missing pending/generated_at")
        return (
            "**STAY-C negation relay: no data** — negation-review spool present but its header "
            "could not be parsed."
        )

    age_hours = (now_utc - generated_at).total_seconds() / 3600.0
    if age_hours > config.staleness_hours:
        log.warning("brief.stayc_negation_relay", state="stale", pending=pending,
                    age_hours=round(age_hours, 1), spool_path=spool_path)
        return (
            f"**STAY-C negation relay: stale** — last update "
            f"{generated_at.strftime(_TS_FORMAT)} "
            f"({int(age_hours)}h ago, threshold {config.staleness_hours}h). "
            "The box sweep may have stopped."
        )

    log.info("brief.stayc_negation_relay", state="fresh", pending=pending)
    if pending == 0:
        return "STAY-C negation: 0 paraphrase candidates awaiting review."   # ILB — idle ≠ broken
    plural = "candidate" if pending == 1 else "candidates"
    return (
        f"STAY-C negation: {pending} paraphrase {plural} awaiting review — "
        "run `alfred scribe negation-candidates` on the box to approve/reject."
    )
