"""The ``reconcile:`` block of the unified config.

Built by hand rather than through the recursive ``_build`` helper — the
block is flat, and hand-rolling sidesteps the ``_build`` collision footgun
entirely (a nested key named ``state`` or ``agent`` would dispatch to the
global dataclass of that name). This is the same choice ``feed/config.py``
documents, for the same reason.

**The store is INSTANCE-scoped, not merely tool-scoped.** On the box every
instance shares one WorkingDirectory and differs only by ``--config``, so a
cwd-relative ``./data/remittance`` would be ONE directory shared across
every instance — the shape that already bit for real when KAL-LE's sync
wrote into Salem's feed store. The default therefore resolves under the
instance's own data dir (``logging.dir``) AND carries the instance slug in
its path. An explicit ``reconcile.store_dir`` always wins.

**When nothing anchors the path, reconcile is OFF.** There is no correct
answer for a config that names no data dir, and guessing the process cwd
is the defect itself — so the guess does not exist. The coercion lives on
the dataclass rather than in the loader so a bare ``ReconcileConfig()``
(the shape a test fixture or a future default_factory reaches) is covered
by the same mechanism as the loaded one.

**No instance name appears anywhere in this file.** The instance comes
from ``telegram.instance.name`` via the canonical
:func:`alfred.audit.instance_name_from_raw`, because a default that names
one instance is wrong on every other one.

Example::

    reconcile:
      enabled: true
      date_order: dmy          # only needed for slash-form dates
      eob_classes:
        "42": documentation_required
        "91": duplicate_denial
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import structlog

from alfred.audit import instance_name_from_raw
from alfred.common.instance_paths import (
    InstanceSlugError,
    configured_logging_dir,
    instance_slug,
)

from .attention import ALL_CLASSES, normalise_eob
from .money import DATE_ORDERS

log = structlog.get_logger(__name__)

#: Directory name under the instance data dir. Mirrors
#: :mod:`alfred.reconcile.paths`, which is the authority for the layout —
#: this constant exists so the config layer can build the same string
#: without importing a Path helper into a config dataclass.
STORE_DIR_NAME = "remittance"

#: How many consistent operator rulings before a code mapping is PROPOSED.
#: Two, not one: a single ruling is as likely to be a one-off as a pattern,
#: and the cost of a premature proposal is the operator learning to skim
#: past the proposals section.
DEFAULT_MIN_CORRECTION_SUPPORT = 2

_unanchored_logged = False


@dataclass
class ReconcileConfig:
    """Typed view of the ``reconcile:`` block."""

    enabled: bool = True
    #: Display name of the instance this config belongs to, from
    #: ``telegram.instance.name``. Empty means the operator omitted it —
    #: honest, and the path resolution refuses rather than guessing.
    instance_name: str = ""
    #: The remittance store root. NO default literal: an unanchored store
    #: is a disabled store, never a cwd-relative one.
    store_dir: str = ""
    #: ``""`` (refuse ambiguous slash dates), ``"dmy"`` or ``"mdy"``.
    date_order: str = ""
    #: EOB code -> attention class. EMPTY by default and deliberately so —
    #: see :mod:`alfred.reconcile.attention` on why inventing codes would
    #: be worse than shipping none.
    eob_classes: dict[str, str] = field(default_factory=dict)
    min_correction_support: int = DEFAULT_MIN_CORRECTION_SUPPORT

    def __post_init__(self) -> None:
        """Normalise, validate, and fail SAFE when there is nowhere to write.

        Fail-safe rather than fail-loud-and-crash: reconcile is an operator
        tool invoked from the CLI, and an unresolvable store should produce
        a clear refusal at the command, not a traceback at import. It is
        LOUD about it — an enabled-but-pathless tool that silently writes
        nowhere is exactly the ambiguity the intentionally-left-blank rule
        exists to kill.
        """
        # EOB codes are matched case-insensitively at classification time,
        # so normalise once here rather than at every lookup.
        self.eob_classes = {
            normalise_eob(k): str(v).strip()
            for k, v in (self.eob_classes or {}).items()
            if str(k).strip()
        }
        unknown = {
            code: cls for code, cls in self.eob_classes.items()
            if cls not in ALL_CLASSES
        }
        if unknown:
            # NOT dropped: classification fails OPEN on these, so the lines
            # still surface. Dropping them here would silence the lines
            # instead, which is the wrong direction for money.
            log.warning(
                "reconcile.config.unknown_eob_classes",
                mappings=unknown,
                known=sorted(ALL_CLASSES),
                detail="these reconcile.eob_classes values name a class this "
                       "build does not define; lines carrying these codes "
                       "fall through to unknown_eob and still surface",
            )

        order = (self.date_order or "").strip().lower()
        if order and order not in DATE_ORDERS:
            log.warning(
                "reconcile.config.bad_date_order",
                configured=self.date_order,
                known=sorted(DATE_ORDERS),
                detail="reconcile.date_order is not 'dmy' or 'mdy'; slash-form "
                       "dates will be REFUSED rather than guessed at",
            )
            order = ""
        self.date_order = order

        try:
            self.min_correction_support = max(1, int(self.min_correction_support))
        except (TypeError, ValueError):
            self.min_correction_support = DEFAULT_MIN_CORRECTION_SUPPORT

        if self.enabled and not (self.store_dir or "").strip():
            self.enabled = False
            global _unanchored_logged
            if not _unanchored_logged:
                _unanchored_logged = True
                log.warning(
                    "reconcile.config.disabled_no_store_dir",
                    reason="unanchored_store_dir",
                    instance_name=self.instance_name,
                    detail="reconcile.store_dir is empty and no instance data "
                           "dir could be derived (set logging.dir and "
                           "telegram.instance.name, or reconcile.store_dir "
                           "explicitly). Reconcile is OFF for this config — "
                           "it will NOT fall back to the process cwd.",
                )


def _default_store_dir(raw: dict[str, Any], instance: str) -> str:
    """``<logging.dir>/remittance/<instance-slug>``, or ``""``.

    Empty when either anchor is missing. Both are required: the data dir
    keeps one instance's store off another's disk location, and the slug
    keeps two instances sharing a data dir from sharing a ledger.

    The slug comes from the shared
    :func:`alfred.common.instance_paths.instance_slug` rather than a local
    ``.lower().replace(...)`` — the whole point of that helper is that
    "what does Salem slug to" has ONE answer, and a second spelling here
    is precisely how a config-derived path and a
    :func:`alfred.reconcile.paths.remittance_root` path would drift onto
    different directories. ``test_config.py`` pins that they agree.

    Blank instance resolves to ``""`` (reconcile disabled by
    ``__post_init__``) rather than raising: a blank name reaches config
    loading legitimately — a minimal test fixture, an operator mid-setup —
    and the established pattern is to resolve empty and let the guard
    refuse, not to raise at load.
    """
    data_dir = configured_logging_dir(raw)
    if not data_dir:
        return ""
    try:
        slug = instance_slug(instance)
    except InstanceSlugError:
        return ""
    # String join, not pathlib: ``Path("./data") / "x"`` normalises the
    # leading ``./`` away, and the byte-identity of a ``./data`` anchor is
    # what makes these defaults safe to introduce without a migration.
    return f"{data_dir.rstrip('/')}/{STORE_DIR_NAME}/{slug}"


def load_from_unified(raw: dict[str, Any]) -> ReconcileConfig:
    """Build :class:`ReconcileConfig` from the unified config dict.

    Schema-tolerant: unknown keys in the ``reconcile:`` block are ignored,
    so a config written for a newer build loads here unchanged.
    """
    block = raw.get("reconcile") if isinstance(raw, dict) else None
    if not isinstance(block, dict):
        block = {}
    known = {
        k: v for k, v in block.items()
        if k in ReconcileConfig.__dataclass_fields__
    }

    instance = instance_name_from_raw(raw)
    # ``instance_name`` is derived, never operator-supplied here: two
    # spellings of "which instance is this" is how a writer and a reader
    # end up on different ledgers.
    known["instance_name"] = instance

    store_dir = known.get("store_dir")
    if not (isinstance(store_dir, str) and store_dir.strip()):
        known["store_dir"] = _default_store_dir(raw, instance)
    else:
        known["store_dir"] = store_dir.strip()

    eob = known.get("eob_classes")
    if not isinstance(eob, dict):
        known["eob_classes"] = {}

    return ReconcileConfig(**known)


__all__ = [
    "DEFAULT_MIN_CORRECTION_SUPPORT",
    "STORE_DIR_NAME",
    "ReconcileConfig",
    "load_from_unified",
]
