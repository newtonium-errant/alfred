"""Where the RRTS invoice snapshot lands — the one derivation, both ends.

The landing path has TWO ends and they live in different dependency tiers:

* the **writer** is the transport receiver (:mod:`alfred.transport.routes_rrts`),
  which imports ``aiohttp`` and therefore only exists when the ``voice``
  extra is installed;
* the **reader** is the reconciler (:mod:`alfred.reconcile`), which is part
  of the BASE install and must never depend on an optional extra to know
  where its own input file is.

So the derivation cannot live with the writer. A reader importing
``routes_rrts`` would make the reconcile CLI fail on a base install; a
reader spelling the segment itself would be the second spelling — which is
how a writer and a reader land on different files while both look correct,
with neither side reporting anything wrong. This module is the third
option: a dependency-free home both ends import, spelling nothing
themselves.

It arrived in two steps, and the second step is the interesting one. The
segment was first lifted out of an inline f-string in the transport wiring
and into ``routes_rrts`` — correct at the time, because the receiver was
still the only end. Building the reader is what revealed that ``aiohttp``
sits between the two ends, so the shared fact had to move once more, to
where BOTH can reach it. One END existed, so one resolution existed; the
same lesson, one layer out.

**String join, not pathlib**, matching
:func:`alfred.common.instance_paths.instance_data_path`: ``Path("./data") /
"x"`` normalises the leading ``./`` away, and byte-identity with a relative
anchor is what lets these paths be derived without moving anything already
on disk.
"""

from __future__ import annotations

from pathlib import Path

#: The filename under the export directory. Fixed, because the reconciler
#: reads it by name — the DIRECTORY is config-derived, this is not.
EXPORT_FILENAME = "invoices.json"

#: Directory segment under the instance data dir. Spelled HERE and nowhere
#: else; ``tests/test_rrts_wiring.py`` pins that property at source level,
#: because behaviour cannot see a duplicate that still agrees.
EXPORT_DIR_NAME = "rrts-export"


def export_dir_for(data_dir: str) -> str:
    """``<data_dir>/rrts-export`` — the landing directory, or ``""``.

    THE one derivation. Both ends call it: the transport wiring resolves the
    receiver's write target through it, and the reconcile reader resolves
    its read source through it. Neither spells the segment.

    Empty ``data_dir`` yields ``""`` — the caller's cue to refuse rather
    than guess. Guessing the process cwd is the defect this whole family of
    path helpers exists to prevent.
    """
    base = (data_dir or "").strip().rstrip("/")
    return f"{base}/{EXPORT_DIR_NAME}" if base else ""


def export_path(export_dir: Path | str) -> Path:
    """``<export_dir>/invoices.json`` — the file the reconciler reads."""
    return Path(export_dir) / EXPORT_FILENAME


def export_path_for(data_dir: str) -> str:
    """``<data_dir>/rrts-export/invoices.json``, or ``""``.

    The whole path in one call, for callers that hold a data dir rather than
    a resolved export directory — which is the reader's shape. Composed from
    the two helpers above so there is still exactly one place that knows
    either segment.
    """
    directory = export_dir_for(data_dir)
    return str(export_path(directory)) if directory else ""


__all__ = [
    "EXPORT_DIR_NAME",
    "EXPORT_FILENAME",
    "export_dir_for",
    "export_path",
    "export_path_for",
]
