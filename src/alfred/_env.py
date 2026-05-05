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


# ---------------------------------------------------------------------------
# .env file auto-loader
# ---------------------------------------------------------------------------
#
# Operator gotcha (P1 from QA 2026-05-05 401-fix validation): running
# ``alfred up`` from a fresh shell that hasn't ``set -a; source .env``
# silently inherits whatever ``ALFRED_TRANSPORT_TOKEN`` was set in the
# shell's prior context (often Salem's value from .env loaded into
# THIS shell hours ago). Per-instance tokens like
# ``ALFRED_KALLE_TRANSPORT_TOKEN`` aren't visible → orchestrator's
# resolver takes ``skipped_unresolved`` → daemons inherit Salem's
# token → KAL-LE's transport server returns 401.
#
# Fix: orchestrator auto-loads ``.env`` itself from the directory
# containing the active config file (``--config config.kalle.yaml``
# → look for ``.env`` next to it). Eliminates the ``set -a; source``
# operator step.
#
# Design choices:
#   * No ``python-dotenv`` dependency — minimal inline parser handles
#     the same shapes (``KEY=value`` / ``KEY="quoted value"`` /
#     ``KEY='single-quoted'``, ``#`` line comments, blank lines).
#     Same behaviour as ``set -a; source .env`` for the keys Alfred
#     uses; complex shell features (subshell substitution, multi-line
#     values) deliberately out of scope.
#   * Existing env vars WIN — ``override=False`` semantics. An explicit
#     ``export ALFRED_TRANSPORT_TOKEN=...`` in the parent shell
#     overrides the .env value, so manual debugging stays predictable.
#   * Missing .env is a no-op, NOT an error. Production deployments
#     (systemd, k8s) set env vars without .env files; the orchestrator
#     must work with both.


def _parse_dotenv_line(line: str) -> tuple[str, str] | None:
    """Parse one .env line into ``(key, value)`` or return ``None``.

    Handles:
      * ``KEY=value`` — bare value, no quoting
      * ``KEY="quoted value"`` — double-quoted, strips surrounding quotes
      * ``KEY='single-quoted'`` — single-quoted, same
      * ``# comment`` lines → None
      * blank lines → None
      * leading/trailing whitespace stripped

    Deliberately does NOT handle:
      * Multi-line values (rare in practice; not worth the parser
        complexity for a stopgap auto-loader)
      * Subshell substitution (``$(cmd)``) — leaks command execution
      * Variable expansion within values (``KEY=${OTHER}``) — env-var
        substitution belongs in the config layer (``${VARNAME}``
        substitutor above), not the .env layer

    Returns ``None`` for any malformed line (e.g. missing ``=``,
    empty key) so the caller can skip + log.
    """
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    # ``export KEY=value`` is a common .env idiom (set -a habit).
    # Strip the leading ``export `` so it parses the same as bare KEY=.
    if stripped.startswith("export "):
        stripped = stripped[len("export "):].lstrip()
    if "=" not in stripped:
        return None
    key, _, value = stripped.partition("=")
    key = key.strip()
    if not key:
        return None
    value = value.strip()
    # Strip matched surrounding quotes (single OR double).
    if len(value) >= 2 and (
        (value[0] == '"' and value[-1] == '"')
        or (value[0] == "'" and value[-1] == "'")
    ):
        value = value[1:-1]
    return key, value


def load_dotenv_file(env_path: "os.PathLike[str] | str") -> dict[str, str]:
    """Parse a .env file into a ``{key: value}`` dict.

    Returns an empty dict when the file is missing OR can't be read
    OR contains no parseable lines. Never raises — operator-friendly
    behaviour for an auto-load shape (a malformed line shouldn't
    crash daemon startup; it just doesn't get loaded). Malformed
    lines are silently skipped (caller can log a count via the
    auto-load wrapper if needed).

    Does NOT touch ``os.environ`` — pure read. The caller decides
    whether to inject (``override=False`` semantics live in the
    auto-loader, not here).
    """
    from pathlib import Path

    path = Path(env_path)
    if not path.is_file():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    out: dict[str, str] = {}
    for raw_line in text.splitlines():
        parsed = _parse_dotenv_line(raw_line)
        if parsed is None:
            continue
        key, value = parsed
        out[key] = value
    return out


def auto_load_dotenv(
    env_path: "os.PathLike[str] | str",
    *,
    override: bool = False,
) -> tuple[int, int]:
    """Load a .env file into ``os.environ`` if present.

    Returns ``(loaded, skipped)`` where ``loaded`` is the number of
    vars actually set in ``os.environ`` and ``skipped`` is the number
    of vars present in the .env but already set in env (preserved by
    the ``override=False`` default).

    ``override=False`` (default): existing env vars win. An explicit
    ``export FOO=...`` in the parent shell survives; the .env value
    only fills GAPS. This preserves manual-debugging predictability —
    operator who wants to override a .env value just does it the
    normal shell way.

    ``override=True``: .env values replace existing env. Reserved for
    test fixtures that want full control over the environment;
    production should never set this.

    Missing file or unreadable file → ``(0, 0)`` (no-op, no raise).
    Per ``feedback_intentionally_left_blank.md``, the caller is
    responsible for emitting a structured log so a missing .env is
    observably distinct from a loaded-empty .env.
    """
    parsed = load_dotenv_file(env_path)
    if not parsed:
        return (0, 0)
    loaded = 0
    skipped = 0
    for key, value in parsed.items():
        if override or key not in os.environ:
            os.environ[key] = value
            loaded += 1
        else:
            skipped += 1
    return (loaded, skipped)


__all__ = [
    "ENV_PLACEHOLDER_RE",
    "auto_load_dotenv",
    "load_dotenv_file",
    "resolve_env_placeholders",
    "substitute_env_in_value",
]
