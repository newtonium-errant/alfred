"""Ledger -> record body. A PURE function, and deliberately so.

The vault record is a RENDER of the ledger (D2), regenerated wholesale
each checkpoint rather than patched. Keeping the render pure is what
makes that safe to re-run: the same ledger always produces the same
body, so a crash between the ledger append and the vault write costs
nothing — the next run reproduces the identical body.

It is also the ONE place a body is produced. Scribe states the same
invariant about ``render_soap``; the value is that "what does the record
look like" has exactly one answer to read.

The seal guard lives in :mod:`alfred.batch.seal`, not here — rendering
is not the same decision as being allowed to write.
"""

from __future__ import annotations

from typing import Any

from .ledger import OUTCOME_QUARANTINED
from .manifest import BatchManifest

#: Opening line of every generated body. Present so a reader (and the
#: operator) can tell at a glance that hand-edits below it are transient.
_REGEN_BANNER = (
    "> **Machine-generated.** This body is regenerated from the batch "
    "ledger each time a scan finishes, so edits made here are "
    "overwritten. Set `batch_status: sealed` in the frontmatter to stop "
    "regeneration and take ownership of the text."
)


def render_body(
    manifest: BatchManifest,
    rows: list[dict[str, Any]],
) -> str:
    """Render the carried record's body from the ledger.

    Pure: depends only on its two arguments. Results appear in MANIFEST
    order (submission order), not ledger order, so the document is
    stable as rows land out of order across runs.
    """
    by_item: dict[str, dict[str, Any]] = {}
    for r in rows:
        iid = str(r.get("item_id") or "")
        if iid:
            by_item[iid] = r

    done = [i for i in manifest.images if i.item_id in by_item]
    pending = [i for i in manifest.images if i.item_id not in by_item]
    quarantined = [
        i for i in done
        if by_item[i.item_id].get("outcome") == OUTCOME_QUARANTINED
    ]

    out: list[str] = [_REGEN_BANNER, ""]
    out.append(
        f"**Progress:** {len(done)} of {len(manifest.images)} scans "
        f"processed."
        + (f" {len(quarantined)} need a look." if quarantined else "")
    )
    out.append("")
    if manifest.instruction.strip():
        out.append("**Instruction given:**")
        out.append("")
        # Blockquote so a multi-line instruction cannot break the
        # surrounding structure.
        for line in manifest.instruction.strip().splitlines():
            out.append(f"> {line}")
        out.append("")

    out.append("## Results")
    out.append("")
    if not done:
        # Intentionally-left-blank: an empty Results section must say it
        # is empty ON PURPOSE, so "nothing processed yet" is
        # distinguishable from "the renderer broke".
        out.append(
            "_No scans processed yet — the batch is queued. This section "
            "fills in as each scan completes._"
        )
        out.append("")
    else:
        for img in done:
            row = by_item[img.item_id]
            flag = (
                " — needs a look"
                if row.get("outcome") == OUTCOME_QUARANTINED
                else ""
            )
            out.append(f"### {img.filename}{flag}")
            out.append("")
            note = str(row.get("note") or "").strip()
            if note:
                out.append(f"> {note}")
                out.append("")
            result = str(row.get("result") or "").strip()
            out.append(result if result else "_(no output recorded)_")
            out.append("")

    if pending:
        out.append("## Not yet processed")
        out.append("")
        for img in pending:
            out.append(f"- {img.filename}")
        out.append("")
    elif done:
        out.append("## Not yet processed")
        out.append("")
        # ILB again: the absence of a pending list is a real signal
        # (the batch finished), not a rendering gap.
        out.append("_None — every submitted scan has been processed._")
        out.append("")

    return "\n".join(out).rstrip() + "\n"


def render_carried_context(
    manifest: BatchManifest,
    rows: list[dict[str, Any]],
    *,
    max_chars: int,
) -> str:
    """The prior-results context for ONE per-scan model call, BOUNDED.

    Deliberately not :func:`render_body`. The body is the operator's document
    and must show every result; this is a prompt fragment whose only job is
    keeping scan N's format and terminology consistent with the scans just
    before it.

    Passing the whole body here made the batch quadratic: scan N carried N-1
    results, so a sixty-scan batch paid for roughly eighteen hundred
    result-readings of text the model is explicitly instructed NOT to
    re-process. This keeps the header and the TAIL — the most recent rows,
    which are the ones consistency is measured against — and drops the distant
    ones. Nothing is lost by dropping them: the ledger holds every result and
    the record renders all of them; only the prompt is trimmed.

    Results appear in MANIFEST order (submission order) like the body, so the
    tail is "the scans just before this one" rather than whichever rows
    happened to land last.

    ``max_chars <= 0`` yields no context at all, which is a legitimate
    operator choice (each scan judged entirely on its own) rather than an
    error — and it is stated in the output, never a silent empty string.
    """
    by_item: dict[str, dict[str, Any]] = {}
    for r in rows:
        iid = str(r.get("item_id") or "")
        if iid:
            by_item[iid] = r
    done = [i for i in manifest.images if i.item_id in by_item]

    if not done:
        return ""
    if max_chars <= 0:
        return ""

    header = f"{len(done)} of {len(manifest.images)} scans processed so far."

    # Build newest-first, then reverse: the tail is what must survive, so the
    # budget is spent from the most recent row backwards.
    blocks: list[str] = []
    used = len(header)
    dropped = 0
    for img in reversed(done):
        row = by_item[img.item_id]
        result = str(row.get("result") or "").strip()
        if not result:
            # A quarantined scan has no result to be consistent WITH, and its
            # note describes a broken image rather than the batch's format.
            dropped += 1
            continue
        block = f"### {img.filename}\n{result}"
        if used + len(block) + 2 > max_chars:
            dropped += 1
            continue
        blocks.append(block)
        used += len(block) + 2

    if not blocks:
        return ""

    parts = [header]
    if dropped:
        # Stated, not silent: the model is being shown a SUBSET, and a prompt
        # that implied otherwise would invite it to reason about totals it
        # cannot see.
        parts.append(
            f"(Showing the {len(blocks)} most recent; {dropped} earlier "
            f"result(s) omitted for length.)"
        )
    parts.append("")
    parts.extend(reversed(blocks))
    return "\n".join(parts).rstrip() + "\n"


def progress_counts(
    manifest: BatchManifest,
    rows: list[dict[str, Any]],
) -> dict[str, int]:
    """``{done, total, failed}`` — the frontmatter progress counters."""
    ids = {str(r.get("item_id")) for r in rows if r.get("item_id")}
    done = [i for i in manifest.images if i.item_id in ids]
    by_item = {
        str(r.get("item_id")): r for r in rows if r.get("item_id")
    }
    failed = sum(
        1 for i in done
        if by_item[i.item_id].get("outcome") == OUTCOME_QUARANTINED
    )
    return {
        "done": len(done),
        "total": len(manifest.images),
        "failed": failed,
    }


__all__ = ["progress_counts", "render_body", "render_carried_context"]
