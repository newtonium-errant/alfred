"""Where the remittance ledger lives — config-derived, never a literal.

Layout, all under the INSTANCE's own data dir::

    <data_dir>/remittance/<instance-slug>/
        ledger.jsonl        # statements + claim lines; the TRUTH
        corrections.jsonl   # operator rulings on classification
        reports/            # generated bulk-review artifacts

Two properties are load-bearing, and both are inherited rather than
re-argued — :mod:`alfred.batch.paths` states them for the batch tree and
the reasoning transfers unchanged.

**Instance-scoped.** On the box every instance shares one
WorkingDirectory and differs only by ``--config``, so a cwd-relative
``./data/remittance/...`` would be ONE directory shared across Salem,
KAL-LE, Hypatia and VERA. That is the #53 shape and it has already bitten
for real (2026-07-31: KAL-LE's sync wrote into Salem's feed store, Salem's
deck dealt foreign cards, the router correctly 409'd them). A blank
instance is a hard error here, not a default — an unscoped path fails at
DERIVATION time rather than quietly at write time.

**Outside the vault.** The ledger is not a vault record and must not live
where a vault tool would find it. The curator watches ``inbox/`` and its
``full_scan`` admits every file that is not hidden, a ``.lock`` or in a
small skip-name set — it does not filter on ``.md``. A JSONL ledger
dropped in the vault would be picked up and processed as if it were an
inbox item. So this package owns its own unwatched directory.

The instance slug DELEGATES to
:func:`alfred.common.instance_paths.instance_slug` rather than keeping a
second copy of the rule: two spellings of "what does Salem slug to" is
how a writer and a reader end up on different files.
"""

from __future__ import annotations

import re
from pathlib import Path

from alfred.common.instance_paths import (
    InstanceSlugError,
    instance_slug as _shared_slug,
)

#: Directory name under the instance data dir.
_REMITTANCE_DIR_NAME = "remittance"
_LEDGER_NAME = "ledger.jsonl"
_CORRECTIONS_NAME = "corrections.jsonl"
_REPORTS_DIR_NAME = "reports"

#: A report name becomes a PATH SEGMENT, so it is constrained to characters
#: that cannot traverse or escape. A name failing this is REJECTED rather
#: than sanitised, for the same reason ``batch.paths.validate_batch_id``
#: rejects: silently rewriting the name would decouple the file on disk from
#: the name the operator was told to look for.
_REPORT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class RemittancePathError(ValueError):
    """A remittance path could not be derived — blank instance or bad name."""


def instance_segment(instance: str) -> str:
    """Filesystem-safe instance segment. Blank is a hard error.

    Re-raises :class:`~alfred.common.instance_paths.InstanceSlugError` as
    this module's own error type so callers catch one exception class for
    every path derivation here, exactly as ``batch.paths`` does.
    """
    try:
        return _shared_slug(instance)
    except InstanceSlugError as exc:
        raise RemittancePathError(
            "remittance paths need an instance name — an unscoped path is "
            "shared across instances on the box and silently mixes one "
            "instance's remittances into another's"
        ) from exc


def validate_report_name(name: str) -> str:
    """Return ``name`` if it is safe as a path segment, else raise."""
    n = (name or "").strip()
    if not _REPORT_NAME_RE.match(n):
        raise RemittancePathError(
            f"invalid report name {name!r} — must be 1-128 chars of "
            f"[A-Za-z0-9._-] starting alphanumeric. Rejected rather than "
            f"sanitised: rewriting the name would decouple the file on "
            f"disk from the name the operator is told to open."
        )
    if ".." in n:
        raise RemittancePathError(
            f"invalid report name {name!r} — '..' cannot appear in a path "
            f"segment even when every character is otherwise allowed."
        )
    return n


def remittance_root(data_dir: Path | str, instance: str) -> Path:
    """``<data_dir>/remittance/<instance-slug>`` — one instance's store.

    Takes a resolved ``data_dir`` rather than the raw config for the same
    reason ``batch.paths.batch_root`` does: the caller holds an already
    resolved value (``logging.dir`` with the legacy fallback applied), and
    re-deriving it here would be a second, divergable resolution of the
    same value.
    """
    return Path(data_dir) / _REMITTANCE_DIR_NAME / instance_segment(instance)


# --- the two entry points -----------------------------------------------------
#
# There are two legitimate ways to reach the same files: from a
# ``(data_dir, instance)`` pair, and from an already-resolved store root
# (which is what ``ReconcileConfig.store_dir`` holds, because an operator
# may set it explicitly and that must win). Both funnel through the SAME
# filename constants below, so the one thing that must never diverge — what
# the ledger file is CALLED — has a single author. ``test_paths.py`` pins
# that the two entry points land on identical paths.


def ledger_path_in(root: Path | str) -> Path:
    """The ledger inside an already-resolved store root."""
    return Path(root) / _LEDGER_NAME


def corrections_path_in(root: Path | str) -> Path:
    """The corrections sidecar inside an already-resolved store root."""
    return Path(root) / _CORRECTIONS_NAME


def reports_dir_in(root: Path | str) -> Path:
    """The reports directory inside an already-resolved store root."""
    return Path(root) / _REPORTS_DIR_NAME


def ledger_path(data_dir: Path | str, instance: str) -> Path:
    return ledger_path_in(remittance_root(data_dir, instance))


def corrections_path(data_dir: Path | str, instance: str) -> Path:
    return corrections_path_in(remittance_root(data_dir, instance))


def reports_dir(data_dir: Path | str, instance: str) -> Path:
    return reports_dir_in(remittance_root(data_dir, instance))


def report_path(data_dir: Path | str, instance: str, name: str) -> Path:
    return reports_dir(data_dir, instance) / validate_report_name(name)


__all__ = [
    "RemittancePathError",
    "corrections_path",
    "corrections_path_in",
    "instance_segment",
    "ledger_path",
    "ledger_path_in",
    "remittance_root",
    "report_path",
    "reports_dir",
    "reports_dir_in",
    "validate_report_name",
]
