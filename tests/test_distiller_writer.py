"""V2 distiller writer — attribution-audit retrofit (2026-04-28).

Tests the deterministic writer (``alfred.distiller.writer``) that
materialises a validated ``LearningCandidate`` into a shadow- or live-
mode record file. The retrofit added:

  - ``attribution_audit`` frontmatter list with at least one entry
  - ``BEGIN_INFERRED`` / ``END_INFERRED`` HTML comment wrappers around
    the body
  - Per-type base embeds matching ``_bundled/scaffold/_templates``
  - A synthesized structured body (H1 + Claim + Evidence Trail + Source
    Records) when no caller-supplied draft is provided

Round-trip lock: produced ``marker_id`` values MUST match the canonical
``_BEGIN_RE`` regex from ``alfred.vault.attribution`` (per
``feedback_marker_id_canonical_regex.md`` — never re-derive). Same shape
as the regression test added in commit ``87de95b`` for the SUPERSEDED-
marker sweep.
"""

from __future__ import annotations

from pathlib import Path

import frontmatter

from alfred.distiller.contracts import LearningCandidate
from alfred.distiller.writer import (
    _BASE_EMBEDS_BY_TYPE,
    _assemble_body,
    _audit_reason,
    write_learn_record,
)
from alfred.vault.attribution import _BEGIN_RE, make_marker_id


# --- Helpers ----------------------------------------------------------------


def _spec(
    *,
    type_: str = "assumption",
    title: str = "Long-Term Lease Justifies Landlord-Funded Plumbing Buildout",
    claim: str = (
        "A 5-10 year lease commitment from Jamie and Marley would give "
        "Wayne Fowler the financial basis to fund a plumbing buildout to "
        "spec, with rent starting at occupancy."
    ),
    evidence: str = (
        "Long-term lease (5-10 yr) gives Wayne the signed commitment he "
        "needs to justify construction; deal structure = Wayne builds to "
        "spec, rent starts at occupancy."
    ),
    sources: list[str] | None = None,
    entities: list[str] | None = None,
    status: str = "active",
    confidence: str = "medium",
) -> LearningCandidate:
    """Build a LearningCandidate that mirrors the worked-example record
    in the retrofit task spec. Defaults are tuned to be a fully-formed
    valid candidate; tests override only the field they're exercising."""
    # Distinguish "caller didn't override" (None → use default) from
    # "caller passed an empty list" (→ honour it). The reason builder
    # treats ``[]`` as ``sources=none``, distinct from a populated list.
    src = (
        ["[[note/Jamie Short-Term Medical Office Rental Options]]"]
        if sources is None
        else list(sources)
    )
    ents = (
        ["[[person/Wayne Fowler]]", "[[person/Jamie Sweetland]]"]
        if entities is None
        else list(entities)
    )
    return LearningCandidate(
        type=type_,  # type: ignore[arg-type]
        title=title,
        confidence=confidence,  # type: ignore[arg-type]
        status=status,
        claim=claim,
        evidence_excerpt=evidence,
        source_links=src,
        entity_links=ents,
    )


# --- Body assembly ----------------------------------------------------------


def test_assemble_body_emits_h1_claim_evidence_sections():
    body = _assemble_body(_spec())

    assert body.startswith(
        "# Long-Term Lease Justifies Landlord-Funded Plumbing Buildout\n"
    )
    assert "## Claim" in body
    assert "## Evidence Trail" in body
    # Evidence excerpt landed under Evidence Trail.
    assert "Long-term lease (5-10 yr)" in body
    # Source links rendered as a sub-bulleted list.
    assert "### Source Records" in body
    assert "- [[note/Jamie Short-Term Medical Office Rental Options]]" in body


def test_assemble_body_includes_per_type_base_embeds():
    """Each learn type emits the canonical scaffold-template embed
    sections in the same order. Drift = V2 records render differently
    from human-authored learn records in Obsidian."""
    for learn_type, expected_sections in _BASE_EMBEDS_BY_TYPE.items():
        spec = _spec(
            type_=learn_type,
            title=f"Test {learn_type.capitalize()} Record Title",
            claim="A claim that satisfies the 20-character minimum length.",
            status={
                "assumption": "active",
                "decision": "draft",
                "constraint": "active",
                "contradiction": "unresolved",
                "synthesis": "draft",
            }[learn_type],
        )
        body = _assemble_body(spec)
        for section in expected_sections:
            assert f"![[{learn_type}.base#{section}]]" in body, (
                f"missing base embed for {learn_type}#{section}"
            )


def test_assemble_body_handles_empty_evidence_and_sources():
    """Pydantic permits empty evidence_excerpt + source_links. Body still
    emits Evidence Trail header (placeholder, gets filled in later) plus
    base embeds — never crashes, always renders the structural shape."""
    spec = LearningCandidate(
        type="constraint",
        title="Sample Constraint With Long Enough Title",
        confidence="low",
        status="active",
        claim="A claim that meets the 20-character minimum requirement.",
        evidence_excerpt="",
        source_links=[],
        entity_links=[],
    )
    body = _assemble_body(spec)

    assert "## Claim" in body
    assert "## Evidence Trail" in body
    # No Source Records sub-section when source_links is empty.
    assert "### Source Records" not in body
    # Base embeds still emitted.
    assert "![[constraint.base#Affected Projects]]" in body
    assert "![[constraint.base#Related]]" in body


def test_assemble_body_tolerates_unwrapped_source_links():
    """Pydantic doesn't enforce wikilink wrapping on source_links —
    a bare ``note/X`` should still render correctly under Source Records."""
    spec = _spec(sources=["note/Bare Link", "[[note/Wrapped Link]]"])
    body = _assemble_body(spec)

    assert "- [[note/Bare Link]]" in body
    assert "- [[note/Wrapped Link]]" in body


# --- Audit reason -----------------------------------------------------------


def test_audit_reason_matches_retrofit_spec_format():
    """The retrofit spec calls for
    ``distiller v2 (type=<type>, sources=<source_links>)``."""
    spec = _spec()
    reason = _audit_reason(spec)

    assert reason.startswith("distiller v2 (")
    assert "type=assumption" in reason
    assert "sources=[[note/Jamie Short-Term Medical Office Rental Options]]" in reason


def test_audit_reason_emits_none_when_no_sources():
    spec = _spec(sources=[])
    assert "sources=none" in _audit_reason(spec)


# --- Shadow write — full retrofit integration -------------------------------


def test_shadow_write_emits_attribution_audit_and_markers(tmp_path: Path):
    shadow_root = tmp_path / "shadow"
    spec = _spec()

    out_path = write_learn_record(spec, shadow_root=shadow_root)
    assert out_path.exists()

    post = frontmatter.load(str(out_path))
    fm = dict(post.metadata)
    body = post.content

    # 1. attribution_audit list present + correctly shaped.
    audit = fm.get("attribution_audit")
    assert isinstance(audit, list) and len(audit) == 1
    entry = audit[0]
    assert entry["agent"] == "distiller"
    assert entry["confirmed_by_andrew"] is False
    assert entry["confirmed_at"] is None
    assert entry["section_title"] == spec.title
    assert "distiller v2" in entry["reason"]
    assert "type=assumption" in entry["reason"]
    assert isinstance(entry["date"], str) and entry["date"]
    # Marker ID shape: inf-YYYYMMDD-distiller-XXXXXX
    marker_id = entry["marker_id"]
    assert marker_id.startswith("inf-")
    assert "-distiller-" in marker_id

    # 2. BEGIN_INFERRED / END_INFERRED markers in body, paired by ID.
    begin_token = f'<!-- BEGIN_INFERRED marker_id="{marker_id}" -->'
    end_token = f'<!-- END_INFERRED marker_id="{marker_id}" -->'
    assert begin_token in body
    assert end_token in body
    # Begin precedes end and there's exactly one pair.
    assert body.count("BEGIN_INFERRED") == 1
    assert body.count("END_INFERRED") == 1
    assert body.index(begin_token) < body.index(end_token)

    # 3. Body content sections present (H1 + Claim + Evidence Trail).
    assert f"# {spec.title}" in body
    assert "## Claim" in body
    assert "## Evidence Trail" in body
    assert spec.claim in body

    # 4. Base embeds present and INSIDE the marker pair (legacy parity).
    for section in ("Depends On This", "Related"):
        embed = f"![[assumption.base#{section}]]"
        assert embed in body
        # Embed lives between the BEGIN and END markers, not after them.
        assert body.index(begin_token) < body.index(embed) < body.index(end_token)


def test_shadow_write_marker_id_round_trips_through_canonical_regex(
    tmp_path: Path,
):
    """Round-trip lock: the produced marker_id must match the canonical
    ``_BEGIN_RE`` regex from ``alfred.vault.attribution``. This is the
    contract that ties V2 records to the SUPERSEDED-marker sweep — same
    regression test shape as commit 87de95b for janitor/superseded_marker.
    """
    shadow_root = tmp_path / "shadow"
    out_path = write_learn_record(_spec(), shadow_root=shadow_root)
    body = frontmatter.load(str(out_path)).content

    # Find the BEGIN_INFERRED line and run the canonical regex on it.
    begin_lines = [
        line for line in body.splitlines() if "BEGIN_INFERRED" in line
    ]
    assert len(begin_lines) == 1, "expected exactly one BEGIN_INFERRED line"
    match = _BEGIN_RE.search(begin_lines[0])
    assert match is not None, (
        f"canonical _BEGIN_RE failed to match: {begin_lines[0]!r}"
    )
    marker_id = match.group(1)

    # Round-trip: rebuilding the marker_id from the same agent + body +
    # date should produce a value that still matches the regex (sanity
    # on the helper's output domain).
    assert marker_id.startswith("inf-")
    assert "-distiller-" in marker_id


def test_shadow_write_synthesises_body_when_draft_empty(tmp_path: Path):
    """Daemon currently calls ``write_learn_record(body_draft="")`` —
    pre-retrofit this produced a bare-frontmatter file with no body,
    no markers, no audit. Verify the writer now synthesises a body."""
    shadow_root = tmp_path / "shadow"
    out_path = write_learn_record(
        _spec(), body_draft="", shadow_root=shadow_root,
    )
    body = frontmatter.load(str(out_path)).content

    assert body.strip(), "body must be non-empty after retrofit"
    assert "BEGIN_INFERRED" in body
    assert "## Claim" in body
    assert "## Evidence Trail" in body


def test_shadow_write_uses_caller_draft_when_provided(tmp_path: Path):
    """A future Week-3 drafter will pass real prose via ``body_draft``.
    The writer must respect that draft (and still wrap it in markers /
    stamp the audit entry) rather than overwriting with the synthesised
    body."""
    shadow_root = tmp_path / "shadow"
    draft = "## Custom Draft\n\nReal prose from Week 3 drafter.\n"

    out_path = write_learn_record(
        _spec(), body_draft=draft, shadow_root=shadow_root,
    )
    body = frontmatter.load(str(out_path)).content

    assert "Real prose from Week 3 drafter" in body
    # Synthesised body is NOT used when a draft is provided — the
    # default H1 should not appear.
    assert "# Long-Term Lease Justifies Landlord-Funded Plumbing Buildout" not in body
    # Markers still applied to the caller-supplied draft.
    assert "BEGIN_INFERRED" in body
    assert "END_INFERRED" in body


def test_shadow_write_idempotent_on_repeat(tmp_path: Path):
    """Re-running the writer with the same spec on an existing shadow
    record short-circuits at the existence check (logged as
    ``writer.shadow.skip_existing``). Behaviour unchanged by the
    retrofit — verify it still holds."""
    shadow_root = tmp_path / "shadow"
    out_path_a = write_learn_record(_spec(), shadow_root=shadow_root)
    body_a = out_path_a.read_text(encoding="utf-8")

    out_path_b = write_learn_record(_spec(), shadow_root=shadow_root)
    body_b = out_path_b.read_text(encoding="utf-8")

    assert out_path_a == out_path_b
    # Skipped → file untouched, byte-for-byte.
    assert body_a == body_b


# --- SUPERSEDED-marker sweep compatibility ---------------------------------


def test_v2_record_body_matches_superseded_marker_scanner(tmp_path: Path):
    """End-to-end: a V2-produced record's BEGIN_INFERRED line must be
    detectable by the same ``_SCAN_INF_RE`` (= canonical ``_BEGIN_RE``)
    that ``janitor/superseded_marker.py`` uses to pair correction notes
    back to inferred blocks. If this fails, V2 records are invisible to
    the SUPERSEDED-marker sweep and Phase 2 confirm/reject — exactly the
    integration gap this retrofit closes."""
    shadow_root = tmp_path / "shadow"
    out_path = write_learn_record(_spec(), shadow_root=shadow_root)
    body = frontmatter.load(str(out_path)).content

    # _BEGIN_RE.search across the body should find the marker_id and
    # match the canonical inf-YYYYMMDD-agent-hash shape.
    match = _BEGIN_RE.search(body)
    assert match is not None
    inf_id = match.group(1)
    assert inf_id.startswith("inf-")
    # Verify the make_marker_id helper is the source of truth (we don't
    # re-derive the format anywhere — the regex roundtrip is the lock).
    assert make_marker_id("distiller", "anything").startswith("inf-")
