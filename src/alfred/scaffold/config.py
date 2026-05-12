"""Scaffold sync per-instance config.

Optional top-level ``scaffold`` block in the unified config::

    scaffold:
      include:
        - "_templates"
        - "_bases"
        - "view"
        - "CLAUDE.md"
        - "README.md"
        - "Start Here.md"
        - "user-profile.md"
      exclude:
        - ".obsidian"
        - ".gitkeep"

The block is **optional**. When absent, the module-level
:data:`alfred.scaffold.sync.DEFAULT_INCLUDE` and
:data:`alfred.scaffold.sync.DEFAULT_EXCLUDE` apply — those are the
Salem-shape canonical defaults.

Closes the structural gap surfaced 2026-05-12 when KAL-LE and Hypatia
applies revealed that the Salem-shape default-include is wrong for
canonical-curation / knowledge-work instances. Both wanted a trimmed
include of just the top-level docs (``README.md``, ``Start Here.md``,
``user-profile.md``) — the per-record-type templates and base views
shipped in the scaffold are Salem-operational-vault shape and would
have written 50+ dead files into either instance's vault.

Three-layer override precedence (highest wins) — documented also in
``cmd_sync`` and the ``cmd_scaffold`` dispatcher:

1. CLI flag (``--include`` / ``--exclude``) — operator override
2. Per-instance config (``scaffold.include`` / ``scaffold.exclude``)
3. Module-level default (Salem-shape fallback)

Schema-tolerance: unknown keys silently dropped via the
``__dataclass_fields__`` filter on :meth:`ScaffoldConfig.from_dict`,
per the project's load-time schema-tolerance contract. A newer config
that grows extra keys can be read by an older binary; an older config
read by a newer binary uses defaults for the new fields.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ScaffoldConfig:
    """Per-instance scaffold-sync configuration.

    Attributes:
        include: List of path-prefixes to include in the sync.
            Override of :data:`alfred.scaffold.sync.DEFAULT_INCLUDE`.
            Empty list = nothing included (use with care — produces the
            ``no_candidates`` empty-state signal). ``None`` (the default
            on a missing field) means "fall back to the module-level
            default" — distinct from empty-list semantics.
        exclude: List of path-prefixes / dot-name exclusions. Same
            ``None`` vs empty-list semantics as ``include``.
    """

    include: list[str] | None = None
    exclude: list[str] | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ScaffoldConfig":
        """Build from a raw dict with schema-tolerance.

        Unknown keys are silently dropped (forward-compatibility).
        ``include`` / ``exclude`` must be lists if present; non-list
        values surface as ``None`` (fall through to module defaults)
        rather than crashing the loader — same fail-soft posture as
        the other config dataclasses.
        """
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        # Normalize list-ish fields: accept list-of-str, coerce
        # scalar-string into a single-item list (operator-friendly for
        # the common one-entry case), reject everything else by
        # falling through to None.
        for key in ("include", "exclude"):
            if key not in known:
                continue
            val = known[key]
            if val is None:
                continue
            if isinstance(val, list):
                # Stringify each entry defensively; YAML can parse a
                # bare ``.obsidian`` as a string but a list of mixed
                # types is an operator error we shouldn't silently
                # accept.
                known[key] = [str(v) for v in val]
            elif isinstance(val, str):
                known[key] = [val] if val else []
            else:
                # Unrecognized shape — fall through to module default
                # by setting None rather than crashing the loader.
                known[key] = None
        return cls(**known)


def load_from_unified(raw: dict[str, Any]) -> ScaffoldConfig:
    """Load the scaffold block from the unified config dict.

    Args:
        raw: The unified config dict (loaded from ``config.yaml``).
            Pass ``{}`` to get a default-shaped config.

    Returns:
        A :class:`ScaffoldConfig`. When no ``scaffold`` section is
        present in ``raw``, returns an instance with ``include=None``
        and ``exclude=None`` — both fields signal "use module-level
        defaults" downstream. Never returns ``None`` so callers can
        always read ``cfg.include`` without a null-check.
    """
    section = raw.get("scaffold", {}) or {}
    if not isinstance(section, dict):
        # Malformed config; treat as missing and fall back to defaults.
        section = {}
    return ScaffoldConfig.from_dict(section)


__all__ = ["ScaffoldConfig", "load_from_unified"]
