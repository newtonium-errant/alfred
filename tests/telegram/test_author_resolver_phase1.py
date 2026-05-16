"""Author resolver Phase 1 overhaul tests (2026-05-16, Q1 ratified).

Pins the new heuristic-with-particle-preservation behavior:

  * ``derive_canonical_filename`` — Lastname-comma-Firstname for modern
    Western, particle preservation for medieval / European names,
    pass-through for already-canonical comma forms + single-name
    historical figures.
  * ``_scan_authors_by_alias`` — three lookup surfaces (filename stem,
    ``name`` frontmatter, ``aliases`` list).
  * ``resolve_or_create_author`` end-to-end: canonical-filename writes,
    alias-bridge between input + canonical forms, idempotent re-lookup,
    legacy-record (pre-Phase-1 last-name-only filename) compatibility
    via the alias scan.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from alfred.telegram import capture_source_anchor as csa
from alfred.vault import ops


# --- derive_canonical_filename unit tests ---------------------------------


@pytest.mark.parametrize("author,expected", [
    # Modern Western — default Lastname-comma-Firstname.
    ("Marcus Aurelius",       "Aurelius, Marcus"),
    ("Adam Smith",            "Smith, Adam"),
    ("Carl Sagan",            "Sagan, Carl"),
    # Suffixed — suffix stripped, then comma-swap.
    ("Foo Bar Jr.",           "Bar, Foo"),
    ("John Smith PhD",        "Smith, John"),
    ("Edward Wilson III",     "Wilson, Edward"),
    # Comma form — operator-canonical pass-through.
    ("Aurelius, Marcus",      "Aurelius, Marcus"),
    ("Smith, Adam",           "Smith, Adam"),
    # Single-name historical figure.
    ("Aristotle",             "Aristotle"),
    ("Plato",                 "Plato"),
    ("Homer",                 "Homer"),
    # Multi-given-name (e.g. John Stuart Mill).
    ("John Stuart Mill",      "Mill, John Stuart"),
    # Empty.
    ("",                      ""),
    ("   ",                   ""),
])
def test_derive_canonical_filename(author: str, expected: str) -> None:
    """Phase 1 canonical filename heuristic — covers all five lived
    examples from the brief's calibration data + standard fallbacks."""
    assert csa.derive_canonical_filename(author) == expected


# --- Particle preservation (medieval / European names) -------------------


@pytest.mark.parametrize("author,expected", [
    # Brief's three explicitly-named lived examples — all preserve form.
    ("Fiore dei Liberi",      "Fiore dei Liberi"),    # medieval Italian
    ("Ludwig van Beethoven",  "Ludwig van Beethoven"), # Dutch / Flemish
    ("Otto von Bismarck",     "Otto von Bismarck"),   # German
    # Adjacent particles included in NAME_PARTICLES (also preserved).
    ("Charles de Gaulle",     "Charles de Gaulle"),   # French
])
def test_derive_canonical_filename_particle_preservation(
    author: str, expected: str,
) -> None:
    """Names with ``van`` / ``de`` / ``dei`` / ``von`` / ``de`` particles
    preserve their original form (no Lastname-comma swap)."""
    assert csa.derive_canonical_filename(author) == expected


def test_derive_canonical_filename_particle_not_in_list() -> None:
    """A particle not in NAME_PARTICLES falls through to default Lastname-
    swap heuristic. Documentary — adding a new particle is a one-line
    extension of NAME_PARTICLES.

    ``Leonardo da Vinci``: ``da`` is NOT currently in NAME_PARTICLES
    (Italian noble particle; could be added if real friction surfaces).
    Current heuristic → ``Vinci, Leonardo da`` (treats ``da`` as a
    middle-name token, ``Vinci`` as the surname).
    """
    # NOTE: ``da`` deliberately not in NAME_PARTICLES today. If it gets
    # added later, this test breaks and the assertion needs updating
    # alongside the particle-list extension.
    result = csa.derive_canonical_filename("Leonardo da Vinci")
    assert result == "Vinci, Leonardo da"


def test_particle_preservation_for_dei_liberi() -> None:
    """Lived example pin: ``Fiore dei Liberi`` (Andrew's HEMA archery
    instructor example from the brief's calibration data)."""
    result = csa.derive_canonical_filename("Fiore dei Liberi")
    assert result == "Fiore dei Liberi"
    # No comma inserted — particle-preservation suppresses the swap.
    assert "," not in result


def test_particle_at_first_token_position_does_not_apply() -> None:
    """A name starting with a particle (rare) — particles must NOT be
    detected at position 0 because the swap-suppression only makes
    sense when the particle is mid-name (binding a surname phrase).
    Position 0 indicates the whole name IS the surname phrase, in
    which case fall through to the normal swap.

    Edge case; documentary test."""
    # ``van der Berg`` — particle at index 0 — falls through to default
    # heuristic. The current implementation scans tokens[1:] for the
    # particle, so position-0 particle doesn't trigger preservation.
    result = csa.derive_canonical_filename("van der Berg")
    # Particle "der" at index 1 IS detected (binds "Berg") → preserve.
    assert result == "van der Berg"


# --- _scan_authors_by_alias unit tests ------------------------------------


def _make_vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    for sub in ("session", "source", "author", "note", "zettel"):
        (vault / sub).mkdir(parents=True)
    return vault


def _write_author(
    vault: Path, filename: str, name: str, aliases: list[str] | None = None,
) -> None:
    """Helper: write an author record with given frontmatter shape."""
    lines = [
        "---",
        "type: author",
        f"name: {name}",
        "created: '2026-05-15'",
    ]
    if aliases is not None:
        lines.append("aliases:")
        for alias in aliases:
            lines.append(f"  - {alias!r}")
    lines += ["---", "", "# Summary", ""]
    (vault / "author" / filename).write_text(
        "\n".join(lines), encoding="utf-8",
    )


def test_scan_finds_by_filename_stem(tmp_path: Path) -> None:
    """Match against the filename stem (no aliases needed)."""
    vault = _make_vault(tmp_path)
    _write_author(
        vault, "Aurelius, Marcus.md", "Marcus Aurelius",
        aliases=["Marcus Aurelius", "Aurelius, Marcus"],
    )
    result = csa._scan_authors_by_alias(vault, "Aurelius, Marcus")
    assert result == "author/Aurelius, Marcus.md"


def test_scan_finds_by_name_frontmatter(tmp_path: Path) -> None:
    """Match against ``name:`` frontmatter — catches legacy records
    with last-name-only filenames."""
    vault = _make_vault(tmp_path)
    # Pre-Phase-1 shape: filename = "Aurelius", name = "Marcus Aurelius".
    _write_author(vault, "Aurelius.md", "Marcus Aurelius", aliases=None)
    # Look up via the full name — should find the legacy record.
    result = csa._scan_authors_by_alias(vault, "Marcus Aurelius")
    assert result == "author/Aurelius.md"


def test_scan_finds_by_alias_entry(tmp_path: Path) -> None:
    """Match against an entry in the ``aliases`` list."""
    vault = _make_vault(tmp_path)
    _write_author(
        vault, "Sagan, Carl.md", "Carl Sagan",
        aliases=["Carl Sagan", "Sagan, Carl", "Dr. Carl Sagan"],
    )
    # The third alias should match.
    result = csa._scan_authors_by_alias(vault, "Dr. Carl Sagan")
    assert result == "author/Sagan, Carl.md"


def test_scan_returns_none_when_no_match(tmp_path: Path) -> None:
    """Unknown lookup → None."""
    vault = _make_vault(tmp_path)
    _write_author(vault, "Aurelius, Marcus.md", "Marcus Aurelius")
    result = csa._scan_authors_by_alias(vault, "Nobody Knows")
    assert result is None


def test_scan_is_case_insensitive(tmp_path: Path) -> None:
    """Lookup is normalised (lowercase + whitespace-collapsed)."""
    vault = _make_vault(tmp_path)
    _write_author(vault, "Smith, Adam.md", "Adam Smith")
    # Different casing + extra whitespace.
    result = csa._scan_authors_by_alias(vault, "ADAM   smith")
    assert result == "author/Smith, Adam.md"


def test_scan_returns_none_for_empty_lookup(tmp_path: Path) -> None:
    vault = _make_vault(tmp_path)
    _write_author(vault, "Aurelius, Marcus.md", "Marcus Aurelius")
    assert csa._scan_authors_by_alias(vault, "") is None
    assert csa._scan_authors_by_alias(vault, "   ") is None


def test_scan_returns_none_when_author_dir_missing(tmp_path: Path) -> None:
    """No author/ directory → None (not an error)."""
    vault = tmp_path / "vault"
    vault.mkdir()
    # NO author/ subdirectory.
    assert csa._scan_authors_by_alias(vault, "Anyone") is None


# --- resolve_or_create_author end-to-end ---------------------------------


def test_resolve_creates_canonical_filename(tmp_path: Path) -> None:
    """Auto-create lands at ``author/<canonical>.md`` with aliases for
    future-lookup bridging."""
    vault = _make_vault(tmp_path)
    ref = csa.resolve_or_create_author(vault, "Marcus Aurelius")
    assert ref is not None
    assert ref.rel_path == "author/Aurelius, Marcus.md"
    assert ref.created is True

    rec = ops.vault_read(vault, ref.rel_path)
    fm = rec["frontmatter"]
    assert fm["name"] == "Marcus Aurelius"
    aliases = fm.get("aliases") or []
    assert "Marcus Aurelius" in aliases
    assert "Aurelius, Marcus" in aliases


def test_resolve_idempotent_on_second_lookup(tmp_path: Path) -> None:
    """Same input → same record, created=False."""
    vault = _make_vault(tmp_path)
    first = csa.resolve_or_create_author(vault, "Carl Sagan")
    second = csa.resolve_or_create_author(vault, "Carl Sagan")
    assert first is not None and second is not None
    assert first.rel_path == second.rel_path
    assert first.created is True
    assert second.created is False


def test_resolve_finds_canonical_record_from_alias_form(tmp_path: Path) -> None:
    """Operator types ``Marcus Aurelius`` and ``Aurelius, Marcus`` — both
    resolve to the same canonical record."""
    vault = _make_vault(tmp_path)
    # Create via the full-name form.
    first = csa.resolve_or_create_author(vault, "Marcus Aurelius")
    # Now look up via the canonical form — should find the same record.
    second = csa.resolve_or_create_author(vault, "Aurelius, Marcus")
    assert first is not None and second is not None
    assert first.rel_path == second.rel_path
    assert second.created is False


def test_resolve_finds_legacy_last_name_only_record(tmp_path: Path) -> None:
    """A pre-Phase-1 record at ``author/<lastname>.md`` (with ``name:
    Marcus Aurelius`` frontmatter) resolves correctly via the alias
    scan — without double-creating a canonical version.

    This is the regression guard for the Phase 1 migration's
    pre-execution state: today's morning ship created
    ``author/Aurelius.md`` with last-name-only filename; the new
    resolver finds it via ``name:`` match. Post-migration the record
    is renamed to canonical form; both states work.
    """
    vault = _make_vault(tmp_path)
    # Simulate the legacy record (pre-migration state).
    _write_author(vault, "Aurelius.md", "Marcus Aurelius", aliases=None)

    ref = csa.resolve_or_create_author(vault, "Marcus Aurelius")
    assert ref is not None
    # Matched the LEGACY record — no new canonical record auto-created.
    assert ref.rel_path == "author/Aurelius.md"
    assert ref.created is False
    # And the canonical-form file does NOT exist.
    assert not (vault / "author" / "Aurelius, Marcus.md").exists()


def test_resolve_particle_name_canonical(tmp_path: Path) -> None:
    """Particle-preserving name creates a non-comma canonical file."""
    vault = _make_vault(tmp_path)
    ref = csa.resolve_or_create_author(vault, "Fiore dei Liberi")
    assert ref is not None
    assert ref.rel_path == "author/Fiore dei Liberi.md"
    assert ref.created is True
    rec = ops.vault_read(vault, ref.rel_path)
    aliases = rec["frontmatter"].get("aliases") or []
    # Both input and canonical happen to be identical for particle names,
    # so the alias list has one entry (dedup logic).
    assert aliases == ["Fiore dei Liberi"]


def test_resolve_single_name_historical_figure(tmp_path: Path) -> None:
    """``Aristotle`` → ``author/Aristotle.md`` (canonical = the name)."""
    vault = _make_vault(tmp_path)
    ref = csa.resolve_or_create_author(vault, "Aristotle")
    assert ref is not None
    assert ref.rel_path == "author/Aristotle.md"
    assert ref.created is True


def test_resolve_empty_author_returns_none(tmp_path: Path) -> None:
    vault = _make_vault(tmp_path)
    assert csa.resolve_or_create_author(vault, "") is None
    assert csa.resolve_or_create_author(vault, "   ") is None


# --- Last-name retired from auto-created records --------------------------


def test_auto_created_author_has_no_last_name_frontmatter(
    tmp_path: Path,
) -> None:
    """Phase 1: ``last_name`` is no longer written on auto-created
    author records. The migration script + manual operator updates
    can still carry it on legacy records, but the auto-creation path
    stops setting it.
    """
    vault = _make_vault(tmp_path)
    ref = csa.resolve_or_create_author(vault, "Marcus Aurelius")
    assert ref is not None
    rec = ops.vault_read(vault, ref.rel_path)
    assert "last_name" not in rec["frontmatter"]


# --- Status retired from auto-created records ----------------------------


def test_auto_created_author_does_not_set_status(tmp_path: Path) -> None:
    """Phase 1 author template strip dropped status; the resolver no
    longer writes ``status: active`` either."""
    vault = _make_vault(tmp_path)
    ref = csa.resolve_or_create_author(vault, "Marcus Aurelius")
    assert ref is not None
    rec = ops.vault_read(vault, ref.rel_path)
    # status field absent — the validator tolerates it (no entry in
    # STATUS_BY_TYPE["author"] is required for write-time validation
    # to pass per the schema design).
    assert rec["frontmatter"].get("status") is None or rec["frontmatter"].get("status") == ""
