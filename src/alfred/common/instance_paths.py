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


def instance_data_path(raw: dict[str, Any], *parts: str) -> str:
    """A path under the instance data dir, joined as a STRING.

    ``instance_data_path({"logging": {"dir": "./data"}}, "voice_calibration")``
    is ``"./data/voice_calibration"`` — the exact literal it replaces. Using
    ``pathlib`` here would normalise the ``./`` away and lose that identity.
    """
    base = instance_data_dir(raw).rstrip("/")
    return "/".join([base, *[p.strip("/") for p in parts if p]])
