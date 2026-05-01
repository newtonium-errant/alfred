"""Shared helpers for vault config normalization across all tools.

The vault config block (`vault:` in config.yaml) historically had a single
``ignore_dirs`` field that conflated two semantically distinct concerns:

1. **Outbound scanning** — directories whose records should NOT be scanned
   for issues. ``note/``, ``inbox/processed/``, etc. should be invisible to
   janitor's structural scan.
2. **Indexing for valid link targets** — directories that should NOT be
   considered valid wikilink targets. This is a much narrower set: a record
   under ``session/`` is a perfectly valid link target even though we don't
   scan it for issues.

Conflating these means dirs in ``ignore_dirs`` (outbound scan exclusion)
also vanish from the stem index, causing valid wikilinks to records in
those dirs to be reported as LINK001. The fix splits the field into two:

- ``dont_scan_dirs`` — outbound scan exclusion (replaces ``ignore_dirs``)
- ``dont_index_dirs`` — valid-link-target index exclusion

Backward compatibility: configs with only legacy ``ignore_dirs`` still work
— that key is treated as ``dont_scan_dirs`` (preserving outbound-scan
behavior). Indexing exclusion defaults to empty, fixing the bug.
"""

from __future__ import annotations

import logging
from typing import Any

# Process-wide flag: have we already emitted the legacy-key deprecation
# warning this process? Set on first emission, never re-fires until a test
# calls ``reset_deprecation_log()``.
#
# Why a plain bool and not an id-keyed set: every tool's ``load_from_unified``
# runs ``_substitute_env(raw)`` first, which deep-copies the config tree.
# So ``raw["vault"]`` has a different ``id()`` per tool, and an id-keyed set
# would fire once per tool (5 tools = 4 redundant warnings observed during
# a single ``alfred up`` boot). The docstring contract is "once per process"
# — a module-level bool matches the contract and removes the footgun.
_DEPRECATION_LOGGED: bool = False


def normalize_vault_block(vault_raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize a vault config dict to the new dont_scan/dont_index shape.

    Takes the raw ``vault:`` section from config.yaml and returns a new
    dict suitable for building a tool's ``VaultConfig`` dataclass. The
    dict is copied — the input is not mutated.

    Behavior:
    - If ``dont_scan_dirs`` is present in the input, it is preserved AND
      copied into ``ignore_dirs`` for back-compat with existing call sites
      that read ``config.vault.ignore_dirs`` (every scanner / snapshot /
      walker in the codebase). Eventually those call sites can switch to
      reading ``dont_scan_dirs`` directly; until then we keep both.
    - If only legacy ``ignore_dirs`` is present, log a one-time deprecation
      hint and treat it as ``dont_scan_dirs`` (i.e. leave it as-is). The
      indexing exclusion defaults to empty — that's the bug fix.
    - ``dont_index_dirs`` is passed through unchanged. Default empty list.

    Returns a new dict; caller must use the returned value.
    """
    if not isinstance(vault_raw, dict):
        # Defensive: callers may pass None or weird YAML. Return an empty
        # dict so the dataclass falls back to its defaults.
        return {}

    out = dict(vault_raw)
    has_new = "dont_scan_dirs" in out
    has_legacy = "ignore_dirs" in out

    if has_new:
        # New-shape config: dont_scan_dirs wins. Mirror to ignore_dirs for
        # back-compat with all the existing call sites that still read
        # ``config.vault.ignore_dirs``.
        out["ignore_dirs"] = list(out["dont_scan_dirs"])
    elif has_legacy:
        # Legacy-only config: log a one-time deprecation hint per process.
        # Process-wide bool dedup — see _DEPRECATION_LOGGED docstring above
        # for why we don't key by id(vault_raw).
        global _DEPRECATION_LOGGED
        if not _DEPRECATION_LOGGED:
            _DEPRECATION_LOGGED = True
            log = logging.getLogger("alfred.vault.config")
            log.warning(
                "vault.ignore_dirs_deprecated: 'vault.ignore_dirs' is "
                "deprecated; rename to 'vault.dont_scan_dirs' (and "
                "optionally add 'vault.dont_index_dirs: []' for valid-link-"
                "target index exclusion). Legacy key still works but will "
                "not be split semantically — index exclusion defaults to "
                "empty (this is the LINK001-on-session bug fix)."
            )
        # ``ignore_dirs`` already in out from the dict() copy.

    # Always ensure dont_index_dirs has a default empty list. Tools'
    # VaultConfig dataclass also has default_factory=list, but being
    # explicit here means tests that build configs from raw dicts get the
    # same behavior as YAML-loaded configs.
    out.setdefault("dont_index_dirs", [])

    return out


def reset_deprecation_log() -> None:
    """Reset the once-per-process flag. Test-only helper.

    Process-wide module state means the deprecation log fires at most once
    per process, but tests that exercise multiple legacy-config loads in
    the same process need a way to verify the warning fires at all.
    """
    global _DEPRECATION_LOGGED
    _DEPRECATION_LOGGED = False
