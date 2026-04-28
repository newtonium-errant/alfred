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

## Attribution-audit retrofit (2026-04-28)

V2 records previously emitted bare frontmatter with no body. That made
them incompatible with the SUPERSEDED-marker sweep
(``janitor/superseded_marker.py``, shipped 2026-04-27) and the audit-
trail tooling, which require a ``BEGIN_INFERRED`` HTML comment + an
``attribution_audit`` frontmatter list to pair correction notes back
to inferred blocks. The writer now:

  - Synthesizes a structured body from the validated ``LearningCandidate``
    (H1, Claim, Evidence Trail, optional Source Records list, base
    embeds) when no ``body_draft`` is supplied.
  - Wraps the entire body — base embeds included — in BEGIN/END
    INFERRED markers via ``alfred.vault.attribution.with_inferred_marker``
    (the canonical helper — never re-derive marker_id format per
    ``feedback_marker_id_canonical_regex.md``).
  - Stamps an ``attribution_audit`` frontmatter entry with the matching
    ``marker_id`` so Phase 2's Daily Sync confirm/reject flow and the
    janitor's SUPERSEDED-marker sweep can find the block.

V2 stays in shadow mode for this work — graduation to the live vault
is Phase A of the distiller rebuild, deliberately deferred. The
retrofit just makes shadow records ready to graduate.
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


# --- Body assembly ----------------------------------------------------------
#
# V2's extractor produces only ``claim`` + ``evidence_excerpt`` +
# ``source_links``. We render those into a tighter body shape than
# legacy's full Context/Options/Decision/Rationale/Consequences scaffold
# — the V2 prompt is intentionally minimalist — but with enough
# structure that Obsidian tag/link tooling and the SUPERSEDED-marker
# sweep can still anchor on the file.
#
# Base embeds match the canonical scaffold templates
# (``_bundled/scaffold/_templates/{type}.md``). Per-type ordering
# preserves reading flow: depends-on/based-on/sources first, related
# second.

# Per-type base embed sections, ordered as they appear in the canonical
# scaffold templates. Keep aligned — drift makes V2 records render
# differently from human-authored learn records.
_BASE_EMBEDS_BY_TYPE: dict[str, tuple[str, ...]] = {
    "assumption": ("Depends On This", "Related"),
    "decision": ("Based On", "Related"),
    "constraint": ("Affected Projects", "Related"),
    "contradiction": ("Related",),
    "synthesis": ("Sources", "Related"),
}


def _audit_reason(spec: LearningCandidate) -> str:
    """Build the ``reason`` string for the attribution_audit entry.

    Shape matches the convention from the retrofit task spec:
    ``distiller v2 (type=<type>, sources=<source_links>)``. Source
    links are rendered as a comma-joined list (or ``none`` when empty),
    paralleling the legacy ``_mark_learn_record_inferred`` shape so a
    future reader scanning audit entries can't tell V1 and V2 apart
    structurally — only by the ``v2`` token in the prefix.
    """
    sources = ", ".join(spec.source_links) if spec.source_links else "none"
    return f"distiller v2 (type={spec.type}, sources={sources})"


def _assemble_body(spec: LearningCandidate) -> str:
    """Render a structured body from the validated spec.

    Shape:

      ``# <Title>``

      ``## Claim``
      <claim>

      ``## Evidence Trail``
      <evidence_excerpt>            (omitted when blank)
      ``### Source Records``        (omitted when no source_links)
      - [[source/Link 1]]
      - [[source/Link 2]]

      ``![[<type>.base#<Section1>]]``
      ``![[<type>.base#<Section2>]]``

    Phase 1 of body parity with legacy. Full Context/Options/Decision/
    Rationale/Consequences sections are NOT emitted — V2's prompt is
    intentionally minimalist. This is enough body to:

      - Make the file scan visually as a learn record (not bare YAML).
      - Anchor the SUPERSEDED-marker sweep's BEGIN_INFERRED detection.
      - Surface base-embed views in Obsidian (Depends On This, Related,
        etc.) so the record shows up under the entities it references.
    """
    parts: list[str] = []

    # Title heading mirrors what the canonical templates emit
    # ("# {{title}}"). spec.title is the validated record name and is
    # always present (Pydantic min_length=5).
    parts.append(f"# {spec.title}")
    parts.append("")

    # Claim — always present (Pydantic min_length=20).
    parts.append("## Claim")
    parts.append("")
    parts.append(spec.claim.strip())
    parts.append("")

    # Evidence Trail — emit the section header even when the excerpt is
    # empty, because the source_links list often lives here. If both
    # are empty we still emit the header for shape consistency; a stub
    # learn record with no evidence is rare but legal.
    parts.append("## Evidence Trail")
    parts.append("")
    if spec.evidence_excerpt:
        parts.append(spec.evidence_excerpt.strip())
        parts.append("")
    if spec.source_links:
        parts.append("### Source Records")
        parts.append("")
        for link in spec.source_links:
            # Pydantic doesn't enforce wikilink wrapping; tolerate both
            # ``[[note/X]]`` and bare ``note/X`` shapes.
            stripped = link.strip()
            if stripped.startswith("[[") and stripped.endswith("]]"):
                parts.append(f"- {stripped}")
            else:
                parts.append(f"- [[{stripped}]]")
        parts.append("")

    # Base embeds — per-type, copied from the canonical scaffold
    # templates. Default to the type-only ``Related`` view if the type
    # isn't in the lookup (defensive — Pydantic restricts ``spec.type``
    # to ``LearnTypeLiteral``, so this branch is unreachable today, but
    # cheap insurance against schema additions).
    sections = _BASE_EMBEDS_BY_TYPE.get(spec.type, ("Related",))
    for section in sections:
        parts.append(f"![[{spec.type}.base#{section}]]")

    # Trim trailing blank lines while keeping a single closing newline.
    while parts and parts[-1] == "":
        parts.pop()
    return "\n".join(parts) + "\n"


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

    # Body: caller-supplied draft wins (Week 3 drafter path); otherwise
    # synthesize a structured body from the spec so V2 records carry
    # the same body shape (heading + claim + evidence trail + base
    # embeds) that legacy emits, just tighter.
    body = body_draft.strip() if body_draft else ""
    if not body:
        body = _assemble_body(spec)

    # Always wrap V2 records — the body is 100% agent-inferred (the
    # extractor is the only thing that can author it). Skipping the
    # marker would leave the record incompatible with the SUPERSEDED-
    # marker sweep and the Daily Sync confirm/reject flow.
    wrapped_body, audit_entry = attribution.with_inferred_marker(
        body,
        section_title=spec.title,
        agent="distiller",
        reason=_audit_reason(spec),
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

    # Same body-and-marker contract as shadow mode — see ``_shadow_write``
    # for rationale. We only stay distinct from shadow on the
    # ``vault_create`` integration (template merge, scope gate,
    # mutation log) — frontmatter and body shape must match 1:1 so
    # diffing shadow vs live is meaningful.
    body = body_draft.strip() if body_draft else ""
    if not body:
        body = _assemble_body(spec)

    wrapped_body, audit_entry = attribution.with_inferred_marker(
        body,
        section_title=spec.title,
        agent="distiller",
        reason=_audit_reason(spec),
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
