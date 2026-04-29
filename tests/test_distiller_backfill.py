"""Tests for the one-time backfill CLI.

KAL-LE distiller-radar Phase 1 (2026-04-29). The backfill walks an
external source directory of session/note ``.md`` files, extracts
learn records from each file's ``## Alfred Learnings`` section, and
writes them to the configured vault path. Source files are read-only.

These tests exercise the full pipeline with a mocked v2 extractor —
the live extractor calls Anthropic and is covered by other suites.
The plumbing under test is:

  - Eligibility detection (file has ``## Alfred Learnings``).
  - Already-processed skip behavior on re-runs.
  - Learn record writes via the deterministic writer.
  - State persistence in ``distiller_backfill_state.json``.
  - ``--dry-run`` short-circuits writes + state updates.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from alfred.distiller import backfill as bf
from alfred.distiller.config import (
    DistillerConfig,
    StateConfig,
    VaultConfig,
)
from alfred.distiller.contracts import ExtractionResult, LearningCandidate


# --- helpers ---------------------------------------------------------------


def _make_config(tmp_path: Path) -> DistillerConfig:
    """Build a minimal DistillerConfig pointing vault + state into tmp_path."""
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir(parents=True, exist_ok=True)
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)

    cfg = DistillerConfig(
        vault=VaultConfig(path=str(vault_dir)),
        state=StateConfig(path=str(state_dir / "distiller_state.json")),
    )
    return cfg


def _write_session(
    source_dir: Path,
    name: str,
    body: str,
    record_type: str = "session",
) -> Path:
    """Write a tiny session-note .md file under source_dir."""
    source_dir.mkdir(parents=True, exist_ok=True)
    rel = source_dir / name
    rel.write_text(
        f"---\ntype: {record_type}\nname: {Path(name).stem}\n---\n\n{body}\n",
        encoding="utf-8",
    )
    return rel


def _fake_extraction(*titles: str) -> ExtractionResult:
    """Build an ExtractionResult with N synthetic LearningCandidates."""
    learnings = []
    for i, title in enumerate(titles):
        learnings.append(LearningCandidate(
            type="assumption",
            title=title,
            confidence="medium",
            status="active",
            claim=f"Synthetic claim for {title}, padded to satisfy min_length=20.",
            evidence_excerpt=f"Excerpt {i}",
            source_links=[],
            entity_links=[],
        ))
    return ExtractionResult(learnings=learnings)


# --- eligibility scan ------------------------------------------------------


def test_scan_eligible_files_picks_up_files_with_section(tmp_path: Path) -> None:
    source = tmp_path / "src"
    _write_session(source, "with-section.md",
        "## Intent\nSetup.\n\n## Alfred Learnings\n\n**X** — y.\n"
    )
    _write_session(source, "without-section.md",
        "## Intent\nSetup.\n\n## Verification\n\nDone.\n"
    )
    state = bf.BackfillState()
    report = bf.scan_eligible_files(source, state)
    assert report.scanned == 2
    assert len(report.eligible) == 1
    assert report.ineligible_count == 1
    assert report.already_processed == 0
    # The eligible one is the file with the section.
    assert report.eligible[0].name == "with-section.md"


def test_scan_eligible_files_skips_already_processed(tmp_path: Path) -> None:
    source = tmp_path / "src"
    f1 = _write_session(source, "a.md", "## Alfred Learnings\n\n- bullet.\n")
    f2 = _write_session(source, "b.md", "## Alfred Learnings\n\n- bullet.\n")
    state = bf.BackfillState()
    state.roots[str(source.resolve())] = bf.BackfillRecord(
        processed_paths=[str(f1.resolve())]
    )
    report = bf.scan_eligible_files(source, state)
    assert report.scanned == 2
    assert len(report.eligible) == 1
    assert report.already_processed == 1
    assert report.eligible[0].name == "b.md"


def test_scan_eligible_files_handles_missing_source_dir(tmp_path: Path) -> None:
    source = tmp_path / "does-not-exist"
    report = bf.scan_eligible_files(source, bf.BackfillState())
    assert report.scanned == 0
    assert report.eligible == []


def test_scan_eligible_files_walks_subdirectories(tmp_path: Path) -> None:
    """rglob picks up .md files in nested folders."""
    source = tmp_path / "src"
    _write_session(source, "top.md", "## Alfred Learnings\n\n- a.\n")
    _write_session(source / "sub" / "deep", "nested.md",
        "## Alfred Learnings\n\n- b.\n"
    )
    report = bf.scan_eligible_files(source, bf.BackfillState())
    assert report.scanned == 2
    assert len(report.eligible) == 2


# --- end-to-end backfill driver -------------------------------------------


@pytest.mark.asyncio
async def test_backfill_writes_learnings_and_persists_state(tmp_path: Path) -> None:
    """Happy path: 2 eligible files, mocked extractor, learnings land in vault."""
    source = tmp_path / "salem-session"
    _write_session(source, "ship-notes-2026-04-29.md",
        "## Intent\n\nShip Phase 1.\n\n"
        "## Alfred Learnings\n\n"
        "**Pattern** — body-hash gate prevents loops.\n"
    )
    _write_session(source, "review-2026-04-30.md",
        "## Intent\n\nReview cycle.\n\n"
        "## Alfred Learnings\n\n"
        "**Process** — review-fix-confirm.\n"
    )
    # One file without a section — should be skipped.
    _write_session(source, "no-section.md", "## Intent\n\nNo flagged section.\n")

    cfg = _make_config(tmp_path)

    async def fake_extract(**kwargs):
        # Per-source mock: emits one learning per call. The titles
        # disambiguate so writes land in distinct slugs.
        body = kwargs["source_body"]
        if "Phase 1" in body:
            return _fake_extraction("Phase 1 ship learning")
        return _fake_extraction("Review cycle learning")

    with patch.object(bf, "v2_extract", side_effect=fake_extract):
        result = await bf.run_backfill(source, cfg, dry_run=False)

    # Two eligible files, both extracted, no errors.
    assert result.scanned == 3
    assert result.eligible == 2
    assert result.already_processed == 0
    assert result.extracted == 2
    assert result.errors == 0
    assert result.learnings_by_type.get("assumption") == 2

    # Both learn records landed in the vault.
    learn_dir = Path(cfg.vault.path) / "assumption"
    assert learn_dir.is_dir()
    learn_files = sorted(learn_dir.glob("*.md"))
    assert len(learn_files) == 2

    # State persisted with both source paths marked processed.
    state = bf.load_backfill_state(cfg)
    rec = state.roots[str(source.resolve())]
    assert rec.backfill_complete is True
    assert len(rec.processed_paths) == 2


@pytest.mark.asyncio
async def test_backfill_dry_run_does_not_write_or_persist(tmp_path: Path) -> None:
    source = tmp_path / "src"
    _write_session(source, "a.md", "## Alfred Learnings\n\n- bullet.\n")

    cfg = _make_config(tmp_path)

    # Even if the extractor were called, dry_run should bail before it.
    async def fake_extract(**kwargs):
        raise AssertionError("extractor must not be called in dry run")

    with patch.object(bf, "v2_extract", side_effect=fake_extract):
        result = await bf.run_backfill(source, cfg, dry_run=True)

    assert result.dry_run is True
    assert result.eligible == 1
    assert result.extracted == 0

    # Vault dir still empty.
    learn_dir = Path(cfg.vault.path) / "assumption"
    assert not learn_dir.exists() or list(learn_dir.glob("*.md")) == []

    # State file not created.
    state_path = bf._backfill_state_path(cfg)
    assert not state_path.exists()


@pytest.mark.asyncio
async def test_backfill_re_run_is_no_op(tmp_path: Path) -> None:
    """After a successful pass, a re-run extracts nothing."""
    source = tmp_path / "src"
    _write_session(source, "a.md", "## Alfred Learnings\n\n- bullet.\n")
    cfg = _make_config(tmp_path)

    async def fake_extract(**kwargs):
        return _fake_extraction("First pass learning")

    with patch.object(bf, "v2_extract", side_effect=fake_extract):
        first = await bf.run_backfill(source, cfg, dry_run=False)
    assert first.extracted == 1

    call_count = 0

    async def fake_extract_count(**kwargs):
        nonlocal call_count
        call_count += 1
        return _fake_extraction("Should-not-call")

    # Second pass: no eligible (already processed).
    with patch.object(bf, "v2_extract", side_effect=fake_extract_count):
        second = await bf.run_backfill(source, cfg, dry_run=False)

    assert call_count == 0
    assert second.eligible == 0
    assert second.already_processed == 1
    assert second.extracted == 0


@pytest.mark.asyncio
async def test_backfill_isolates_per_source_extractor_errors(tmp_path: Path) -> None:
    """One bad source doesn't poison the rest of the batch."""
    source = tmp_path / "src"
    _write_session(source, "a-good.md",
        "## Alfred Learnings\n\n- pattern.\n"
    )
    _write_session(source, "b-bad.md",
        "## Alfred Learnings\n\n- another.\n"
    )
    cfg = _make_config(tmp_path)

    call_n = 0

    async def fake_extract(**kwargs):
        nonlocal call_n
        call_n += 1
        if call_n == 1:
            return _fake_extraction("Good learning")
        raise RuntimeError("simulated SDK error on second source")

    with patch.object(bf, "v2_extract", side_effect=fake_extract):
        result = await bf.run_backfill(source, cfg, dry_run=False)

    # Both eligible, one extracted, one error.
    assert result.eligible == 2
    assert result.extracted == 1
    assert result.errors == 1
    # The error path increments the counter but doesn't crash the run.


@pytest.mark.asyncio
async def test_backfill_isolates_per_source_parse_errors(tmp_path: Path) -> None:
    """Malformed YAML frontmatter on one source must not abort the batch.

    Regression for the 2026-04-29 KAL-LE backfill crash: one of Salem's
    session notes had bad YAML, ``frontmatter.loads`` raised a
    ``yaml.scanner.ScannerError``, and the per-source try/except (which
    only wrapped the extractor) didn't catch it — the whole asyncio.run
    aborted 11 sources in.
    """
    source = tmp_path / "src"
    source.mkdir(parents=True, exist_ok=True)

    # Three good files.
    _write_session(source, "good-1.md", "## Alfred Learnings\n\n- one.\n")
    _write_session(source, "good-2.md", "## Alfred Learnings\n\n- two.\n")
    _write_session(source, "good-3.md", "## Alfred Learnings\n\n- three.\n")

    # One bad-YAML file — triggers ``yaml.scanner.ScannerError`` in
    # ``frontmatter.loads``. The colon inside the unquoted value at
    # column 175 reproduces the original failure shape: a mapping value
    # appearing where YAML doesn't expect one.
    bad = source / "bad-yaml.md"
    bad.write_text(
        "---\n"
        "type: session\n"
        "name: bad\n"
        "tags: [a, b: this is not legal here, c]\n"  # mapping-in-flow-seq
        "---\n\n"
        "## Alfred Learnings\n\n- bullet.\n",
        encoding="utf-8",
    )

    cfg = _make_config(tmp_path)

    async def fake_extract(**kwargs):
        # One learning per call. Title disambiguates so writes don't collide.
        body = kwargs["source_body"]
        if "one" in body:
            return _fake_extraction("Learning one")
        if "two" in body:
            return _fake_extraction("Learning two")
        return _fake_extraction("Learning three")

    with patch.object(bf, "v2_extract", side_effect=fake_extract):
        result = await bf.run_backfill(source, cfg, dry_run=False)

    # Four eligible files (3 good + 1 bad). Three good ones extracted,
    # one bad one logged + counted as error. The bad file must NOT
    # crash the batch.
    assert result.eligible == 4
    assert result.extracted == 3
    assert result.errors == 1
    assert result.learnings_by_type.get("assumption") == 3

    # State persisted — bad source is marked processed so a re-run
    # doesn't retry it indefinitely. Operator must remediate the
    # source file (or delete the state) to retry.
    state = bf.load_backfill_state(cfg)
    rec = state.roots[str(source.resolve())]
    processed = set(rec.processed_paths)
    assert str(bad.resolve()) in processed
    # All three good files also recorded.
    assert len(processed) == 4
    # error_count snapshotted.
    assert rec.error_count == 1
    # backfill_complete stays False because errors > 0.
    assert rec.backfill_complete is False


@pytest.mark.asyncio
async def test_backfill_persists_state_after_each_source(tmp_path: Path) -> None:
    """State is flushed after every source so a mid-run crash preserves progress.

    Regression for the 2026-04-29 KAL-LE backfill crash: 102 records had
    been successfully written to the vault, but the state file didn't
    exist on disk after the crash because state was only persisted at
    end-of-run. Now: simulate a crash on the 3rd source and verify the
    state file on disk records the first two sources.
    """
    source = tmp_path / "src"
    source.mkdir(parents=True, exist_ok=True)
    f1 = _write_session(source, "first.md", "## Alfred Learnings\n\n- one.\n")
    f2 = _write_session(source, "second.md", "## Alfred Learnings\n\n- two.\n")
    f3 = _write_session(source, "third.md", "## Alfred Learnings\n\n- three.\n")

    cfg = _make_config(tmp_path)
    state_path = bf._backfill_state_path(cfg)

    call_n = 0

    async def fake_extract(**kwargs):
        nonlocal call_n
        call_n += 1
        if call_n == 1:
            return _fake_extraction("First learning")
        if call_n == 2:
            return _fake_extraction("Second learning")
        # Simulate a non-isolated crash on the third source.
        # We use the per-source-isolated path here (extractor error)
        # because the extractor try/except is the closest analogue
        # to a recoverable crash; the test asserts state was already
        # flushed for the first two sources BEFORE this point.
        raise RuntimeError("simulated crash on third source")

    with patch.object(bf, "v2_extract", side_effect=fake_extract):
        result = await bf.run_backfill(source, cfg, dry_run=False)

    # The third source's extractor error is isolated (existing behavior),
    # but the more important assertion is that state was flushed
    # incrementally — first two sources are on disk.
    assert state_path.exists()
    state = bf.load_backfill_state(cfg)
    rec = state.roots[str(source.resolve())]
    processed = set(rec.processed_paths)
    assert str(f1.resolve()) in processed
    assert str(f2.resolve()) in processed
    # Third source attempted (extractor was called) — recorded as
    # processed because the extractor-error branch marks it.
    assert str(f3.resolve()) in processed
    # error_count reflects the third-source failure.
    assert rec.error_count == 1
    # extracted_count reflects the two successes.
    assert rec.extracted_count == 2
    # Final result mirrors the persisted state.
    assert result.extracted == 2
    assert result.errors == 1


@pytest.mark.asyncio
async def test_backfill_state_persists_when_extractor_aborts_run(tmp_path: Path) -> None:
    """Even if a non-isolated exception bubbles out of run_backfill,
    state from successful sources before the crash is on disk.

    This is the strictest formulation of the cadence contract: simulate
    a crash that is NOT caught by the per-source try/except (e.g., one
    that fires inside ``write_learn_record`` was the original 2026-04-29
    failure mode wasn't actually — that one was the parser. But the
    cadence guarantee should hold for ANY crash, not just the ones we've
    seen). We use ``write_learn_record`` patched to raise ``BaseException``
    (which the per-record ``except Exception`` won't catch) on the third
    source.
    """
    source = tmp_path / "src"
    source.mkdir(parents=True, exist_ok=True)
    f1 = _write_session(source, "first.md", "## Alfred Learnings\n\n- one.\n")
    f2 = _write_session(source, "second.md", "## Alfred Learnings\n\n- two.\n")
    _write_session(source, "third.md", "## Alfred Learnings\n\n- three.\n")

    cfg = _make_config(tmp_path)
    state_path = bf._backfill_state_path(cfg)

    async def fake_extract(**kwargs):
        body = kwargs["source_body"]
        if "one" in body:
            return _fake_extraction("First learning")
        if "two" in body:
            return _fake_extraction("Second learning")
        return _fake_extraction("Third learning")

    call_n = 0
    real_writer = bf.write_learn_record

    def crashing_writer(**kwargs):
        nonlocal call_n
        call_n += 1
        if call_n <= 2:
            return real_writer(**kwargs)
        # Use BaseException so the per-record ``except Exception`` does
        # NOT catch this — simulating a hard abort on the third source.
        raise KeyboardInterrupt("simulated hard abort during write")

    with patch.object(bf, "v2_extract", side_effect=fake_extract):
        with patch.object(bf, "write_learn_record", side_effect=crashing_writer):
            with pytest.raises(KeyboardInterrupt):
                await bf.run_backfill(source, cfg, dry_run=False)

    # State file must exist with first two sources recorded — even
    # though run_backfill itself aborted. This is the cadence contract.
    assert state_path.exists()
    state = bf.load_backfill_state(cfg)
    rec = state.roots[str(source.resolve())]
    processed = set(rec.processed_paths)
    assert str(f1.resolve()) in processed
    assert str(f2.resolve()) in processed
    # Two successful extractions on disk.
    assert rec.extracted_count == 2


@pytest.mark.asyncio
async def test_backfill_does_not_modify_source_files(tmp_path: Path) -> None:
    """Source files must be byte-identical before and after backfill."""
    source = tmp_path / "src"
    f = _write_session(source, "a.md",
        "## Alfred Learnings\n\n- pattern.\n"
    )
    before = f.read_bytes()
    cfg = _make_config(tmp_path)

    async def fake_extract(**kwargs):
        return _fake_extraction("Test learning")

    with patch.object(bf, "v2_extract", side_effect=fake_extract):
        await bf.run_backfill(source, cfg, dry_run=False)

    after = f.read_bytes()
    assert before == after


@pytest.mark.asyncio
async def test_backfill_handles_extraction_with_zero_learnings(tmp_path: Path) -> None:
    """Eligible file → extractor returns []. Source still marked processed."""
    source = tmp_path / "src"
    _write_session(source, "a.md", "## Alfred Learnings\n\n- bullet.\n")
    cfg = _make_config(tmp_path)

    async def fake_extract(**kwargs):
        return ExtractionResult(learnings=[])

    with patch.object(bf, "v2_extract", side_effect=fake_extract):
        result = await bf.run_backfill(source, cfg, dry_run=False)

    assert result.eligible == 1
    assert result.extracted == 0
    assert result.errors == 0

    # Source path marked processed — re-runs are no-ops even when the
    # first pass yielded no learnings (don't re-spend the LLM cost).
    state = bf.load_backfill_state(cfg)
    rec = state.roots[str(source.resolve())]
    assert len(rec.processed_paths) == 1


# --- state schema-tolerance ------------------------------------------------


def test_load_backfill_state_filters_unknown_fields(tmp_path: Path) -> None:
    """Forward-compat: state file with extra fields loads cleanly."""
    cfg = _make_config(tmp_path)
    path = bf._backfill_state_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Synthetic state file with an unknown ``future_field`` key.
    path.write_text(
        '{"roots": {"/some/path": {"backfill_complete": true, '
        '"processed_paths": ["a.md"], "future_field": "ignored"}}}',
        encoding="utf-8",
    )
    state = bf.load_backfill_state(cfg)
    assert "/some/path" in state.roots
    rec = state.roots["/some/path"]
    assert rec.backfill_complete is True
    assert rec.processed_paths == ["a.md"]


def test_load_backfill_state_missing_file_is_empty(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    state = bf.load_backfill_state(cfg)
    assert state.roots == {}


def test_save_then_load_backfill_state_roundtrips(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    state = bf.BackfillState(roots={
        "/x": bf.BackfillRecord(
            backfill_complete=True,
            processed_paths=["a.md", "b.md"],
            last_run_at="2026-04-29T12:00:00+00:00",
            eligible_count=2,
            extracted_count=3,
            error_count=0,
        )
    })
    bf.save_backfill_state(cfg, state)
    out = bf.load_backfill_state(cfg)
    assert "/x" in out.roots
    assert out.roots["/x"].backfill_complete is True
    assert out.roots["/x"].processed_paths == ["a.md", "b.md"]
    assert out.roots["/x"].extracted_count == 3
