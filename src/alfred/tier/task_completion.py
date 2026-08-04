"""Neutral task-record completion writer — the board's task lane (C1b).

Phase C slice 1b: the task-origin slot card's ✓ was honestly disabled in C1
(no talker task-completion writer existed). Operator friction elevated it — this
module DEFINES the deterministic completion path for a task record, so the board
and (later) the talker share ONE writer, single-source-of-doneness with the
tier overlay's ``_task_is_done_today`` predicate.

**What it writes = exactly what the predicate reads** (``tier.compute._task_is_done_today``):
  * ``status: done`` — flips the record OUT of ``OPEN_STATUSES`` (``{todo, active,
    blocked}``); ``done`` is the completion status in the task vocab.
  * ``completed: <date>`` — the predicate's today-check reads the first present of
    ``(completed, done, completed_at, closed)``; we use ``completed`` (never the
    ``done:`` KEY — ``done`` is already the status VALUE, a collision trap).

Contract (same law as the routine/tier board writers):
  * Exact-path addressed — the caller passes the producer's stamped
    ``evidence.path`` (``task/<Name>.md``); NO fuzzy, NO name→stem resolution.
  * Idempotent — a task already ``status: done`` → ``idempotent_noop`` (no write).
  * Fail-loud on an unknown status vocabulary — a record whose current status is
    not in the task STATUS registry is a lifecycle we refuse to GUESS through
    (``invalid_status``); never blindly stamp ``done`` over an unknown state.
  * Locked + atomic — ``file_rmw_lock`` on the record + ``.tmp`` → ``os.replace``
    (the house-consistent idiom the routine/tier board writers use; the web
    transport writes in-process, so this is the sibling-consistent route).
  * Structured result + one named log per branch (intentionally-left-blank).

v1 is DONE-ONLY: the prior open status ({todo|active|blocked}) is not cleanly
restorable (a done task could have been any of the three; restoring a fixed one
would guess a lifecycle), so the board's ``undo_done`` stays ``unsupported`` for
the task lane. A future symmetric undo would stamp the prior status at done-time.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import frontmatter  # type: ignore[import-untyped]
import structlog
import yaml

from alfred.common.file_lock import file_rmw_lock
from alfred.vault.paths import VaultContainmentError, resolve_in_vault
from alfred.vault.schema import STATUS_BY_TYPE

log = structlog.get_logger(__name__)

# The completion status + the field the predicate's today-check reads. Kept next
# to the vocab import so a schema change surfaces here.
_TASK_STATUSES: frozenset[str] = STATUS_BY_TYPE["task"]  # {todo,active,blocked,done,cancelled}
DONE_STATUS = "done"
COMPLETED_FIELD = "completed"

# result kinds — DONE_KIND-style vocab (string values mirror the routine/tier
# writers where they overlap, so a caller maps them without translating).
TASK_DONE_KIND_SUCCESS = "success"
TASK_DONE_KIND_IDEMPOTENT_NOOP = "idempotent_noop"
TASK_DONE_KIND_UNKNOWN_RECORD = "unknown_record"
TASK_DONE_KIND_INVALID_STATUS = "invalid_status"


@dataclass
class TaskCompletionResult:
    """Outcome of a :func:`mark_task_done` call.

    ``kind`` is the discriminator. ``name`` is the task's frontmatter name (echoed
    for the caller's reply). ``date`` is the ISO completion date on the success /
    idempotent paths. ``status`` is the record's status AFTER the call (``done``
    on success/noop; the offending value on ``invalid_status``). ``changed`` is
    True only on a real write.
    """

    kind: str
    path: str
    name: str = ""
    date: str = ""
    status: str = ""
    changed: bool = False

    @property
    def ok(self) -> bool:
        """True for the non-error end-states (the task IS done)."""
        return self.kind in (TASK_DONE_KIND_SUCCESS, TASK_DONE_KIND_IDEMPOTENT_NOOP)


def _write_record(record_path: Path, fm: dict, content: str) -> None:
    """Serialise ``fm`` (operator key order preserved) + ``content`` atomically —
    the same round-trip as the routine/tier board writers."""
    fm_yaml = yaml.dump(
        fm, default_flow_style=False, allow_unicode=True, sort_keys=False,
    )
    out = f"---\n{fm_yaml}---\n\n{content}\n"
    tmp = record_path.with_name(record_path.name + ".tmp")
    tmp.write_text(out, encoding="utf-8")
    os.replace(tmp, record_path)


def mark_task_done(
    vault_path: Path,
    task_path: str,
    date: str,
) -> TaskCompletionResult:
    """Mark the task at ``task_path`` (relative to ``vault_path``) done on ``date``.

    Sets ``status: done`` + ``completed: <date>`` — exactly what
    ``tier.compute._task_is_done_today`` reads — under ``file_rmw_lock`` with an
    atomic write. Returns:
      * ``unknown_record`` — no file at ``task_path``, unreadable, or not a
        ``type: task`` record (the path doesn't address a task).
      * ``invalid_status`` — the record's current status is outside the task
        STATUS vocab: fail loud, never guess the lifecycle (no write).
      * ``idempotent_noop`` — already ``status: done`` (no write).
      * ``success`` — flipped to done (one write).
    """
    # Arc #18 containment gate. ``task_path`` arrives as the producer-stamped
    # ``evidence["path"]``, which for a CURATED task is interpolated from a
    # wikilink read back out of the daily file (``tier.compute._curated_task_path``
    # does ``f"task/{name}.md"``) — and that name is persisted from an
    # unsanitised LLM tool argument. So it is composed BEFORE it is trusted.
    # Refuse outside-vault targets as ``unknown_record`` (the path does not
    # address a task in this vault) rather than widening the result vocabulary;
    # the distinct signal lives in the log event.
    try:
        record_path = resolve_in_vault(vault_path, task_path, writer="tier.task_done")
    except VaultContainmentError:
        log.warning("tier.task_done.path_escape_denied", path=task_path, date=date)
        return TaskCompletionResult(kind=TASK_DONE_KIND_UNKNOWN_RECORD, path=task_path)

    if not record_path.is_file():
        log.info("tier.task_done.unknown_record", path=task_path, date=date)
        return TaskCompletionResult(kind=TASK_DONE_KIND_UNKNOWN_RECORD, path=task_path)

    with file_rmw_lock(record_path):
        try:
            post = frontmatter.load(str(record_path))
        except (OSError, ValueError, yaml.YAMLError) as exc:
            log.warning(
                "tier.task_done.read_failed", path=task_path, date=date, error=str(exc),
            )
            return TaskCompletionResult(kind=TASK_DONE_KIND_UNKNOWN_RECORD, path=task_path)
        fm = dict(post.metadata or {})

        if str(fm.get("type") or "").strip().lower() != "task":
            log.info(
                "tier.task_done.unknown_record", path=task_path, date=date,
                reason="not_a_task_record",
            )
            return TaskCompletionResult(kind=TASK_DONE_KIND_UNKNOWN_RECORD, path=task_path)

        name = str(fm.get("name") or record_path.stem)
        status = str(fm.get("status") or "todo").lower()

        # Fail loud on an unknown lifecycle — never stamp done over a status the
        # schema doesn't define (an operator typo / a foreign record).
        if status not in _TASK_STATUSES:
            log.warning(
                "tier.task_done.invalid_status", path=task_path, date=date,
                status=status, known=sorted(_TASK_STATUSES),
            )
            return TaskCompletionResult(
                kind=TASK_DONE_KIND_INVALID_STATUS, path=task_path, name=name,
                status=status,
            )

        if status == DONE_STATUS:
            log.info("tier.task_done.idempotent_noop", path=task_path, name=name, date=date)
            return TaskCompletionResult(
                kind=TASK_DONE_KIND_IDEMPOTENT_NOOP, path=task_path, name=name,
                date=str(fm.get(COMPLETED_FIELD) or date), status=DONE_STATUS,
            )

        fm["status"] = DONE_STATUS
        fm[COMPLETED_FIELD] = date
        _write_record(record_path, fm, post.content)

    log.info("tier.task_done.success", path=task_path, name=name, date=date)
    return TaskCompletionResult(
        kind=TASK_DONE_KIND_SUCCESS, path=task_path, name=name, date=date,
        status=DONE_STATUS, changed=True,
    )


__all__ = [
    "COMPLETED_FIELD",
    "DONE_STATUS",
    "TASK_DONE_KIND_IDEMPOTENT_NOOP",
    "TASK_DONE_KIND_INVALID_STATUS",
    "TASK_DONE_KIND_SUCCESS",
    "TASK_DONE_KIND_UNKNOWN_RECORD",
    "TaskCompletionResult",
    "mark_task_done",
]
