"""Typed config for the Google Calendar integration.

Loaded via ``load_from_unified(raw)`` like every other tool. The
``gcal:`` block lives at the top level of ``config.yaml`` (alongside
``transport:``, ``brief:``, etc.) — keeping it out of ``vault:`` /
``transport:`` because it's not exclusively owned by either:

  * The transport handler (Salem) consumes it for conflict-check + sync.
  * The ``alfred gcal`` CLI (any instance) consumes it for authorize /
    status / test-write.
  * Future consumers (V.E.R.A. RRTS calendar, STAY-C client calendar)
    will each define their own gcal block in their own config.

Default-disabled per spec — instances that don't want GCal don't get
charged any setup cost. Hypatia / KAL-LE leave ``enabled: false``.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ENV_RE = re.compile(r"\$\{(\w+)\}")


def _substitute_env(value: Any) -> Any:
    """Recursively replace ``${VAR}`` placeholders. Same pattern as every other tool."""
    if isinstance(value, str):
        def _replace(m: re.Match) -> str:
            return os.environ.get(m.group(1), m.group(0))
        return ENV_RE.sub(_replace, value)
    if isinstance(value, dict):
        return {k: _substitute_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute_env(v) for v in value]
    return value


# Default OAuth scope. Wide enough to read both calendars + write to
# the Alfred calendar; narrow enough that we can't mutate calendar
# objects themselves.
DEFAULT_SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/calendar.events",
)


@dataclass
class GCalConfig:
    """Top-level ``gcal:`` config block.

    Defaults match the operator-setup spec: credentials + token under
    ``~/alfred/data/secrets/`` so they live alongside other Alfred
    secrets and don't accidentally get committed.

    All calendar IDs are populated from env vars by default — the
    operator drops them into ``.env`` after Andrew's manual GCal/OAuth
    setup is done. Empty string = unconfigured.
    """

    # Master switch. Default false so a fresh install / Hypatia / KAL-LE
    # config doesn't accidentally start trying to talk to Google.
    enabled: bool = False

    # OAuth client credentials (downloaded from Google Cloud Console).
    credentials_path: str = "~/alfred/data/secrets/gcal_credentials.json"

    # Cached OAuth token (created/refreshed by the adapter).
    token_path: str = "~/alfred/data/secrets/gcal_token.json"

    # The dedicated "Alfred" calendar Salem writes to (R/W). Populated
    # via env so different instances can point at different calendars
    # without checking secrets into git.
    alfred_calendar_id: str = ""

    # Andrew's primary calendar (read-only by application policy).
    # Salem queries this for conflict-check; never writes to it.
    primary_calendar_id: str = ""

    # OAuth scopes. Defaults to the narrowest scope that supports R/W
    # on events. Override only if a future consumer needs broader
    # access (e.g. a calendar-creation flow — would require
    # ``calendar`` not ``calendar.events``).
    scopes: list[str] = field(default_factory=lambda: list(DEFAULT_SCOPES))


# --- Builder ---------------------------------------------------------------


def _build(data: dict[str, Any]) -> GCalConfig:
    """Recursively build :class:`GCalConfig` from a raw dict.

    Tolerates extra keys (forward-compat) and unknown types (treats as
    default). The schema-tolerance contract: a config written by an
    older tool version with new keys must not crash the loader, and
    a config written by a newer version with extra keys silently
    ignores them on rollback.
    """
    if not isinstance(data, dict):
        return GCalConfig()
    known = {
        "enabled",
        "credentials_path",
        "token_path",
        "alfred_calendar_id",
        "primary_calendar_id",
        "scopes",
    }
    kwargs: dict[str, Any] = {k: v for k, v in data.items() if k in known}
    # Coerce bool-ish values; default False.
    if "enabled" in kwargs:
        kwargs["enabled"] = bool(kwargs["enabled"])
    # Coerce paths to str (Path coercion happens at use-site).
    for path_key in ("credentials_path", "token_path"):
        if path_key in kwargs and kwargs[path_key] is not None:
            kwargs[path_key] = str(kwargs[path_key])
    # Coerce calendar IDs to str; missing/null becomes empty string.
    for id_key in ("alfred_calendar_id", "primary_calendar_id"):
        val = kwargs.get(id_key, "")
        kwargs[id_key] = "" if val is None else str(val)
    # Scopes: list of strings; tolerate scalar.
    if "scopes" in kwargs:
        scopes = kwargs["scopes"]
        if isinstance(scopes, str):
            kwargs["scopes"] = [scopes]
        elif isinstance(scopes, list):
            kwargs["scopes"] = [str(s) for s in scopes]
        else:
            kwargs["scopes"] = list(DEFAULT_SCOPES)
    return GCalConfig(**kwargs)


def load_from_unified(raw: dict[str, Any]) -> GCalConfig:
    """Build :class:`GCalConfig` from a pre-loaded unified config dict.

    Returns all-default (disabled) config when the ``gcal`` section is
    absent. Callers MUST check ``config.enabled`` before invoking the
    adapter — silently defaulting to "no GCal" is the right behaviour
    for instances that haven't opted in.
    """
    raw = _substitute_env(raw)
    section = raw.get("gcal", {}) or {}
    return _build(section)


def load_config(path: str | Path = "config.yaml") -> GCalConfig:
    """Convenience loader for CLI use — reads ``config.yaml`` directly."""
    import yaml

    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return load_from_unified(raw)
