"""Batch data layer (#83): paths, manifest, ledger, render, seal.

The properties under test, in the order they matter:

  1. **Paths are config-derived and instance-scoped.** A cwd-relative
     or unscoped path would be shared across every instance on the box
     (the #53 shape). Blank instance is a hard error, not a default.
  2. **The ledger is append-only and content-addressed.** Idempotency
     is what makes a resumed batch cheap and a replayed image free.
  3. **The render is pure.** Same ledger, same body — which is what
     makes wholesale regeneration safe to re-run after a crash.
  4. **The seal guard is an allowlist of one.** Missing / unknown /
     sealed all refuse; only ``open`` regenerates.
"""

from __future__ import annotations

import json

import pytest

from alfred.batch.ledger import (
    OUTCOME_OK,
    OUTCOME_QUARANTINED,
    append_row,
    build_row,
    has_processed,
    load_rows,
    processed_item_ids,
)
from alfred.batch.manifest import (
    BatchImage,
    BatchManifest,
    load_manifest,
    save_manifest,
)
from alfred.batch.paths import (
    BatchPathError,
    batch_dir,
    batch_root,
    images_dir,
    ledger_path,
    manifest_path,
    validate_batch_id,
)
from alfred.batch.render import progress_counts, render_body
from alfred.batch.seal import (
    BATCH_STATUS_OPEN,
    BATCH_STATUS_SEALED,
    BatchSealedError,
    assert_regenerable,
    is_regenerable,
)

_RAW = {"logging": {"dir": "/tmp/does-not-need-to-exist/data"}}


def _manifest(n: int = 3) -> BatchManifest:
    return BatchManifest(
        batch_id="batch-20260811-aaaa",
        instruction="Reconcile these against the July statement.",
        created_at="2026-08-11T10:00:00+00:00",
        instance="Salem",
        images=[
            BatchImage(
                item_id=f"hash{i}",
                filename=f"scan-{i}.jpg",
                sha256=f"hash{i}" * 8,
                bytes=1000 + i,
                media_type="image/jpeg",
            )
            for i in range(n)
        ],
    )


# ---------------------------------------------------------------------------
# paths — config-derived, instance-scoped
# ---------------------------------------------------------------------------


def test_paths_derive_from_configured_data_dir() -> None:
    root = batch_root(_RAW, "Salem")
    assert str(root) == "/tmp/does-not-need-to-exist/data/batch/salem"


def test_paths_are_instance_scoped() -> None:
    """Two instances must never share a batch directory (#53)."""
    assert batch_root(_RAW, "Salem") != batch_root(_RAW, "KAL-LE")


def test_blank_instance_is_a_hard_error() -> None:
    """Fail loud at derivation, not quietly at write time."""
    with pytest.raises(BatchPathError, match="instance name"):
        batch_root(_RAW, "")


def test_instance_slug_normalises_spaces_and_case() -> None:
    assert str(batch_root(_RAW, "KAL LE")).endswith("/kal-le")


def test_batch_layout() -> None:
    d = batch_dir(_RAW, "Salem", "batch-1")
    assert manifest_path(_RAW, "Salem", "batch-1") == d / "manifest.json"
    assert ledger_path(_RAW, "Salem", "batch-1") == d / "ledger.jsonl"
    assert images_dir(_RAW, "Salem", "batch-1") == d / "images"


@pytest.mark.parametrize(
    "bad", ["../escape", "a/b", "", "   ", "-leading", "x" * 65, "with space"],
)
def test_bad_batch_ids_rejected(bad: str) -> None:
    """A batch id becomes a path segment — traversal must not be possible."""
    with pytest.raises(BatchPathError):
        validate_batch_id(bad)


@pytest.mark.parametrize("good", ["b1", "batch-20260811-abc123", "A_9"])
def test_good_batch_ids_accepted(good: str) -> None:
    assert validate_batch_id(good) == good


def test_paths_never_use_a_cwd_relative_literal(tmp_path) -> None:
    """The debris-guard property: nothing resolves under ``./data``."""
    raw = {"logging": {"dir": str(tmp_path)}}
    assert str(batch_root(raw, "Salem")).startswith(str(tmp_path))


# ---------------------------------------------------------------------------
# manifest
# ---------------------------------------------------------------------------


def test_manifest_round_trip(tmp_path) -> None:
    m = _manifest()
    p = tmp_path / "manifest.json"
    save_manifest(p, m)
    back = load_manifest(p)
    assert back is not None
    assert back.batch_id == m.batch_id
    assert back.instruction == m.instruction
    assert back.item_ids() == ["hash0", "hash1", "hash2"]


def test_manifest_save_is_atomic_leaving_no_tmp(tmp_path) -> None:
    p = tmp_path / "manifest.json"
    save_manifest(p, _manifest())
    assert [f.name for f in tmp_path.iterdir()] == ["manifest.json"]


def test_manifest_schema_tolerance(tmp_path) -> None:
    """A newer writer's extra fields must not crash an older loader."""
    p = tmp_path / "manifest.json"
    p.write_text(
        json.dumps({
            "batch_id": "b1",
            "instruction": "do it",
            "created_at": "now",
            "future_field": "ignored",
            "images": [{
                "item_id": "h",
                "filename": "f.jpg",
                "sha256": "s",
                "bytes": 1,
                "media_type": "image/jpeg",
                "future_image_field": "also ignored",
            }],
        }),
        encoding="utf-8",
    )
    m = load_manifest(p)
    assert m is not None
    assert m.item_ids() == ["h"]


def test_manifest_missing_returns_none(tmp_path) -> None:
    assert load_manifest(tmp_path / "nope.json") is None


def test_manifest_corrupt_returns_none_not_raise(tmp_path) -> None:
    """A campaign work-list must degrade, not take down the whole run."""
    p = tmp_path / "manifest.json"
    p.write_text("{not json", encoding="utf-8")
    assert load_manifest(p) is None


def test_manifest_image_lookup() -> None:
    m = _manifest()
    assert m.image_for("hash1").filename == "scan-1.jpg"
    assert m.image_for("nope") is None


# ---------------------------------------------------------------------------
# ledger — append-only, content-addressed
# ---------------------------------------------------------------------------


def test_ledger_appends_accumulate(tmp_path) -> None:
    p = tmp_path / "ledger.jsonl"
    append_row(p, build_row(
        item_id="h0", filename="a.jpg", outcome=OUTCOME_OK, result="one",
    ))
    append_row(p, build_row(
        item_id="h1", filename="b.jpg", outcome=OUTCOME_OK, result="two",
    ))
    rows = load_rows(p)
    assert [r["item_id"] for r in rows] == ["h0", "h1"]
    assert [r["result"] for r in rows] == ["one", "two"]


def test_ledger_is_the_idempotency_key(tmp_path) -> None:
    p = tmp_path / "ledger.jsonl"
    append_row(p, build_row(
        item_id="h0", filename="a.jpg", outcome=OUTCOME_OK, result="one",
    ))
    rows = load_rows(p)
    assert has_processed(rows, "h0") is True
    assert has_processed(rows, "h1") is False
    assert processed_item_ids(rows) == {"h0"}


def test_ledger_missing_file_is_empty_not_error(tmp_path) -> None:
    assert load_rows(tmp_path / "nope.jsonl") == []


def test_ledger_torn_tail_line_is_skipped_not_fatal(tmp_path) -> None:
    """A crash mid-append must not cost every completed result."""
    p = tmp_path / "ledger.jsonl"
    append_row(p, build_row(
        item_id="h0", filename="a.jpg", outcome=OUTCOME_OK, result="kept",
    ))
    with open(p, "a", encoding="utf-8") as f:
        f.write('{"item_id": "h1", "resu')  # torn write
    rows = load_rows(p)
    assert [r["item_id"] for r in rows] == ["h0"]
    assert rows[0]["result"] == "kept"


def test_ledger_row_without_item_id_is_skipped(tmp_path) -> None:
    p = tmp_path / "ledger.jsonl"
    with open(p, "w", encoding="utf-8") as f:
        f.write(json.dumps({"result": "orphan"}) + "\n")
    assert load_rows(p) == []


def test_ledger_creates_parent_tree(tmp_path) -> None:
    p = tmp_path / "deep" / "nested" / "ledger.jsonl"
    append_row(p, build_row(
        item_id="h", filename="a.jpg", outcome=OUTCOME_OK, result="x",
    ))
    assert load_rows(p)[0]["item_id"] == "h"


def test_build_row_records_outcome_and_timestamp() -> None:
    row = build_row(
        item_id="h", filename="a.jpg", outcome=OUTCOME_QUARANTINED,
        result="", note="too blurry", model="m",
    )
    assert row["outcome"] == OUTCOME_QUARANTINED
    assert row["note"] == "too blurry"
    assert row["model"] == "m"
    assert row["processed_at"]


# ---------------------------------------------------------------------------
# render — pure
# ---------------------------------------------------------------------------


def test_render_is_pure_same_inputs_same_output() -> None:
    m = _manifest()
    rows = [build_row(
        item_id="hash0", filename="scan-0.jpg", outcome=OUTCOME_OK,
        result="line one", when=None,
    )]
    # Drop the timestamp's influence by rendering twice with the SAME rows.
    assert render_body(m, rows) == render_body(m, rows)


def test_render_orders_by_manifest_not_ledger() -> None:
    """Rows land out of order across runs; the document must be stable."""
    m = _manifest()
    rows = [
        build_row(item_id="hash2", filename="scan-2.jpg",
                  outcome=OUTCOME_OK, result="third"),
        build_row(item_id="hash0", filename="scan-0.jpg",
                  outcome=OUTCOME_OK, result="first"),
    ]
    body = render_body(m, rows)
    assert body.index("scan-0.jpg") < body.index("scan-2.jpg")


def test_render_empty_ledger_says_so_explicitly() -> None:
    """Intentionally-left-blank: queued must not look like broken."""
    body = render_body(_manifest(), [])
    assert "No scans processed yet" in body
    assert "0 of 3 scans processed" in body


def test_render_lists_pending_images() -> None:
    m = _manifest()
    rows = [build_row(item_id="hash0", filename="scan-0.jpg",
                      outcome=OUTCOME_OK, result="done")]
    body = render_body(m, rows)
    assert "## Not yet processed" in body
    assert "- scan-1.jpg" in body
    assert "- scan-2.jpg" in body


def test_render_complete_batch_says_none_pending() -> None:
    m = _manifest(1)
    rows = [build_row(item_id="hash0", filename="scan-0.jpg",
                      outcome=OUTCOME_OK, result="done")]
    body = render_body(m, rows)
    assert "every submitted scan has been processed" in body


def test_render_flags_quarantined_scans() -> None:
    m = _manifest(1)
    rows = [build_row(
        item_id="hash0", filename="scan-0.jpg",
        outcome=OUTCOME_QUARANTINED, result="", note="too blurry to read",
    )]
    body = render_body(m, rows)
    assert "needs a look" in body
    assert "too blurry to read" in body


def test_render_carries_the_regeneration_warning() -> None:
    """The operator must be told edits here are transient."""
    body = render_body(_manifest(), [])
    assert "Machine-generated" in body
    assert "batch_status" in body


def test_render_includes_the_instruction() -> None:
    body = render_body(_manifest(), [])
    assert "Reconcile these against the July statement." in body


def test_progress_counts() -> None:
    m = _manifest(3)
    rows = [
        build_row(item_id="hash0", filename="a", outcome=OUTCOME_OK,
                  result="x"),
        build_row(item_id="hash1", filename="b",
                  outcome=OUTCOME_QUARANTINED, result=""),
    ]
    assert progress_counts(m, rows) == {"done": 2, "total": 3, "failed": 1}


# ---------------------------------------------------------------------------
# seal — allowlist of one, fail-closed
# ---------------------------------------------------------------------------


def test_open_record_is_regenerable() -> None:
    assert is_regenerable({"batch_status": BATCH_STATUS_OPEN}) is True
    assert_regenerable({"batch_status": BATCH_STATUS_OPEN}, batch_id="b1")


def test_sealed_record_refuses() -> None:
    with pytest.raises(BatchSealedError, match="SEALED"):
        assert_regenerable(
            {"batch_status": BATCH_STATUS_SEALED}, batch_id="b1",
        )


def test_missing_status_refuses() -> None:
    """Fail-CLOSED: an absent marker is not permission."""
    with pytest.raises(BatchSealedError, match=r"\(missing\)"):
        assert_regenerable({"title": "something"}, batch_id="b1")


def test_unknown_future_status_refuses() -> None:
    """Allowlist, not denylist — an unrecognised value must stop the write."""
    with pytest.raises(BatchSealedError, match="SEALED"):
        assert_regenerable({"batch_status": "archived"}, batch_id="b1")


def test_none_frontmatter_refuses() -> None:
    assert is_regenerable(None) is False
    with pytest.raises(BatchSealedError):
        assert_regenerable(None, batch_id="b1")


def test_seal_message_names_the_recovery_path() -> None:
    """A refusal the operator cannot act on is a dead end."""
    with pytest.raises(BatchSealedError) as ei:
        assert_regenerable({"batch_status": "sealed"}, batch_id="b1")
    msg = str(ei.value)
    assert "ledger still holds every result" in msg
    assert BATCH_STATUS_OPEN in msg


def test_seal_refusal_is_logged_with_reason() -> None:
    """Observability: the refusal must be greppable, with its cause."""
    import structlog

    with structlog.testing.capture_logs() as captured:
        with pytest.raises(BatchSealedError):
            assert_regenerable(
                {"batch_status": "sealed"}, batch_id="b1",
                record_path="note/Batch.md",
            )
    matches = [
        c for c in captured
        if c.get("event") == "batch.seal.regenerate_refused"
    ]
    assert len(matches) == 1
    assert matches[0]["batch_id"] == "b1"
    assert matches[0]["status"] == "sealed"
    assert matches[0]["record_path"] == "note/Batch.md"
