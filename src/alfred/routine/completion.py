"""Neutral routine completion-log writers — the SHARED write leaf.

Phase C slice 1 (board DONE path) extracted the pure ``completion_log``
mutation out of ``routine/cli.py``'s ``cmd_done`` (a ~300-line CLI function
tangled with fuzzy resolution + self-correcting-matcher capture + canary
emission) so that BOTH the talker's ``routine_done`` (via ``cmd_done``) and the
board's ``/feed/act`` completion dispatcher call the IDENTICAL write function.
Single writer per lane — the recording-trust guarantee.

Contract (deliberately NARROW vs the CLI):
  * NO fuzzy matching. The verbatim ``item_text`` must be present on the record
    (``EXACT`` existence check) — else ``unknown_item``. The CLI keeps its
    strict+fuzzy resolution cascade and passes the CANONICAL item text down;
    the board passes the producer's own stamped ``item_text`` evidence. Both
    are already-resolved verbatim strings by the time they reach here.
  * Locked + atomic. The whole read → mutate → write runs under
    ``file_rmw_lock`` on the record path (the #37 pattern, the SAME sidecar
    ``tier.promote`` and the item-CRUD verbs lock) so concurrent routine
    writers serialize; the write itself is ``.tmp`` → ``os.replace`` (torn-read
    fix).
  * Idempotent. ``done`` on an already-logged (item, date) is an
    ``idempotent_noop`` that skips the write entirely (no mtime bump, no YAML
    reflow). ``undone`` on a not-logged (item, date) is ``not_logged`` — same
    untouched-file discipline.
  * Structured return, no I/O side effects beyond the record write and the
    named log line. The caller owns operator-facing canary / reply rendering.

This module is a LEAF: it imports nothing from ``routine/cli.py`` or
``routine/cli_items.py`` (which both import canary constants from ``cli.py``),
so there is no import cycle. It owns its own load/normalise/write mirror of the
``cli_items`` primitives — the completion-log normalisation shape is identical
by construction and pinned by tests.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date as _date
from pathlib import Path

import frontmatter  # type: ignore[import-untyped]
import structlog
import yaml

from alfred.common.file_lock import file_rmw_lock

log = structlog.get_logger(__name__)

# --- result kinds ------------------------------------------------------------
# String VALUES match the ``routine/cli.py`` canary kinds where they overlap
# (``success`` / ``idempotent_noop`` / ``unknown_item`` / ``unknown_record``)
# so the CLI maps a writer result straight onto its existing DONE_KIND_* canary
# without translating strings. cli.py keeps its own frozen DONE_KIND_* export
# (the talker/SKILL cross-agent contract); these are the writer's own vocab.
DONE_KIND_SUCCESS = "success"
DONE_KIND_IDEMPOTENT_NOOP = "idempotent_noop"
DONE_KIND_UNKNOWN_ITEM = "unknown_item"
DONE_KIND_UNKNOWN_RECORD = "unknown_record"
# undo (un-log) kinds — mirror cli_items.py's ITEM_KIND_UNLOGGED / NOT_LOGGED.
UNDONE_KIND_UNLOGGED = "unlogged"
UNDONE_KIND_NOT_LOGGED = "not_logged"


@dataclass
class RoutineCompletionResult:
    """Outcome of a :func:`mark_routine_item_done` / ``_undone`` call.

    ``kind`` is the discriminator (a ``DONE_KIND_*`` / ``UNDONE_KIND_*``
    constant). ``item`` is the verbatim on-record item text acted on (echoed
    for the caller's reply). ``date`` is the ISO date. ``changed`` is True on a
    real write (append on done, removal on undone), False on the idempotent
    no-op / not-logged / unknown paths. ``remaining_dates`` carries the
    post-removal date list (undone only). ``record_path`` is the absolute path
    written.
    """

    kind: str
    item: str
    date: str
    record_path: Path | None = None
    changed: bool = False
    remaining_dates: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True for the non-error end-states (the item IS in the desired
        completion state): success / idempotent_noop / unlogged / not_logged.
        ``unknown_item`` / ``unknown_record`` are the error end-states."""
        return self.kind in (
            DONE_KIND_SUCCESS,
            DONE_KIND_IDEMPOTENT_NOOP,
            UNDONE_KIND_UNLOGGED,
            UNDONE_KIND_NOT_LOGGED,
        )


def _normalise_completion_log(raw: object, *, path: Path) -> dict[str, list[str]]:
    """Coerce a record's ``completion_log`` frontmatter into
    ``{item_text: [iso_date, ...]}``.

    Mirror of ``cli_items._load_record_state``'s normalisation (scalar → single
    list, YAML-native date → isoformat, non-str/date entries dropped). Kept
    byte-identical in behaviour so the extracted writer produces the same
    on-disk shape ``cmd_done`` produced inline — INCLUDING the
    ``completion_log_not_dict`` warning an operator hand-edit (list-instead-of-
    dict) fires, so the corrupt-record observability signal the CLI emitted
    survives the extraction (intentionally-left-blank; pinned by
    ``test_corrupt_record_json_stdout_stays_clean``).
    """
    if raw is not None and not isinstance(raw, dict):
        # Operator hand-edit dropped the dict — restore, but keep the signal.
        log.warning(
            "routine.completion.completion_log_not_dict",
            path=str(path),
            type=type(raw).__name__,
        )
    if not isinstance(raw, dict):
        return {}
    out: dict[str, list[str]] = {}
    for key, val in raw.items():
        if isinstance(val, list):
            normalised: list[str] = []
            for v in val:
                if isinstance(v, _date):
                    normalised.append(v.isoformat())
                elif isinstance(v, str):
                    normalised.append(v)
                # else: silently drop (defensive against operator hand-edit)
            out[str(key)] = normalised
        elif isinstance(val, (str, _date)):
            out[str(key)] = [val.isoformat() if isinstance(val, _date) else val]
        else:
            out[str(key)] = []
    return out


def _on_record_item_texts(fm: dict) -> set[str]:
    """The set of verbatim ``items[].text`` on a record (stripped)."""
    raw_items = fm.get("items") or []
    if not isinstance(raw_items, list):
        return set()
    texts: set[str] = set()
    for it in raw_items:
        if not isinstance(it, dict):
            continue
        t = str((it or {}).get("text") or "").strip()
        if t:
            texts.add(t)
    return texts


def _write_record(record_path: Path, fm: dict, content: str) -> None:
    """Serialise ``fm`` (key order preserved) + ``content`` back atomically.

    Same round-trip as ``cmd_done`` / ``cli_items._write_record_state``:
    ``yaml.dump`` with ``sort_keys=False`` (preserve operator key order),
    ``.tmp`` → ``os.replace`` (torn-read fix). Pairs with the enclosing
    ``file_rmw_lock`` (lost-update fix).
    """
    fm_yaml = yaml.dump(
        fm, default_flow_style=False, allow_unicode=True, sort_keys=False,
    )
    out = f"---\n{fm_yaml}---\n\n{content}\n"
    tmp = record_path.with_name(record_path.name + ".tmp")
    tmp.write_text(out, encoding="utf-8")
    os.replace(tmp, record_path)


def mark_routine_item_done(
    record_path: Path,
    item_text: str,
    date: str,
) -> RoutineCompletionResult:
    """Append ``date`` to ``completion_log[item_text]`` on ``record_path``.

    ``record_path`` is the ALREADY-RESOLVED record file (both callers hold it:
    ``cmd_done`` via its resolution cascade, the board via the producer's
    stamped ``path`` evidence — resolving here would re-introduce the WARN-1
    name-vs-stem crash the vault-wide-fuzzy path already dodges). ``item_text``
    is the verbatim on-record text (NO fuzzy here — the caller canonicalises).

    Returns:
      * ``unknown_record`` — the file doesn't exist / can't be read.
      * ``unknown_item`` — ``item_text`` is not verbatim on the record's items.
      * ``idempotent_noop`` — ``date`` already logged for the item (no write).
      * ``success`` — appended (one write).

    The whole load → check → append → write runs under ``file_rmw_lock`` so a
    concurrent completion (board double-tap, or talker + board racing the same
    item) can't lost-update. Emits ONE named log per branch
    (intentionally-left-blank) so the outcome is grep-able.
    """
    record_path = Path(record_path)
    if not record_path.is_file():
        log.info(
            "routine.completion.done.unknown_record",
            path=str(record_path), item=item_text, date=date,
        )
        return RoutineCompletionResult(
            kind=DONE_KIND_UNKNOWN_RECORD, item=item_text, date=date,
            record_path=record_path,
        )

    with file_rmw_lock(record_path):
        try:
            post = frontmatter.load(str(record_path))
        except (OSError, ValueError, yaml.YAMLError) as exc:
            log.warning(
                "routine.completion.done.read_failed",
                path=str(record_path), item=item_text, date=date,
                error=str(exc),
            )
            return RoutineCompletionResult(
                kind=DONE_KIND_UNKNOWN_RECORD, item=item_text, date=date,
                record_path=record_path,
            )
        fm = dict(post.metadata or {})

        # EXACT existence check — the verbatim item must be on the record. NO
        # fuzzy: the caller already resolved to the canonical text. A miss here
        # is an honest dead-end (item removed between resolution and write, or a
        # board evidence key that no longer matches the record).
        if item_text not in _on_record_item_texts(fm):
            log.info(
                "routine.completion.done.unknown_item",
                path=str(record_path), item=item_text, date=date,
            )
            return RoutineCompletionResult(
                kind=DONE_KIND_UNKNOWN_ITEM, item=item_text, date=date,
                record_path=record_path,
            )

        completion_log = _normalise_completion_log(fm.get("completion_log"), path=record_path)
        existing = completion_log.get(item_text, [])
        if date in existing:
            log.info(
                "routine.completion.done.idempotent_noop",
                path=str(record_path), item=item_text, date=date,
            )
            return RoutineCompletionResult(
                kind=DONE_KIND_IDEMPOTENT_NOOP, item=item_text, date=date,
                record_path=record_path, changed=False,
            )

        completion_log[item_text] = existing + [date]
        fm["completion_log"] = completion_log
        _write_record(record_path, fm, post.content)

    log.info(
        "routine.completion.done.success",
        path=str(record_path), item=item_text, date=date,
    )
    return RoutineCompletionResult(
        kind=DONE_KIND_SUCCESS, item=item_text, date=date,
        record_path=record_path, changed=True,
    )


def mark_routine_item_undone(
    record_path: Path,
    item_text: str,
    date: str,
) -> RoutineCompletionResult:
    """Remove ONE ``date`` from ``completion_log[item_text]`` — the surgical
    inverse of :func:`mark_routine_item_done`.

    Same locked/atomic/no-fuzzy discipline. When the list empties, the key is
    KEPT as ``[]`` (mirror ``cmd_done``'s shape; only ``item remove`` drops the
    whole key). Returns:
      * ``unknown_record`` — file missing / unreadable.
      * ``unknown_item`` — ``item_text`` not verbatim on the record.
      * ``not_logged`` — ``date`` wasn't present (file left UNTOUCHED — the
        desired end-state already holds; idempotent, not an error).
      * ``unlogged`` — removed (one write).
    """
    record_path = Path(record_path)
    if not record_path.is_file():
        log.info(
            "routine.completion.undone.unknown_record",
            path=str(record_path), item=item_text, date=date,
        )
        return RoutineCompletionResult(
            kind=DONE_KIND_UNKNOWN_RECORD, item=item_text, date=date,
            record_path=record_path,
        )

    with file_rmw_lock(record_path):
        try:
            post = frontmatter.load(str(record_path))
        except (OSError, ValueError, yaml.YAMLError) as exc:
            log.warning(
                "routine.completion.undone.read_failed",
                path=str(record_path), item=item_text, date=date,
                error=str(exc),
            )
            return RoutineCompletionResult(
                kind=DONE_KIND_UNKNOWN_RECORD, item=item_text, date=date,
                record_path=record_path,
            )
        fm = dict(post.metadata or {})

        if item_text not in _on_record_item_texts(fm):
            log.info(
                "routine.completion.undone.unknown_item",
                path=str(record_path), item=item_text, date=date,
            )
            return RoutineCompletionResult(
                kind=DONE_KIND_UNKNOWN_ITEM, item=item_text, date=date,
                record_path=record_path,
            )

        completion_log = _normalise_completion_log(fm.get("completion_log"), path=record_path)
        existing = completion_log.get(item_text, [])
        if date not in existing:
            # ILB no-op: leave the file untouched (bytes + mtime).
            log.info(
                "routine.completion.undone.not_logged",
                path=str(record_path), item=item_text, date=date,
            )
            return RoutineCompletionResult(
                kind=UNDONE_KIND_NOT_LOGGED, item=item_text, date=date,
                record_path=record_path, changed=False,
                remaining_dates=list(existing),
            )

        remaining = [d for d in existing if d != date]
        completion_log[item_text] = remaining
        fm["completion_log"] = completion_log
        _write_record(record_path, fm, post.content)

    log.info(
        "routine.completion.undone.unlogged",
        path=str(record_path), item=item_text, date=date,
        remaining=len(remaining),
    )
    return RoutineCompletionResult(
        kind=UNDONE_KIND_UNLOGGED, item=item_text, date=date,
        record_path=record_path, changed=True, remaining_dates=remaining,
    )


__all__ = [
    "DONE_KIND_IDEMPOTENT_NOOP",
    "DONE_KIND_SUCCESS",
    "DONE_KIND_UNKNOWN_ITEM",
    "DONE_KIND_UNKNOWN_RECORD",
    "RoutineCompletionResult",
    "UNDONE_KIND_NOT_LOGGED",
    "UNDONE_KIND_UNLOGGED",
    "mark_routine_item_done",
    "mark_routine_item_undone",
]
