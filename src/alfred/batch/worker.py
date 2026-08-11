"""Process ONE scan: read it, record the result, re-render the record.

Shape follows ``instructor.executor.execute`` — a headless in-process
Anthropic call, no Session, vault writes gated by ``check_scope`` — with
one deliberate simplification: **the model gets no tools.**

That falls out of D2. The ledger is the truth and the record is a
derived render, so the model's only job is to look at an image and
return text; the ledger append and the wholesale re-render are
deterministic code running under ``vera_batch``. A tool-using model
could mutate the vault directly, which would make ``verify()``
meaningless (a clean return is not evidence — that is the 907 signature
the drip runner exists to catch) and would put an LLM behind a
body_replace grant. Text in, text out, code writes.

Order of operations, and why:

  1. **Already in the ledger? Return.** Content-addressed idempotency.
     Free, and it makes a resumed batch cheap.
  2. **Seal check.** BEFORE the model call, so a sealed batch costs
     nothing — there is no point paying for a result with nowhere to go.
  3. **Model call.** Exceptions propagate UNCAUGHT so the drip runner's
     ``_is_quota_block`` can classify a rate-limit or auth failure as
     BLOCKED (a pause, not a verdict: no attempt consumed, retried first
     next run). Swallowing them here would convert the quota guarantee
     into silent budget burn.
  4. **Append the ledger row**, fsync'd — the record of a paid-for call.
  5. **Re-render the vault record** from the full ledger.

Steps 4 and 5 are ordered so a crash between them loses nothing: the
render is pure, so the next run reproduces the identical body.
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

from .._anthropic_compat import messages_create_kwargs
from .ledger import (
    OUTCOME_OK,
    OUTCOME_QUARANTINED,
    append_row,
    build_row,
    has_processed,
    load_rows,
)
from .manifest import BatchImage, BatchManifest
from .render import progress_counts, render_body
from .seal import assert_regenerable

log = structlog.get_logger(__name__)

#: The scope every vault write in this module runs under.
BATCH_SCOPE = "vera_batch"

#: Prefix the model is asked to emit when it cannot read a scan well
#: enough to answer. Quarantine is a FIRST-CLASS outcome, not an error:
#: the run continues, the row records why, and the render surfaces it.
QUARANTINE_MARKER = "CANNOT_READ:"

_SYSTEM_PROMPT = """\
You are processing one scanned image from a batch, against a single \
instruction that applies to every scan in the batch.

Answer for THIS image only. Be concise and factual: transcribe or \
extract what the instruction asks for, and do not speculate about \
content you cannot actually see.

You are also shown the results already recorded for earlier scans in \
this batch. Use them for consistency of format and terminology. Do NOT \
restate or re-process them.

If the image is too blurry, cropped, dark, or otherwise unreadable to \
answer the instruction honestly, reply with exactly:
{marker} <one short sentence naming what is wrong with the scan>
Do not guess. A scan flagged this way is surfaced to the operator, \
which is the correct outcome — a plausible-looking invention is not.
""".format(marker=QUARANTINE_MARKER)


def build_user_content(
    *,
    image_bytes: bytes,
    media_type: str,
    instruction: str,
    carried_context: str,
) -> list[dict[str, Any]]:
    """The user turn: the image, the instruction, and prior results.

    Cross-image context comes from the carried record's current render
    (D1) rather than from batching images together — that is what keeps
    each drip item independent and re-runnable.
    """
    parts: list[dict[str, Any]] = [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": base64.standard_b64encode(image_bytes).decode(
                    "ascii",
                ),
            },
        },
        {"type": "text", "text": f"Instruction: {instruction}"},
    ]
    if carried_context.strip():
        parts.append({
            "type": "text",
            "text": (
                "Results already recorded for earlier scans in this "
                "batch (for consistency only — do not re-process):\n\n"
                + carried_context
            ),
        })
    return parts


def _extract_text(response: Any) -> str:
    """Concatenate the assistant's text blocks."""
    out: list[str] = []
    for block in getattr(response, "content", None) or []:
        if getattr(block, "type", None) == "text":
            out.append(getattr(block, "text", "") or "")
    return "\n".join(p for p in out if p).strip()


def call_model(
    client: Any,
    *,
    image_bytes: bytes,
    media_type: str,
    instruction: str,
    carried_context: str,
    model: str,
    max_tokens: int,
) -> str:
    """One vision turn. Exceptions propagate — see the module docstring."""
    response = client.messages.create(
        **messages_create_kwargs(
            model=model,
            max_tokens=max_tokens,
            system=_SYSTEM_PROMPT,
            messages=[{
                "role": "user",
                "content": build_user_content(
                    image_bytes=image_bytes,
                    media_type=media_type,
                    instruction=instruction,
                    carried_context=carried_context,
                ),
            }],
        )
    )
    return _extract_text(response)


def classify_result(text: str) -> tuple[str, str, str]:
    """``(outcome, result, note)`` from the model's reply.

    An empty reply is quarantined rather than recorded as a successful
    empty result: "the model returned nothing" and "this scan has no
    relevant content" are different facts, and conflating them would
    hide a broken run behind a wall of blank sections.
    """
    stripped = (text or "").strip()
    if not stripped:
        return (
            OUTCOME_QUARANTINED,
            "",
            "the model returned no text for this scan",
        )
    if stripped.startswith(QUARANTINE_MARKER):
        note = stripped[len(QUARANTINE_MARKER):].strip()
        return (
            OUTCOME_QUARANTINED,
            "",
            note or "the scan could not be read",
        )
    return OUTCOME_OK, stripped, ""


def process_one(
    *,
    client: Any,
    manifest: BatchManifest,
    ledger_file: Path,
    images_root: Path,
    vault_path: Path | str,
    item_id: str,
    model: str,
    max_tokens: int,
) -> str:
    """Process one image. Returns the outcome, or ``"skipped"``.

    Raises :class:`~alfred.batch.seal.BatchSealedError` when the carried
    record has been taken by the operator, and re-raises any model
    exception uncaught (the runner classifies quota/auth as BLOCKED).
    """
    from alfred.vault.ops import vault_edit, vault_read

    rows = load_rows(ledger_file)
    if has_processed(rows, item_id):
        # Content-addressed idempotency: a replay is free.
        log.info(
            "batch.worker.already_processed",
            batch_id=manifest.batch_id,
            item_id=item_id,
            detail="ran, nothing to do — this scan already has a "
                   "ledger row",
        )
        return "skipped"

    image: BatchImage | None = manifest.image_for(item_id)
    if image is None:
        raise ValueError(
            f"batch {manifest.batch_id}: item {item_id!r} is not in the "
            f"manifest — the work-list and the manifest have diverged"
        )

    record_path = manifest.record_path
    if not record_path:
        raise ValueError(
            f"batch {manifest.batch_id}: manifest carries no "
            f"record_path — the carried record was never minted, so "
            f"there is nowhere to render results"
        )

    # (2) Seal check BEFORE spending anything.
    # ``vault_read`` takes no ``scope`` kwarg (verified against ops.py:870
    # — reads are not gated at that layer), so the scope's ``read: True``
    # documents intent rather than being consulted here.
    record = vault_read(vault_path, record_path)
    fm = (record.get("frontmatter") or {}) if isinstance(record, dict) else {}
    assert_regenerable(
        fm, batch_id=manifest.batch_id, record_path=record_path,
    )

    image_file = Path(images_root) / image.filename
    image_bytes = image_file.read_bytes()

    # (3) Model call — exceptions propagate for the runner to classify.
    text = call_model(
        client,
        image_bytes=image_bytes,
        media_type=image.media_type,
        instruction=manifest.instruction,
        carried_context=render_body(manifest, rows),
        model=model,
        max_tokens=max_tokens,
    )
    outcome, result, note = classify_result(text)

    # (4) Record the paid-for result.
    append_row(ledger_file, build_row(
        item_id=item_id,
        filename=image.filename,
        outcome=outcome,
        result=result,
        model=model,
        note=note,
    ))

    # (5) Re-render from the FULL ledger (pure — safe to re-run).
    rows = load_rows(ledger_file)
    counts = progress_counts(manifest, rows)
    vault_edit(
        vault_path,
        record_path,
        body_replace=render_body(manifest, rows),
        set_fields={
            "batch_items_done": counts["done"],
            "batch_items_total": counts["total"],
            "batch_items_failed": counts["failed"],
            "batch_updated_at": datetime.now(timezone.utc).isoformat(),
        },
        scope=BATCH_SCOPE,
    )

    log.info(
        "batch.worker.processed",
        batch_id=manifest.batch_id,
        item_id=item_id,
        outcome=outcome,
        done=counts["done"],
        total=counts["total"],
        failed=counts["failed"],
    )
    return outcome


__all__ = [
    "BATCH_SCOPE",
    "QUARANTINE_MARKER",
    "build_user_content",
    "call_model",
    "classify_result",
    "process_one",
]
