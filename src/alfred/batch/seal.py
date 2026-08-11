"""The regenerate guard — refuse to overwrite a body that is not ours to write.

The batch worker rewrites its carried record's body wholesale on every
checkpoint. That is safe only when two things hold: the record is THIS
batch's, and its body is still machine-owned. This module refuses when
either fails, and it refuses BEFORE the model is called so a doomed item
costs nothing.

Two distinct causes, deliberately not conflated (see
:func:`assert_regenerable` for the ordering and the reasoning):

  * **Wrong owner** — the record belongs to a different batch. A wiring
    fault. Added after gate review WARN-1: the scope layer's ownership
    check is a PRESENCE check and cannot tell one batch from another,
    because ``check_scope`` never receives the expected batch id.
  * **Sealed** — the record is ours, but the operator has taken its
    body. Expected and benign.

The seal half's shape is copied deliberately from scribe's
``_update_or_refuse_ai_draft``:

  * **An allowlist of ONE, not a denylist of sealed values.** The record
    is regenerable iff ``batch_status == "open"``. A missing status, a
    typo'd status, an unknown future status, and an explicitly sealed
    one all REFUSE. Fail-closed is the whole point: the failure mode
    this guard prevents is unrecoverable, so an unrecognised value must
    stop the write rather than be assumed benign.
  * **Log then raise**, with the substring ``SEALED`` in the message —
    the caller classifies on it (scribe's caller does the same), so the
    wording is a load-bearing contract, not cosmetic prose.
  * **Terminal, not retriable.** A sealed record will still be sealed
    next run; retrying would burn the item's attempts and spend API
    credit producing results that can never be rendered.

The reason ``batch_status`` is absent from ``VERA_BATCH_EDIT_FIELDS`` is
this guard: the worker must not be able to un-seal a record it is being
refused. Only ``vault_create`` (at batch-open) and the operator set it.
"""

from __future__ import annotations

from typing import Any

import structlog

from ..vault.scope import BATCH_OWNER_FIELD

log = structlog.get_logger(__name__)

#: The ONE status under which the body may be regenerated.
BATCH_STATUS_OPEN = "open"

#: The status the operator (or a completion step) sets to take the body.
BATCH_STATUS_SEALED = "sealed"


class BatchSealedError(RuntimeError):
    """Regeneration refused — the body is not ours to overwrite.

    Raised for BOTH refusal causes (wrong owner, sealed). The name is
    kept for the seal case it was introduced for and because widening it
    would churn every caller for no behavioural gain; the two causes are
    distinguished by their log event rather than by type, since nothing
    branches on them today.

    A SEAL refusal's message always contains ``SEALED`` so a caller can
    classify it without importing this module (scribe's caller does the
    same). A WRONG-OWNER message deliberately does not — it names both
    batch ids instead, because "sealed" would be the wrong word for a
    record that was never ours.
    """


def is_regenerable(frontmatter: dict[str, Any] | None) -> bool:
    """True iff the record's STATUS still permits regeneration.

    Status only — this deliberately says nothing about WHICH batch owns
    the record. Ownership identity is :func:`assert_regenerable`'s job,
    because it is the only one of the two that receives the expected
    batch id. Keeping the name honest matters here: a helper called
    "is_regenerable" that silently ignored ownership is what let the
    WARN-1 case through review the first time.
    """
    if not isinstance(frontmatter, dict):
        return False
    return frontmatter.get("batch_status") == BATCH_STATUS_OPEN


def assert_regenerable(
    frontmatter: dict[str, Any] | None,
    *,
    batch_id: str,
    record_path: str = "",
) -> None:
    """Raise :class:`BatchSealedError` unless the record is regenerable.

    Called BEFORE the model is invoked, not after. Checking first is
    what makes a sealed batch cost nothing: there is no point paying for
    a vision call whose result has nowhere to land.

    TWO refusals, in this order, because they mean different things:

      1. **Wrong owner.** The record belongs to a DIFFERENT batch. This
         is the gate-review WARN-1 case, and it is a BUG rather than an
         operator action — a manifest whose ``record_path`` points at
         another batch's note, a stale path after a rename, or an id
         collision. The scope layer cannot catch it: ``check_scope``
         never receives the expected batch id, so its ownership check
         can only prove "owned by SOME batch". This function is the one
         place that holds both the expected id and the record's own, so
         it is the only place the IDENTITY can be compared. Checked
         first: being pointed at the wrong record entirely is more
         urgent than that record's status, and reporting "sealed" for
         someone else's note would send the operator to unseal a record
         that was never ours.
      2. **Sealed.** The record is ours but the operator has taken its
         body. Expected, benign, terminal.

    Both raise the same class; they are told apart by their log event
    (``batch.seal.wrong_owner`` vs ``batch.seal.regenerate_refused``)
    rather than by an exception hierarchy no caller currently branches
    on.
    """
    owner = (
        str(frontmatter.get(BATCH_OWNER_FIELD) or "").strip()
        if isinstance(frontmatter, dict)
        else ""
    )
    if owner and batch_id and owner != batch_id:
        log.warning(
            "batch.seal.wrong_owner",
            batch_id=batch_id,
            record_owner=owner,
            record_path=record_path,
            detail="carried record belongs to a different batch — "
                   "refusing to replace its body",
        )
        raise BatchSealedError(
            f"batch {batch_id}: carried record is owned by a DIFFERENT "
            f"batch ({BATCH_OWNER_FIELD}={owner!r}, expected "
            f"{batch_id!r}) — refusing to replace its body. Regenerating "
            f"here would overwrite another batch's results with this "
            f"one's. This is a wiring fault, not an operator action: "
            f"the manifest's record_path points at the wrong record."
        )

    if is_regenerable(frontmatter):
        return
    status = (
        frontmatter.get("batch_status")
        if isinstance(frontmatter, dict)
        else None
    )
    log.warning(
        "batch.seal.regenerate_refused",
        batch_id=batch_id,
        record_path=record_path,
        status=str(status) if status else "(missing)",
        detail="carried record is not an open batch — refusing to "
               "replace its body",
    )
    raise BatchSealedError(
        f"batch {batch_id}: carried record is SEALED "
        f"(batch_status={status or '(missing)'}) — its body belongs to "
        f"the operator now and a wholesale regeneration would destroy "
        f"their edits. Refusing. The ledger still holds every result; "
        f"set batch_status back to '{BATCH_STATUS_OPEN}' to resume "
        f"rendering, or read the results from the ledger directly."
    )


__all__ = [
    "BATCH_STATUS_OPEN",
    "BATCH_STATUS_SEALED",
    "BatchSealedError",
    "assert_regenerable",
    "is_regenerable",
]
