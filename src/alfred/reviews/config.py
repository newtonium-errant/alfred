"""Reviews config — project name → vault path resolution map.

Hardcoded defaults match the four bash_exec-allowed repo roots.
Override via the ``kalle.projects`` block in unified config::

    kalle:
      projects:
        aftermath-lab: /home/andrew/aftermath-lab
        alfred:        /home/andrew/aftermath-alfred
        rrts:          /home/andrew/aftermath-rrts
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

ENV_RE = re.compile(r"\$\{(\w+)\}")


def _substitute_env(value: Any) -> Any:
    if isinstance(value, str):
        def _replace(m: re.Match) -> str:
            return os.environ.get(m.group(1), m.group(0))
        return ENV_RE.sub(_replace, value)
    if isinstance(value, dict):
        return {k: _substitute_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute_env(v) for v in value]
    return value


_DEFAULT_PROJECTS: dict[str, str] = {
    "aftermath-lab": str(Path.home() / "aftermath-lab"),
    "alfred": str(Path.home() / "aftermath-alfred"),
    "rrts": str(Path.home() / "aftermath-rrts"),
}


def resolve_projects(raw: dict[str, Any]) -> dict[str, Path]:
    """Return the project-name → vault-path map.

    Defaults can be partially overridden by ``kalle.projects``. Unknown
    keys in the override block are merged in (not validated against a
    closed set) so a fifth project can be added without code changes.
    """
    raw = _substitute_env(raw)
    out: dict[str, Path] = {k: Path(v) for k, v in _DEFAULT_PROJECTS.items()}
    overrides = ((raw.get("kalle") or {}).get("projects") or {})
    if isinstance(overrides, dict):
        for name, path in overrides.items():
            if isinstance(name, str) and isinstance(path, str) and path:
                out[name] = Path(path)
    return out


def resolve_project_path(raw: dict[str, Any], project: str) -> Path:
    """Resolve a project name to its vault root or raise KeyError.

    The error message lists the known projects so the caller's CLI can
    surface a clear "did you mean…" line.
    """
    projects = resolve_projects(raw)
    if project not in projects:
        known = ", ".join(sorted(projects)) or "(none configured)"
        raise KeyError(
            f"unknown project: {project!r}. Known projects: {known}"
        )
    return projects[project]
