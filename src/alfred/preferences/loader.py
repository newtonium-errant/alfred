"""Filesystem loader for operator-preference records.

Scans a vault's ``preference/`` directory for ``status: active``
records and returns them as ``Preference`` dataclasses. The loader is
intentionally inert toward the calling instance — it reads whatever
``vault_path`` it's pointed at. Per-instance routing (Salem reads
her own; Hypatia reads BOTH Salem's and her own; KAL-LE reads
Salem's only — see ``project_operator_preferences_v1.md`` Hard
Contract #7) lives at the call site.

Forward-compat: unknown fields on a preference record are silently
preserved in ``raw`` rather than dropped. The dataclass exposes
typed convenience accessors for the V1 fields and gives consumers
``raw`` for everything else — same pattern as the state-tolerance
contract in ``CLAUDE.md``.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import frontmatter
import structlog

log = structlog.get_logger(__name__)
_logging_log = logging.getLogger(__name__)  # for sync tests using caplog


@dataclass(frozen=True)
class Preference:
    """A single operator-preference record loaded from disk.

    V1 fields:
    - ``slug`` — filename stem (preference identifier — stable, used
      for conflict detection by local-vs-canonical matching).
    - ``name`` — human-readable title (frontmatter ``name``).
    - ``shape`` — ``"action"`` or ``"voice"``.
    - ``scope`` — ``"universal"`` or ``"instance"``.
    - ``applies_to_instance`` — instance name when ``scope ==
      "instance"`` (None for universal).
    - ``applies_to_user`` — V1 always None; reserved for V.E.R.A.
      multi-user differentiation.
    - ``cites_canonical`` — wikilink to a canonical preference this
      record overrides/extends/rejects, or None.
    - ``source_quote`` — verbatim quote from the originating
      conversation establishing this preference.
    - ``source_session`` — wikilink to the originating session.
    - ``matcher`` — Shape-A matcher dispatch (None for Shape B).
    - ``body`` — full markdown body (the ``## Policy`` section in
      particular is what voice consumers concatenate).
    - ``path`` — absolute filesystem path the record was loaded from.
    - ``raw`` — full frontmatter dict for forward-compat consumers.
    """

    slug: str
    name: str
    shape: str
    scope: str
    applies_to_instance: str | None
    applies_to_user: str | None
    cites_canonical: str | None
    source_quote: str
    source_session: str
    matcher: dict[str, Any] | None
    body: str
    path: Path
    raw: dict[str, Any] = field(default_factory=dict)


def _coerce_string(value: Any) -> str:
    """Best-effort coerce a frontmatter scalar to a non-None string.

    Empty / missing / non-string values become ``""``. Used for the
    optional-but-tolerated string fields (``source_quote``,
    ``source_session``) where None and empty are equivalent.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def _coerce_optional_string(value: Any) -> str | None:
    """Coerce a value to ``str | None`` — empty strings become None.

    Used for the ``applies_to_instance`` / ``applies_to_user`` /
    ``cites_canonical`` fields where None and empty string are
    semantically identical (both = "this field doesn't apply").
    """
    if value is None or value == "":
        return None
    if isinstance(value, str):
        s = value.strip()
        return s if s else None
    return str(value)


def _coerce_matcher(value: Any) -> dict[str, Any] | None:
    """Coerce the matcher field to ``dict | None``.

    Schema requires a nested ``{domain, rule, args}`` dict for Shape
    A records. Shape B records have no matcher (None). Defensive:
    non-dict + non-None values are treated as None (the consumer
    won't dispatch, the record is silently dropped from gate paths).
    """
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    return None


def _build_preference(path: Path, fm: dict[str, Any], body: str) -> Preference:
    """Construct a ``Preference`` from parsed frontmatter."""
    return Preference(
        slug=path.stem,
        name=_coerce_string(fm.get("name")),
        shape=_coerce_string(fm.get("shape")),
        scope=_coerce_string(fm.get("scope")),
        applies_to_instance=_coerce_optional_string(fm.get("applies_to_instance")),
        applies_to_user=_coerce_optional_string(fm.get("applies_to_user")),
        cites_canonical=_coerce_optional_string(fm.get("cites_canonical")),
        source_quote=_coerce_string(fm.get("source_quote")),
        source_session=_coerce_string(fm.get("source_session")),
        matcher=_coerce_matcher(fm.get("matcher")),
        body=body,
        path=path,
        raw=dict(fm),
    )


def load_active_preferences(
    vault_path: str | Path,
    *,
    shape: str | None = None,
) -> list[Preference]:
    """Load ``status: active`` preference records from ``<vault>/preference/``.

    Args:
        vault_path: vault root. The loader appends ``preference/`` to
            this and scans for ``*.md`` files. Missing directory
            returns an empty list (logged for observability).
        shape: optional filter — pass ``"action"`` to get only Shape A
            records, ``"voice"`` for only Shape B. Defaults to None
            (all active records).

    Returns:
        List of ``Preference`` dataclasses, sorted by filename for
        deterministic ordering. ``status: revoked`` records and
        records missing the ``type: preference`` marker are excluded.

    The directory-missing path emits an info-level log
    (``preferences.no_directory``) so the operator-grep pattern
    distinguishes "no preferences yet" from "loader broken." Per
    ``feedback_intentionally_left_blank.md``.
    """
    vault = Path(vault_path)
    pref_dir = vault / "preference"
    if not pref_dir.is_dir():
        log.info(
            "preferences.no_directory",
            vault_path=str(vault),
            pref_dir=str(pref_dir),
            shape_filter=shape,
            detail="no preference/ directory at this vault — zero active preferences",
        )
        _logging_log.info(
            "preferences.no_directory vault_path=%s pref_dir=%s",
            str(vault), str(pref_dir),
        )
        return []

    out: list[Preference] = []
    for md_file in sorted(pref_dir.glob("*.md")):
        try:
            post = frontmatter.load(str(md_file))
        except Exception as exc:
            log.warning(
                "preferences.parse_failed",
                path=str(md_file),
                error=str(exc),
            )
            _logging_log.warning(
                "preferences.parse_failed path=%s error=%s",
                str(md_file), str(exc),
            )
            continue
        fm = dict(post.metadata)
        if fm.get("type") != "preference":
            # Defensive: some unrelated file slipped into the
            # preference/ directory. Skip silently — the file may be
            # a README or test fixture.
            continue
        if fm.get("status") != "active":
            continue
        if shape is not None and fm.get("shape") != shape:
            continue
        out.append(_build_preference(md_file, fm, post.content))

    log.info(
        "preferences.loaded",
        vault_path=str(vault),
        count=len(out),
        shape_filter=shape,
    )
    _logging_log.info(
        "preferences.loaded vault_path=%s count=%d shape_filter=%s",
        str(vault), len(out), shape,
    )
    return out
