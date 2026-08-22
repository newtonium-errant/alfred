"""Neutral task-record CANCELLATION writer — the board's task lane (#103).

The operator's friction, verbatim: *"There's no quick way to remove a card once
assigned without talking to Salem directly. For example, TheJamieClinic email
setup no longer needs to be done, so I don't want to mark it done, but I do want
it removed from the list as cancelled."*

So this module writes the THIRD disposition. ``done`` says *I did it*, snooze
says *yes, but later*, and ``cancelled`` says *never — moot*. The board could
already say the first two and the operator had to open a conversation for the
third.

A SEPARATE MODULE FROM ``task_completion``, and the separation is structural
rather than tidy. ``daily_sync.action_router`` pins its verb families by what
they can REACH: ``_dispatch_slot_snooze``'s docstring says it "imports no
completion writer and calls none, so a snooze cannot fake a completion no matter
what evidence arrives", and a test reads that function's bytecode names to keep
it true. A cancel gets the same guarantee for free by living in a module that
does not contain ``mark_task_done`` — had these two writers shared a file, the
cancel dispatcher would have had to import the completion writer's module to
reach its own function, and the guarantee would have degraded from *structural*
to *by convention*.

**What it writes**, and every field earns its place:
  * ``status: cancelled`` — a legal ``task`` status since the schema's
    beginning (``vault.schema`` ``STATUS_BY_TYPE["task"]``) and, until this
    module, written to a task by nothing in ``src/alfred/`` except two one-shot
    migration scripts. It is NOT in ``tier.compute.OPEN_STATUSES``, which is
    what makes the write sufficient on its own to retire the card everywhere
    (see the return-suppression note below).
  * ``cancelled_at: <date>`` — SAMPLED, NOT MINTED. This is the house spelling,
    taken from ``alfred.scripts.migrate_routine_recurring_bills`` and
    ``alfred.scripts.migrate_tier_phase1``, both of which stamp
    ``status: cancelled`` + ``cancelled_at: <ISO date>`` on task records and
    have their own pins on it. The completion writer's sibling field is
    ``completed``; picking ``cancelled`` to rhyme with it would have MINTED a
    second spelling for a field the vault already carries.
  * ``cancelled_from: <prior status>`` — the one NEW field, and it is what makes
    :func:`restore_cancelled_task` exact. ``task_completion``'s own module
    docstring names this remedy: *"A future symmetric undo would stamp the prior
    status at done-time."* Cancel is the first verb in a position to do it,
    because this writer is being written now. Without it an undo would have to
    GUESS between ``todo`` / ``active`` / ``blocked``, which is precisely the
    objection that leaves ``undo_done`` unsupported on this lane.

**What it deliberately does NOT write: a free-text reason.** The gesture that
reaches this writer is a rung on a hold-menu — it carries no text box, so a
``cancel_reason`` field would be born empty at every board call site and stay
empty, which is the conditionally-present-field drift ``feedback_intentionally
_left_blank`` rules out (an always-absent field cannot be told apart from a
producer that never ran). What IS knowable without asking is provenance, not
motive: ``cancelled_from`` + ``cancelled_at`` answer *what state it was in* and
*when it stopped*, and the record keeps its body, its links and its history per
the GCal precedent (``status: cancelled`` is struck through and KEPT, never
deleted). An operator who wants to say WHY still has the conversational path —
``vault-talker/SKILL.md``'s ``task`` → ``set_fields {"status": "cancelled"}``
recipe is unchanged and can carry any annotation he dictates. The board is the
fast path; the conversation is the annotated one.

**No body write, and that is what keeps cancel and undo exact inverses.** A
dated body line cannot be cleanly removed by an undo without string surgery, a
whole failure class; keeping the mutation purely in frontmatter means
:func:`restore_cancelled_task` is a true inverse rather than an approximate one.
(The migration scripts DO body-append — correctly, because a migration is
one-way and carries a backlink a reader needs. An undoable board gesture is the
opposite case.)

Contract (the same law as ``task_completion`` and the routine/tier writers):
  * Exact-path addressed — the caller passes the producer's stamped
    ``evidence.path`` (``task/<Name>.md``); NO fuzzy, NO name→stem resolution.
  * Containment-gated — ``resolve_in_vault`` before any read or write.
  * Idempotent — already ``status: cancelled`` → ``idempotent_noop`` (no write).
  * Fail-loud on an unknown status vocabulary — never stamp over a lifecycle we
    cannot name (``invalid_status``).
  * Locked + atomic — ``file_rmw_lock`` + ``.tmp`` → ``os.replace``.
  * Structured result + one named log per branch (intentionally-left-blank).

**Return suppression comes free, and is pinned rather than assumed.** The card
in the operator's report surfaced via ``surface_reason: returned`` /
``source: auto-returned``, produced by ``tier.compute._returned_task_candidates``
— which skips any record whose ``status`` is not exactly ``"todo"``. So the
status write alone stops the return; there is no second suppression list to
maintain, and no snooze-store entry to write. The same single filter retires the
card from the T1/T2 pools (``status not in OPEN_STATUSES``) and from the
``reminder_returned`` sweep (whose ``_refresh_or_retire`` retires on "status
left the scheduler's eligible set (done / cancelled)"). Every one of those is
driven by a test rather than reasoned about — see
``tests/tier/test_task_cancel.py``.
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

# The task status vocabulary, read from the schema registry rather than spelled
# here — a status added or removed upstream surfaces at this import, not as a
# silent divergence. (Premise-pinned in the test module against ``schema.py``:
# a constant DERIVED from the thing it is checked against would move with the
# bug and score a mutation RED 0.)
_TASK_STATUSES: frozenset[str] = STATUS_BY_TYPE["task"]  # {todo,active,blocked,done,cancelled}

#: The disposition this module writes.
CANCELLED_STATUS = "cancelled"

#: The date field. HOUSE SPELLING — see the module docstring; both migration
#: scripts already stamp this exact key on task records.
CANCELLED_AT_FIELD = "cancelled_at"

#: The prior-status field that makes the undo exact rather than a guess.
CANCELLED_FROM_FIELD = "cancelled_from"

#: The statuses a cancel may be applied to, and equally the ones a restore may
#: put back. ``done`` is excluded on purpose in BOTH directions: cancelling a
#: task you already finished is meaningless (the router refuses it with its own
#: named reason), and restoring INTO ``done`` would be a completion claim this
#: module has no business making.
_RESTORABLE_STATUSES: frozenset[str] = frozenset({"todo", "active", "blocked"})

# result kinds — mirrors ``task_completion``'s vocabulary where the two overlap,
# so a caller maps them without translating.
TASK_CANCEL_KIND_SUCCESS = "success"
TASK_CANCEL_KIND_IDEMPOTENT_NOOP = "idempotent_noop"
TASK_CANCEL_KIND_UNKNOWN_RECORD = "unknown_record"
TASK_CANCEL_KIND_INVALID_STATUS = "invalid_status"
TASK_CANCEL_KIND_ALREADY_DONE = "already_done"

# restore-only kinds.
TASK_RESTORE_KIND_SUCCESS = "success"
TASK_RESTORE_KIND_NOT_CANCELLED = "not_cancelled"
TASK_RESTORE_KIND_UNKNOWN_RECORD = "unknown_record"
TASK_RESTORE_KIND_NO_PRIOR_STATUS = "no_prior_status"


@dataclass
class TaskCancelResult:
    """Outcome of a :func:`mark_task_cancelled` / :func:`restore_cancelled_task`.

    ``kind`` is the discriminator. ``name`` is the task's frontmatter name
    (echoed for the caller's reply). ``status`` is the record's status AFTER the
    call — ``cancelled`` on a successful cancel, the restored status on a
    successful restore, the offending value on ``invalid_status`` /
    ``already_done``. ``prior_status`` is what the cancel recorded (or what the
    restore put back). ``changed`` is True only on a real write.
    """

    kind: str
    path: str
    name: str = ""
    date: str = ""
    status: str = ""
    prior_status: str = ""
    changed: bool = False

    @property
    def ok(self) -> bool:
        """True for the non-error end-states (the record IS in the target state).

        Note ``already_done`` is NOT ok: the record is closed, but it is closed
        in a way the caller did not ask for and must report differently.
        """
        return self.kind in (TASK_CANCEL_KIND_SUCCESS, TASK_CANCEL_KIND_IDEMPOTENT_NOOP)


def _write_record(record_path: Path, fm: dict, content: str) -> None:
    """Serialise ``fm`` (operator key order preserved) + ``content`` atomically —
    the same round-trip as ``task_completion`` and the routine/tier board
    writers. Deliberately duplicated rather than imported: importing it would
    pull ``mark_task_done`` into this module's import graph and give up the
    structural guarantee the module docstring is built on."""
    fm_yaml = yaml.dump(
        fm, default_flow_style=False, allow_unicode=True, sort_keys=False,
    )
    out = f"---\n{fm_yaml}---\n\n{content}\n"
    tmp = record_path.with_name(record_path.name + ".tmp")
    tmp.write_text(out, encoding="utf-8")
    os.replace(tmp, record_path)


def _resolve_task_path(
    vault_path: Path, task_path: str, *, writer: str, event: str,
) -> Path | None:
    """Containment-gate ``task_path`` and confirm a file is there, or ``None``.

    ``task_path`` arrives as the producer-stamped ``evidence["path"]``, which for
    a curated task is interpolated from a wikilink read back out of the daily
    file — i.e. composed from an unsanitised LLM tool argument BEFORE it is
    trusted. Same gate, same reasoning, as ``task_completion.mark_task_done``.
    """
    try:
        record_path = resolve_in_vault(vault_path, task_path, writer=writer)
    except VaultContainmentError:
        log.warning(f"{event}.path_escape_denied", path=task_path)
        return None
    if not record_path.is_file():
        log.info(f"{event}.unknown_record", path=task_path)
        return None
    return record_path


def mark_task_cancelled(
    vault_path: Path,
    task_path: str,
    date: str,
) -> TaskCancelResult:
    """Cancel the task at ``task_path`` (relative to ``vault_path``) on ``date``.

    Sets ``status: cancelled`` + ``cancelled_at: <date>`` +
    ``cancelled_from: <prior status>`` under ``file_rmw_lock`` with an atomic
    write. Returns:
      * ``unknown_record`` — no file at ``task_path``, unreadable, outside the
        vault, or not a ``type: task`` record.
      * ``invalid_status`` — the current status is outside the task STATUS
        vocab: fail loud, never guess the lifecycle (no write).
      * ``already_done`` — the task is ``status: done``. Cancelling a completed
        task is meaningless and would ERASE a completion record, so it is
        refused with its own reason rather than folded into ``invalid_status``
        (a refusal that cannot say why reads identically to one firing for an
        unrelated cause).
      * ``idempotent_noop`` — already ``status: cancelled`` (no write).
      * ``success`` — cancelled (one write).
    """
    record_path = _resolve_task_path(
        vault_path, task_path, writer="tier.task_cancel", event="tier.task_cancel",
    )
    if record_path is None:
        return TaskCancelResult(kind=TASK_CANCEL_KIND_UNKNOWN_RECORD, path=task_path)

    with file_rmw_lock(record_path):
        try:
            post = frontmatter.load(str(record_path))
        except (OSError, ValueError, yaml.YAMLError) as exc:
            log.warning(
                "tier.task_cancel.read_failed", path=task_path, date=date,
                error=str(exc),
            )
            return TaskCancelResult(kind=TASK_CANCEL_KIND_UNKNOWN_RECORD, path=task_path)
        fm = dict(post.metadata or {})

        if str(fm.get("type") or "").strip().lower() != "task":
            log.info(
                "tier.task_cancel.unknown_record", path=task_path, date=date,
                reason="not_a_task_record",
            )
            return TaskCancelResult(kind=TASK_CANCEL_KIND_UNKNOWN_RECORD, path=task_path)

        name = str(fm.get("name") or record_path.stem)
        status = str(fm.get("status") or "todo").lower()

        if status not in _TASK_STATUSES:
            log.warning(
                "tier.task_cancel.invalid_status", path=task_path, date=date,
                status=status, known=sorted(_TASK_STATUSES),
            )
            return TaskCancelResult(
                kind=TASK_CANCEL_KIND_INVALID_STATUS, path=task_path, name=name,
                status=status,
            )

        if status == CANCELLED_STATUS:
            log.info(
                "tier.task_cancel.idempotent_noop", path=task_path, name=name,
                date=date,
            )
            return TaskCancelResult(
                kind=TASK_CANCEL_KIND_IDEMPOTENT_NOOP, path=task_path, name=name,
                date=str(fm.get(CANCELLED_AT_FIELD) or date),
                status=CANCELLED_STATUS,
                prior_status=str(fm.get(CANCELLED_FROM_FIELD) or ""),
            )

        # A completed task is closed ALREADY, and closing it a second way would
        # overwrite the completion. Named refusal, not a silent accept.
        if status not in _RESTORABLE_STATUSES:
            log.info(
                "tier.task_cancel.already_done", path=task_path, name=name,
                date=date, status=status,
            )
            return TaskCancelResult(
                kind=TASK_CANCEL_KIND_ALREADY_DONE, path=task_path, name=name,
                status=status,
            )

        fm["status"] = CANCELLED_STATUS
        fm[CANCELLED_AT_FIELD] = date
        fm[CANCELLED_FROM_FIELD] = status
        _write_record(record_path, fm, post.content)

    log.info(
        "tier.task_cancel.success", path=task_path, name=name, date=date,
        prior_status=status,
    )
    return TaskCancelResult(
        kind=TASK_CANCEL_KIND_SUCCESS, path=task_path, name=name, date=date,
        status=CANCELLED_STATUS, prior_status=status, changed=True,
    )


def restore_cancelled_task(
    vault_path: Path,
    task_path: str,
) -> TaskCancelResult:
    """Reverse a cancel — put the task back in the status it was cancelled from.

    Reads ``cancelled_from`` and restores it, clearing ``cancelled_at`` and
    ``cancelled_from``. Exact, never a guess: this is the whole reason the cancel
    stamps the prior status.

      * ``unknown_record`` — as :func:`mark_task_cancelled`.
      * ``not_cancelled`` — the record is not ``status: cancelled``; there is
        nothing to reverse (no write).
      * ``no_prior_status`` — the record IS cancelled but carries no usable
        ``cancelled_from``. THE HONEST DEAD-END, and the live case for it is a
        cancel that did not come from this writer: a task the operator cancelled
        through Salem (``vault_edit set_fields {"status": "cancelled"}``) or one
        of the two migration scripts stamps no provenance, so its prior status is
        genuinely unknown. Guessing ``todo`` here would resurrect a ``blocked``
        task as actionable — the exact lifecycle-guess that keeps ``undo_done``
        unsupported on this lane. Refuse and say so (no write).
      * ``success`` — restored (one write).
    """
    record_path = _resolve_task_path(
        vault_path, task_path, writer="tier.task_restore", event="tier.task_restore",
    )
    if record_path is None:
        return TaskCancelResult(kind=TASK_RESTORE_KIND_UNKNOWN_RECORD, path=task_path)

    with file_rmw_lock(record_path):
        try:
            post = frontmatter.load(str(record_path))
        except (OSError, ValueError, yaml.YAMLError) as exc:
            log.warning(
                "tier.task_restore.read_failed", path=task_path, error=str(exc),
            )
            return TaskCancelResult(
                kind=TASK_RESTORE_KIND_UNKNOWN_RECORD, path=task_path,
            )
        fm = dict(post.metadata or {})

        if str(fm.get("type") or "").strip().lower() != "task":
            log.info(
                "tier.task_restore.unknown_record", path=task_path,
                reason="not_a_task_record",
            )
            return TaskCancelResult(
                kind=TASK_RESTORE_KIND_UNKNOWN_RECORD, path=task_path,
            )

        name = str(fm.get("name") or record_path.stem)
        status = str(fm.get("status") or "todo").lower()

        if status != CANCELLED_STATUS:
            log.info(
                "tier.task_restore.not_cancelled", path=task_path, name=name,
                status=status,
            )
            return TaskCancelResult(
                kind=TASK_RESTORE_KIND_NOT_CANCELLED, path=task_path, name=name,
                status=status,
            )

        prior = str(fm.get(CANCELLED_FROM_FIELD) or "").strip().lower()
        if prior not in _RESTORABLE_STATUSES:
            log.info(
                "tier.task_restore.no_prior_status", path=task_path, name=name,
                recorded=prior or "",
                detail=(
                    "cancelled without recorded provenance (not a board cancel) "
                    "— refusing to guess the lifecycle"
                ),
            )
            return TaskCancelResult(
                kind=TASK_RESTORE_KIND_NO_PRIOR_STATUS, path=task_path, name=name,
                status=CANCELLED_STATUS, prior_status=prior,
            )

        fm["status"] = prior
        fm.pop(CANCELLED_AT_FIELD, None)
        fm.pop(CANCELLED_FROM_FIELD, None)
        _write_record(record_path, fm, post.content)

    log.info(
        "tier.task_restore.success", path=task_path, name=name, restored_to=prior,
    )
    return TaskCancelResult(
        kind=TASK_RESTORE_KIND_SUCCESS, path=task_path, name=name, status=prior,
        prior_status=prior, changed=True,
    )


__all__ = [
    "CANCELLED_AT_FIELD",
    "CANCELLED_FROM_FIELD",
    "CANCELLED_STATUS",
    "TASK_CANCEL_KIND_ALREADY_DONE",
    "TASK_CANCEL_KIND_IDEMPOTENT_NOOP",
    "TASK_CANCEL_KIND_INVALID_STATUS",
    "TASK_CANCEL_KIND_SUCCESS",
    "TASK_CANCEL_KIND_UNKNOWN_RECORD",
    "TASK_RESTORE_KIND_NO_PRIOR_STATUS",
    "TASK_RESTORE_KIND_NOT_CANCELLED",
    "TASK_RESTORE_KIND_SUCCESS",
    "TASK_RESTORE_KIND_UNKNOWN_RECORD",
    "TaskCancelResult",
    "mark_task_cancelled",
    "restore_cancelled_task",
]
