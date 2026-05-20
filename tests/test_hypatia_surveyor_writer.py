"""Acceptance test surface for ``surveyor.writer.write_alfred_tags`` on
the Hypatia-only record type set (Phase 5 Sub-arc C).

Per the multi-instance wiring antipattern memo
(``feedback_multi_instance_wiring_pattern.md``): each new per-instance
wiring step gets a smoke test that confirms the SHARED writer code path
behaves correctly on the NEW instance's record types. Sub-arc A enabled
the surveyor on Hypatia and the live 24h pass produced 70%+ tag coverage
on zettel/ and 48%+ on note/ — proving the writer handles Hypatia types
in practice. Sub-arc C pins that working behavior as test surface so a
future regression (a type silently breaking the writer) surfaces here
instead of via vault-reviewer noticing missing tags.

Design ratification (Andrew, 2026-05-20): Path A "ship-then-narrow."
Sub-arc C ships the test surface only. No EXCLUDE_ALFRED_TAGS_TYPES
constant or production code change today — Hypatia vault has 0 MOC,
0 author, 0 memo records at this writing, so the noise-vs-signal
narrow decision cannot be evaluated. Defer until those record types
materialize. The pinned-constant test below (test_hypatia_alfred_tags_
type_set_matches_schema) acts as the drift-detector: if a new Hypatia
type lands in ``KNOWN_TYPES_HYPATIA`` without joining
``HYPATIA_ALFRED_TAGS_TYPES`` here, the test surfaces it.

writer.write_alfred_tags behavior pinned by these tests (verified
against ``src/alfred/surveyor/writer.py`` lines 43-89):
  1. Tags REPLACE — ``post.metadata["alfred_tags"] = tags`` (line 81),
     NOT merge. The dispatch prompt characterised this as "merge"; the
     actual code replaces. Tests assert replace semantics.
  2. Skip-if-equal uses NORMALIZED comparison — sorted + deduped sets
     compare equal so input order / duplicates don't drive spurious
     writes. The skip path emits ``writer.tags_unchanged``.
  3. The genuine-change path emits ``writer.tags_updated`` and appends
     one ``modify`` line to the audit log.
  4. List order on disk preserves the input order (not normalized) —
     the normalization is for COMPARISON only, not storage.

Log-emission pin (builder rule #9 — feedback_log_emission_test_pattern.md):
each path that emits a structured log event has a matching
``structlog.testing.capture_logs`` assertion so observability
silently-degrades-across-refactors are caught.
"""

from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent

import frontmatter
import pytest
import structlog

from alfred.surveyor.state import PipelineState
from alfred.surveyor.writer import VaultWriter
from alfred.vault.schema import KNOWN_TYPES_HYPATIA, TYPE_DIRECTORY


# ---------------------------------------------------------------------------
# Hypatia type set under writer acceptance test.
# ---------------------------------------------------------------------------
# Mirrors ``KNOWN_TYPES_HYPATIA`` (vault/schema.py). The pinned-constant
# test ``test_hypatia_alfred_tags_type_set_matches_schema`` asserts this
# equality so a future addition to ``KNOWN_TYPES_HYPATIA`` surfaces here
# without test coverage rather than silently passing.
#
# Per ratified design Path A (2026-05-20): no narrowing today. All
# 23 Hypatia types receive the parametrized acceptance pass. If a
# subsequent narrow decision (e.g., exclude MOC + author from
# alfred_tags writeback) lands, this set splits into an "allowed" + a
# "denied" subset, and the denied subset gets its own test asserting
# the writer skips them.
HYPATIA_ALFRED_TAGS_TYPES: frozenset[str] = frozenset({
    "document", "concept", "source", "citation", "template",
    "fiction-continuity", "fiction-story", "fiction-structure",
    "fiction-world", "fiction-voice", "fiction-character",
    "practice-session",
    "essay", "voice", "voice-cluster", "method",
    "author",
    "memo", "zettel", "MOC", "question", "research-pointer",
    "article",
})


def _type_directory_for(record_type: str) -> str:
    """Resolve the on-disk subdirectory for a Hypatia type.

    Mirrors the ``vault_create`` routing: explicit ``TYPE_DIRECTORY``
    entry takes precedence, otherwise default to the type name itself.
    Without this, ``essay`` would land at ``essay/`` (no entry would
    use the type-name fallback) instead of the actual
    ``document/essay/`` location.
    """
    return TYPE_DIRECTORY.get(record_type, record_type)


def _seed_typed_record(
    vault: Path,
    record_type: str,
    name: str,
    existing_tags: list[str] | None = None,
) -> str:
    """Write a minimal record of ``record_type`` into the vault. Returns
    the rel_path the writer would consume.

    Mirrors the real on-disk shape — frontmatter ``type:`` + ``name:`` +
    ``created:`` + optional ``alfred_tags:`` + a short body. Filename is
    ``<name>.md`` under ``TYPE_DIRECTORY[record_type]`` (so
    ``essay`` records correctly land at ``document/essay/<name>.md``).
    """
    directory = _type_directory_for(record_type)
    fm_tags = (
        f"alfred_tags: {existing_tags!r}\n" if existing_tags is not None else ""
    )
    content = dedent(
        f"""\
        ---
        type: {record_type}
        name: {name}
        created: 2026-05-20
        {fm_tags}---

        Hypatia surveyor acceptance test fixture body.
        """
    )
    rel_path = f"{directory}/{name}.md"
    target = vault / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return rel_path


@pytest.fixture
def writer_factory(tmp_vault: Path, tmp_path: Path):
    """Return a callable that builds a fresh (writer, state, audit_path)
    tuple. Each test gets its own tuple — sharing a writer across
    sub-tests would let the audit log accumulate cross-test entries.
    """
    def _build():
        audit_path = tmp_path / "vault_audit.log"
        state = PipelineState(tmp_path / "surveyor_state.json")
        writer = VaultWriter(tmp_vault, state, audit_log_path=audit_path)
        return writer, state, audit_path

    return _build


# ---------------------------------------------------------------------------
# Parametrized acceptance — per-type write + roundtrip
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("record_type", sorted(HYPATIA_ALFRED_TAGS_TYPES))
def test_write_alfred_tags_succeeds_on_hypatia_type(
    record_type: str,
    tmp_vault: Path,
    writer_factory,
) -> None:
    """``write_alfred_tags`` writes ``alfred_tags`` to a record of every
    Hypatia type. Verifies:
      - The write succeeds (file's alfred_tags field updated)
      - The frontmatter roundtrips cleanly (no schema corruption)
      - Other frontmatter fields are preserved (type/name/created intact)
      - List order on disk matches input order (not normalized — see
        module docstring point 4)
      - Body content is untouched
    """
    writer, state, audit_path = writer_factory()
    rel_path = _seed_typed_record(tmp_vault, record_type, f"Seed {record_type}")

    proposed_tags = [f"{record_type}-cluster", "topic-a", "topic-b"]
    writer.write_alfred_tags(rel_path, proposed_tags)

    # Roundtrip: re-parse the file and confirm the tags landed exactly.
    post = frontmatter.load(str(tmp_vault / rel_path))
    assert post.metadata["alfred_tags"] == proposed_tags, (
        f"Hypatia type '{record_type}': alfred_tags should be written in "
        f"input order. Got: {post.metadata.get('alfred_tags')!r}"
    )

    # Other frontmatter fields preserved verbatim.
    assert post.metadata["type"] == record_type
    assert post.metadata["name"] == f"Seed {record_type}"
    assert post.metadata["created"] == "2026-05-20" or str(
        post.metadata["created"]
    ).startswith("2026-05-20")

    # Body preserved.
    assert "Hypatia surveyor acceptance test fixture body." in post.content

    # State + audit log updated for a genuine write.
    assert rel_path in state.files, (
        f"writer must register state hash for {record_type} write"
    )
    audit_lines = audit_path.read_text(encoding="utf-8").splitlines()
    assert len(audit_lines) == 1, (
        f"genuine write must append exactly one audit line for {record_type}"
    )
    entry = json.loads(audit_lines[0])
    assert entry["tool"] == "surveyor"
    assert entry["op"] == "modify"
    assert entry["path"] == rel_path


@pytest.mark.parametrize("record_type", sorted(HYPATIA_ALFRED_TAGS_TYPES))
def test_write_alfred_tags_preserves_other_frontmatter_fields(
    record_type: str,
    tmp_vault: Path,
    writer_factory,
) -> None:
    """Adjacent frontmatter fields (type-specific: ``status``, ``author``,
    ``source``, ``mocs``, etc.) are NOT disturbed by an alfred_tags write.
    Seeds a record with type-appropriate sibling fields, writes tags,
    asserts every sibling survives. Regression-style — catches the case
    where a future writer refactor drops fields outside its target.
    """
    writer, _state, _audit_path = writer_factory()

    # Seed with a richer frontmatter shape — type-relevant siblings.
    # The exact field set doesn't have to match each type's required
    # schema; the writer doesn't validate. The point is: every field
    # survives a tag write.
    directory = _type_directory_for(record_type)
    rel_path = f"{directory}/Sibling Test.md"
    target = tmp_vault / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        dedent(
            f"""\
            ---
            type: {record_type}
            name: Sibling Test
            created: 2026-05-20
            status: active
            tags: ["existing-yaml-tag"]
            description: "A record with rich frontmatter."
            related: ["[[zettel/Some Related Thing]]"]
            ---

            Body.
            """
        ),
        encoding="utf-8",
    )

    writer.write_alfred_tags(rel_path, ["new-tag"])

    post = frontmatter.load(str(target))
    assert post.metadata["type"] == record_type
    assert post.metadata["name"] == "Sibling Test"
    assert post.metadata["status"] == "active"
    assert post.metadata["tags"] == ["existing-yaml-tag"]
    assert post.metadata["description"] == "A record with rich frontmatter."
    assert post.metadata["related"] == ["[[zettel/Some Related Thing]]"]
    assert post.metadata["alfred_tags"] == ["new-tag"]


# ---------------------------------------------------------------------------
# Replace semantics — IMPORTANT correction vs dispatch-prompt claim
# ---------------------------------------------------------------------------


def test_write_alfred_tags_replaces_not_merges(
    tmp_vault: Path,
    writer_factory,
) -> None:
    """Pin the replace-not-merge contract. ``write_alfred_tags`` REPLACES
    existing ``alfred_tags`` with the proposed list — it does NOT compute
    a union of existing + new.

    This was incorrectly characterised as "merge" in the Sub-arc C
    dispatch prompt. The actual code (writer.py:81) does
    ``post.metadata["alfred_tags"] = tags`` after the skip-if-equal
    check. If a future refactor introduces merge semantics, this test
    surfaces the contract break.

    Choice of fixture type: ``zettel`` (most-used Hypatia surveyor target
    type per Sub-arc A's 70% coverage stat). The contract is type-
    independent; one canonical type is sufficient.
    """
    writer, _state, _audit_path = writer_factory()
    rel_path = _seed_typed_record(
        tmp_vault, "zettel", "Replace Semantics",
        existing_tags=["old-tag-a", "old-tag-b"],
    )

    # Write proposes tags that share NO members with existing.
    writer.write_alfred_tags(rel_path, ["new-tag-c"])

    post = frontmatter.load(str(tmp_vault / rel_path))
    # If this were merge: ["old-tag-a", "old-tag-b", "new-tag-c"].
    # Actual replace contract: just the new list.
    assert post.metadata["alfred_tags"] == ["new-tag-c"], (
        "write_alfred_tags REPLACES; if this fails to ['new-tag-c'] the "
        "writer has been changed to merge — that's a contract break, "
        "not a fixture bug. See writer.py:81 + module docstring point 1."
    )
    assert "old-tag-a" not in post.metadata["alfred_tags"]
    assert "old-tag-b" not in post.metadata["alfred_tags"]


# ---------------------------------------------------------------------------
# Idempotency — skip-if-equal contract
# ---------------------------------------------------------------------------


def test_write_alfred_tags_idempotent_same_tags(
    tmp_vault: Path,
    writer_factory,
) -> None:
    """Re-calling ``write_alfred_tags`` with the same tags is a no-op:
    file mtime unchanged, audit log unchanged, ``writer.tags_unchanged``
    log emitted. Pins the early-return contract that prevents cluster-
    sweep churn (the surveyor proposes tags every sweep; semantically
    identical lists must not drive vault writes).
    """
    writer, state, audit_path = writer_factory()
    rel_path = _seed_typed_record(tmp_vault, "zettel", "Idempotent")

    # First write — establishes the tags + audit entry.
    writer.write_alfred_tags(rel_path, ["alpha", "beta"])
    mtime_after_first = (tmp_vault / rel_path).stat().st_mtime_ns
    audit_lines_after_first = audit_path.read_text(
        encoding="utf-8"
    ).splitlines()
    assert len(audit_lines_after_first) == 1

    # Second write — same tags. Must be a no-op.
    with structlog.testing.capture_logs() as captured:
        writer.write_alfred_tags(rel_path, ["alpha", "beta"])

    mtime_after_second = (tmp_vault / rel_path).stat().st_mtime_ns
    assert mtime_after_second == mtime_after_first, (
        "idempotent re-write must not touch the file"
    )

    # Audit log unchanged.
    audit_lines_after_second = audit_path.read_text(
        encoding="utf-8"
    ).splitlines()
    assert audit_lines_after_second == audit_lines_after_first, (
        "idempotent re-write must not append to audit log"
    )

    # Log-emission pin (builder rule #9): tags_unchanged event fires.
    matches = [c for c in captured if c.get("event") == "writer.tags_unchanged"]
    assert len(matches) == 1, (
        f"writer.tags_unchanged must fire on idempotent write; "
        f"captured events: {[c.get('event') for c in captured]}"
    )
    assert matches[0]["path"] == rel_path
    assert matches[0]["tag_count"] == 2


def test_write_alfred_tags_idempotent_normalized_equality(
    tmp_vault: Path,
    writer_factory,
) -> None:
    """The skip-if-equal contract uses NORMALIZED comparison (sorted +
    deduped sets). Input order shuffled + duplicates added in the
    re-write must still match the existing normalized set and skip.

    Pins writer.py:70-71: ``norm_existing = sorted(set(...))``.
    """
    writer, _state, audit_path = writer_factory()
    rel_path = _seed_typed_record(
        tmp_vault, "zettel", "Normalized Equality",
        existing_tags=["alpha", "beta"],
    )

    # The fixture wrote alfred_tags via frontmatter. First confirm the
    # writer sees existing tags + a shuffled+duped input as equal.
    with structlog.testing.capture_logs() as captured:
        writer.write_alfred_tags(rel_path, ["beta", "alpha", "beta"])

    # No write happened.
    assert not audit_path.exists() or audit_path.read_text() == "", (
        "shuffled+duped re-write that normalizes to existing must be "
        "skipped (no audit entry)"
    )

    # Log-emission pin: tags_unchanged with the normalized count (2,
    # not 3 — the duplicate `beta` collapses).
    matches = [c for c in captured if c.get("event") == "writer.tags_unchanged"]
    assert len(matches) == 1
    assert matches[0]["tag_count"] == 2, (
        "tag_count must reflect normalized (set-deduped) size"
    )


def test_write_alfred_tags_writes_when_tags_differ_emits_updated_log(
    tmp_vault: Path,
    writer_factory,
) -> None:
    """Inverse of the idempotency pin: a genuine difference between
    existing and proposed tags MUST drive a write + ``writer.tags_updated``
    log emission. Pins the log on the writing path (builder rule #9).
    """
    writer, _state, audit_path = writer_factory()
    rel_path = _seed_typed_record(
        tmp_vault, "zettel", "Genuine Change",
        existing_tags=["alpha"],
    )

    with structlog.testing.capture_logs() as captured:
        writer.write_alfred_tags(rel_path, ["alpha", "beta"])

    # File rewritten.
    post = frontmatter.load(str(tmp_vault / rel_path))
    assert post.metadata["alfred_tags"] == ["alpha", "beta"]

    # Audit log gained one line.
    audit_lines = audit_path.read_text(encoding="utf-8").splitlines()
    assert len(audit_lines) == 1

    # Log-emission pin: tags_updated with before/after counts + tags.
    matches = [c for c in captured if c.get("event") == "writer.tags_updated"]
    assert len(matches) == 1, (
        f"writer.tags_updated must fire on genuine-change write; "
        f"captured events: {[c.get('event') for c in captured]}"
    )
    assert matches[0]["path"] == rel_path
    assert matches[0]["before_count"] == 1
    assert matches[0]["after_count"] == 2
    assert matches[0]["tags"] == ["alpha", "beta"]


# ---------------------------------------------------------------------------
# Pinned-constant test — drift detector
# ---------------------------------------------------------------------------


def test_hypatia_alfred_tags_type_set_matches_schema() -> None:
    """Drift detector: ``HYPATIA_ALFRED_TAGS_TYPES`` (this file) must
    equal ``KNOWN_TYPES_HYPATIA`` (vault/schema.py) until/unless a
    deliberate narrowing decision lands.

    Per ratified design Path A (2026-05-20): all Hypatia types receive
    surveyor alfred_tags writeback today; no exclusion list. If a
    future ship narrows the surface (e.g., excludes MOC + author when
    those record types accumulate and vault-reviewer surfaces noise),
    this test splits:
      - HYPATIA_ALFRED_TAGS_TYPES = KNOWN_TYPES_HYPATIA - excluded_set
      - A new test asserting excluded_set's writer behavior (whatever
        the narrow chose — early-return? raise? skip via type filter?)

    Failing here today means a new type joined ``KNOWN_TYPES_HYPATIA``
    without being added to the parametrized acceptance pass. Fix by
    adding the type to ``HYPATIA_ALFRED_TAGS_TYPES`` (if it should
    receive tags) OR by introducing the narrowing surface + excluded
    set + new denied-type test.
    """
    assert HYPATIA_ALFRED_TAGS_TYPES == KNOWN_TYPES_HYPATIA, (
        f"HYPATIA_ALFRED_TAGS_TYPES drifted from KNOWN_TYPES_HYPATIA.\n"
        f"  Missing from test set: "
        f"{KNOWN_TYPES_HYPATIA - HYPATIA_ALFRED_TAGS_TYPES}\n"
        f"  Extra in test set: "
        f"{HYPATIA_ALFRED_TAGS_TYPES - KNOWN_TYPES_HYPATIA}\n"
        "If a new Hypatia type was added, mirror it here. If a narrow "
        "decision should exclude it from surveyor alfred_tags, add the "
        "exclusion surface + denied-type test."
    )


# ---------------------------------------------------------------------------
# Defensive — frontmatter without alfred_tags field
# ---------------------------------------------------------------------------


def test_write_alfred_tags_creates_field_when_absent(
    tmp_vault: Path,
    writer_factory,
) -> None:
    """When the record has no existing ``alfred_tags`` field at all,
    the writer treats it as ``[]`` and writes the proposed list.

    Captures the early-fixture state of a freshly-curated Hypatia
    zettel (the capture-mode auto-creation path does NOT pre-seed
    alfred_tags — surveyor fills it on the first cluster sweep).
    """
    writer, _state, audit_path = writer_factory()
    # Seed WITHOUT alfred_tags (existing_tags=None).
    rel_path = _seed_typed_record(
        tmp_vault, "zettel", "No Existing Tags",
        existing_tags=None,
    )

    # Confirm pre-state: no alfred_tags in frontmatter.
    pre = frontmatter.load(str(tmp_vault / rel_path))
    assert "alfred_tags" not in pre.metadata

    writer.write_alfred_tags(rel_path, ["first-tag"])

    post = frontmatter.load(str(tmp_vault / rel_path))
    assert post.metadata["alfred_tags"] == ["first-tag"]
    # Audit log got the write (it IS a genuine change: [] → ["first-tag"]).
    audit_lines = audit_path.read_text(encoding="utf-8").splitlines()
    assert len(audit_lines) == 1
