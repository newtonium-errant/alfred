"""Tests for ``scripts/distiller_rebuild_compare.py`` — N-way mode.

Covers the Path C Phase 1 spike's 3-way comparison path
(2026-05-06). The pre-existing pairwise (shadow vs legacy) path
ships without test coverage; this file does NOT add backfill tests
for that path — its production use is closing, the spike is the
forward-looking consumer.

Coverage:

  * BackendRun:
      - latency_seconds reads .latency_seconds stamp file
      - returns None when stamp file absent / unreadable / malformed

  * diff_nway:
      - all-agree case (3 backends, same type, same conf, similar claims)
      - one-backend-missed case (None in per_backend for that name)
      - type-drift case (anth says assumption, 32B says decision,
        72B says assumption → type_agreement = 2/3)
      - empty backends → empty diff list
      - claim_similarity_pairs: only emitted for present-pair-with-claims
      - confidence_distribution: None for missing record OR record with
        no/invalid confidence

  * Aggregate functions:
      - _aggregate_count_match: drift vs FIRST backend; ±20% threshold
      - _aggregate_type_match: unanimous / majority / split bucketing
      - _aggregate_confidence_calibration: per-bucket pct rollup
      - _aggregate_claim_similarity: pairwise mean / median / below-0.7
      - _aggregate_latency: per-backend wall time

  * CLI:
      - _parse_backend_arg: NAME=PATH parsing + validation
      - --backend arg routes to N-way mode
      - Backwards compat: legacy --shadow-root / --vault-root still work
        when --backend is absent
      - Single --backend (solo-run) works without comparison

  * Format output:
      - format_md_pivot produces well-formed markdown with all sections
      - format_json_pivot returns parseable JSON with the expected keys
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Module loader — script lives outside src/, load by path
# ---------------------------------------------------------------------------


_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "distiller_rebuild_compare.py"


@pytest.fixture(scope="module")
def compare_mod():
    """Load the script as a module for direct API testing.

    The script wasn't designed as an importable module (it's a CLI
    tool with ``if __name__ == "__main__"``), but ``importlib`` can
    load it by path. The module insertion order matters — the script
    inserts ``REPO_ROOT/src`` into ``sys.path`` at import time so
    ``alfred.vault.schema`` resolves; tests rely on that side effect.
    """
    spec = importlib.util.spec_from_file_location(
        "_distiller_rebuild_compare", _SCRIPT_PATH,
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_distiller_rebuild_compare"] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_record(
    compare_mod,  # noqa: ANN001
    *,
    slug: str,
    record_type: str = "assumption",
    confidence: str = "high",
    claim: str = "the world is round",
    title: str | None = None,
    path: Path | None = None,
):
    """Build a Record dataclass instance directly, no disk I/O."""
    return compare_mod.Record(
        path=path or Path(f"/fake/{record_type}/{slug}.md"),
        slug=slug,
        title=title or slug,
        mtime=0.0,
        meta={
            "type": record_type,
            "confidence": confidence,
            "name": title or slug,
        },
        body="",
        record_type=record_type,
        claim=claim,
    )


def _make_run(
    compare_mod,  # noqa: ANN001
    *,
    name: str,
    records: list,  # list[Record]
    root: Path | None = None,
):
    """Build a BackendRun. Records keyed by slug.lower()."""
    return compare_mod.BackendRun(
        name=name,
        root=root or Path(f"/fake/backend/{name}"),
        records={r.slug.lower(): r for r in records},
    )


def _seed_record_on_disk(
    vault_root: Path, *,
    record_type: str,
    slug: str,
    confidence: str = "high",
    claim: str = "the world is round",
) -> Path:
    """Write a real .md file under ``vault_root/<type>/<slug>.md``."""
    type_dir = vault_root / record_type
    type_dir.mkdir(parents=True, exist_ok=True)
    path = type_dir / f"{slug}.md"
    path.write_text(
        f"---\n"
        f"type: {record_type}\n"
        f"name: {slug}\n"
        f"confidence: {confidence}\n"
        f"---\n"
        f"\n"
        f"## Claim\n"
        f"{claim}\n",
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------------------
# BackendRun
# ---------------------------------------------------------------------------


def test_backend_run_latency_reads_stamp_file(compare_mod, tmp_path):
    stamp = tmp_path / ".latency_seconds"
    stamp.write_text("123.45\n", encoding="utf-8")
    run = _make_run(compare_mod, name="anthropic", records=[], root=tmp_path)
    assert run.latency_seconds == 123.45


def test_backend_run_latency_none_when_stamp_absent(compare_mod, tmp_path):
    """No stamp → None. Spike harness writes the stamp; pre-existing
    output trees won't have it (graceful fallback)."""
    run = _make_run(compare_mod, name="anth", records=[], root=tmp_path)
    assert run.latency_seconds is None


def test_backend_run_latency_none_when_stamp_malformed(
    compare_mod, tmp_path,
):
    """Operator-corrupted stamp file → None (NOT crash). Defensive."""
    stamp = tmp_path / ".latency_seconds"
    stamp.write_text("not a number\n", encoding="utf-8")
    run = _make_run(compare_mod, name="anth", records=[], root=tmp_path)
    assert run.latency_seconds is None


def test_backend_run_total_records(compare_mod):
    run = _make_run(
        compare_mod, name="anth",
        records=[
            _make_record(compare_mod, slug="r1"),
            _make_record(compare_mod, slug="r2"),
        ],
    )
    assert run.total_records == 2


# ---------------------------------------------------------------------------
# diff_nway — headline scenarios
# ---------------------------------------------------------------------------


def test_diff_nway_all_agree(compare_mod):
    """3 backends produce same record (slug + type + confidence + claim).
    Type agreement should be 1.0; all 3 pairwise similarities above
    threshold."""
    runs = [
        _make_run(compare_mod, name="anthropic", records=[
            _make_record(compare_mod, slug="r1", claim="the sky is blue"),
        ]),
        _make_run(compare_mod, name="ollama-32b", records=[
            _make_record(compare_mod, slug="r1", claim="the sky is blue"),
        ]),
        _make_run(compare_mod, name="ollama-72b", records=[
            _make_record(compare_mod, slug="r1", claim="the sky is blue"),
        ]),
    ]
    diffs = compare_mod.diff_nway(runs)
    assert len(diffs) == 1
    diff = diffs[0]
    assert diff.slug == "r1"
    assert diff.type_agreement == 1.0
    # All 3 backends present.
    assert all(rec is not None for rec in diff.per_backend.values())
    # 3 pairwise similarity entries (3 choose 2).
    assert len(diff.claim_similarity_pairs) == 3
    # All similarities are 1.0 (identical claims).
    assert all(s == 1.0 for s in diff.claim_similarity_pairs.values())


def test_diff_nway_one_backend_missed(compare_mod):
    """3 backends, only 2 produced this record. The missing backend
    appears as None in per_backend; type_agreement based on present
    set (2/2 = 1.0 since both agree)."""
    runs = [
        _make_run(compare_mod, name="anthropic", records=[
            _make_record(compare_mod, slug="r1"),
        ]),
        _make_run(compare_mod, name="ollama-32b", records=[
            # Missed this slug entirely.
        ]),
        _make_run(compare_mod, name="ollama-72b", records=[
            _make_record(compare_mod, slug="r1"),
        ]),
    ]
    diffs = compare_mod.diff_nway(runs)
    assert len(diffs) == 1
    diff = diffs[0]
    assert diff.per_backend["anthropic"] is not None
    assert diff.per_backend["ollama-32b"] is None  # missed
    assert diff.per_backend["ollama-72b"] is not None
    # Both present backends agree → 1.0.
    assert diff.type_agreement == 1.0
    # Only one pair has both sides present (anth-72b).
    assert len(diff.claim_similarity_pairs) == 1


def test_diff_nway_type_drift_two_thirds_agreement(compare_mod):
    """3 backends emit different types: 2 say assumption, 1 says
    decision. type_agreement = 2/3 ≈ 0.667."""
    runs = [
        _make_run(compare_mod, name="anthropic", records=[
            _make_record(compare_mod, slug="r1", record_type="assumption"),
        ]),
        _make_run(compare_mod, name="ollama-32b", records=[
            _make_record(compare_mod, slug="r1", record_type="decision"),
        ]),
        _make_run(compare_mod, name="ollama-72b", records=[
            _make_record(compare_mod, slug="r1", record_type="assumption"),
        ]),
    ]
    diffs = compare_mod.diff_nway(runs)
    diff = diffs[0]
    assert diff.type_agreement == round(2 / 3, 3)


def test_diff_nway_split_three_ways(compare_mod):
    """Pathological case: 3 backends, 3 different types. Each gets 1
    vote; max-count is 1; agreement = 1/3."""
    runs = [
        _make_run(compare_mod, name="a", records=[
            _make_record(compare_mod, slug="r1", record_type="assumption"),
        ]),
        _make_run(compare_mod, name="b", records=[
            _make_record(compare_mod, slug="r1", record_type="decision"),
        ]),
        _make_run(compare_mod, name="c", records=[
            _make_record(compare_mod, slug="r1", record_type="constraint"),
        ]),
    ]
    diffs = compare_mod.diff_nway(runs)
    diff = diffs[0]
    assert diff.type_agreement == round(1 / 3, 3)


def test_diff_nway_empty_backends(compare_mod):
    """All backends produced zero records → empty diff list."""
    runs = [
        _make_run(compare_mod, name="anth", records=[]),
        _make_run(compare_mod, name="32b", records=[]),
    ]
    assert compare_mod.diff_nway(runs) == []


def test_diff_nway_union_of_slugs(compare_mod):
    """Each backend produced a different record. Diff has 3 rows
    (union), each with one present + others None."""
    runs = [
        _make_run(compare_mod, name="anth", records=[
            _make_record(compare_mod, slug="anth-only"),
        ]),
        _make_run(compare_mod, name="32b", records=[
            _make_record(compare_mod, slug="32b-only"),
        ]),
        _make_run(compare_mod, name="72b", records=[
            _make_record(compare_mod, slug="72b-only"),
        ]),
    ]
    diffs = compare_mod.diff_nway(runs)
    assert len(diffs) == 3
    slugs = {d.slug for d in diffs}
    assert slugs == {"anth-only", "32b-only", "72b-only"}


def test_diff_nway_confidence_distribution(compare_mod):
    """Per-record confidence_distribution captures each backend's
    confidence for that record. Missing record → None. No-confidence
    field → None."""
    runs = [
        _make_run(compare_mod, name="anth", records=[
            _make_record(compare_mod, slug="r1", confidence="high"),
        ]),
        _make_run(compare_mod, name="32b", records=[
            _make_record(compare_mod, slug="r1", confidence="medium"),
        ]),
        _make_run(compare_mod, name="72b", records=[
            # Missed this slug.
        ]),
    ]
    diffs = compare_mod.diff_nway(runs)
    diff = diffs[0]
    assert diff.confidence_distribution["anth"] == "high"
    assert diff.confidence_distribution["32b"] == "medium"
    assert diff.confidence_distribution["72b"] is None


# ---------------------------------------------------------------------------
# Aggregate functions
# ---------------------------------------------------------------------------


def test_aggregate_count_match_drift_below_threshold(compare_mod):
    """Backends within ±20% of baseline → drift_flagged=False."""
    runs = [
        _make_run(compare_mod, name="anth", records=[
            _make_record(compare_mod, slug=f"r{i}") for i in range(10)
        ]),
        _make_run(compare_mod, name="32b", records=[
            _make_record(compare_mod, slug=f"r{i}") for i in range(11)
        ]),  # +10% drift
    ]
    counts = compare_mod._aggregate_count_match(runs)
    assert counts["anth"]["total"] == 10
    assert counts["anth"]["drift_flagged"] is False  # baseline
    assert counts["32b"]["total"] == 11
    assert counts["32b"]["drift_flagged"] is False  # +10% < 20%


def test_aggregate_count_match_drift_above_threshold(compare_mod):
    """Backend >20% off baseline → drift_flagged=True."""
    runs = [
        _make_run(compare_mod, name="anth", records=[
            _make_record(compare_mod, slug=f"r{i}") for i in range(10)
        ]),
        _make_run(compare_mod, name="32b", records=[
            _make_record(compare_mod, slug=f"r{i}") for i in range(13)
        ]),  # +30% drift
    ]
    counts = compare_mod._aggregate_count_match(runs)
    assert counts["32b"]["drift_flagged"] is True


def test_aggregate_count_match_zero_baseline(compare_mod):
    """Defensive: zero records on baseline → no div-by-zero crash.
    Drift fraction degenerate but the harness must not raise."""
    runs = [
        _make_run(compare_mod, name="anth", records=[]),
        _make_run(compare_mod, name="32b", records=[
            _make_record(compare_mod, slug="r1"),
        ]),
    ]
    # Should not raise.
    counts = compare_mod._aggregate_count_match(runs)
    assert counts["anth"]["total"] == 0
    assert counts["32b"]["total"] == 1


def test_aggregate_type_match_unanimous_majority_split(compare_mod):
    """Three rows: one unanimous, one majority, one split."""
    runs = [
        _make_run(compare_mod, name="a", records=[
            _make_record(compare_mod, slug="unan", record_type="assumption"),
            _make_record(compare_mod, slug="maj", record_type="assumption"),
            _make_record(compare_mod, slug="split", record_type="assumption"),
        ]),
        _make_run(compare_mod, name="b", records=[
            _make_record(compare_mod, slug="unan", record_type="assumption"),
            _make_record(compare_mod, slug="maj", record_type="assumption"),
            _make_record(compare_mod, slug="split", record_type="decision"),
        ]),
        _make_run(compare_mod, name="c", records=[
            _make_record(compare_mod, slug="unan", record_type="assumption"),
            _make_record(compare_mod, slug="maj", record_type="decision"),
            _make_record(compare_mod, slug="split", record_type="constraint"),
        ]),
    ]
    diffs = compare_mod.diff_nway(runs)
    summary = compare_mod._aggregate_type_match(diffs, runs)
    assert summary["unanimous"] == 1
    assert summary["majority"] == 1
    assert summary["split"] == 1


def test_aggregate_confidence_calibration_per_bucket_pct(compare_mod):
    """Each backend's confidence distribution rolled up by bucket
    with percentages."""
    runs = [
        _make_run(compare_mod, name="anth", records=[
            _make_record(compare_mod, slug="r1", confidence="high"),
            _make_record(compare_mod, slug="r2", confidence="medium"),
            _make_record(compare_mod, slug="r3", confidence="low"),
            _make_record(compare_mod, slug="r4", confidence="high"),
        ]),
    ]
    diffs = compare_mod.diff_nway(runs)
    calib = compare_mod._aggregate_confidence_calibration(diffs, runs)
    anth = calib["anth"]
    assert anth["high"]["count"] == 2
    assert anth["medium"]["count"] == 1
    assert anth["low"]["count"] == 1
    assert anth["high"]["pct"] == 0.5
    assert anth["medium"]["pct"] == 0.25
    assert anth["low"]["pct"] == 0.25


def test_aggregate_claim_similarity_pairwise(compare_mod):
    """Pairwise mean / median / below-threshold across all rows for
    each backend pair."""
    runs = [
        _make_run(compare_mod, name="a", records=[
            _make_record(compare_mod, slug="r1", claim="alpha bravo charlie"),
            _make_record(compare_mod, slug="r2", claim="delta echo foxtrot"),
        ]),
        _make_run(compare_mod, name="b", records=[
            _make_record(compare_mod, slug="r1", claim="alpha bravo charlie"),
            _make_record(compare_mod, slug="r2", claim="totally unrelated text"),
        ]),
    ]
    diffs = compare_mod.diff_nway(runs)
    sim = compare_mod._aggregate_claim_similarity(diffs, runs)
    # Single pair "a__b" with 2 ratios.
    assert "a__b" in sim
    pair = sim["a__b"]
    assert pair["n_pairs"] == 2
    # r1 is identical (1.0); r2 is divergent.
    # Below-threshold count: r2 should be below 0.7.
    assert pair["below_threshold"] >= 1


def test_aggregate_claim_similarity_no_pairs_when_one_backend(
    compare_mod,
):
    """Single backend → no pair combinations → empty dict."""
    runs = [
        _make_run(compare_mod, name="solo", records=[
            _make_record(compare_mod, slug="r1"),
        ]),
    ]
    diffs = compare_mod.diff_nway(runs)
    sim = compare_mod._aggregate_claim_similarity(diffs, runs)
    assert sim == {}


def test_aggregate_latency(compare_mod, tmp_path):
    """Per-backend latency from stamp files; missing → None."""
    a_root = tmp_path / "a"
    b_root = tmp_path / "b"
    a_root.mkdir()
    b_root.mkdir()
    (a_root / ".latency_seconds").write_text("100.5\n", encoding="utf-8")
    # b_root has no stamp file → None.

    runs = [
        _make_run(compare_mod, name="a", records=[], root=a_root),
        _make_run(compare_mod, name="b", records=[], root=b_root),
    ]
    latency = compare_mod._aggregate_latency(runs)
    assert latency["a"] == 100.5
    assert latency["b"] is None


# ---------------------------------------------------------------------------
# CLI parsing
# ---------------------------------------------------------------------------


def test_parse_backend_arg_valid(compare_mod):
    name, path = compare_mod._parse_backend_arg("anthropic=/tmp/anth")
    assert name == "anthropic"
    assert path == Path("/tmp/anth")


def test_parse_backend_arg_strips_whitespace(compare_mod):
    name, path = compare_mod._parse_backend_arg("  ollama-72b  =  /tmp/72b  ")
    assert name == "ollama-72b"
    assert path == Path("/tmp/72b")


def test_parse_backend_arg_missing_equals(compare_mod):
    """Required NAME=PATH separator. argparse-friendly error."""
    import argparse
    with pytest.raises(argparse.ArgumentTypeError, match="no '='"):
        compare_mod._parse_backend_arg("anthropic-tmp")


def test_parse_backend_arg_empty_name(compare_mod):
    import argparse
    with pytest.raises(argparse.ArgumentTypeError, match="NAME must be non-empty"):
        compare_mod._parse_backend_arg("=/tmp/path")


def test_parse_backend_arg_empty_path(compare_mod):
    import argparse
    with pytest.raises(argparse.ArgumentTypeError, match="PATH must be non-empty"):
        compare_mod._parse_backend_arg("anthropic=")


# ---------------------------------------------------------------------------
# format_md_pivot — well-formed markdown
# ---------------------------------------------------------------------------


def test_format_md_pivot_includes_all_sections(compare_mod):
    """All five aggregate metrics + per-record pivot + disagreements
    section appear in the output."""
    runs = [
        _make_run(compare_mod, name="a", records=[
            _make_record(compare_mod, slug="r1", record_type="assumption"),
        ]),
        _make_run(compare_mod, name="b", records=[
            _make_record(compare_mod, slug="r1", record_type="assumption"),
        ]),
    ]
    diffs = compare_mod.diff_nway(runs)
    out = compare_mod.format_md_pivot(diffs, runs, ["assumption"])
    # Section headers.
    assert "# Distiller N-way comparison" in out
    assert "## Count match" in out
    assert "## Type match" in out
    assert "## Confidence calibration" in out
    assert "## Claim similarity" in out
    assert "## Latency" in out
    assert "## Per-record pivot" in out
    assert "## Disagreements" in out


def test_format_md_pivot_intentionally_left_blank_when_no_disagreements(
    compare_mod,
):
    """Per ``feedback_intentionally_left_blank.md``: when every record
    agrees, the disagreements section explicitly says so rather than
    being empty."""
    runs = [
        _make_run(compare_mod, name="a", records=[
            _make_record(compare_mod, slug="r1", record_type="assumption"),
        ]),
        _make_run(compare_mod, name="b", records=[
            _make_record(compare_mod, slug="r1", record_type="assumption"),
        ]),
    ]
    diffs = compare_mod.diff_nway(runs)
    out = compare_mod.format_md_pivot(diffs, runs, ["assumption"])
    assert "No disagreements to surface" in out


def test_format_md_pivot_surfaces_type_split(compare_mod):
    """A type-disagreement row appears in the disagreements section."""
    runs = [
        _make_run(compare_mod, name="a", records=[
            _make_record(compare_mod, slug="rsplit", record_type="assumption"),
        ]),
        _make_run(compare_mod, name="b", records=[
            _make_record(compare_mod, slug="rsplit", record_type="decision"),
        ]),
    ]
    diffs = compare_mod.diff_nway(runs)
    out = compare_mod.format_md_pivot(diffs, runs, ["assumption", "decision"])
    assert "Type splits" in out
    assert "rsplit" in out


# ---------------------------------------------------------------------------
# format_json_pivot — parseable JSON
# ---------------------------------------------------------------------------


def test_format_json_pivot_parseable(compare_mod):
    """JSON output is parseable + has the expected top-level keys."""
    runs = [
        _make_run(compare_mod, name="a", records=[
            _make_record(compare_mod, slug="r1"),
        ]),
        _make_run(compare_mod, name="b", records=[
            _make_record(compare_mod, slug="r1"),
        ]),
    ]
    diffs = compare_mod.diff_nway(runs)
    out = compare_mod.format_json_pivot(diffs, runs)
    parsed = json.loads(out)
    assert set(parsed.keys()) >= {
        "backends", "count_match", "type_match",
        "confidence_calibration", "claim_similarity", "latency", "rows",
    }
    assert parsed["backends"] == ["a", "b"]
    assert len(parsed["rows"]) == 1
    assert parsed["rows"][0]["slug"] == "r1"


def test_format_json_pivot_encodes_pair_keys_as_strings(compare_mod):
    """Pairwise keys are tuples in Python; JSON requires string keys.
    The encoder must convert ``(a, b)`` to ``"a__b"``."""
    runs = [
        _make_run(compare_mod, name="a", records=[
            _make_record(compare_mod, slug="r1", claim="x"),
        ]),
        _make_run(compare_mod, name="b", records=[
            _make_record(compare_mod, slug="r1", claim="x"),
        ]),
    ]
    diffs = compare_mod.diff_nway(runs)
    out = compare_mod.format_json_pivot(diffs, runs)
    parsed = json.loads(out)
    pairs = parsed["rows"][0]["claim_similarity_pairs"]
    # Tuple keys converted to "a__b".
    assert "a__b" in pairs


# ---------------------------------------------------------------------------
# CLI integration — end-to-end via main() with on-disk fixtures
# ---------------------------------------------------------------------------


def test_main_nway_mode_routes_via_backend_flag(
    compare_mod, tmp_path, capsys,
):
    """When --backend is passed, main() routes to the N-way path
    instead of pairwise. Output is the N-way pivot, not the pairwise
    AGREED/DISAGREED layout."""
    # Seed two backend trees with one assumption record each.
    anth = tmp_path / "anthropic"
    o32b = tmp_path / "ollama-32b"
    _seed_record_on_disk(anth, record_type="assumption", slug="r1")
    _seed_record_on_disk(o32b, record_type="assumption", slug="r1")

    rc = compare_mod.main([
        "--backend", f"anthropic={anth}",
        "--backend", f"ollama-32b={o32b}",
        "--type", "assumption",
        "--since", "999999",  # don't filter on mtime in tests
        "--format", "json",
    ])
    assert rc == 0
    captured = capsys.readouterr()
    parsed = json.loads(captured.out)
    assert parsed["backends"] == ["anthropic", "ollama-32b"]
    assert "count_match" in parsed


def test_main_nway_mode_solo_backend_works(
    compare_mod, tmp_path, capsys,
):
    """Single --backend → solo-run stats, no comparison. Edge case
    per the brief — operator runs the harness against ONE tree to
    inspect counts/distributions without comparing."""
    solo = tmp_path / "solo"
    _seed_record_on_disk(solo, record_type="assumption", slug="r1")
    _seed_record_on_disk(solo, record_type="assumption", slug="r2")

    rc = compare_mod.main([
        "--backend", f"solo={solo}",
        "--type", "assumption",
        "--since", "999999",
        "--format", "json",
    ])
    assert rc == 0
    captured = capsys.readouterr()
    parsed = json.loads(captured.out)
    assert parsed["backends"] == ["solo"]
    # No pairwise comparisons possible with a single backend.
    assert parsed["claim_similarity"] == {}
    # But count + calibration still surface.
    assert parsed["count_match"]["solo"]["total"] == 2


def test_main_nway_mode_missing_backend_root_returns_error(
    compare_mod, tmp_path, capsys,
):
    """Per the spike spec: NO silent skip. A bad --backend path → exit
    1 + clear error so the operator sees the broken backend."""
    real = tmp_path / "real"
    real.mkdir()
    rc = compare_mod.main([
        "--backend", f"real={real}",
        "--backend", f"missing={tmp_path / 'does_not_exist'}",
        "--type", "assumption",
        "--since", "999999",
    ])
    assert rc == 1
    captured = capsys.readouterr()
    assert "not a directory" in captured.err
    assert "missing" in captured.err


def test_main_pairwise_mode_still_works_when_backend_absent(
    compare_mod, tmp_path, capsys,
):
    """Backwards-compat: --shadow-root / --vault-root still drive the
    pairwise path when --backend isn't passed. Existing operators +
    CI workflows keep working unchanged."""
    shadow = tmp_path / "shadow"
    vault = tmp_path / "vault"
    _seed_record_on_disk(shadow, record_type="assumption", slug="r1")
    _seed_record_on_disk(vault, record_type="assumption", slug="r1")

    rc = compare_mod.main([
        "--shadow-root", str(shadow),
        "--vault-root", str(vault),
        "--type", "assumption",
        "--since", "999999",
        "--format", "json",
    ])
    assert rc == 0
    captured = capsys.readouterr()
    # Pairwise output shape — has "pairs" / "orphans_in_shadow" /
    # "orphans_in_legacy" keys, NOT the N-way "backends" / "rows" keys.
    parsed = json.loads(captured.out)
    assert "pairs" in parsed
    assert "orphans_in_shadow" in parsed
    assert "rows" not in parsed
    assert "backends" not in parsed
