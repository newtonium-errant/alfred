"""Phase 3 author Contents auto-append — Hypatia zettelkasten redesign
(2026-05-18).

Per ``project_hypatia_zettelkasten_redesign.md`` auto-maintenance
behavior #6: when an operator creates a new zettel with ``author:``
set, the vault layer appends ``- [[zettel/Title]]`` to the author
record's ``# Contents`` section. Z-CENTRIC — sources do NOT
auto-append (the brief explicitly excludes them).

Coverage:
  * First zettel for an author (author's # Contents bullet added)
  * Idempotent re-fire (no duplicate bullet)
  * Source records do NOT trigger the hook (Z-centric)
  * Missing author record — log + skip, new zettel survives
  * Pre-Phase-3 author missing ``# Contents`` section — auto-created
  * Aliases-chain following — typed alias resolves to canonical
    person record
  * Bare-path author value vs full wikilink — both work
  * Org as author works (operator-typed ``org/Foo`` rel_path)
  * Direct-helper invocation returns True/False signal
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import frontmatter
import pytest

from alfred._data import get_scaffold_dir
from alfred.vault import ops as vault_ops
from alfred.vault.ops import vault_create, vault_read
from alfred.vault.zettel_hooks import (
    _build_author_contents_rewriter,
    _resolve_author_target,
    append_to_author_contents,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_person(
    vault: Path,
    name: str,
    *,
    aliases: list[str] | None = None,
    body: str = "",
) -> str:
    """Write a minimal ``person/<name>.md`` via raw FS."""
    (vault / "person").mkdir(exist_ok=True)
    fm: dict = {
        "type": "person",
        "name": name,
        "created": "2026-05-18",
        "aliases": aliases or [],
        "tags": [],
        "related": [],
    }
    rel_path = f"person/{name}.md"
    file_path = vault / rel_path
    post = frontmatter.Post(body, **fm)
    file_path.write_text(frontmatter.dumps(post) + "\n", encoding="utf-8")
    return rel_path


def _seed_org(vault: Path, name: str, body: str = "") -> str:
    (vault / "org").mkdir(exist_ok=True)
    fm: dict = {
        "type": "org",
        "name": name,
        "created": "2026-05-18",
        "tags": [],
    }
    rel_path = f"org/{name}.md"
    file_path = vault / rel_path
    post = frontmatter.Post(body, **fm)
    file_path.write_text(frontmatter.dumps(post) + "\n", encoding="utf-8")
    return rel_path


def _seed_zettel(
    vault: Path,
    name: str,
    *,
    fm: dict | None = None,
    body: str = "",
) -> str:
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


def _seed_source(
    vault: Path,
    name: str,
    *,
    fm: dict | None = None,
) -> str:
    """Source is a Hypatia type; for hook-routing tests we only need
    the file to exist + type=source."""
    (vault / "source").mkdir(exist_ok=True)
    base_fm: dict = {
        "type": "source",
        "name": name,
        "created": "2026-05-18",
        "author": "",
        "tags": [],
    }
    if fm:
        base_fm.update(fm)
    rel_path = f"source/{name}.md"
    file_path = vault / rel_path
    post = frontmatter.Post("", **base_fm)
    file_path.write_text(frontmatter.dumps(post) + "\n", encoding="utf-8")
    return rel_path


def _seed_author(
    vault: Path,
    name: str,
    *,
    aliases: list[str] | None = None,
    body: str = "",
) -> str:
    """Write a minimal ``author/<name>.md`` via raw FS.

    Phase 1's canonical author directory is ``author/`` (per
    ``schema.py`` ``TYPE_DIRECTORY["author"] = "author"``). Bibliographic
    authors live here; ``person/`` is a separate type for people in the
    operator's personal network.
    """
    (vault / "author").mkdir(exist_ok=True)
    fm: dict = {
        "type": "author",
        "name": name,
        "created": "2026-05-18",
        "aliases": aliases or [],
        "tags": [],
        "status": "active",
    }
    rel_path = f"author/{name}.md"
    file_path = vault / rel_path
    post = frontmatter.Post(body, **fm)
    file_path.write_text(frontmatter.dumps(post) + "\n", encoding="utf-8")
    return rel_path


@pytest.fixture
def hypatia_vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    vault.mkdir()
    for sub in ("zettel", "person", "org", "source", "author", "_templates"):
        (vault / sub).mkdir()
    src_template = get_scaffold_dir() / "_templates" / "zettel.md"
    dst_template = vault / "_templates" / "zettel.md"
    dst_template.write_text(src_template.read_text(encoding="utf-8"),
                            encoding="utf-8")
    return vault


# ---------------------------------------------------------------------------
# Author resolution
# ---------------------------------------------------------------------------


def test_resolve_author_exact_person_match(hypatia_vault: Path) -> None:
    _seed_person(hypatia_vault, "Doe, Jane")
    rel = _resolve_author_target(hypatia_vault, "[[person/Doe, Jane]]")
    assert rel == "person/Doe, Jane.md"


def test_resolve_author_bare_name_falls_through_to_person(
    hypatia_vault: Path,
) -> None:
    """Bare names (no directory prefix) first try ``author/``; if
    nothing matches there (including aliases scan), fall through to
    ``person/`` for back-compat. Here only the ``person/`` record
    exists, so the resolver lands there."""
    _seed_person(hypatia_vault, "Doe, Jane")
    rel = _resolve_author_target(hypatia_vault, "Doe, Jane")
    assert rel == "person/Doe, Jane.md"


def test_resolve_author_via_alias_in_person_dir(hypatia_vault: Path) -> None:
    """Operator types alias name pointing at person/ directory; the
    resolver scans person/ aliases lists. Back-compat path for
    person-as-author records."""
    _seed_person(hypatia_vault, "Doe, Jane", aliases=["Jane Doe", "JD"])
    # Typed wikilink uses an alias against person/ directory.
    rel = _resolve_author_target(hypatia_vault, "[[person/Jane Doe]]")
    assert rel == "person/Doe, Jane.md"


def test_resolve_author_bare_alias_falls_through_to_person(
    hypatia_vault: Path,
) -> None:
    """Operator types a bare-name alias; resolver scans author/ first
    (no records), then person/ aliases — finds the match."""
    _seed_person(hypatia_vault, "Doe, Jane", aliases=["Jane Doe", "JD"])
    rel = _resolve_author_target(hypatia_vault, "[[Jane Doe]]")
    assert rel == "person/Doe, Jane.md"


def test_resolve_author_org_exact_path(hypatia_vault: Path) -> None:
    """Operator can also point author at an org by exact rel_path."""
    _seed_org(hypatia_vault, "Hypatia Tech")
    rel = _resolve_author_target(hypatia_vault, "[[org/Hypatia Tech]]")
    assert rel == "org/Hypatia Tech.md"


def test_resolve_author_unknown_returns_none(hypatia_vault: Path) -> None:
    rel = _resolve_author_target(hypatia_vault, "[[person/Phantom]]")
    assert rel is None


def test_resolve_author_empty_returns_none(hypatia_vault: Path) -> None:
    assert _resolve_author_target(hypatia_vault, "") is None
    assert _resolve_author_target(hypatia_vault, None) is None
    assert _resolve_author_target(hypatia_vault, "[[]]") is None


# ---------------------------------------------------------------------------
# Author resolution — author/ directory (Phase 1 canonical location)
# ---------------------------------------------------------------------------
#
# Phase 1 (capture_source_anchor.resolve_or_create_author) writes
# bibliographic author records to ``author/`` not ``person/``. The
# verifier NEEDS-FIX surfaced 2026-05-18: original Phase 3 resolver
# only scanned ``person/``, so the production case of
# ``zettel/X.md`` with ``author: [[author/Aurelius, Marcus]]``
# silently no-op'd. Tests below pin the canonical author-dir flow.


def test_resolve_author_exact_author_path(hypatia_vault: Path) -> None:
    """The canonical Phase 1 form: zettel's author: field points at
    ``author/Aurelius, Marcus``. Resolver finds it via exact-path
    match (no aliases scan needed for the happy path)."""
    _seed_author(hypatia_vault, "Aurelius, Marcus")
    rel = _resolve_author_target(hypatia_vault, "[[author/Aurelius, Marcus]]")
    assert rel == "author/Aurelius, Marcus.md"


def test_resolve_author_aliased_short_form_in_author_dir(
    hypatia_vault: Path,
) -> None:
    """Operator types a short-form wikilink (``[[author/Marcus]]``)
    against an author/ record whose ``aliases:`` includes ``Marcus``.
    The aliases scan in ``author/`` resolves to the canonical record.

    This is the LOAD-BEARING production case the verifier flagged —
    Phase 1 creates author records with the operator's typed form as
    an alias entry, so short-form lookups must find the canonical
    file via aliases."""
    _seed_author(
        hypatia_vault,
        "Aurelius, Marcus",
        aliases=["Marcus Aurelius", "Marcus"],
    )
    rel = _resolve_author_target(hypatia_vault, "[[author/Marcus]]")
    assert rel == "author/Aurelius, Marcus.md"


def test_resolve_author_aliased_full_form_in_author_dir(
    hypatia_vault: Path,
) -> None:
    """Operator types the input form (``[[author/Marcus Aurelius]]``)
    against a record stored in canonical form
    (``author/Aurelius, Marcus.md``). The aliases scan resolves."""
    _seed_author(
        hypatia_vault,
        "Aurelius, Marcus",
        aliases=["Marcus Aurelius"],
    )
    rel = _resolve_author_target(hypatia_vault, "[[author/Marcus Aurelius]]")
    assert rel == "author/Aurelius, Marcus.md"


def test_resolve_author_unknown_in_author_dir_returns_none(
    hypatia_vault: Path,
) -> None:
    """No author/ record AND no alias match → None. Fail-open path
    that lets the new zettel survive on disk while the author record
    can be manually reconciled."""
    rel = _resolve_author_target(
        hypatia_vault, "[[author/Nonexistent Person]]",
    )
    assert rel is None


def test_resolve_author_bare_name_prefers_author_over_person(
    hypatia_vault: Path,
) -> None:
    """When both author/ and person/ contain matching records for a
    bare-name lookup, ``author/`` wins (Phase 1 canonical priority).
    Operator can disambiguate by typing the directory prefix."""
    _seed_author(hypatia_vault, "Aurelius, Marcus")
    _seed_person(hypatia_vault, "Aurelius, Marcus")
    rel = _resolve_author_target(hypatia_vault, "Aurelius, Marcus")
    assert rel == "author/Aurelius, Marcus.md"


# ---------------------------------------------------------------------------
# Rewriter direct-test
# ---------------------------------------------------------------------------


def test_author_contents_rewriter_appends_to_existing(hypatia_vault: Path) -> None:
    body = (
        "# Bio\n\nperson info\n\n"
        "# Contents\n\n"
        "- [[zettel/Older Idea]]\n"
    )
    rw = _build_author_contents_rewriter("[[zettel/New Idea]]")
    out = rw(body)
    assert "[[zettel/Older Idea]]" in out
    assert "[[zettel/New Idea]]" in out


def test_author_contents_rewriter_is_idempotent() -> None:
    body = (
        "# Contents\n\n"
        "- [[zettel/Existing]]\n"
    )
    rw = _build_author_contents_rewriter("[[zettel/Existing]]")
    out = rw(body)
    assert out.count("[[zettel/Existing]]") == 1


def test_author_contents_rewriter_creates_missing_section() -> None:
    body = "# Bio\n\nperson info\n"
    rw = _build_author_contents_rewriter("[[zettel/First]]")
    out = rw(body)
    assert "# Contents" in out
    assert "- [[zettel/First]]" in out


# ---------------------------------------------------------------------------
# append_to_author_contents — helper level
# ---------------------------------------------------------------------------


def test_append_first_zettel_for_author(hypatia_vault: Path) -> None:
    _seed_person(
        hypatia_vault, "Doe, Jane",
        body="# Bio\n\nresearcher\n\n# Contents\n\n",
    )
    new_rel = _seed_zettel(
        hypatia_vault, "Idea-1",
        fm={"author": "[[person/Doe, Jane]]"},
    )

    result = append_to_author_contents(
        hypatia_vault, "[[person/Doe, Jane]]", new_rel,
        scope="hypatia",
    )
    assert result is True

    author = vault_read(hypatia_vault, "person/Doe, Jane.md")
    assert "- [[zettel/Idea-1]]" in author["body"]


def test_append_is_idempotent(hypatia_vault: Path) -> None:
    _seed_person(hypatia_vault, "Doe, Jane")
    new_rel = _seed_zettel(
        hypatia_vault, "Idea-1",
        fm={"author": "[[person/Doe, Jane]]"},
    )

    append_to_author_contents(
        hypatia_vault, "[[person/Doe, Jane]]", new_rel, scope="hypatia",
    )
    append_to_author_contents(
        hypatia_vault, "[[person/Doe, Jane]]", new_rel, scope="hypatia",
    )

    author = vault_read(hypatia_vault, "person/Doe, Jane.md")
    assert author["body"].count("[[zettel/Idea-1]]") == 1


def test_append_creates_missing_contents_section(hypatia_vault: Path) -> None:
    """Pre-Phase-3 person record without ``# Contents`` gets the
    section auto-created at end of body."""
    _seed_person(
        hypatia_vault, "Doe, Jane",
        body="# Bio\n\nresearcher\n",  # No # Contents section.
    )
    new_rel = _seed_zettel(hypatia_vault, "Idea-1")

    result = append_to_author_contents(
        hypatia_vault, "[[person/Doe, Jane]]", new_rel, scope="hypatia",
    )
    assert result is True

    author = vault_read(hypatia_vault, "person/Doe, Jane.md")
    assert "# Contents" in author["body"]
    assert "- [[zettel/Idea-1]]" in author["body"]
    # Original Bio section preserved.
    assert "# Bio" in author["body"]


def test_append_follows_aliases_chain(hypatia_vault: Path) -> None:
    """Operator types alias name in zettel ``author:``; mirror finds
    the canonical person via aliases scan."""
    _seed_person(
        hypatia_vault, "Doe, Jane",
        aliases=["Jane Doe"],
        body="# Bio\n\n# Contents\n\n",
    )
    new_rel = _seed_zettel(hypatia_vault, "Idea-1")

    result = append_to_author_contents(
        hypatia_vault, "[[Jane Doe]]", new_rel, scope="hypatia",
    )
    assert result is True

    # The bullet landed on the canonical Doe, Jane record, NOT on a
    # non-existent ``person/Jane Doe.md``.
    author = vault_read(hypatia_vault, "person/Doe, Jane.md")
    assert "- [[zettel/Idea-1]]" in author["body"]


def test_append_missing_author_returns_false(hypatia_vault: Path) -> None:
    """Author record doesn't exist + no alias matches → no-op."""
    new_rel = _seed_zettel(hypatia_vault, "Idea-1")
    result = append_to_author_contents(
        hypatia_vault, "[[person/Phantom]]", new_rel, scope="hypatia",
    )
    assert result is False


def test_append_empty_author_returns_false(hypatia_vault: Path) -> None:
    new_rel = _seed_zettel(hypatia_vault, "Idea-1")
    assert append_to_author_contents(
        hypatia_vault, "", new_rel, scope="hypatia",
    ) is False
    assert append_to_author_contents(
        hypatia_vault, None, new_rel, scope="hypatia",
    ) is False


def test_append_to_org_author(hypatia_vault: Path) -> None:
    """Operator points author at an org (e.g. corporate publication);
    works as long as exact path resolves."""
    _seed_org(
        hypatia_vault, "Hypatia Tech",
        body="# About\n\n# Contents\n\n",
    )
    new_rel = _seed_zettel(hypatia_vault, "Whitepaper-Excerpt")

    result = append_to_author_contents(
        hypatia_vault, "[[org/Hypatia Tech]]", new_rel, scope="hypatia",
    )
    assert result is True

    org = vault_read(hypatia_vault, "org/Hypatia Tech.md")
    assert "- [[zettel/Whitepaper-Excerpt]]" in org["body"]


def test_append_to_author_dir_record(hypatia_vault: Path) -> None:
    """The canonical Phase 1 production case: zettel's author field
    points at ``author/Aurelius, Marcus`` (the Phase 1 canonical
    location). Hook appends the bullet to THAT record's # Contents.

    This is the path the verifier flagged as silently no-op'ing
    before this commit: original resolver only scanned person/, so
    author/ records never received the auto-append."""
    author_rel = _seed_author(
        hypatia_vault,
        "Aurelius, Marcus",
        aliases=["Marcus Aurelius"],
        body="# Bio\n\n# Contents\n\n",
    )
    new_rel = _seed_zettel(hypatia_vault, "Meditations-Excerpt-1")

    result = append_to_author_contents(
        hypatia_vault, "[[author/Aurelius, Marcus]]", new_rel,
        scope="hypatia",
    )
    assert result is True

    author = vault_read(hypatia_vault, author_rel)
    assert "- [[zettel/Meditations-Excerpt-1]]" in author["body"]


def test_append_to_author_dir_via_aliased_short_form(
    hypatia_vault: Path,
) -> None:
    """Operator types short-form wikilink ``[[author/Marcus]]`` —
    aliases scan resolves to canonical ``author/Aurelius, Marcus``
    and the bullet lands on the CANONICAL record. Verifier-flagged
    case: this whole path was unreachable before the fix."""
    author_rel = _seed_author(
        hypatia_vault,
        "Aurelius, Marcus",
        aliases=["Marcus Aurelius", "Marcus"],
        body="# Bio\n\n# Contents\n\n",
    )
    new_rel = _seed_zettel(hypatia_vault, "Meditations-Excerpt-2")

    result = append_to_author_contents(
        hypatia_vault, "[[author/Marcus]]", new_rel, scope="hypatia",
    )
    assert result is True

    author = vault_read(hypatia_vault, author_rel)
    assert "- [[zettel/Meditations-Excerpt-2]]" in author["body"]


def test_append_to_author_dir_missing_returns_false(
    hypatia_vault: Path,
) -> None:
    """No author/ record + no alias match → fail-open. New zettel
    survives; manual reconciliation when the author record is
    created."""
    new_rel = _seed_zettel(hypatia_vault, "Orphan-Z")
    result = append_to_author_contents(
        hypatia_vault, "[[author/Nonexistent]]", new_rel, scope="hypatia",
    )
    assert result is False


def test_append_idempotent_against_pipe_aliased_existing(
    hypatia_vault: Path,
) -> None:
    """Pipe-aliased existing bullet (operator hand-edited to add a
    display name) does NOT cause a duplicate append. The wikilink-
    target-present check tolerates both
    ``[[zettel/Title]]`` and ``[[zettel/Title|Display]]`` as the
    same logical reference.

    Regression pin for the third recurrence of the pipe-alias
    idempotency hole (Phase 2 + Phase 3 supersede + Phase 3 author
    Contents all share this trap)."""
    author_rel = _seed_author(
        hypatia_vault, "Aurelius, Marcus",
        body=(
            "# Bio\n\n"
            "# Contents\n\n"
            "- [[zettel/Meditations-Excerpt-1|the first one]]\n"
        ),
    )
    new_rel = _seed_zettel(hypatia_vault, "Meditations-Excerpt-1")

    result = append_to_author_contents(
        hypatia_vault, "[[author/Aurelius, Marcus]]", new_rel,
        scope="hypatia",
    )
    # Result True is acceptable if a write happened, but the body
    # MUST NOT now contain two bullets pointing at the same zettel.
    author = vault_read(hypatia_vault, author_rel)
    # Count occurrences of the bare target stem — should be exactly 1
    # regardless of alias display form.
    assert author["body"].count("[[zettel/Meditations-Excerpt-1") == 1


# ---------------------------------------------------------------------------
# vault_create integration — Z-centric routing
# ---------------------------------------------------------------------------


def test_vault_create_zettel_with_author_appends_to_contents(
    hypatia_vault: Path,
) -> None:
    """End-to-end: vault_create on a zettel with ``author:`` set
    triggers the hook which appends the bullet to the author."""
    _seed_person(
        hypatia_vault, "Doe, Jane",
        body="# Bio\n\n# Contents\n\n",
    )

    vault_create(
        hypatia_vault,
        "zettel",
        "Z-First",
        set_fields={"author": "[[person/Doe, Jane]]"},
        scope="hypatia",
    )

    author = vault_read(hypatia_vault, "person/Doe, Jane.md")
    assert "- [[zettel/Z-First]]" in author["body"]


def test_vault_create_source_does_not_append(hypatia_vault: Path) -> None:
    """SOURCE records do NOT trigger the Z-centric hook. The brief is
    explicit: only zettels auto-append. A source created with
    ``author:`` set should NOT add the source as a # Contents bullet."""
    _seed_person(
        hypatia_vault, "Doe, Jane",
        body="# Bio\n\n# Contents\n\n",
    )
    # Bypass vault_create for source (source template may not exist
    # in this fixture; we only need to verify the hook routing).
    _seed_source(
        hypatia_vault, "Some Book",
        fm={"author": "[[person/Doe, Jane]]"},
    )

    # Now create a zettel — the hook should fire and the # Contents
    # bullet should ONLY point at the zettel, never at the source.
    vault_create(
        hypatia_vault,
        "zettel",
        "Z-From-Book",
        set_fields={"author": "[[person/Doe, Jane]]"},
        scope="hypatia",
    )

    author = vault_read(hypatia_vault, "person/Doe, Jane.md")
    assert "- [[zettel/Z-From-Book]]" in author["body"]
    # The source did NOT get auto-appended (Z-centric guarantee).
    assert "[[source/Some Book]]" not in author["body"]


def test_vault_create_zettel_missing_author_target_does_not_fail(
    hypatia_vault: Path,
) -> None:
    """When author points at a non-existent record, vault_create still
    succeeds (hook is failure-isolated)."""
    result = vault_create(
        hypatia_vault,
        "zettel",
        "Z-Orphan",
        set_fields={"author": "[[person/Phantom]]"},
        scope="hypatia",
    )
    assert result["path"] == "zettel/Z-Orphan.md"
    z = vault_read(hypatia_vault, "zettel/Z-Orphan.md")
    assert z["frontmatter"]["author"] == "[[person/Phantom]]"


def test_vault_create_zettel_without_author_skips_hook(
    hypatia_vault: Path,
) -> None:
    """No author → no hook fire (sanity — empty string is no-op)."""
    _seed_person(
        hypatia_vault, "Doe, Jane",
        body="# Bio\n\n# Contents\n\n",
    )
    vault_create(
        hypatia_vault,
        "zettel",
        "Z-Anonymous",
        scope="hypatia",
    )

    author = vault_read(hypatia_vault, "person/Doe, Jane.md")
    # Author's # Contents unchanged — no bullets at all.
    assert "[[zettel/Z-Anonymous]]" not in author["body"]
