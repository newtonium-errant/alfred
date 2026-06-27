"""Daily tier curation — the V2 tier system's data home.

Tier-V2 (2026-05-29) reframes tier as a **daily curation ritual** stored
in ``vault/daily/<date>.md`` rather than as persistent task attributes.
The operator picks each day's T1/T2/T3 shortlists in the morning via
talker (Ship 4) and the brief renders the curated lists going forward
that day (Ship 2).

This module is the data layer Ships 2/3/4 consume:

  * :class:`DailyCuration` — typed dataclass for the ``tier_curation``
    frontmatter block. Three tier arrays + curation timestamp +
    optional rollover-from anchor.
  * :func:`load_daily_curation` — read ``vault/daily/<date>.md`` and
    extract the ``tier_curation`` block. Returns ``None`` when the
    file or block is absent (Ship 2 brief uses ``None`` as the
    "un-curated state, show selection pools" signal).
  * :func:`save_tier_curation` — write the ``tier_curation`` block
    into the daily file while preserving every other frontmatter key
    (the routine aggregator's ``type``/``date``/``routines_contributing``/
    ``critical_pending`` keys MUST survive). Read-modify-write through
    python-frontmatter so the body content stays exactly as written.

Schema (the cross-Ship contract — field names + ``source`` enum values
are stable surfaces for Ships 2 + 4 to consume):

    tier_curation:
      t1:
        - task: "[[task/Steph Yang ROE]]"
          source: "auto-due"
          confirmed: true
      t2:
        - task: "[[task/RRTS Bug List — Burn Through]]"
          source: "operator"
      t3:
        - item: "Walk Fergus"
          source: "aspirational"
        - item: "Read for an hour"
          source: "operator-adhoc"
      curated_at: "2026-05-29T07:14:00-03:00"
      rollover_from: "2026-05-28"

Shape notes (load-bearing):

  * **T1/T2 entries carry EITHER ``task:`` (wikilink string) OR
    ``routine_item:`` (discriminated-union dict).** The ``task:``
    shape points at a concrete ``task/`` record:

        - task: "[[task/Steph Yang ROE]]"
          source: "auto-due"

    The ``routine_item:`` shape (added 2026-05-29 Phase 2A Ship B)
    points at a recurring item inside a ``routine/`` record:

        - routine_item: {record: "Recurring Bills + Admin", text: "Pay Clinic Rental ..."}
          source: "auto-due-routine"
          confirmed: true

    Decision #1 (Plan-ratified): discriminated-union via separate
    keys, NOT a hybrid ``task: [[routine/X#item]]`` overload. The
    operator-curated entry preserves the routine-record + item-text
    structure so the brief render layer can reconstruct the wikilink
    cleanly. Exactly one of the two keys is populated; ``to_dict``
    drops the absent shape for clean YAML.
  * **T3 entries use ``item:`` (free-text string).** T3 is intentions,
    not necessarily tasks — the operator types "walk Fergus" or "read
    for an hour" which is a routine-item-name OR an ad-hoc one-liner,
    not a ``task/`` wikilink. The brief renders the text verbatim;
    the talker may anchor common items back to routine records via
    Ship 4 SKILL parse heuristics but the data layer doesn't enforce
    that.
  * **``confirmed`` is T1-only and optional.** Auto-T1 candidates
    surfaced from due dates start with ``confirmed: false``;
    operator confirmation via talker flips to ``true``. T2/T3 entries
    have no ``confirmed`` field (operator add IS the confirmation).
  * **``source`` is a six-value enum** for T1/T2 (canonical strings —
    Ship 4's SKILL will quote these verbatim):
    - ``"auto-due"`` — task-origin: surfaced from due-today/tomorrow
    - ``"auto-escalate"`` — task-origin: ``escalate_at_days`` window
    - ``"auto-due-routine"`` — routine-origin T1 (Ship B): the routine
      item's ``due_pattern`` resolves into the T1 window
    - ``"auto-surface-routine"`` — routine-origin T2 ramp (Ship B):
      the item's ``surface_at_days`` window opens before T1 escalation
    - ``"operator"`` — explicit operator add via talker
    - ``"rollover"`` — pre-populated from yesterday's incomplete (T1/T2 only)
  * **``rollover_from`` is optional.** Present when the curation was
    pre-populated from yesterday's daily file; absent when the curation
    is fresh-from-scratch (e.g. operator deleted the file and ran the
    aggregator again).

Round-trip discipline: every field that exists in the schema is
preserved on load → save. The :class:`DailyCuration` dataclass uses
``schema-tolerance`` per the CLAUDE.md state-load contract: extra keys
in the YAML (e.g. a future Ship 7 adds a ``notes`` field) are tolerated
on load, dropped on save. The shipped fields are exhaustive for V2;
new fields require a constant + ``from_dict``/``to_dict`` update in
this module.

Loose-coupling boundary: this module reads/writes ONLY the
``tier_curation`` frontmatter block. The routine aggregator owns the
body content + the rest of the frontmatter; the talker reads the
selection-pool surface via existing helpers. Edits here MUST NOT
touch the file body.
"""

from __future__ import annotations

import contextlib
import os
from dataclasses import dataclass, field
from datetime import date as _date
from pathlib import Path
from typing import Any, Iterator

import frontmatter  # type: ignore[import-untyped]
import structlog

# fcntl is POSIX-only. The fleet runs on Linux (prod box + WSL dev), so
# it's always available there; the guarded import keeps the module
# importable on a hypothetical non-POSIX box (the lock then degrades to a
# no-op — see daily_file_lock).
try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    _fcntl = None  # type: ignore[assignment]

log = structlog.get_logger(__name__)


@contextlib.contextmanager
def daily_file_lock(daily_file_path: Path) -> Iterator[None]:
    """Exclusive cross-process lock around a daily-file read-modify-write.

    Step 5 lost-update fix (2026-06-27): ``daily/<date>.md`` has TWO
    read-preserve-write writers in SEPARATE processes — the routine
    aggregator daemon (05:59 fire) and ``save_tier_curation`` (talker /
    operator, any time). The atomic ``.tmp`` → ``os.replace`` write
    (2026-06-26) closed torn READS but NOT the lost-update race: writer A
    reads → writer B reads the same state → A writes → B writes
    (preserving A's now-STALE view) → A's keys are clobbered. The
    operator-facing symptom is a just-made tier-curation confirmation
    silently lost when the aggregate pass fires mid-edit.

    This wraps each writer's WHOLE RMW (read existing → merge → atomic
    write) in an ``fcntl.flock(LOCK_EX)`` on a sidecar lock file, so the
    two RMWs serialize: the second writer blocks until the first's write
    lands, then reads the fresh state.

    ``daily_file_path`` is the RESOLVED daily-file path the caller is
    about to write (``<...>/<date>.md``); the lock is that path with a
    ``.lock`` suffix. Deriving the lock from the actual file path (not a
    re-hardcoded ``daily/``) means both writers lock the SAME sidecar as
    long as they write the SAME file — the lock is structurally
    consistent with whatever path each writer resolved, even if the
    aggregator's configurable output dir ever diverges from the curation
    layer's path (that would be a pre-existing file-path bug, not a lock
    bug). The lock file is created on demand, never deleted (a stable
    0-byte sidecar; deleting it would reopen a create-race on the lock).

    Degrades to a no-op (with a warn) if ``fcntl`` is unavailable —
    preserves the prior atomic-only behaviour on non-POSIX rather than
    crashing. The fleet is Linux, so this path is defensive-only.
    """
    if _fcntl is None:  # pragma: no cover - non-POSIX fallback
        log.warning(
            "tier.daily_curation.flock_unavailable",
            detail=(
                "fcntl not available (non-POSIX); daily-file writes fall "
                "back to atomic-only (torn-reads closed, lost-update "
                "window OPEN). The fleet is Linux; this path is defensive."
            ),
        )
        yield
        return

    lock_path = daily_file_path.with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # Open (create if absent) the sidecar lock file. ``a`` so concurrent
    # creators don't truncate each other; we never write to it — the
    # flock on the fd is the whole mechanism.
    with open(lock_path, "a", encoding="utf-8") as lock_fd:
        _fcntl.flock(lock_fd.fileno(), _fcntl.LOCK_EX)
        try:
            yield
        finally:
            _fcntl.flock(lock_fd.fileno(), _fcntl.LOCK_UN)


# Canonical ``source`` enum values — Ships 2 + 4 reference these as
# stable string contracts. Sets used for validation at load time;
# unknown sources are tolerated (a future Ship 5/6 may add new
# canonical values without breaking the loader) but the load logs an
# info event so operators can spot drift.
#
# Phase 2A Ship B (2026-05-29) added ``"auto-due-routine"`` +
# ``"auto-surface-routine"`` for routine-origin T1/T2 entries. Ship D
# SKILL must quote these verbatim — the talker recognises operator
# replies based on these source-string distinctions.
T1_T2_SOURCES: frozenset[str] = frozenset({
    "auto-due",
    "auto-escalate",
    "auto-due-routine",
    "auto-surface-routine",
    "operator",
    "rollover",
})

# T3 sources include ``aspirational`` (picked from today's routine
# Aspirational items) and ``operator-adhoc`` (a one-liner the operator
# typed that doesn't anchor to any routine record). ``rollover`` is
# T1/T2-only per the spec (T3 is "today's intentions" — rolling over
# self-care intentions doesn't match the daily-fresh framing).
T3_SOURCES: frozenset[str] = frozenset({
    "aspirational",
    "operator",
    "operator-adhoc",
})


@dataclass
class T1T2Entry:
    """One T1 or T2 selection — discriminated union over origin.

    Phase 2A Ship B (2026-05-29) extended the entry shape to support
    routine-origin items via a discriminated-union pattern:

      * ``task`` populated, ``routine_item`` None — task-origin entry
        (the original Tier-V2 Ship 1 shape). ``task`` is the wikilink
        string (e.g. ``"[[task/Steph Yang ROE]]"``).
      * ``routine_item`` populated, ``task`` None — routine-origin
        entry. ``routine_item`` is a dict with keys ``record`` (the
        routine record name, e.g. ``"Recurring Bills + Admin"``) and
        ``text`` (the item's text, e.g. ``"Pay Clinic Rental ..."``).
        The brief render layer reconstructs the
        ``[[routine/<record>]]`` wikilink + item-text inline.

    ``source`` is one of :data:`T1_T2_SOURCES`. The source string
    discriminates origin: ``"auto-due"`` / ``"auto-escalate"`` are
    task-origin; ``"auto-due-routine"`` / ``"auto-surface-routine"``
    are routine-origin. ``"operator"`` + ``"rollover"`` may be either.

    ``confirmed`` is T1-only + optional (auto-surfaced T1 starts
    ``False``; operator confirmation via talker flips to ``True``);
    for T2 entries it stays ``None`` (operator-add IS the confirmation,
    no separate flag needed).

    Invariant (load-bearing — Ship D SKILL must honour): exactly one
    of ``task`` / ``routine_item`` is populated. The loader is
    tolerant of edge cases (both set, neither set) but logs the
    anomaly; ``to_dict`` always serializes exactly one shape for
    clean YAML.
    """

    task: str | None = None
    routine_item: dict[str, Any] | None = None
    source: str = "operator"
    confirmed: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the YAML-shaped dict.

        Emits exactly one of ``task`` / ``routine_item`` (drops the
        absent shape so the YAML stays clean — no ``task: null`` or
        ``routine_item: null`` clutter). Drops ``confirmed=None`` so
        T2 entries (which never carry the field) don't emit
        ``confirmed: null``.

        Precedence (defensive — should not fire in practice): if both
        ``task`` and ``routine_item`` are set, ``task`` wins. The
        invariant says exactly one is populated; this fallback
        preserves operator data on the existing-task case rather
        than dropping it.
        """
        out: dict[str, Any] = {}
        if self.task is not None:
            out["task"] = self.task
        elif self.routine_item is not None:
            out["routine_item"] = self.routine_item
        out["source"] = self.source
        if self.confirmed is not None:
            out["confirmed"] = self.confirmed
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> T1T2Entry:
        """Build from a YAML-loaded dict.

        Discriminated-union parse:
          * ``task`` present → task-origin entry.
          * ``routine_item`` dict present → routine-origin entry.
          * Both absent → empty entry (loader-tolerant; the caller's
            T1T2-list parser already filters entries missing both
            shapes via :meth:`DailyCuration._parse_t12_list`).

        Schema-tolerance per the CLAUDE.md load-time contract: unknown
        keys are silently ignored so a future Ship that adds a field
        doesn't break the loader.
        """
        task_raw = data.get("task")
        routine_item_raw = data.get("routine_item")
        # Discriminate: prefer task when both are present (matches
        # to_dict precedence). Most operator hand-edits will set
        # exactly one.
        task: str | None = None
        routine_item: dict[str, Any] | None = None
        if isinstance(task_raw, str) and task_raw.strip():
            task = task_raw
        elif isinstance(routine_item_raw, dict):
            # Defensive shape validation — require record + text keys.
            record = routine_item_raw.get("record")
            text = routine_item_raw.get("text")
            if isinstance(record, str) and isinstance(text, str):
                routine_item = {"record": record, "text": text}
        return cls(
            task=task,
            routine_item=routine_item,
            source=str(data.get("source", "operator")),
            confirmed=(
                bool(data["confirmed"]) if "confirmed" in data else None
            ),
        )


@dataclass
class T3Entry:
    """One T3 selection — free-text intention.

    ``item`` is the operator-facing string (e.g. ``"Walk Fergus"``).
    ``source`` is one of :data:`T3_SOURCES`. No ``confirmed`` field
    (T3 is operator-curated; the add IS the confirmation).
    """

    item: str
    source: str

    def to_dict(self) -> dict[str, Any]:
        return {"item": self.item, "source": self.source}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> T3Entry:
        return cls(
            item=str(data.get("item", "")),
            source=str(data.get("source", "")),
        )


@dataclass
class DailyCuration:
    """One day's tier curation — the data Ships 2/3/4 consume.

    Round-trip: ``DailyCuration.from_dict(data).to_dict() == data``
    within the canonical-field set (extra keys dropped per the
    schema-tolerance contract; this is deliberate, not a bug).

    Empty buckets are valid: a freshly-created file with
    ``t1=[], t2=[], t3=[]`` represents "operator hasn't curated yet"
    (Ship 2 uses this state to render selection-pools instead of
    curated lists).
    """

    t1: list[T1T2Entry] = field(default_factory=list)
    t2: list[T1T2Entry] = field(default_factory=list)
    t3: list[T3Entry] = field(default_factory=list)
    curated_at: str | None = None  # ISO-8601 wall-clock timestamp
    rollover_from: str | None = None  # ISO date of source day

    def to_dict(self) -> dict[str, Any]:
        """Serialize the curation to the YAML-shaped dict.

        Optional fields are dropped when ``None`` — keeps the on-disk
        shape minimal. Empty tier arrays are PRESERVED (the empty list
        is the "operator started curating but bucket is empty" signal;
        absence would conflate with the "block doesn't exist" signal
        which :func:`load_daily_curation` returns as ``None``).
        """
        out: dict[str, Any] = {
            "t1": [e.to_dict() for e in self.t1],
            "t2": [e.to_dict() for e in self.t2],
            "t3": [e.to_dict() for e in self.t3],
        }
        if self.curated_at is not None:
            out["curated_at"] = self.curated_at
        if self.rollover_from is not None:
            out["rollover_from"] = self.rollover_from
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DailyCuration:
        """Build a DailyCuration from a YAML-loaded ``tier_curation`` dict.

        Schema-tolerance contract:
          * Missing tier arrays default to empty list (a YAML with
            only ``t1: [...]`` and no ``t2``/``t3`` key still loads).
          * Tier arrays that aren't lists are coerced to empty
            (defensive against operator hand-edit corruption).
          * Per-entry dicts that fail validation (missing required
            fields) are silently dropped — caller decides what to do
            with a partial curation.
          * Unknown top-level keys are ignored.

        Per the CLAUDE.md ``load()`` schema-tolerance contract.
        """

        def _parse_t12_list(raw: Any) -> list[T1T2Entry]:
            if not isinstance(raw, list):
                return []
            out: list[T1T2Entry] = []
            for entry in raw:
                if not isinstance(entry, dict):
                    continue
                # Discriminated-union: require source + EITHER task or
                # routine_item. Entries with neither shape are silently
                # dropped (operator hand-edit corruption defense).
                if "source" not in entry:
                    continue
                has_task = (
                    isinstance(entry.get("task"), str)
                    and entry.get("task").strip()
                )
                has_routine_item = isinstance(
                    entry.get("routine_item"), dict
                )
                if not (has_task or has_routine_item):
                    continue
                out.append(T1T2Entry.from_dict(entry))
            return out

        def _parse_t3_list(raw: Any) -> list[T3Entry]:
            if not isinstance(raw, list):
                return []
            out: list[T3Entry] = []
            for entry in raw:
                if not isinstance(entry, dict):
                    continue
                if "item" not in entry or "source" not in entry:
                    continue
                out.append(T3Entry.from_dict(entry))
            return out

        return cls(
            t1=_parse_t12_list(data.get("t1")),
            t2=_parse_t12_list(data.get("t2")),
            t3=_parse_t3_list(data.get("t3")),
            curated_at=(
                str(data["curated_at"])
                if data.get("curated_at") is not None
                else None
            ),
            rollover_from=(
                str(data["rollover_from"])
                if data.get("rollover_from") is not None
                else None
            ),
        )


# ---------------------------------------------------------------------------
# File-system helpers
# ---------------------------------------------------------------------------


def _daily_file_path(vault_path: Path, today: _date) -> Path:
    """Resolve ``<vault>/daily/<YYYY-MM-DD>.md`` — the routine aggregator's
    canonical output path.

    Centralised so a future refactor of the daily file naming convention
    propagates to every caller.
    """
    return vault_path / "daily" / f"{today.isoformat()}.md"


def load_daily_curation(
    vault_path: Path, today: _date,
) -> DailyCuration | None:
    """Read the daily file + extract the ``tier_curation`` block.

    Returns ``None`` when:
      * The daily file doesn't exist (routine aggregator hasn't run
        yet today, OR operator deleted the file).
      * The file exists but has no ``tier_curation`` frontmatter key
        (aggregator ran but no curation has been done yet today).
      * The file exists but parsing fails (logged + None to avoid
        crashing the caller; Ship 2 treats parse-failure as
        un-curated and renders selection pools).

    Returns a populated :class:`DailyCuration` when the block exists
    and parses cleanly. Ship 2 brief uses presence-of-curation as the
    "render curated lists" signal; absence as the "render selection
    pools" signal.

    Per ``feedback_intentionally_left_blank``: every return path emits
    a named log event so operators can distinguish each shape from
    each other without grepping the code.
    """
    daily_file = _daily_file_path(vault_path, today)
    if not daily_file.exists():
        log.info(
            "tier.daily_curation.no_daily_file",
            path=str(daily_file),
            date=today.isoformat(),
            detail=(
                "vault/daily/<date>.md not yet written by the routine "
                "aggregator (fires at 05:59 Halifax). Caller treats "
                "this as 'un-curated state'."
            ),
        )
        return None

    try:
        post = frontmatter.load(str(daily_file))
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "tier.daily_curation.parse_failed",
            path=str(daily_file),
            date=today.isoformat(),
            error=str(exc),
        )
        return None

    raw_block = post.metadata.get("tier_curation") if post.metadata else None
    if not isinstance(raw_block, dict):
        # Either key absent or wrong type — both signal "no curation."
        log.info(
            "tier.daily_curation.no_tier_curation_block",
            path=str(daily_file),
            date=today.isoformat(),
            detail=(
                "daily file exists but ``tier_curation`` frontmatter "
                "key is missing (aggregator wrote a clean file; no "
                "talker curation yet today)."
            ),
        )
        return None

    curation = DailyCuration.from_dict(raw_block)
    log.info(
        "tier.daily_curation.loaded",
        path=str(daily_file),
        date=today.isoformat(),
        t1_count=len(curation.t1),
        t2_count=len(curation.t2),
        t3_count=len(curation.t3),
        has_rollover=curation.rollover_from is not None,
    )
    return curation


def save_tier_curation(
    vault_path: Path, today: _date, curation: DailyCuration,
) -> None:
    """Write the ``tier_curation`` block into the daily file.

    READ-MODIFY-WRITE: the existing file content (other frontmatter
    keys + body) is preserved verbatim. Only the ``tier_curation``
    key is set/replaced. This is the cross-cutting contract with the
    routine aggregator: aggregator owns ``type``/``date``/
    ``routines_contributing``/``critical_pending`` + body; this module
    owns ``tier_curation``. Each layer reads-preserves-writes the
    other's keys.

    Behavior:
      * If the daily file doesn't exist → create it with ONLY the
        ``tier_curation`` block + minimal frontmatter (``type: daily``,
        ``date: <iso>``) + empty body. The routine aggregator's next
        fire will read-preserve-write this curation; operators see a
        usable file even before the aggregator runs.
      * If the file exists → load via python-frontmatter, replace the
        ``tier_curation`` value (or add it), write atomically.

    Atomic write (Step 2 writer-race fix, 2026-06-26): ``.tmp`` →
    ``os.replace``, in LOCKSTEP with the routine aggregator (which now
    does the same). The daily file has two writers — this one (owns
    ``tier_curation``) and the aggregator (owns the rest + body) — both
    doing read-preserve-write of the whole file. A non-atomic write left
    a window where a concurrent reader (the brief) or the other writer
    could observe a truncated file. The tmp suffix is
    WRITER-DISTINGUISHED (``.curation.tmp``) so the two writers' tmp
    files never collide. Per the project-standard atomic-write contract
    (transport/instructor/curator state.py).

    Lost-update lock (Step 5, 2026-06-27): the WHOLE read-merge-write is
    wrapped in :func:`daily_file_lock` (exclusive ``fcntl.flock`` on the
    ``.lock`` sidecar) so a concurrent aggregator RMW can't read stale,
    write, and clobber this curation — the two writers serialize. Atomic
    write alone closed torn-reads; the lock closes lost-update.

    Per ``feedback_intentionally_left_blank``: every write emits a
    named log event with the curation counts so operators can grep
    for "did the save land?" without re-reading the file.
    """
    daily_file = _daily_file_path(vault_path, today)
    daily_file.parent.mkdir(parents=True, exist_ok=True)

    # Serialize the whole RMW against the aggregator's RMW on the same
    # daily file (Step 5 lost-update fix). The read below must stay valid
    # through the write; the lock guarantees no other writer interleaves.
    with daily_file_lock(daily_file):
        if daily_file.exists():
            try:
                post = frontmatter.load(str(daily_file))
            except Exception as exc:  # noqa: BLE001
                # Defensive: a corrupt file forces caller to delete +
                # retry. Don't overwrite blindly — operator may have
                # hand-edits we'd lose.
                log.warning(
                    "tier.daily_curation.save_aborted_corrupt_file",
                    path=str(daily_file),
                    date=today.isoformat(),
                    error=str(exc),
                )
                raise
            existing_meta = dict(post.metadata or {})
            body = post.content or ""
        else:
            # Fresh file — seed minimum frontmatter so a downstream
            # reader (brief, janitor) sees a well-formed ``type: daily``
            # record.
            existing_meta = {"type": "daily", "date": today.isoformat()}
            body = ""
            log.info(
                "tier.daily_curation.created_fresh_daily_file",
                path=str(daily_file),
                date=today.isoformat(),
                detail=(
                    "daily file did not exist; seeded minimum frontmatter "
                    "(``type: daily``, ``date: <iso>``) + empty body. The "
                    "routine aggregator's next fire will "
                    "read-preserve-write this curation."
                ),
            )

        # Replace / add the tier_curation block.
        existing_meta["tier_curation"] = curation.to_dict()

        new_post = frontmatter.Post(body, **existing_meta)
        tmp_path = daily_file.with_suffix(".curation.tmp")
        # orphan-tmp cleanup (reviewer NOTE, 2026-06-27): a failed
        # os.replace would otherwise leave a stale .curation.tmp orphan
        # (self-heals next run, but invisible to scanners). try/finally
        # removes it on any failure path; on success os.replace has
        # already moved it, so unlink(missing_ok=True) is a no-op.
        try:
            tmp_path.write_text(
                frontmatter.dumps(new_post) + "\n", encoding="utf-8",
            )
            os.replace(tmp_path, daily_file)
        finally:
            tmp_path.unlink(missing_ok=True)

    log.info(
        "tier.daily_curation.saved",
        path=str(daily_file),
        date=today.isoformat(),
        t1_count=len(curation.t1),
        t2_count=len(curation.t2),
        t3_count=len(curation.t3),
        has_rollover=curation.rollover_from is not None,
    )


__all__ = [
    "DailyCuration",
    "T1T2Entry",
    "T1_T2_SOURCES",
    "T3Entry",
    "T3_SOURCES",
    "daily_file_lock",
    "load_daily_curation",
    "save_tier_curation",
]
