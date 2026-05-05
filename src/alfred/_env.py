"""Shared env-var ${VARNAME} substitution helpers.

The ``${VARNAME}`` placeholder shape appears across every config
loader (``transport/config.py``, ``daily_sync/config.py``,
``distiller/config.py``, etc.) AND in the orchestrator's runtime
env-injection path. Three+ copy-pastes of the same regex + same
``os.environ.get(...)``-with-fallback semantics is the seed for
future drift, especially around edge cases like empty-string env
values (P0 from QA 2026-05-05 surfaced a same-shape gap on the
runtime-injection side).

This module is the canonical home for the shape. New callers should
import from here instead of re-defining ``ENV_RE`` + their own
substitution function. Existing callers migrate incidentally as
touched — full sweep is out of scope for any single ship.

Public surface:

  * :data:`ENV_PLACEHOLDER_RE` — the canonical regex.
  * :func:`resolve_env_placeholders` — substitute placeholders in a
    single string.
  * :func:`substitute_env_in_value` — recursive variant that walks
    dicts / lists; matches the older ``_substitute_env`` shape used
    by every config loader.

Both substituters use the SAME coalesce semantics:
``os.environ.get(name) or fallback`` — coalesces both the ``None``
case (env var absent) AND the empty-string case (env var explicitly
set to ``""``). The latter matters when an operator intentionally
empties a token to break authentication during testing — without
the empty-string coalesce, the empty value would propagate as
``Bearer `` (empty) and produce a different 401 shape than the
"placeholder unresolved" case the defensive guard is meant to catch.
"""

from __future__ import annotations

import os
import re
from typing import Any


# Canonical placeholder regex. Matches ``${VARNAME}`` where VARNAME
# is one or more word characters (``[A-Za-z0-9_]+``).
ENV_PLACEHOLDER_RE: re.Pattern[str] = re.compile(r"\$\{(\w+)\}")


def resolve_env_placeholders(value: str) -> str:
    """Substitute ``${VARNAME}`` placeholders against ``os.environ``.

    Returns the input unchanged when no placeholders are present.
    Unresolved placeholders (env var not set OR set to empty string)
    stay as the literal ``${VARNAME}`` so callers can detect
    "still unresolved" via ``startswith("${")``.

    The empty-string coalesce semantics are load-bearing: an env
    var explicitly set to ``""`` (operator emptying a token to test
    auth-failure paths) MUST be treated the same as an unset var —
    leaking an empty bearer token into headers produces a different
    failure shape than the "unresolved placeholder" path the
    defensive guard targets.
    """
    def _replace(m: re.Match[str]) -> str:
        name = m.group(1)
        # Coalesce: missing env (None) AND empty-string env both
        # fall through to the literal placeholder. Operator who
        # intentionally empties a token gets the same defensive
        # guard as one who never set it.
        return os.environ.get(name) or m.group(0)
    return ENV_PLACEHOLDER_RE.sub(_replace, value)


def substitute_env_in_value(value: Any) -> Any:
    """Recursively substitute ``${VARNAME}`` in strings inside a
    nested dict / list / scalar structure.

    Mirrors the ``_substitute_env`` shape every config loader has
    re-implemented locally. Returns NEW collections (does not mutate
    inputs); scalars pass through.
    """
    if isinstance(value, str):
        return resolve_env_placeholders(value)
    if isinstance(value, dict):
        return {k: substitute_env_in_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute_env_in_value(v) for v in value]
    return value


__all__ = [
    "ENV_PLACEHOLDER_RE",
    "resolve_env_placeholders",
    "substitute_env_in_value",
]
