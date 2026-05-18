"""Phase 3 supersede chain mirror — Hypatia zettelkasten redesign
(2026-05-18).

Per ``project_hypatia_zettelkasten_redesign.md`` auto-maintenance
behavior #8: when an operator creates a new zettel with
``supersedes: [[zettel/Old]]`` in frontmatter, the vault layer
auto-mirrors:

  1. ``superseded_by: [[zettel/New]]`` on the old zettel
  2. ``## Superseded by\\n- [[zettel/New]] (YYYY-MM-DD)`` body bullet

The mirror is idempotent (re-runs don't duplicate) and failure-
isolated (hook failure never breaks ``vault_create``).

Coverage:
  * First-supersede on a clean old zettel (frontmatter + body
    mirrored)
  * Idempotent re-fire (no duplicate bullets, no frontmatter churn)
  * Chain extension (old zettel already has a superseded_by — new
    target wins, old bullet preserved)
  * Missing-target — warn + skip, new zettel still lands on disk
  * Self-supersede — rejected at vault_create
  * Pre-Phase-3 old zettel missing ``## Superseded by`` section —
    section auto-created
  * Body normalize: wikilink form vs bare path supersedes value
  * Direct-helper invocation (mirror_supersedes_chain) returns
    True/False signal as documented
  * Template ships ``# Supersedes`` scaffold section (regression
    pin)
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import frontmatter
import pytest

from alfred._data import get_scaffold_dir
from alfred.vault import ops as vault_ops
from alfred.vault.ops import VaultError, vault_create, vault_read
from alfred.vault.zettel_hooks import (
    _build_superseded_by_rewriter,
    _find_h2_or_h1_section_start,
    _normalize_wikilink_target,
    mirror_supersedes_chain,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_zettel(
    vault: Path,
    name: str,
    *,
    fm: dict | None = None,
    body: str = "",
) -> str:
    """Write a minimal ``zettel/<name>.md`` via raw FS (bypassing
    vault_create + scope). Returns the rel_path."""
    (vault / "zettel").mkdir(exist_ok=True)
    base_fm: dict = {
        "type": "zettel",
        "name": name,
        "created": "2026-05-18",
        "author": "",
        "source": "",
        "mocs": [],
        "supersedes": "",
        "superseded_by": "",
        "tags": [],
        "status": "open",
    }
    if fm:
        base_fm.update(fm)
    rel_path = f"zettel/{name}.md"
    file_path = vault / rel_path
    post = frontmatter.Post(body, **base_fm)
    file_path.write_text(frontmatter.dumps(post) + "\n", encoding="utf-8")
    return rel_path


@pytest.fixture
def hypatia_vault(tmp_path: Path) -> Path:
    """Vault layout for Hypatia-zettel work — includes ``zettel/`` and
    the bundled zettel template wired up so vault_create works."""
    vault = tmp_path / "vault"
    vault.mkdir()
    for sub in ("zettel", "person", "_templates"):
        (vault / sub).mkdir()
    # Copy the bundled zettel template so vault_create's _load_template
    # finds it. Same pattern as other Hypatia integration tests.
    src_template = get_scaffold_dir() / "_templates" / "zettel.md"
    dst_template = vault / "_templates" / "zettel.md"
    dst_template.write_text(src_template.read_text(encoding="utf-8"),
                            encoding="utf-8")
    return vault


# ---------------------------------------------------------------------------
# Template regression pin (Deliverable A scaffold contract)
# ---------------------------------------------------------------------------


def test_zettel_template_has_supersedes_body_section() -> None:
    """The zettel template ships an empty ``# Supersedes`` section
    where the operator writes the WHY narrative when creating a
    superseding zettel. Hypatia does NOT auto-write here — operator-
    only zone."""
    template_path = get_scaffold_dir() / "_templates" / "zettel.md"
    body = template_path.read_text(encoding="utf-8")
    # H1 line-anchored detection — body has top-level # Premise,
    # # Notes, # Supersedes, etc. The section should appear between
    # # Notes and # Follow Up Questions per the locked-plan order.
    assert "\n# Supersedes\n" in body, (
        "zettel template must include empty `# Supersedes` section "
        "for operator-written WHY narrative on superseding zettels."
    )


def test_zettel_template_has_supersedes_frontmatter_field() -> None:
    """Frontmatter contract: ``supersedes`` + ``superseded_by`` are
    schema fields on the zettel template. Operator sets ``supersedes:``
    on creation; hook mirrors ``superseded_by:`` onto the old zettel."""
    template_path = get_scaffold_dir() / "_templates" / "zettel.md"
    post = frontmatter.load(template_path)
    fm = post.metadata
    assert "supersedes" in fm
    assert "superseded_by" in fm
    # Both empty by default — operator fills supersedes when creating
    # a replacement zettel; superseded_by is hook-managed (never set
    # by operator on creation of THIS zettel).
    assert fm["supersedes"] == ""
    assert fm["superseded_by"] == ""


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_normalize_wikilink_target_variants() -> None:
    assert _normalize_wikilink_target("[[zettel/Old]]") == "zettel/Old"
    assert _normalize_wikilink_target("zettel/Old") == "zettel/Old"
    assert _normalize_wikilink_target("[[zettel/Old|Display]]") == "zettel/Old"
    assert _normalize_wikilink_target("[[zettel/Old.md]]") == "zettel/Old"
    assert _normalize_wikilink_target("") == ""
    assert _normalize_wikilink_target(None) == ""
    assert _normalize_wikilink_target("   ") == ""


def test_find_h2_or_h1_section_line_anchored() -> None:
    """Substring-in-h3 must NOT false-match — line-anchored check."""
    body = (
        "# Premise\n\nsome content\n"
        "### Notes about supersession\n"   # H3 with substring overlap
        "should not match\n\n"
        "## Superseded by\n\n- old bullet\n"
    )
    idx = _find_h2_or_h1_section_start(body, "## Superseded by")
    # Should match the literal H2 line, NOT the H3 with "Superseded" in it.
    assert idx >= 0
    # Verify the match is line-anchored
    assert body[idx:idx + len("## Superseded by")] == "## Superseded by"
    assert idx == 0 or body[idx - 1] == "\n"


# ---------------------------------------------------------------------------
# Rewriter direct-test (Deliverable A)
# ---------------------------------------------------------------------------


def test_superseded_by_rewriter_appends_to_existing_section() -> None:
    body = (
        "# Premise\n\n"
        "## Superseded by\n\n"
        "(none)\n\n"
        "# Notes\n\nstuff\n"
    )
    rw = _build_superseded_by_rewriter("[[zettel/New]]", "2026-05-18")
    out = rw(body)
    assert "- [[zettel/New]] (2026-05-18)" in out
    # Original "# Notes" section preserved.
    assert "# Notes\n\nstuff" in out


def test_superseded_by_rewriter_is_idempotent() -> None:
    body = (
        "## Superseded by\n\n"
        "- [[zettel/New]] (2026-05-18)\n"
    )
    rw = _build_superseded_by_rewriter("[[zettel/New]]", "2026-05-18")
    out = rw(body)
    # Bullet count unchanged.
    assert out.count("[[zettel/New]]") == 1


def test_superseded_by_rewriter_idempotent_against_pipe_alias() -> None:
    """Operator hand-edits the audit bullet to add a display name:
    ``- [[zettel/New|the better version]] (2026-05-18)``. The next
    auto-maintenance fire must NOT append a second bullet for the
    same target — the wikilink-target-present check tolerates pipe-
    aliased display forms.

    Regression pin for the recurring pipe-alias idempotency hole."""
    body = (
        "## Superseded by\n\n"
        "- [[zettel/New|the better version]] (2026-05-18)\n"
    )
    rw = _build_superseded_by_rewriter("[[zettel/New]]", "2026-05-19")
    out = rw(body)
    # Target stem appears exactly once (the pipe-aliased existing
    # form). No duplicate bullet appended.
    assert out.count("[[zettel/New") == 1


def test_superseded_by_rewriter_creates_missing_section() -> None:
    """Pre-Phase-3 zettels lacking the section get it appended at
    end of body — auto-maintenance intent is real-on-disk."""
    body = "# Premise\n\nsome content\n"
    rw = _build_superseded_by_rewriter("[[zettel/New]]", "2026-05-18")
    out = rw(body)
    assert "## Superseded by" in out
    assert "- [[zettel/New]] (2026-05-18)" in out


# ---------------------------------------------------------------------------
# mirror_supersedes_chain — end-to-end at the helper level
# ---------------------------------------------------------------------------


def test_mirror_first_supersede(hypatia_vault: Path) -> None:
    """Clean old zettel → frontmatter + body mirrored on first run."""
    _seed_zettel(hypatia_vault, "Old", body="# Premise\n\nold content\n")
    new_rel = _seed_zettel(
        hypatia_vault, "New",
        fm={"supersedes": "[[zettel/Old]]"},
        body="# Premise\n\n# Supersedes\n\nreason\n",
    )

    result = mirror_supersedes_chain(
        hypatia_vault, new_rel, "[[zettel/Old]]",
        scope="hypatia", today_iso="2026-05-18",
    )
    assert result is True

    old = vault_read(hypatia_vault, "zettel/Old.md")
    assert old["frontmatter"]["superseded_by"] == "[[zettel/New]]"
    assert "## Superseded by" in old["body"]
    assert "- [[zettel/New]] (2026-05-18)" in old["body"]


def test_mirror_is_idempotent(hypatia_vault: Path) -> None:
    """Re-firing the mirror doesn't duplicate bullets or churn fields."""
    _seed_zettel(hypatia_vault, "Old", body="# Premise\n\nold\n")
    new_rel = _seed_zettel(
        hypatia_vault, "New",
        fm={"supersedes": "[[zettel/Old]]"},
    )

    mirror_supersedes_chain(
        hypatia_vault, new_rel, "[[zettel/Old]]",
        scope="hypatia", today_iso="2026-05-18",
    )
    mirror_supersedes_chain(
        hypatia_vault, new_rel, "[[zettel/Old]]",
        scope="hypatia", today_iso="2026-05-18",
    )

    old = vault_read(hypatia_vault, "zettel/Old.md")
    body = old["body"]
    assert body.count("[[zettel/New]]") == 1


def test_mirror_chain_extension(hypatia_vault: Path) -> None:
    """Old zettel already superseded by V2; now V3 supersedes the
    same old. Direct-parent rule: V3 overrides superseded_by on Old
    (most-recent wins), but V2's bullet stays in the body for audit."""
    _seed_zettel(
        hypatia_vault, "Old",
        fm={"superseded_by": "[[zettel/V2]]"},
        body=(
            "# Premise\n\n"
            "## Superseded by\n\n"
            "- [[zettel/V2]] (2026-05-10)\n"
        ),
    )
    v3_rel = _seed_zettel(
        hypatia_vault, "V3",
        fm={"supersedes": "[[zettel/Old]]"},
    )

    mirror_supersedes_chain(
        hypatia_vault, v3_rel, "[[zettel/Old]]",
        scope="hypatia", today_iso="2026-05-18",
    )

    old = vault_read(hypatia_vault, "zettel/Old.md")
    # Frontmatter: most-recent wins.
    assert old["frontmatter"]["superseded_by"] == "[[zettel/V3]]"
    # Body: both bullets preserved (V2 audit + V3 audit).
    assert "[[zettel/V2]]" in old["body"]
    assert "[[zettel/V3]]" in old["body"]


def test_mirror_missing_target_warns_and_skips(
    hypatia_vault: Path,
) -> None:
    """Pointing supersedes at a non-existent zettel logs + returns False;
    no crash, no on-disk effect."""
    new_rel = _seed_zettel(
        hypatia_vault, "New",
        fm={"supersedes": "[[zettel/DoesNotExist]]"},
    )
    result = mirror_supersedes_chain(
        hypatia_vault, new_rel, "[[zettel/DoesNotExist]]",
        scope="hypatia",
    )
    assert result is False
    # New zettel survives untouched.
    assert (hypatia_vault / new_rel).exists()


def test_mirror_self_supersede_at_helper_layer_rejected(
    hypatia_vault: Path,
) -> None:
    """Defense-in-depth: helper returns False if a zettel's
    supersedes points at itself (the upstream vault_create gate
    raises VaultError, but the helper still self-guards)."""
    new_rel = _seed_zettel(
        hypatia_vault, "New",
        fm={"supersedes": "[[zettel/New]]"},
    )
    result = mirror_supersedes_chain(
        hypatia_vault, new_rel, "[[zettel/New]]",
        scope="hypatia",
    )
    assert result is False


def test_mirror_normalizes_bare_path_value(
    hypatia_vault: Path,
) -> None:
    """Operator may type ``supersedes: zettel/Old`` (bare) instead of
    wikilink — same outcome."""
    _seed_zettel(hypatia_vault, "Old", body="# Premise\n")
    new_rel = _seed_zettel(
        hypatia_vault, "New",
        fm={"supersedes": "zettel/Old"},
    )
    result = mirror_supersedes_chain(
        hypatia_vault, new_rel, "zettel/Old",
        scope="hypatia",
    )
    assert result is True

    old = vault_read(hypatia_vault, "zettel/Old.md")
    # Mirrored wikilink is always full bracket form (canonical).
    assert old["frontmatter"]["superseded_by"] == "[[zettel/New]]"


def test_mirror_empty_supersedes_is_noop(
    hypatia_vault: Path,
) -> None:
    """Empty / missing supersedes value → no-op no-crash."""
    new_rel = _seed_zettel(hypatia_vault, "New")
    assert mirror_supersedes_chain(
        hypatia_vault, new_rel, "", scope="hypatia",
    ) is False
    assert mirror_supersedes_chain(
        hypatia_vault, new_rel, None, scope="hypatia",
    ) is False


# ---------------------------------------------------------------------------
# vault_create integration — self-supersede gate
# ---------------------------------------------------------------------------


def test_vault_create_rejects_self_supersede(hypatia_vault: Path) -> None:
    """vault_create raises VaultError when the new zettel's
    ``supersedes:`` points at itself. Fails fast — no file lands on
    disk."""
    with pytest.raises(VaultError) as exc_info:
        vault_create(
            hypatia_vault,
            "zettel",
            "Self-Referential",
            set_fields={"supersedes": "[[zettel/Self-Referential]]"},
            scope="hypatia",
        )
    assert "supersede itself" in str(exc_info.value).lower()
    # No on-disk effect.
    assert not (hypatia_vault / "zettel" / "Self-Referential.md").exists()


def test_vault_create_self_supersede_via_bare_path(
    hypatia_vault: Path,
) -> None:
    """Self-supersede works against bare-path form too (operator may
    omit brackets)."""
    with pytest.raises(VaultError):
        vault_create(
            hypatia_vault,
            "zettel",
            "BarePath",
            set_fields={"supersedes": "zettel/BarePath"},
            scope="hypatia",
        )


def test_vault_create_supersede_fires_mirror_hook(
    hypatia_vault: Path,
) -> None:
    """End-to-end: vault_create on a zettel with supersedes set
    triggers the hook which mirrors the old zettel."""
    # Seed the old zettel first (so the mirror target exists).
    _seed_zettel(hypatia_vault, "OldZ", body="# Premise\n\nold\n")

    vault_create(
        hypatia_vault,
        "zettel",
        "NewZ",
        set_fields={"supersedes": "[[zettel/OldZ]]"},
        scope="hypatia",
    )

    # Verify the new zettel landed on disk with the supersedes field
    # AND the old zettel was mirrored.
    new = vault_read(hypatia_vault, "zettel/NewZ.md")
    assert new["frontmatter"]["supersedes"] == "[[zettel/OldZ]]"

    old = vault_read(hypatia_vault, "zettel/OldZ.md")
    assert old["frontmatter"]["superseded_by"] == "[[zettel/NewZ]]"
    assert "## Superseded by" in old["body"]
    assert "[[zettel/NewZ]]" in old["body"]


def test_vault_create_supersede_missing_target_still_creates_new(
    hypatia_vault: Path,
) -> None:
    """When the supersede target doesn't exist, vault_create still
    succeeds (hook is failure-isolated)."""
    result = vault_create(
        hypatia_vault,
        "zettel",
        "NewZ",
        set_fields={"supersedes": "[[zettel/Phantom]]"},
        scope="hypatia",
    )
    assert result["path"] == "zettel/NewZ.md"
    # NewZ landed.
    new = vault_read(hypatia_vault, "zettel/NewZ.md")
    assert new["frontmatter"]["supersedes"] == "[[zettel/Phantom]]"
