"""The batch manifest — a frozen inventory of what was submitted.

The manifest is written ONCE by the save-only route and is thereafter
read-only. It is the campaign's work-list source, which is the reason it
is frozen rather than re-derived: ``gmail_backlog`` inventories its
work-list live from a directory and that is correct for a queue that
drains, but a batch's work-list must NOT shrink as images are processed
— the drip runner sources in-flight candidates from STATE and computes
``total`` as the union of work-list and state, so a work-list that
disappeared under it would report nonsense progress and could never
re-attempt a failed item.

One image = one manifest entry = one drip item, keyed by the image's
CONTENT HASH (D1: per-image drip items). A resubmitted identical image
dedupes to the same item id and is therefore never processed twice.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)


@dataclass
class BatchImage:
    """One submitted scan.

    ``item_id`` is the sha256[:16] of the file's bytes and doubles as the
    drip item id and the ledger row key. Content-addressing is what makes
    the whole pipeline idempotent: a replayed image, a resubmitted batch,
    or a crashed-and-resumed run all converge on the same row.
    """

    item_id: str
    filename: str
    sha256: str
    bytes: int
    media_type: str

    @classmethod
    def from_dict(cls, data: dict) -> "BatchImage":
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)


@dataclass
class BatchManifest:
    """The frozen submission record for one batch."""

    batch_id: str
    instruction: str
    created_at: str
    instance: str = ""
    #: Vault-relative path of the carried record, filled in once the
    #: record is minted. Empty until then.
    record_path: str = ""
    submitted_by: str = ""
    images: list[BatchImage] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "BatchManifest":
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        raw_images = known.pop("images", None) or []
        images = [
            BatchImage.from_dict(i) for i in raw_images if isinstance(i, dict)
        ]
        return cls(**known, images=images)

    def item_ids(self) -> list[str]:
        """Item ids in submission order — the campaign's work-list."""
        return [i.item_id for i in self.images]

    def image_for(self, item_id: str) -> "BatchImage | None":
        for i in self.images:
            if i.item_id == item_id:
                return i
        return None


def save_manifest(path: Path | str, manifest: BatchManifest) -> None:
    """Persist atomically (``mkstemp`` -> ``os.replace``).

    Same shape as ``drip.state.save_state`` and ``scribe.ledger``: a
    torn manifest would be a work-list that lost items, which the runner
    cannot distinguish from a batch that was always smaller.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(manifest.to_dict(), f, indent=2, ensure_ascii=False)
        os.replace(tmp, p)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def load_manifest(path: Path | str) -> "BatchManifest | None":
    """Read a manifest, or ``None`` when absent/unreadable.

    Degrades to ``None`` rather than raising, for the same reason
    ``scribe.ledger.load_ledger`` does: the caller (a campaign
    work-list) must be able to report "nothing to do" for a batch whose
    directory is missing without taking down every other campaign in the
    same run. A bare ``except Exception`` is deliberate — a torn file
    yields ``UnicodeDecodeError``, deep nesting yields ``RecursionError``,
    and neither is an ``OSError`` or ``JSONDecodeError``.
    """
    p = Path(path)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001 — any read failure degrades to None
        log.warning(
            "batch.manifest.unreadable",
            path=p.name,
            error_class=type(e).__name__,
        )
        return None
    if not isinstance(data, dict):
        log.warning(
            "batch.manifest.unreadable", path=p.name, error_class="NotADict",
        )
        return None
    return BatchManifest.from_dict(data)


__all__ = [
    "BatchImage",
    "BatchManifest",
    "load_manifest",
    "save_manifest",
]
