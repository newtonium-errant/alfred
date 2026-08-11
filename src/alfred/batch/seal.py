"""The seal guard — refuse to regenerate a record the operator has taken.

The batch worker rewrites its carried record's body wholesale on every
checkpoint. That is safe only while the body is still machine-owned.
The moment the operator seals the record, the body is THEIRS, and a
regeneration would silently destroy their edits with no vault-side
recovery.

Shape copied deliberately from scribe's ``_update_or_refuse_ai_draft``:

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

log = structlog.get_logger(__name__)

#: The ONE status under which the body may be regenerated.
BATCH_STATUS_OPEN = "open"

#: The status the operator (or a completion step) sets to take the body.
BATCH_STATUS_SEALED = "sealed"


class BatchSealedError(RuntimeError):
    """The carried record is sealed — regeneration refused.

    Message always contains ``SEALED`` so callers can classify it
    without matching on the exception type across module boundaries.
    """


def is_regenerable(frontmatter: dict[str, Any] | None) -> bool:
    """True iff the record's body is still machine-owned."""
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
    """
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
