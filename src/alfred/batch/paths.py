"""Where a batch's files live — config-derived, never a literal.

Layout, all under the INSTANCE's own data dir::

    <data_dir>/batch/<instance-slug>/<batch_id>/
        manifest.json     # the campaign's work-list source
        ledger.jsonl      # append-only; the TRUTH (D2)
        images/           # the saved scans

Two properties are load-bearing.

**Instance-scoped.** On the box every instance shares one
WorkingDirectory and differs only by ``--config``, so a cwd-relative
``./data/batch/...`` would be ONE directory shared across Salem,
KAL-LE, Hypatia and VERA — the #53 shape that already bit for real
(2026-07-31, KAL-LE's sync writing into Salem's feed store). The
instance slug is required and blank is a hard error, mirroring
``drip.state.campaign_state_path``.

**Outside the vault.** The images deliberately do NOT go to
``<vault>/inbox/`` even though ``telegram.vision.save_image_to_inbox``
exists and would be the obvious reuse. The curator WATCHES that
directory and its ``full_scan`` (``curator/watcher.py``) admits every
file that is not hidden, a ``.lock``, or in a small skip-name set — it
does not filter on ``.md``. Thirty scans dropped there would be picked
up by the curator and processed a second time, in parallel with this
campaign, burning LLM spend and minting junk records. So the batch owns
its own unwatched directory. The content-hash naming convention from
the chat image path IS reused; only the destination differs.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..common.instance_paths import instance_data_path

#: Directory name under the instance data dir.
_BATCH_DIR_NAME = "batch"
_IMAGES_DIR_NAME = "images"
_MANIFEST_NAME = "manifest.json"
_LEDGER_NAME = "ledger.jsonl"

#: A batch id is minted by the route and then used as a PATH SEGMENT, so
#: it is constrained to characters that cannot traverse or escape. Any
#: id failing this is rejected rather than sanitised — silently rewriting
#: an id would decouple the directory from the id in the vault record.
_BATCH_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


class BatchPathError(ValueError):
    """A batch path could not be derived — blank instance or bad id."""


def instance_slug(instance: str) -> str:
    """Filesystem-safe instance segment. Blank is a hard error.

    Mirrors ``drip.state.campaign_state_path``: an unscoped path is
    shared across instances on the box and silently corrupts state, so
    the failure is loud at derivation time rather than quiet at write
    time.
    """
    slug = (instance or "").strip().lower().replace(" ", "-")
    if not slug:
        raise BatchPathError(
            "batch paths need an instance name — an unscoped path is "
            "shared across instances on the box and silently mixes one "
            "instance's batches into another's"
        )
    return slug


def validate_batch_id(batch_id: str) -> str:
    """Return ``batch_id`` if it is safe as a path segment, else raise."""
    bid = (batch_id or "").strip()
    if not _BATCH_ID_RE.match(bid):
        raise BatchPathError(
            f"invalid batch id {batch_id!r} — must be 1-64 chars of "
            f"[A-Za-z0-9_-] starting alphanumeric. Rejected rather than "
            f"sanitised: rewriting the id would decouple the directory "
            f"from the id recorded in the vault record."
        )
    return bid


def batch_root(raw: dict, instance: str) -> Path:
    """``<data_dir>/batch/<instance-slug>`` — every batch for one instance."""
    return Path(
        instance_data_path(raw, _BATCH_DIR_NAME, instance_slug(instance)),
    )


def batch_dir(raw: dict, instance: str, batch_id: str) -> Path:
    """``<data_dir>/batch/<instance-slug>/<batch_id>``."""
    return batch_root(raw, instance) / validate_batch_id(batch_id)


def manifest_path(raw: dict, instance: str, batch_id: str) -> Path:
    return batch_dir(raw, instance, batch_id) / _MANIFEST_NAME


def ledger_path(raw: dict, instance: str, batch_id: str) -> Path:
    return batch_dir(raw, instance, batch_id) / _LEDGER_NAME


def images_dir(raw: dict, instance: str, batch_id: str) -> Path:
    return batch_dir(raw, instance, batch_id) / _IMAGES_DIR_NAME


__all__ = [
    "BatchPathError",
    "batch_dir",
    "batch_root",
    "images_dir",
    "instance_slug",
    "ledger_path",
    "manifest_path",
    "validate_batch_id",
]
