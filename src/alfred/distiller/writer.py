"""Deterministic writer for learn records — Python owns frontmatter.

Week 1 MVP of the distiller rebuild (see memory
``project_distiller_rebuild.md``). Complements the non-agentic
extractor (``extractor.py``) — the LLM picks field *values*, Python
assembles the *file*. The frontmatter shape is derived mechanically
from ``vault/schema.py``; the LLM is never invited to compose YAML.

Two write modes:
  - **Live mode** (``shadow_root=None``) — goes through
    ``ops.vault_create(scope='distiller')``. The scope kwarg (added
    2026-04-24 in commit ``3bd0678``) runs the distiller scope gate,
    so even a malformed LearningCandidate that slips past Pydantic
    can't land a field the SKILL forbids.
  - **Shadow mode** (``shadow_root`` set) — writes directly under
    ``shadow_root / TYPE_DIRECTORY[type] / slug.md`` with the same
    frontmatter+body shape ``vault_create`` produces. The operator
    can diff shadow against legacy output in Week 2 before any live
    rollout decision.

Week 1 scope: body is passed through as-is (expect empty string or
minimal stub). Real prose drafting ships Week 3 via ``drafter.py``.
"""

from __future__ import annotations

import datetime as _dt
import re
from pathlib import Path

import frontmatter

from alfred.vault import attribution
from alfred.vault.ops import vault_create
from alfred.vault.schema import TYPE_DIRECTORY

from .contracts import LearningCandidate
from .utils import get_logger

log = get_logger(__name__)


# --- Slug helper ------------------------------------------------------------
#
# Keep this local so a future vault-wide slug helper doesn't silently
# break the writer. Rules:
#   - ASCII letters + digits + spaces survive; everything else is a dash.
#   - Collapse runs of dashes and trim.
#   - Length-cap 80 chars to keep filenames reasonable on exFAT.
#
# Don't lowercase — Obsidian is case-sensitive on Linux, and the
# title casing is load-bearing for human-readable file lists.

_SLUG_NONALNUM = re.compile(r"[^A-Za-z0-9 \-_]+")
_SLUG_DASH_RUN = re.compile(r"-{2,}")


def _slugify(title: str, max_length: int = 80) -> str:
    """Turn a learning title into a safe markdown filename stem."""
    stripped = title.strip()
    cleaned = _SLUG_NONALNUM.sub(" ", stripped)
    # Collapse whitespace to single spaces (preserved for readability).
    cleaned = " ".join(cleaned.split())
    # Collapse run-on dashes from edge cases like "foo---bar".
    cleaned = _SLUG_DASH_RUN.sub("-", cleaned)
    if not cleaned:
        cleaned = "untitled"
    return cleaned[:max_length].rstrip(" -")


# --- Frontmatter assembly ---------------------------------------------------


def _assemble_frontmatter(
    spec: LearningCandidate,
) -> dict:
    """Build the learn-record frontmatter dict from a validated spec.

    Mirrors what ``vault_create`` emits on the live path, so shadow-mode
    output is diff-comparable. Fields:
      - ``type``, ``name``, ``created`` — core identity (``vault_create``
        would also set these in live mode; we include them here so
        shadow files match 1:1).
      - ``status``, ``confidence``, ``claim``, ``evidence_excerpt`` —
        from the Pydantic spec.
      - ``source_links``, ``entity_links`` — only included when non-
        empty, to keep the YAML tidy.
      - ``project`` — included only when non-None.
    """
    fm: dict = {
        "type": spec.type,
        "name": spec.title,
        "status": spec.status,
        "confidence": spec.confidence,
        "claim": spec.claim,
        "created": _dt.date.today().isoformat(),
    }
    if spec.evidence_excerpt:
        fm["evidence_excerpt"] = spec.evidence_excerpt
    if spec.source_links:
        fm["source_links"] = list(spec.source_links)
    if spec.entity_links:
        fm["entity_links"] = list(spec.entity_links)
    if spec.project is not None:
        fm["project"] = spec.project
    return fm


# --- Shadow write (no scope gate — shadow_root lives outside vault) --------


def _shadow_write(
    spec: LearningCandidate,
    body_draft: str,
    shadow_root: Path,
) -> Path:
    """Write the record under ``shadow_root`` without touching the vault.

    We don't call ``vault_create`` here because scope checks assume a
    real vault layout (template loading, near-match checks, base-embed
    injection, wikilink validation). Shadow mode is about producing a
    mechanically-identical frontmatter+body shape for diff comparison,
    not about reproducing every live-path side effect.
    """
    directory = TYPE_DIRECTORY.get(spec.type, spec.type)
    slug = _slugify(spec.title)
    rel_path = Path(directory) / f"{slug}.md"
    file_path = shadow_root / rel_path

    if file_path.exists():
        # Shadow dedup by filename is fine — the same spec rendered
        # twice should produce the same slug, and a collision means
        # the extractor proposed a near-duplicate. Log and skip.
        log.info(
            "writer.shadow.skip_existing",
            path=str(rel_path),
            type=spec.type,
        )
        return file_path

    fm = _assemble_frontmatter(spec)

    # Wrap the body in BEGIN_INFERRED markers if it carries inferred
    # prose — mirrors the live-path attribution contract (see
    # ``alfred.vault.attribution``). Empty body: no marker (there's no
    # prose to attribute).
    body: str = body_draft or ""
    if body.strip():
        wrapped_body, audit_entry = attribution.with_inferred_marker(
            body,
            section_title=spec.title,
            agent="distiller",
            reason="distiller v2 extractor (shadow mode)",
        )
        attribution.append_audit_entry(fm, audit_entry)
        body = wrapped_body

    file_path.parent.mkdir(parents=True, exist_ok=True)
    post = frontmatter.Post(body, **fm)
    file_path.write_text(frontmatter.dumps(post) + "\n", encoding="utf-8")

    log.info(
        "writer.shadow.wrote",
        path=str(rel_path),
        type=spec.type,
        shadow_root=str(shadow_root),
    )
    return file_path


# --- Live write (goes through vault_create + scope gate) --------------------


def _live_write(
    spec: LearningCandidate,
    body_draft: str,
    vault_path: Path,
) -> Path:
    """Write the record to the live vault via ``vault_create``.

    ``scope="distiller"`` runs the distiller scope gate in ``ops.vault_create``
    so the write is permission-checked even though we assembled the
    frontmatter ourselves. Belt-and-braces — Pydantic validation is the
    primary gate, scope is a secondary line.
    """
    slug = _slugify(spec.title)

    # ``vault_create`` builds frontmatter from the template + the
    # ``set_fields`` dict we pass. To keep the LLM out of the frontmatter
    # entirely, we pre-assemble every field we care about and let the
    # template only contribute structural defaults (base-embeds, etc).
    set_fields = _assemble_frontmatter(spec)
    # ``vault_create`` re-applies ``type`` and the title field itself
    # from its ``record_type`` / ``name`` args; leaving those in
    # set_fields is harmless (they get overwritten to the same values).

    body: str = body_draft or ""
    if body.strip():
        wrapped_body, audit_entry = attribution.with_inferred_marker(
            body,
            section_title=spec.title,
            agent="distiller",
            reason="distiller v2 extractor",
        )
        attribution.append_audit_entry(set_fields, audit_entry)
        body = wrapped_body

    result = vault_create(
        vault_path,
        spec.type,
        slug,
        set_fields=set_fields,
        body=body or None,
        scope="distiller",
    )
    log.info(
        "writer.live.wrote",
        path=result["path"],
        type=spec.type,
        warnings=len(result.get("warnings") or []),
    )
    return vault_path / result["path"]


# --- Public entry point -----------------------------------------------------


def write_learn_record(
    spec: LearningCandidate,
    body_draft: str = "",
    shadow_root: Path | None = None,
    vault_path: Path | None = None,
) -> Path:
    """Write a single learn-type record deterministically.

    Modes:
      - ``shadow_root`` set → writes under ``shadow_root``. ``vault_path``
        is not required. Used in Week 1+2 to produce diff-comparable
        output without touching the live vault.
      - ``shadow_root`` None → writes via ``vault_create(scope='distiller')``.
        ``vault_path`` is required.

    Returns the absolute ``Path`` the file landed at.
    """
    if shadow_root is not None:
        return _shadow_write(spec, body_draft, shadow_root)

    if vault_path is None:
        raise ValueError(
            "write_learn_record: either shadow_root or vault_path must be set"
        )
    return _live_write(spec, body_draft, vault_path)
