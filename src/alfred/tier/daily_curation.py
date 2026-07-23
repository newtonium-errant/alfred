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

    ``done_at`` (Arc #20, 2026-07-22) is the ISO ``YYYY-MM-DD`` date the
    operator checked this item off, or ``None`` when still open. A DATE
    (not a bare bool) so the daily goal can ask "done *today*?" and
    back-dating ("raked leaves yesterday") works. This is the ONLY
    done-state home for a free-text T3 item: unlike task-origin /
    routine-origin T1/T2 entries (which resolve done-ness through their
    backing ``task/`` status or ``completion_log``), a free-text T3 item
    has no backing record. It rides the existing ``tier_curation``
    top-level field allowlist because it is NESTED inside the entry (a
    CHILD of the already-allowed ``tier_curation`` field) — NOT a
    sibling top-level ``done`` key, which the talker scope gate
    (``check_talker_tier_curation_fields``) correctly rejects. Written
    only by the deterministic :func:`mark_t3_done` mutator, never by an
    LLM whole-block ``tier_curation`` rewrite.
    """

    item: str
    source: str
    done_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the YAML-shaped dict.

        ``done_at`` is emitted ONLY when set (mirrors the optional-drop
        pattern on :meth:`DailyCuration.to_dict`'s ``curated_at`` /
        ``rollover_from``): an open item stays a clean two-key
        ``{item, source}`` so byte-stability is preserved for every
        never-marked-done item.
        """
        out: dict[str, Any] = {"item": self.item, "source": self.source}
        if self.done_at is not None:
            out["done_at"] = self.done_at
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> T3Entry:
        """Build from a YAML-loaded dict.

        ``done_at`` is read schema-tolerantly: any present non-null value
        is coerced to ``str``; absent / null ⟹ ``None`` (open). The
        ``item`` + ``source`` requirement enforced by
        :meth:`DailyCuration._parse_t3_list` is unchanged — ``done_at``
        is a purely additive field.
        """
        done_at_raw = data.get("done_at")
        return cls(
            item=str(data.get("item", "")),
            source=str(data.get("source", "")),
            done_at=str(done_at_raw) if done_at_raw is not None else None,
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


def _write_curation_into_daily_file(
    daily_file: Path, today: _date, curation: DailyCuration,
) -> None:
    """Read-preserve-write the ``tier_curation`` block into ``daily_file``.

    The atomic ``.curation.tmp`` → ``os.replace`` write body, factored out
    of :func:`save_tier_curation` so :func:`mark_t3_done` /
    :func:`mark_t3_undone` can share it WITHIN a single lock hold.

    **PRECONDITION: the caller MUST already hold** ``daily_file_lock`` for
    ``daily_file``. This function does NOT take the lock — it is the
    lock-free inner write shared by every locked caller. The load →
    match → mutate → write of a done-state flip has to be one locked
    critical section (otherwise two ``tier_done`` calls race: A reads,
    B reads, A writes, B writes-preserving-A's-stale-view → A's flip is
    lost — the exact Step-5 lost-update shape). ``mark_t3_done`` can't
    just call :func:`save_tier_curation` after its own read, because that
    would re-enter :func:`daily_file_lock` on a SECOND fd for the same
    file and self-deadlock (``flock(LOCK_EX)`` on a distinct open file
    description blocks even within the same process). Hence the shared
    lock-free inner write.

    Behaviour is byte-identical to the pre-refactor
    :func:`save_tier_curation` inner block: preserve other frontmatter
    keys + body verbatim, seed a minimal ``type: daily`` file when
    absent, atomic ``.curation.tmp`` write with orphan-tmp cleanup.
    """
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
        _write_curation_into_daily_file(daily_file, today, curation)

    log.info(
        "tier.daily_curation.saved",
        path=str(daily_file),
        date=today.isoformat(),
        t1_count=len(curation.t1),
        t2_count=len(curation.t2),
        t3_count=len(curation.t3),
        has_rollover=curation.rollover_from is not None,
    )


# ---------------------------------------------------------------------------
# Arc #20 (2026-07-22) — free-text T3 done-state mutators
# ---------------------------------------------------------------------------
#
# The ONLY authorised writers of ``T3Entry.done_at``. Deterministic
# single-field mutators — CODE, not the LLM, is the allowlist: they can
# flip ``done_at`` on a matched T3 entry and nothing else (never
# ``type``/``date``/other entries/body). They write through the same
# ``daily_file_lock`` + atomic ``.curation.tmp`` path as
# ``save_tier_curation`` (via the shared lock-free
# ``_write_curation_into_daily_file``), touching only the top-level
# ``tier_curation`` key — so the write honours the existing
# ``TALKER_TIER_CURATION_FIELDS`` allowlist with ZERO widening.
#
# ``kind`` discriminator — cross-agent contract the talker's
# ``_dispatch_tier_done`` / ``_dispatch_tier_undone`` route on (and the
# vault-talker SKILL quotes verbatim). Mirrors the ``routine_done`` B1
# ``DONE_KIND_*`` string values so operator phrasing routes consistently
# across both surfaces. Rename any of these = update the talker
# dispatcher + SKILL in lockstep.
TIER_DONE_KIND_SUCCESS = "success"
TIER_DONE_KIND_IDEMPOTENT_NOOP = "idempotent_noop"
TIER_DONE_KIND_AMBIGUOUS_ITEM = "ambiguous_item"
TIER_DONE_KIND_UNKNOWN_ITEM = "unknown_item"
TIER_DONE_KIND_FUTURE_DATE_REJECTED = "future_date_rejected"
# Undo-specific kinds — the inverse of ``mark_t3_done``. ``unmarked`` = a
# ``done_at`` was cleared; ``not_marked`` = the item was already open
# (idempotent no-op, NOT an error — distinct from ``idempotent_noop`` so
# the talker can voice "that wasn't checked off, nothing to undo" vs
# "already done"). Per intentionally-left-blank the no-op is explicit.
# ``ambiguous_item`` / ``unknown_item`` are shared with the done path.
TIER_UNDONE_KIND_UNMARKED = "unmarked"
TIER_UNDONE_KIND_NOT_MARKED = "not_marked"


@dataclass
class TierDoneResult:
    """Result of a :func:`mark_t3_done` / :func:`mark_t3_undone` call.

    ``kind`` is the discriminator (one of the ``TIER_DONE_KIND_*`` /
    ``TIER_UNDONE_KIND_*`` constants) the talker routes on. The other
    fields carry what the talker needs to voice the reply:

      * ``item`` — the MATCHED T3 item text (canonical form from the
        curation, not the operator's fuzzy query) on success / noop /
        idempotent paths; the operator's raw query on unknown/ambiguous.
      * ``date`` — ISO date of the daily file acted on.
      * ``done_at`` — the stamped ISO date on ``success`` /
        ``idempotent_noop``; ``None`` otherwise.
      * ``candidates`` — for ``ambiguous_item`` the matched item texts to
        ask back on; for ``unknown_item`` the day's full T3 item list
        (so the talker can say "here's what IS on today's T3 list").
    """

    kind: str
    item: str | None = None
    date: str | None = None
    done_at: str | None = None
    candidates: list[str] = field(default_factory=list)


def _match_t3_entries(query: str, entries: list[T3Entry]) -> list[T3Entry]:
    """Fuzzy-match ``query`` against each T3 entry's ``item`` text.

    Reuses the routine matcher (:func:`alfred.routine.cli._matches_item`)
    so ``tier_done`` and ``routine_done`` resolve the SAME operator
    phrasing identically (the design's consistency requirement) and the
    same learned glossary substrate can serve both when P5 wires it in.
    Lazy function-level import: ``routine.cli`` imports nothing from
    ``tier`` (no module-load cycle), and this mirrors ``tier/compute.py``'s
    existing lazy-import-of-routine pattern. ``glossary=None`` here ⟹ the
    plain fuzzy ladder (the P5 self-correcting glossary read wires in as
    its own slice).
    """
    from alfred.routine.cli import _matches_item

    return [e for e in entries if _matches_item(query, e.item)]


def mark_t3_done(
    vault_path: Path,
    item: str,
    *,
    completed_at: _date,
    today: _date,
) -> TierDoneResult:
    """Mark a free-text T3 item done on ``completed_at``'s daily file.

    Loads the daily file for ``completed_at`` (so a back-date resolves
    the item on the day it was curated), fuzzy-matches ``item`` against
    that day's ``t3[].item`` strings, and stamps ``done_at =
    completed_at`` on the single match. ``today`` gates future dates.

    The WHOLE read → match → mutate → write runs inside ``daily_file_lock``
    (one critical section) so two concurrent ``tier_done`` calls can't
    lost-update each other's flips — see
    :func:`_write_curation_into_daily_file`'s precondition note for why
    calling :func:`save_tier_curation` here would self-deadlock instead.

    Returns a :class:`TierDoneResult`; every branch emits a named log
    event (per intentionally-left-blank) so an operator can grep the
    outcome without re-reading the file. Only mutates on a single match
    — the honest "isn't on the list" dead-end (``unknown_item``, #19) is
    preserved for the truly-untracked item.
    """
    if not item or not item.strip():
        log.info("tier.mark_t3_done.empty_query", date=completed_at.isoformat())
        return TierDoneResult(
            kind=TIER_DONE_KIND_UNKNOWN_ITEM,
            item=item,
            date=completed_at.isoformat(),
        )

    if completed_at > today:
        log.info(
            "tier.mark_t3_done.future_date_rejected",
            date=completed_at.isoformat(),
            today=today.isoformat(),
            item=item,
        )
        return TierDoneResult(
            kind=TIER_DONE_KIND_FUTURE_DATE_REJECTED,
            item=item,
            date=completed_at.isoformat(),
        )

    daily_file = _daily_file_path(vault_path, completed_at)
    with daily_file_lock(daily_file):
        curation = load_daily_curation(vault_path, completed_at)
        if curation is None:
            log.info(
                "tier.mark_t3_done.no_curation",
                date=completed_at.isoformat(),
                item=item,
                detail=(
                    "no daily file / tier_curation block for the target "
                    "date — the item is untracked, honest #19 dead-end."
                ),
            )
            return TierDoneResult(
                kind=TIER_DONE_KIND_UNKNOWN_ITEM,
                item=item,
                date=completed_at.isoformat(),
            )

        matches = _match_t3_entries(item, curation.t3)
        if not matches:
            log.info(
                "tier.mark_t3_done.unknown_item",
                date=completed_at.isoformat(),
                item=item,
                t3_count=len(curation.t3),
            )
            return TierDoneResult(
                kind=TIER_DONE_KIND_UNKNOWN_ITEM,
                item=item,
                date=completed_at.isoformat(),
                candidates=[e.item for e in curation.t3],
            )
        if len(matches) > 1:
            log.info(
                "tier.mark_t3_done.ambiguous_item",
                date=completed_at.isoformat(),
                item=item,
                match_count=len(matches),
            )
            return TierDoneResult(
                kind=TIER_DONE_KIND_AMBIGUOUS_ITEM,
                item=item,
                date=completed_at.isoformat(),
                candidates=[e.item for e in matches],
            )

        matched = matches[0]
        target_iso = completed_at.isoformat()
        if matched.done_at == target_iso:
            log.info(
                "tier.mark_t3_done.idempotent_noop",
                date=target_iso,
                item=matched.item,
            )
            return TierDoneResult(
                kind=TIER_DONE_KIND_IDEMPOTENT_NOOP,
                item=matched.item,
                date=target_iso,
                done_at=target_iso,
            )

        matched.done_at = target_iso
        _write_curation_into_daily_file(daily_file, completed_at, curation)

    log.info(
        "tier.mark_t3_done.success",
        date=target_iso,
        item=matched.item,
    )
    return TierDoneResult(
        kind=TIER_DONE_KIND_SUCCESS,
        item=matched.item,
        date=target_iso,
        done_at=target_iso,
    )


def mark_t3_undone(
    vault_path: Path,
    item: str,
    *,
    on_date: _date,
) -> TierDoneResult:
    """Clear ``done_at`` on a free-text T3 item — the inverse of
    :func:`mark_t3_done`.

    Loads ``on_date``'s daily file, fuzzy-matches ``item``, and clears
    ``done_at`` on the single match. Same locked-RMW discipline as
    :func:`mark_t3_done`. No future-date gate — undoing any date is
    harmless (you can only clear a ``done_at`` that exists).

    Returns ``unmarked`` when a ``done_at`` was cleared, ``not_marked``
    when the item was already open (idempotent, NOT an error), or the
    shared ``ambiguous_item`` / ``unknown_item`` kinds.
    """
    if not item or not item.strip():
        log.info("tier.mark_t3_undone.empty_query", date=on_date.isoformat())
        return TierDoneResult(
            kind=TIER_DONE_KIND_UNKNOWN_ITEM,
            item=item,
            date=on_date.isoformat(),
        )

    daily_file = _daily_file_path(vault_path, on_date)
    with daily_file_lock(daily_file):
        curation = load_daily_curation(vault_path, on_date)
        if curation is None:
            log.info(
                "tier.mark_t3_undone.no_curation",
                date=on_date.isoformat(),
                item=item,
            )
            return TierDoneResult(
                kind=TIER_DONE_KIND_UNKNOWN_ITEM,
                item=item,
                date=on_date.isoformat(),
            )

        matches = _match_t3_entries(item, curation.t3)
        if not matches:
            log.info(
                "tier.mark_t3_undone.unknown_item",
                date=on_date.isoformat(),
                item=item,
                t3_count=len(curation.t3),
            )
            return TierDoneResult(
                kind=TIER_DONE_KIND_UNKNOWN_ITEM,
                item=item,
                date=on_date.isoformat(),
                candidates=[e.item for e in curation.t3],
            )
        if len(matches) > 1:
            log.info(
                "tier.mark_t3_undone.ambiguous_item",
                date=on_date.isoformat(),
                item=item,
                match_count=len(matches),
            )
            return TierDoneResult(
                kind=TIER_DONE_KIND_AMBIGUOUS_ITEM,
                item=item,
                date=on_date.isoformat(),
                candidates=[e.item for e in matches],
            )

        matched = matches[0]
        if matched.done_at is None:
            log.info(
                "tier.mark_t3_undone.not_marked",
                date=on_date.isoformat(),
                item=matched.item,
            )
            return TierDoneResult(
                kind=TIER_UNDONE_KIND_NOT_MARKED,
                item=matched.item,
                date=on_date.isoformat(),
            )

        previous = matched.done_at
        matched.done_at = None
        _write_curation_into_daily_file(daily_file, on_date, curation)

    log.info(
        "tier.mark_t3_undone.unmarked",
        date=on_date.isoformat(),
        item=matched.item,
        was_done_at=previous,
    )
    return TierDoneResult(
        kind=TIER_UNDONE_KIND_UNMARKED,
        item=matched.item,
        date=on_date.isoformat(),
    )


__all__ = [
    "DailyCuration",
    "T1T2Entry",
    "T1_T2_SOURCES",
    "T3Entry",
    "T3_SOURCES",
    "TIER_DONE_KIND_AMBIGUOUS_ITEM",
    "TIER_DONE_KIND_FUTURE_DATE_REJECTED",
    "TIER_DONE_KIND_IDEMPOTENT_NOOP",
    "TIER_DONE_KIND_SUCCESS",
    "TIER_DONE_KIND_UNKNOWN_ITEM",
    "TIER_UNDONE_KIND_NOT_MARKED",
    "TIER_UNDONE_KIND_UNMARKED",
    "TierDoneResult",
    "daily_file_lock",
    "load_daily_curation",
    "mark_t3_done",
    "mark_t3_undone",
    "save_tier_curation",
]
