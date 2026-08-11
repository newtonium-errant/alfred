"""Where an instance's data lives — the one resolver behind default paths.

On the box every instance shares one WorkingDirectory and differs only by
``--config``, so a cwd-relative default (``./data/<something>``) is not
"per-tool" at all: it is ONE shared file across Salem, KAL-LE, Hypatia and
VERA. That is the #53 shape, and it has bitten for real — KAL-LE's sync wrote
into Salem's feed store (2026-07-31), Salem's deck then dealt KAL-LE's cards
and the router correctly 409'd them.

The anchor is ``logging.dir``: the dedicated data-dir base every instance
config sets (Salem ``./data``, KAL-LE ``/home/andrew/.alfred/kalle/data``).
Roughly twenty sites already read it inline as
``(raw.get("logging") or {}).get("dir", "./data")``; this module is that idiom
with the two robustness holes closed — a non-dict ``logging:`` block, and an
explicitly BLANK ``dir`` (which ``.get(..., "./data")`` hands back as ``""``,
turning ``<dir>/scribe/inbox`` into the root-anchored ``/scribe/inbox``).

**Byte-identity is the property that makes this safe to retrofit.** Salem's
``logging.dir`` IS ``./data``, so every default derived through here resolves
to exactly the string the cwd-relative literal produced — same file, zero
migration, nothing to move on the box. The join is a STRING join rather than
``pathlib`` for precisely that reason: ``Path("./data") / "x"`` normalises to
``data/x`` and would break the identity.

An explicitly configured path ALWAYS wins over anything derived here.
"""

from __future__ import annotations

from typing import Any

# The legacy cwd-relative data dir. Also the fallback when a config carries no
# ``logging.dir`` at all — keeps minimal test configs and Salem identical.
LEGACY_DATA_DIR = "./data"


def configured_logging_dir(raw: dict[str, Any]) -> str | None:
    """``logging.dir`` from a unified config dict, or ``None`` when unusable.

    Unusable means: no ``logging:`` block, a non-dict one, a missing ``dir``,
    or a blank/whitespace ``dir``. Callers that want the legacy fallback use
    :func:`instance_data_dir`; callers that need to distinguish "configured"
    from "defaulted" read this directly.
    """
    block = raw.get("logging") if isinstance(raw, dict) else None
    if isinstance(block, dict):
        d = block.get("dir")
        if isinstance(d, str) and d.strip():
            return d.strip()
    return None


def instance_data_dir(raw: dict[str, Any]) -> str:
    """The instance's data directory — ``logging.dir``, else ``./data``."""
    return configured_logging_dir(raw) or LEGACY_DATA_DIR


class InstanceSlugError(ValueError):
    """Raised when an instance-scoped name is derived from a blank instance."""


def instance_slug(instance: str) -> str:
    """Filesystem-safe instance segment. Blank is a hard error.

    THE one derivation, lifted here from ``batch.paths`` (#84) so the state
    files that carry an instance in their NAME and the directories that carry
    one in their PATH cannot disagree about what "Salem" slugs to. Two
    spellings of the same rule is how a writer and a reader end up on
    different files.

    ``batch.paths.instance_slug`` now delegates and re-raises as its own
    ``BatchPathError`` so that module's contract is unchanged.
    """
    slug = (instance or "").strip().lower().replace(" ", "-")
    if not slug:
        raise InstanceSlugError(
            "an instance-scoped path needs an instance name — an unscoped "
            "path is shared across instances on the box and silently mixes "
            "one instance's state into another's"
        )
    return slug


def instance_state_filename(stem: str, instance: str, *, suffix: str) -> str:
    """``<stem>.<slug>.<suffix>`` — e.g. ``email_calibration.salem.jsonl``.

    The naming shape these state files already use, with the instance
    DERIVED rather than baked in (#84). Seven files carried a literal
    ``.salem.`` on shared code paths: on the box every instance shares one
    WorkingDirectory and differs only by ``--config``, so KAL-LE running the
    same subsystem wrote into a file named for Salem — the 2026-07-31
    feed-store shape, with the instance name in the filename making it look
    intentional.

    **BYTE-IDENTICAL FOR SALEM, so there is nothing to migrate.**
    ``instance.name`` is ``"Salem"``, which slugs to ``salem``, which
    reproduces the previous literal exactly. That is the same property
    :func:`instance_data_path` relies on, and it is why this fix needs no
    rename shim and no deploy step: Salem's live calibration corpora and
    snooze store keep working because their paths do not move.

    An EMPTY instance yields ``<stem>.<suffix>`` with no instance segment
    (see :func:`instance_state_filename_or_unscoped`) rather than raising —
    callers in the config layer resolve to empty when the operator omitted
    ``telegram.instance.name``, and the daemon-start guard is what refuses
    that case. What must not happen is an unnamed instance silently
    inheriting Salem's filename, which is what the literal did.
    """
    return f"{stem}.{instance_slug(instance)}.{suffix}"


def instance_state_filename_or_unscoped(
    stem: str, instance: str, *, suffix: str,
) -> str:
    """:func:`instance_state_filename`, or ``<stem>.<suffix>`` when blank.

    The config-layer variant. Blank instance names reach config loading
    legitimately (minimal test fixtures; an operator mid-setup), and the
    established pattern — see ``routine.config.load_from_unified`` — is to
    resolve to empty and let the daemon-start guard refuse, NOT to raise at
    load. The unscoped name is deliberately not Salem's: an unnamed instance
    that quietly adopted ``.salem.`` is precisely the defect being fixed.
    """
    try:
        return instance_state_filename(stem, instance, suffix=suffix)
    except InstanceSlugError:
        return f"{stem}.{suffix}"


def instance_data_path(raw: dict[str, Any], *parts: str) -> str:
    """A path under the instance data dir, joined as a STRING.

    ``instance_data_path({"logging": {"dir": "./data"}}, "voice_calibration")``
    is ``"./data/voice_calibration"`` — the exact literal it replaces. Using
    ``pathlib`` here would normalise the ``./`` away and lose that identity.
    """
    base = instance_data_dir(raw).rstrip("/")
    return "/".join([base, *[p.strip("/") for p in parts if p]])
