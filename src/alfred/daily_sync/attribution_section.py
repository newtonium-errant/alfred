"""Attribution-audit section provider — Phase 2 of the calibration audit arc.

c1 (``src/alfred/vault/attribution.py``) shipped the marker primitives.
c2 (``src/alfred/telegram/conversation.py``) wired Salem's vault_create
+ vault_edit body_append callsites so every agent-inferred body lands
with a BEGIN_INFERRED/END_INFERRED wrap and an ``attribution_audit``
frontmatter entry. That closed the WRITE half of the audit gap.

This module closes the READ half: every Daily Sync, Salem surfaces up
to N unconfirmed audit entries as a numbered batch Andrew can confirm
("6 confirm" → flip ``confirmed_by_andrew`` true) or reject
("6 reject" → strip the marked section + drop the entry). Without this
read path the markers go in but nothing ever acts on them.

Sampling strategy::

    1. Walk ``vault/**/*.md`` (or just ``daily_sync.attribution.scan_paths``
       when configured) and parse each file's ``attribution_audit``
       frontmatter via ``parse_audit_entries``.
    2. Keep entries where ``confirmed_by_andrew is False`` AND
       ``confirmed_at is None`` — anything already-confirmed stays
       silent, which is the intentionally-left-blank steady state.
    3. Sort by ``date`` descending (most recent unconfirmed first) so
       Andrew sees fresh markers before stale ones.
    4. Cap at ``daily_sync.attribution.batch_size`` (default 5).

Item rendering (matches the spec in the c3 task):

    6. [salem 2026-04-23 18:44 — note/Marker Smoke Test]
       Section: "Marker Smoke Test"
       Content: "Testing the attribution audit marker. ..."
       Reason: talker conversation turn (session=78a7c5a2)

The leading number is GLOBAL across the Daily Sync — the assembler
passes ``start_index`` so attribution items pick up where email
calibration left off (5 email items → attribution starts at 6).

Empty state: emits ``"## Attribution audit\\n\\nNo attribution items
pending review.\\n"`` per the intentionally-left-blank principle from
``feedback_intentionally_left_blank.md`` — silence is a bug, an
explicit "nothing to do" is observability.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import frontmatter
import structlog

from alfred.vault.attribution import (
    CONFIRMED_VIA_BACKFILL,
    CONFIRMED_VIA_TIMEOUT,
    AuditEntry,
    confirm_marker,
    parse_audit_entries,
)
from alfred.vault.paths import vault_relative

from .attribution_corpus import AttributionCorpusEntry, append_entry
from .attribution_quality import (
    DEFAULT_WINDOW_DAYS,
    attribution_quality_stats,
    render_quality_line,
)
from .confidence import load_state, save_state
from .config import DailySyncConfig

log = structlog.get_logger(__name__)


# Default attribution config when the ``daily_sync.attribution`` block is
# absent — enabled, batch of 5, scan the whole vault. Mirrored in
# ``config.yaml.example`` so the default behaviour is documented.
_DEFAULT_BATCH_SIZE = 5

# --- #63a auto-confirm policy ------------------------------------------------
# How long an unconfirmed entry sits before the sweep confirms it on the
# operator's behalf. The ruling is 24 hours.
#
# TIMING DEVIATION, stated rather than buried: the sweep runs on the daily-sync
# schedule, so the EFFECTIVE window is 24-48h from the entry's date, not exactly
# 24h — an entry created just after one fire waits nearly a full day before the
# next fire can even look at it, and only then is it 24h old. This matches the
# ruling's intent ("nobody objected within a day") and is the honest cost of
# hosting the sweep on an existing daily job rather than adding a timer.
AUTO_CONFIRM_AFTER_HOURS = 24

# State key holding the moment the auto-confirm policy first ran on this
# instance. Stamped ONCE, never moved: it is the line that separates "existed
# before the policy" (backfill) from "lived under the policy" (timeout_24h).
POLICY_START_STATE_KEY = "attribution_policy_start_at"

# The ONE sweep event. Named as a constant because the operator's grep is a
# consumer: a silent rename would strand it.
SWEEP_EVENT = "daily_sync.attribution.auto_confirm_sweep"


@dataclass
class AttributionItem:
    """One item in a Daily Sync attribution-audit batch.

    All fields are display-derived from the underlying audit entry +
    the wrapped body content. Persisted into the state file's
    ``last_batch.attribution_items`` list so the reply dispatcher can
    resolve "item 6" → ``(record_path, marker_id)`` without re-reading
    the underlying record.
    """

    item_number: int  # 1-indexed, GLOBAL across Daily Sync sections
    record_path: str  # vault-relative
    marker_id: str
    agent: str
    date: str  # ISO 8601 from the audit entry
    section_title: str
    reason: str
    content_preview: str  # first ~140 chars of the wrapped body
    # #63a — the operator contested this inference. Carried into the feed
    # item's evidence so the producer can re-derive the needs-you tier on
    # EVERY sync, rather than the revert living only in one store write that
    # the next reconcile would flatten back to FYI.
    contested: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_number": self.item_number,
            "record_path": self.record_path,
            "marker_id": self.marker_id,
            "agent": self.agent,
            "date": self.date,
            "section_title": self.section_title,
            "reason": self.reason,
            "content_preview": self.content_preview,
            "contested": self.contested,
        }


@dataclass
class _Candidate:
    record_path: str  # vault-relative
    entry: AuditEntry
    content_preview: str
    parsed_date: datetime | None  # for sorting


def _attribution_settings(config: DailySyncConfig) -> tuple[bool, int, list[str]]:
    """Return ``(enabled, batch_size, scan_paths)`` for attribution.

    The c2 ``DailySyncConfig`` dataclass doesn't yet carry an
    ``attribution`` block — when it lands, this helper will pull from
    ``config.attribution.*``. Until then we read defensively from the
    raw ``getattr`` so a tests-only ``DailySyncConfig`` that doesn't
    set the block still works (default: enabled, batch 5, full vault).
    """
    block = getattr(config, "attribution", None)
    if block is None:
        return (True, _DEFAULT_BATCH_SIZE, [])
    enabled = bool(getattr(block, "enabled", True))
    batch_size = int(getattr(block, "batch_size", _DEFAULT_BATCH_SIZE))
    scan_paths = list(getattr(block, "scan_paths", []) or [])
    return (enabled, batch_size, scan_paths)


def _parse_iso(date_str: str) -> datetime | None:
    """Tolerant ISO-8601 parser. Returns ``None`` on failure (so the
    candidate sorts last in a stable way)."""
    if not date_str:
        return None
    try:
        # ``fromisoformat`` handles the offsets we emit (``+00:00``)
        # and naive forms.
        return datetime.fromisoformat(date_str)
    except ValueError:
        return None


def _content_preview(body: str, marker_id: str, *, limit: int = 140) -> str:
    """Extract the wrapped content for ``marker_id`` from ``body``.

    Returns the first ``limit`` chars of the content between BEGIN/END
    markers, with whitespace collapsed. Returns an empty string when
    the marker isn't found in body — defensive for cases where the
    audit entry is in frontmatter but the body has been edited to
    remove the marker (Andrew may have manually cleaned up).
    """
    from alfred.vault.attribution import find_marker_bounds

    if not body:
        return ""
    bounds = find_marker_bounds(body, marker_id)
    if bounds is None:
        return ""
    begin, end = bounds
    lines = body.splitlines()
    inner = "\n".join(lines[begin + 1: end])
    text = " ".join(inner.split())
    if len(text) > limit:
        text = text[: limit - 3].rstrip() + "..."
    return text


def _short_record_label(record_path: str) -> str:
    """Trim ``note/Foo.md`` → ``note/Foo`` for the rendered header."""
    if record_path.endswith(".md"):
        return record_path[:-3]
    return record_path


def _short_date(iso_date: str) -> str:
    """Render the audit entry date as ``YYYY-MM-DD HH:MM`` (UTC).

    Falls back to the raw string when parsing fails so we don't drop
    the date entirely.
    """
    parsed = _parse_iso(iso_date)
    if parsed is None:
        return iso_date
    return parsed.strftime("%Y-%m-%d %H:%M")


def _walk_vault(vault_path: Path, scan_paths: list[str]) -> Iterable[Path]:
    """Yield every ``*.md`` file to scan.

    When ``scan_paths`` is empty, walks the whole vault. Otherwise
    walks each subpath (joined to vault_path). Ignores hidden dirs
    (``.obsidian`` etc.) and the conventional ``_templates`` /
    ``_bases`` scaffolding so the scan doesn't trip on records
    whose ``attribution_audit`` field is illustrative not real.
    """
    skip_dirs = {".obsidian", ".git", "_templates", "_bases", "_docs"}
    roots: list[Path]
    if scan_paths:
        roots = [vault_path / p for p in scan_paths]
    else:
        roots = [vault_path]
    seen: set[Path] = set()
    for root in roots:
        if not root.exists():
            continue
        if root.is_file():
            if root.suffix == ".md":
                yield root
            continue
        for md in root.rglob("*.md"):
            # Skip if any ancestor is a skip_dir relative to the vault.
            #
            # Arc #18 M6: via ``vault_relative``, which resolves BOTH sides.
            # Bare ``relative_to`` is lexical, so it raises the moment either
            # side is resolved while the other carries the configured spelling
            # — production's shape (the vault is configured through a symlink).
            #
            # The old ``except ValueError: rel_parts = md.parts`` fallback was
            # NOT the "skip dirs stop matching" hazard it was flagged as: the
            # absolute parts are a suffix-superset of the relative ones, so
            # every skip_dir in the relative portion still matched. The real
            # exposure was the opposite — OVER-skipping, if a component of the
            # vault root ever collided with a skip_dir name, which would
            # silently skip the whole vault. Resolving both sides removes the
            # fallback entirely rather than picking a better one.
            rel_parts = tuple(vault_relative(vault_path, md).split("/"))
            if any(part in skip_dirs for part in rel_parts):
                continue
            if md in seen:
                continue
            seen.add(md)
            yield md


def _read_candidates(vault_path: Path, scan_paths: list[str]) -> list[_Candidate]:
    """Walk the vault, parse audit entries, return unconfirmed candidates."""
    candidates: list[_Candidate] = []
    for md_file in _walk_vault(vault_path, scan_paths):
        try:
            post = frontmatter.load(str(md_file))
        except Exception:
            # Malformed YAML or unreadable file — skip with a log.
            log.info(
                "daily_sync.attribution.read_failed",
                path=str(md_file),
            )
            continue
        fm = post.metadata or {}
        entries = parse_audit_entries(fm)
        if not entries:
            continue
        body = post.content or ""
        # Arc #18 M6 — load-bearing, not hygiene. This string becomes
        # ``_Candidate.record_path``, which is persisted into ``last_batch`` and
        # later fed to ``reply_dispatch._resolve_attribution_correction`` — a
        # writer that now runs it through ``resolve_in_vault``.
        #
        # This comment used to say that gate "REFUSES an absolute path by
        # design". It does not, and #39 retired the claim: an absolute path is
        # honoured iff it resolves INSIDE the vault. Measured on a symlinked
        # root, the absolute path the old ``except ValueError: rel_path =
        # str(md_file)`` fallback would emit is ACCEPTED by the gate — so the
        # stated hazard (a legitimate confirm turning into a refusal) is not
        # the real one either.
        #
        # The real reason to keep ``vault_relative`` is that this string is
        # PERSISTED into ``last_batch``. An absolute path bakes the host's
        # layout into state: it stops resolving the moment the vault moves or
        # its symlink is repointed, and it is meaningless on another instance.
        # The relative form survives both. The ValueError the old fallback
        # caught is real — ``md_file.resolve().relative_to(vault_path)`` raises
        # under a symlinked root — and ``vault_relative`` resolves BOTH sides,
        # which is what actually fixes it.
        rel_path = vault_relative(vault_path, md_file)
        for entry in entries:
            if entry.confirmed_by_andrew or entry.confirmed_at is not None:
                continue
            preview = _content_preview(body, entry.marker_id)
            parsed = _parse_iso(entry.date)
            candidates.append(_Candidate(
                record_path=rel_path,
                entry=entry,
                content_preview=preview,
                parsed_date=parsed,
            ))
    return candidates


def _sort_key(candidate: _Candidate) -> tuple[int, datetime, str, str]:
    """Sort newest-first; missing dates sort last; record_path tiebreaks."""
    parsed = candidate.parsed_date
    if parsed is None:
        # Use a sentinel that sorts AFTER any real date — the negation
        # below (descending order) flips it back to "last".
        return (1, datetime.min, candidate.record_path, candidate.entry.marker_id)
    return (0, parsed, candidate.record_path, candidate.entry.marker_id)


def build_batch(
    vault_path: Path,
    config: DailySyncConfig,
    *,
    start_index: int = 1,
) -> list[AttributionItem]:
    """Sample a batch and return it as :class:`AttributionItem` rows.

    Public surface for the daemon and any future ``/attribution_audit``
    slash command. Returns ``[]`` when the vault has nothing
    unconfirmed (the steady state once Andrew is caught up).

    ``start_index`` (1-based, GLOBAL across Daily Sync sections) lets
    the assembler keep numbering continuous — when email calibration
    rendered 5 items, attribution starts at 6.
    """
    enabled, batch_size, scan_paths = _attribution_settings(config)
    if not enabled or batch_size <= 0:
        return []
    candidates = _read_candidates(vault_path, scan_paths)
    if not candidates:
        return []
    # Sort newest-first. ``_sort_key`` returns a tuple that sorts
    # oldest-first by ``(parsed,)``, so we reverse for newest-first.
    sorted_candidates = sorted(
        candidates,
        key=_sort_key,
        reverse=True,
    )
    # ``reverse=True`` flips the missing-date sentinel too — re-correct
    # by partitioning so dated items lead and undated trail.
    dated = [c for c in sorted_candidates if c.parsed_date is not None]
    undated = [c for c in sorted_candidates if c.parsed_date is None]
    # ``dated`` is currently newest-first; ``undated`` we keep stable
    # by record_path order (deterministic for the same vault state).
    undated.sort(key=lambda c: (c.record_path, c.entry.marker_id))
    ordered = dated + undated
    chosen = ordered[:batch_size]
    return [
        AttributionItem(
            item_number=start_index + i,
            record_path=c.record_path,
            marker_id=c.entry.marker_id,
            agent=c.entry.agent,
            date=c.entry.date,
            section_title=c.entry.section_title,
            reason=c.entry.reason,
            content_preview=c.content_preview,
            contested=c.entry.contested,
        )
        for i, c in enumerate(chosen)
    ]


# ---------------------------------------------------------------------------
# #63a — the 24h auto-confirm sweep
# ---------------------------------------------------------------------------


@dataclass
class SweepResult:
    """Counts from one auto-confirm sweep. Every field is also logged.

    ``audited`` is every UNCONFIRMED entry the walk saw — the denominator. The
    rest partition it: entries that were confirmed this run (``auto_confirmed``,
    split into ``timed_out`` + ``backfilled``), entries deliberately left alone
    (``contested_preserved``, ``undated_preserved``), and the remainder, which
    are simply not old enough yet.
    """

    audited: int = 0
    auto_confirmed: int = 0
    timed_out: int = 0
    backfilled: int = 0
    contested_preserved: int = 0
    undated_preserved: int = 0
    records_written: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "audited": self.audited,
            "auto_confirmed": self.auto_confirmed,
            "timed_out": self.timed_out,
            "backfilled": self.backfilled,
            "contested_preserved": self.contested_preserved,
            "undated_preserved": self.undated_preserved,
            "records_written": self.records_written,
        }


def _as_utc(dt: datetime) -> datetime:
    """Coerce to aware UTC. A naive timestamp is assumed UTC — the same
    assumption ``vault.attribution._iso`` makes when it writes one."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _policy_start(config: DailySyncConfig, now: datetime) -> datetime:
    """Read the persisted policy-start instant, stamping ``now`` on first use.

    Stamped once and never moved. If it drifted forward each run, entries
    created between runs would keep landing on the wrong side of the line and
    be labelled ``backfill`` forever — which is precisely the inflation of the
    attribution-quality signal the ruling forbids.
    """
    state = load_state(config.state.path)
    raw = state.get(POLICY_START_STATE_KEY)
    if isinstance(raw, str) and raw:
        parsed = _parse_iso(raw)
        if parsed is not None:
            return _as_utc(parsed)
    state[POLICY_START_STATE_KEY] = now.isoformat()
    save_state(config.state.path, state)
    log.info(
        "daily_sync.attribution.policy_start_stamped",
        policy_start_at=now.isoformat(),
    )
    return now


def auto_confirm_sweep(
    vault_path: Path,
    config: DailySyncConfig,
    *,
    now: datetime | None = None,
) -> SweepResult:
    """Confirm untouched attribution entries older than the policy window.

    The operator ruling: attribution confirmations are consistently correct, so
    reviewing them costs him time for no information. They auto-confirm after
    24h **unless contested**.

    Three things this deliberately does NOT do:

      * It never touches a CONTESTED entry. A contest is the operator saying the
        machine got it wrong; auto-confirming past that would overrule him
        silently, which is the exact failure the contest door exists to prevent.
      * It never re-stamps an ALREADY-confirmed entry, so an operator confirm is
        never downgraded to a machine one by a later run.
      * It never confirms an entry whose date won't parse. An age that can't be
        computed hasn't been demonstrated to exceed the window, and the safe
        direction on an audit trail is to leave the human a decision rather than
        to invent an endorsement.

    Runs the SAME ``_walk_vault`` the review batch uses, under the same
    ``scan_paths``, so the sweep and the batch can never disagree about which
    records are in scope — two walkers with two answers is how an entry becomes
    invisible to one surface and live on the other.
    """
    when = _as_utc(now or datetime.now(timezone.utc))
    result = SweepResult()
    enabled, _batch_size, scan_paths = _attribution_settings(config)
    if not enabled:
        # ILB: an explicit "ran, did nothing, and here is why" beats silence.
        log.info(SWEEP_EVENT, **result.to_dict(), skipped="attribution_disabled")
        return result

    policy_start = _policy_start(config, when)
    cutoff = when - timedelta(hours=AUTO_CONFIRM_AFTER_HOURS)
    corpus_path = getattr(config.attribution, "corpus_path", "") or ""

    for md_file in _walk_vault(vault_path, scan_paths):
        try:
            post = frontmatter.load(str(md_file))
        except Exception:
            log.info("daily_sync.attribution.read_failed", path=str(md_file))
            continue
        fm = post.metadata or {}
        entries = parse_audit_entries(fm)
        if not entries:
            continue
        rel_path = vault_relative(vault_path, md_file)
        confirmed_here: list[tuple[AuditEntry, str]] = []

        for entry in entries:
            if entry.confirmed_by_andrew or entry.confirmed_at is not None:
                continue  # already resolved — not part of the denominator
            result.audited += 1
            if entry.contested:
                result.contested_preserved += 1
                continue
            parsed = _parse_iso(entry.date)
            if parsed is None:
                result.undated_preserved += 1
                log.info(
                    "daily_sync.attribution.unparseable_date",
                    record_path=rel_path,
                    marker_id=entry.marker_id,
                    date=entry.date[:64],
                )
                continue
            if _as_utc(parsed) > cutoff:
                continue  # still inside its window
            via = (
                CONFIRMED_VIA_BACKFILL
                if _as_utc(parsed) < policy_start
                else CONFIRMED_VIA_TIMEOUT
            )
            confirm_marker(fm, entry.marker_id, by="auto", at=when, via=via)
            confirmed_here.append((entry, via))

        if not confirmed_here:
            continue

        post.metadata = fm
        try:
            md_file.write_text(
                frontmatter.dumps(post) + "\n", encoding="utf-8",
            )
        except OSError as exc:
            log.warning(
                "daily_sync.attribution.sweep_write_failed",
                record_path=rel_path,
                error=str(exc),
            )
            continue

        result.records_written += 1
        for entry, via in confirmed_here:
            result.auto_confirmed += 1
            if via == CONFIRMED_VIA_BACKFILL:
                result.backfilled += 1
            else:
                result.timed_out += 1
            if not corpus_path:
                continue
            try:
                append_entry(corpus_path, AttributionCorpusEntry(
                    type="attribution_auto_confirm",
                    marker_id=entry.marker_id,
                    record_path=rel_path,
                    agent=entry.agent,
                    section_title=entry.section_title,
                    marker_date=entry.date,
                    andrew_action="auto_confirm",
                    action_at=when.isoformat(),
                    confirmed_via=via,
                ))
            except OSError as exc:
                # One unwritable corpus row must not abort the sweep — the vault
                # write already landed, and dropping the rest would leave the
                # trail worse off than a single missing row.
                log.warning(
                    "daily_sync.attribution.sweep_corpus_write_failed",
                    record_path=rel_path,
                    marker_id=entry.marker_id,
                    error=str(exc),
                )

    # ILB: ONE grep-able event on EVERY run, including the all-zero steady state
    # this reaches once the backlog is cleared. A sweep that logged only when it
    # did work would be indistinguishable from a sweep that stopped running.
    log.info(SWEEP_EVENT, **result.to_dict(), policy_start_at=policy_start.isoformat())
    return result


def render_batch(
    items: list[AttributionItem], quality_line: str = "",
) -> str:
    """Render the attribution batch as the section body.

    Format (per spec)::

        ## Attribution audit (5 items)

        6. [salem 2026-04-23 18:44 — note/Marker Smoke Test]
           Section: "Marker Smoke Test"
           Content: "Testing the attribution audit marker. ..."
           Reason: talker conversation turn (session=78a7c5a2)

    Reply hints at the bottom mirror the email-calibration section's
    style so Andrew has one consistent reply grammar.
    """
    if not items:
        # Empty state — intentionally-left-blank principle: a section
        # header that says "nothing to do" beats a missing section
        # because operator visibility is the load-bearing property.
        # #72 item 2 — the quality line rides the EMPTY state too. A caught-up
        # operator sees this section every day and never sees a batch; hanging
        # the metric off the batch would mean it renders only when there is
        # already something to look at, which is the opposite of the point.
        body = "## Attribution audit\n\nNo attribution items pending review.\n"
        return f"{body}\n{quality_line}\n" if quality_line else body
    plural = "s" if len(items) != 1 else ""
    lines = [f"## Attribution audit ({len(items)} item{plural})", ""]
    for item in items:
        record_label = _short_record_label(item.record_path)
        date_label = _short_date(item.date)
        lines.append(
            f"{item.item_number}. [{item.agent} {date_label} — {record_label}]"
        )
        lines.append(f'   Section: "{item.section_title}"')
        if item.content_preview:
            lines.append(f'   Content: "{item.content_preview}"')
        if item.reason:
            lines.append(f"   Reason: {item.reason}")
        lines.append("")
    lines.append(
        "Reply with `N confirm` to keep, `N reject` to strip the section. "
        "Anything you leave alone confirms itself after a day."
    )
    if quality_line:
        lines.append("")
        lines.append(quality_line)
    return "\n".join(lines).rstrip()


# ---------------------------------------------------------------------------
# Section provider entry point + registration
# ---------------------------------------------------------------------------


# Module-level vault-path holder, mirrors ``email_section`` for
# consistency. Daemon sets this once at startup; tests may set it
# directly before invoking the section provider.
_VAULT_PATH_HOLDER: dict[str, Path] = {}


def set_vault_path(vault_path: Path) -> None:
    """Configure the module-level vault path used by the section provider.

    Idempotent — daemon calls once at startup, tests may call repeatedly.
    """
    _VAULT_PATH_HOLDER["path"] = vault_path


def get_vault_path() -> Path | None:
    """Return the currently-configured vault path (None if unset)."""
    return _VAULT_PATH_HOLDER.get("path")


# Holder for the most recent batch so the daemon can persist the
# item ↔ marker mapping after assembly. Mirrors ``email_section`` —
# the assembler signature returns only a string, so per-section
# metadata flows through this side channel.
_LAST_BATCH_HOLDER: dict[str, list[AttributionItem]] = {"items": []}


def consume_last_batch() -> list[AttributionItem]:
    """Return and clear the most recently-built batch.

    Called by the daemon after :func:`assemble_message` so it can
    persist the item ↔ marker mapping into the Daily Sync state file
    under ``last_batch.attribution_items``.
    """
    items = _LAST_BATCH_HOLDER.get("items", [])
    _LAST_BATCH_HOLDER["items"] = []
    return items


def peek_last_batch_count() -> int:
    """Return the count of items in the most-recently-built batch.

    Non-destructive — used by the assembler's ``item_count_after`` hook
    to advance the global ``start_index`` after this provider runs,
    without consuming the batch (the daemon calls ``consume_last_batch``
    afterwards to actually persist the mapping).
    """
    return len(_LAST_BATCH_HOLDER.get("items", []))


def attribution_audit_section(
    config: DailySyncConfig,
    today: date,
    *,
    start_index: int = 1,
) -> str | None:
    """Section provider — builds and renders the attribution-audit batch.

    Registered with priority 25 (between friction's reserved slot 20
    and open-questions' reserved slot 30, AFTER email calibration at
    10). Returns the empty-state header when there's nothing pending,
    NOT ``None`` — the intentionally-left-blank principle is
    load-bearing here. Returns ``None`` only when attribution is
    disabled or the vault path isn't configured.
    """
    enabled, _batch_size, _scan_paths = _attribution_settings(config)
    if not enabled:
        return None
    vault_path = get_vault_path()
    if vault_path is None or not vault_path.is_dir():
        return None
    items = build_batch(vault_path, config, start_index=start_index)
    _LAST_BATCH_HOLDER["items"] = items
    # #72 item 2 — computed here rather than inside render_batch so the
    # renderer stays a pure function of what it is handed (it is called
    # directly by tests and by the reply surfaces).
    corpus_path = getattr(config.attribution, "corpus_path", "") or ""
    window = int(getattr(config.attribution, "quality_window_days", 0)
                 or DEFAULT_WINDOW_DAYS)
    quality_line = render_quality_line(
        attribution_quality_stats(corpus_path, window_days=window)
    ) if corpus_path else ""
    return render_batch(items, quality_line)


def register() -> None:
    """Idempotent provider registration. Safe to call multiple times.

    Registers at priority 25 — between the friction-queue slot (20,
    reserved) and open-questions slot (30, reserved). Email calibration
    at priority 10 renders first; attribution renders second.
    """
    from . import assembler
    if "attribution_audit" in assembler.registered_providers():
        return
    assembler.register_provider(
        "attribution_audit",
        priority=25,
        provider=attribution_audit_section,
        item_count_after=peek_last_batch_count,
    )


__all__ = [
    "AUTO_CONFIRM_AFTER_HOURS",
    "POLICY_START_STATE_KEY",
    "SWEEP_EVENT",
    "AttributionItem",
    "SweepResult",
    "attribution_audit_section",
    "auto_confirm_sweep",
    "build_batch",
    "consume_last_batch",
    "get_vault_path",
    "peek_last_batch_count",
    "register",
    "render_batch",
    "set_vault_path",
]
