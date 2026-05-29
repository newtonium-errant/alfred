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

  * **T1/T2 entries use ``task:`` (wikilink string).** T1/T2 are
    operator-selected subsets of the open task pool — every entry
    must point at a concrete ``task/`` record.
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
  * **``source`` is a five-value enum** (canonical strings — Ship 4's
    SKILL will quote these verbatim):
    - ``"auto-due"`` — surfaced from due-today/tomorrow
    - ``"auto-escalate"`` — surfaced from the ``escalate_at_days`` window
    - ``"operator"`` — explicit operator add via talker
    - ``"aspirational"`` — picked from today's routine Aspirational items
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

from dataclasses import dataclass, field
from datetime import date as _date
from pathlib import Path
from typing import Any

import frontmatter  # type: ignore[import-untyped]
import structlog

log = structlog.get_logger(__name__)


# Canonical ``source`` enum values — Ships 2 + 4 reference these as
# stable string contracts. Sets used for validation at load time;
# unknown sources are tolerated (a future Ship 5/6 may add new
# canonical values without breaking the loader) but the load logs an
# info event so operators can spot drift.
T1_T2_SOURCES: frozenset[str] = frozenset({
    "auto-due",
    "auto-escalate",
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
    """One T1 or T2 selection — points at a concrete ``task/`` record.

    ``task`` is the wikilink string (e.g. ``"[[task/Steph Yang ROE]]"``).
    ``source`` is one of :data:`T1_T2_SOURCES`. ``confirmed`` is T1-only
    + optional (auto-surfaced T1 starts ``False``; operator confirmation
    flips to ``True``); for T2 entries it stays ``None`` (operator-add
    IS the confirmation, no separate flag needed).
    """

    task: str
    source: str
    confirmed: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the YAML-shaped dict.

        Drops ``confirmed=None`` so T2 entries (which never carry the
        field) don't emit ``confirmed: null`` in the YAML — keeps the
        on-disk shape clean.
        """
        out: dict[str, Any] = {"task": self.task, "source": self.source}
        if self.confirmed is not None:
            out["confirmed"] = self.confirmed
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> T1T2Entry:
        """Build from a YAML-loaded dict.

        Schema-tolerance per the CLAUDE.md load-time contract: unknown
        keys are silently ignored so a future Ship that adds a field
        doesn't break the loader.
        """
        return cls(
            task=str(data.get("task", "")),
            source=str(data.get("source", "")),
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
                if "task" not in entry or "source" not in entry:
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
        ``tier_curation`` value (or add it), write atomically through
        the standard frontmatter.dumps path.

    Atomic write via tempfile + rename is NOT implemented here yet —
    matches the routine aggregator's existing pattern (which also
    does a direct write). A future arc may add atomic writes to both
    layers in lockstep; doing it unilaterally here would create
    asymmetric contract surface.

    Per ``feedback_intentionally_left_blank``: every write emits a
    named log event with the curation counts so operators can grep
    for "did the save land?" without re-reading the file.
    """
    daily_file = _daily_file_path(vault_path, today)
    daily_file.parent.mkdir(parents=True, exist_ok=True)

    if daily_file.exists():
        try:
            post = frontmatter.load(str(daily_file))
        except Exception as exc:  # noqa: BLE001
            # Defensive: a corrupt file forces caller to delete + retry.
            # Don't overwrite blindly — operator may have hand-edits we'd
            # lose.
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
        # Fresh file — seed minimum frontmatter so a downstream reader
        # (brief, janitor) sees a well-formed ``type: daily`` record.
        existing_meta = {"type": "daily", "date": today.isoformat()}
        body = ""
        log.info(
            "tier.daily_curation.created_fresh_daily_file",
            path=str(daily_file),
            date=today.isoformat(),
            detail=(
                "daily file did not exist; seeded minimum frontmatter "
                "(``type: daily``, ``date: <iso>``) + empty body. The "
                "routine aggregator's next fire will read-preserve-write "
                "this curation."
            ),
        )

    # Replace / add the tier_curation block.
    existing_meta["tier_curation"] = curation.to_dict()

    new_post = frontmatter.Post(body, **existing_meta)
    daily_file.write_text(frontmatter.dumps(new_post) + "\n", encoding="utf-8")

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
    "load_daily_curation",
    "save_tier_curation",
]
