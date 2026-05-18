"""Tests for the Daily Sync reply dispatcher.

Covers:
- reply_targets_daily_sync matches when message_id is in persisted batch.
- reply_targets_daily_sync rejects when no batch persisted.
- handle_daily_sync_reply returns None when reply isn't aimed at us.
- all_ok reply writes one corpus row per item with confirmed tier.
- Per-item correction writes the right andrew_priority + note.
- Modifier resolution against the batch's classifier_priority.
- Unparseable fragments land in the result's unparsed list.
- Item-number out of range produces an error in the result.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from alfred.daily_sync.config import DailySyncConfig
from alfred.daily_sync.confidence import save_state
from alfred.daily_sync.corpus import iter_corrections
from alfred.daily_sync.reply_dispatch import (
    handle_daily_sync_reply,
    reply_targets_daily_sync,
)


def _config(tmp_path: Path) -> DailySyncConfig:
    cfg = DailySyncConfig(enabled=True, batch_size=5)
    cfg.corpus.path = str(tmp_path / "corpus.jsonl")
    cfg.state.path = str(tmp_path / "state.json")
    return cfg


def _seed_batch(cfg: DailySyncConfig, *, items: list[dict], message_ids: list[int]) -> None:
    """Persist a fake last_batch into the daily-sync state file."""
    save_state(cfg.state.path, {
        "last_batch": {
            "date": "2026-04-22",
            "items": items,
            "message_ids": message_ids,
        },
    })


def _item(num: int, *, priority: str, sender: str = "alice@example.com") -> dict:
    return {
        "item_number": num,
        "record_path": f"note/Item{num}.md",
        "classifier_priority": priority,
        "classifier_action_hint": None,
        "classifier_reason": f"reason {num}",
        "sender": sender,
        "subject": f"Subject {num}",
        "snippet": f"Snippet {num}",
    }


def test_reply_targets_matches_persisted_id(tmp_path: Path):
    cfg = _config(tmp_path)
    _seed_batch(cfg, items=[_item(1, priority="medium")], message_ids=[100, 101])
    assert reply_targets_daily_sync(cfg, 100) is True
    assert reply_targets_daily_sync(cfg, 101) is True
    assert reply_targets_daily_sync(cfg, 999) is False


def test_reply_targets_no_batch_returns_false(tmp_path: Path):
    cfg = _config(tmp_path)
    assert reply_targets_daily_sync(cfg, 100) is False


def test_handle_returns_none_when_not_a_match(tmp_path: Path):
    cfg = _config(tmp_path)
    _seed_batch(cfg, items=[_item(1, priority="medium")], message_ids=[100])
    result = handle_daily_sync_reply(cfg, parent_message_id=999, reply_text="✅")
    assert result is None


def test_handle_all_ok_writes_one_row_per_item(tmp_path: Path):
    cfg = _config(tmp_path)
    _seed_batch(cfg, items=[
        _item(1, priority="high"),
        _item(2, priority="medium"),
        _item(3, priority="low"),
    ], message_ids=[100])

    result = handle_daily_sync_reply(cfg, parent_message_id=100, reply_text="✅")
    assert result is not None
    assert result["all_ok"] is True
    assert result["confirmed_count"] == 3
    rows = list(iter_corrections(cfg.corpus.path))
    assert len(rows) == 3
    # andrew_priority echoes classifier_priority on all_ok
    for row in rows:
        assert row.andrew_priority == row.classifier_priority


def test_handle_per_item_modifier_resolution(tmp_path: Path):
    cfg = _config(tmp_path)
    _seed_batch(cfg, items=[
        _item(1, priority="medium"),
        _item(2, priority="high"),
    ], message_ids=[100])

    # "1 down" → medium goes to low; "2 down" → high goes to medium
    result = handle_daily_sync_reply(cfg, 100, "1 down, 2 down")
    assert result is not None
    assert result["confirmed_count"] == 2
    rows = list(iter_corrections(cfg.corpus.path))
    by_path = {r.record_path: r for r in rows}
    assert by_path["note/Item1.md"].andrew_priority == "low"
    assert by_path["note/Item2.md"].andrew_priority == "medium"


def test_handle_explicit_tier_with_note(tmp_path: Path):
    cfg = _config(tmp_path)
    _seed_batch(cfg, items=[_item(2, priority="medium")], message_ids=[100])

    result = handle_daily_sync_reply(
        cfg, 100, "2: actually high — Jamie was waiting"
    )
    assert result is not None
    assert result["confirmed_count"] == 1
    rows = list(iter_corrections(cfg.corpus.path))
    assert len(rows) == 1
    row = rows[0]
    assert row.andrew_priority == "high"
    assert "Jamie was waiting" in row.andrew_reason


def test_handle_item_number_out_of_range(tmp_path: Path):
    cfg = _config(tmp_path)
    _seed_batch(cfg, items=[_item(1, priority="medium")], message_ids=[100])

    result = handle_daily_sync_reply(cfg, 100, "5 down")
    assert result is not None
    assert result["confirmed_count"] == 0
    assert any("not in last batch" in u for u in result["unparsed"])


def test_handle_mixed_valid_and_invalid(tmp_path: Path):
    cfg = _config(tmp_path)
    _seed_batch(cfg, items=[
        _item(1, priority="medium"),
        _item(2, priority="high"),
    ], message_ids=[100])

    result = handle_daily_sync_reply(cfg, 100, "1 down, totally not a fragment")
    assert result is not None
    assert result["confirmed_count"] == 1
    assert result["unparsed"]
    rows = list(iter_corrections(cfg.corpus.path))
    assert len(rows) == 1
    assert rows[0].record_path == "note/Item1.md"


def test_handle_corpus_row_carries_metadata(tmp_path: Path):
    cfg = _config(tmp_path)
    items = [_item(1, priority="medium", sender="jamie@example.com")]
    _seed_batch(cfg, items=items, message_ids=[100])
    handle_daily_sync_reply(cfg, 100, "1 down")
    rows = list(iter_corrections(cfg.corpus.path))
    assert rows[0].sender == "jamie@example.com"
    assert rows[0].subject == "Subject 1"
    assert rows[0].snippet == "Snippet 1"
    assert rows[0].timestamp  # set


def test_handle_duplicate_verb_writes_via_tag(tmp_path: Path):
    # Stage 1 (2026-05-15) — when Andrew flags item N as a duplicate
    # of item M via ``N duplicate`` or ``N duplicate of M``, the
    # resulting corpus row carries ``via="duplicate-of-M"`` so future
    # few-shot rotation can detect the operator's "X is a duplicate
    # of Y" signal.
    cfg = _config(tmp_path)
    _seed_batch(cfg, items=[
        _item(4, priority="spam"),
        _item(5, priority="medium"),  # classifier got this one wrong
    ], message_ids=[100])

    result = handle_daily_sync_reply(cfg, 100, "4 ok, 5 duplicate")
    assert result is not None
    assert result["confirmed_count"] == 2
    rows = list(iter_corrections(cfg.corpus.path))
    by_path = {r.record_path: r for r in rows}
    # Item 4: ok confirms classifier_priority, no via tag.
    assert by_path["note/Item4.md"].andrew_priority == "spam"
    assert by_path["note/Item4.md"].via == ""
    # Item 5: duplicate-of-4 → andrew_priority inherits spam,
    # via tag set.
    assert by_path["note/Item5.md"].andrew_priority == "spam"
    assert by_path["note/Item5.md"].via == "duplicate-of-4"


def test_handle_duplicate_explicit_pointer_writes_via_tag(tmp_path: Path):
    # ``N duplicate of M`` explicit pointer form.
    cfg = _config(tmp_path)
    _seed_batch(cfg, items=[
        _item(1, priority="high"),
        _item(2, priority="low"),
        _item(3, priority="medium"),
    ], message_ids=[100])

    result = handle_daily_sync_reply(cfg, 100, "1 ok, 2 ok, 3 duplicate of 1")
    assert result is not None
    assert result["confirmed_count"] == 3
    rows = list(iter_corrections(cfg.corpus.path))
    by_path = {r.record_path: r for r in rows}
    # Item 3: explicit pointer to item 1 → inherit "high".
    assert by_path["note/Item3.md"].andrew_priority == "high"
    assert by_path["note/Item3.md"].via == "duplicate-of-1"


# -----------------------------------------------------------------------------
# Confirmation message framing — N (item count) vs M (corpus row count)
# regression tests for the 2026-05-18 friction-driven ship.
#
# Background: feb052c's email cluster fan-out is correct — when item N's
# subject is a cluster of K near-identical records, one correction
# legitimately fans out to K corpus rows. But the confirmation message
# was rendering M (rows written) where it should have rendered N (items
# the operator replied about), so 5 corrections that produced 6 corpus
# rows surfaced as "6 corrections" — misleading the operator's count.
#
# The fix splits the count into ``corrections_count`` (N) and
# ``confirmed_count``/``written_count`` (M); when M > N the message
# surfaces both with the sibling delta. When M == N (no cluster fan-out)
# the existing single-number format is preserved.
# -----------------------------------------------------------------------------


def _cluster_item(
    num: int,
    *,
    priority: str,
    cluster_record_paths: list[str],
    sender: str = "viewpoint@example.com",
) -> dict:
    """An email item with a c5 cluster — correction fans out to all paths."""
    base = _item(num, priority=priority, sender=sender)
    base["record_path"] = cluster_record_paths[0]
    base["cluster_record_paths"] = list(cluster_record_paths)
    return base


def test_handle_message_singleton_items_no_cluster_parenthetical(tmp_path: Path):
    """N == M case: 3 singleton items, 3 corrections → 3 corpus rows.

    Existing simple format must be preserved — no parenthetical, no
    sibling counter. Regression guard against the framing fix
    accidentally adding a parenthetical when there's no cluster fan-out.
    """
    cfg = _config(tmp_path)
    _seed_batch(cfg, items=[
        _item(1, priority="medium"),
        _item(2, priority="high"),
        _item(3, priority="low"),
    ], message_ids=[100])

    result = handle_daily_sync_reply(cfg, 100, "1 down, 2 down, 3 up")
    assert result is not None
    assert result["confirmed_count"] == 3
    assert result["corrections_count"] == 3
    msg = result["message"]
    # Per-item path → "Calibration: applied N correction(s)."
    assert "Calibration: applied 3 correction(s)." in msg
    # No parenthetical mentioning corpus rows / cluster siblings.
    assert "corpus rows" not in msg
    assert "cluster sibling" not in msg


def test_handle_message_cluster_expansion_5_emails_6_rows_1_sibling(tmp_path: Path):
    """The Queue #12 friction case: 5 emails corrected, item 1 cluster=2.

    Operator sent corrections for items 1-5. Item 1's subject was a
    ViewPoint listing cluster with 2 records (cluster_record_paths
    has 2 entries); the others are singletons. Fan-out writes
    6 corpus rows total. The confirmation message must say
    "5 correction(s) (6 corpus rows, including 1 cluster sibling(s))"
    so the operator's mental count matches.
    """
    cfg = _config(tmp_path)
    _seed_batch(cfg, items=[
        _cluster_item(
            1, priority="low",
            cluster_record_paths=[
                "note/ViewPointA.md",
                "note/ViewPointB.md",
            ],
        ),
        _item(2, priority="medium"),
        _item(3, priority="low"),
        _item(4, priority="medium"),
        _item(5, priority="high"),
    ], message_ids=[100])

    result = handle_daily_sync_reply(
        cfg, 100, "1 down, 2 down, 3 ok, 4 down, 5 down",
    )
    assert result is not None
    # M (corpus rows): 2 (item 1's cluster) + 1 + 1 + 1 + 1 = 6
    assert result["confirmed_count"] == 6
    # N (operator-visible items): 5
    assert result["corrections_count"] == 5
    rows = list(iter_corrections(cfg.corpus.path))
    assert len(rows) == 6
    msg = result["message"]
    # Headline asserts the 5-vs-6 framing fix.
    assert (
        "Calibration: applied 5 correction(s) "
        "(6 corpus rows, including 1 cluster sibling(s))." in msg
    )


def test_handle_message_all_ok_with_cluster_expansion_surfaces_siblings(tmp_path: Path):
    """All-ok (✅) path: same N-vs-M drift — same fix.

    When ✅ confirms a batch where any email item is a cluster, the
    confirmation "Calibration: confirmed all X item(s)" must surface
    the cluster fan-out so the operator doesn't miscount. Three email
    items, item 1 cluster=2 → N=3, M=4.
    """
    cfg = _config(tmp_path)
    _seed_batch(cfg, items=[
        _cluster_item(
            1, priority="medium",
            cluster_record_paths=[
                "note/ClusterA.md",
                "note/ClusterB.md",
            ],
        ),
        _item(2, priority="medium"),
        _item(3, priority="low"),
    ], message_ids=[100])

    result = handle_daily_sync_reply(cfg, 100, "✅")
    assert result is not None
    assert result["all_ok"] is True
    assert result["confirmed_count"] == 4  # M (corpus rows)
    assert result["corrections_count"] == 3  # N (items)
    msg = result["message"]
    assert (
        "Calibration: confirmed all 3 item(s) "
        "(4 corpus rows, including 1 cluster sibling(s))." in msg
    )


def test_handle_message_all_ok_no_cluster_keeps_simple_form(tmp_path: Path):
    """All-ok path, no cluster fan-out → simple "confirmed all N item(s)".

    Regression guard: the framing fix must NOT introduce a parenthetical
    in the M == N case.
    """
    cfg = _config(tmp_path)
    _seed_batch(cfg, items=[
        _item(1, priority="medium"),
        _item(2, priority="high"),
    ], message_ids=[100])

    result = handle_daily_sync_reply(cfg, 100, "✅")
    assert result is not None
    assert result["all_ok"] is True
    assert result["confirmed_count"] == 2
    assert result["corrections_count"] == 2
    msg = result["message"]
    assert "Calibration: confirmed all 2 item(s)." in msg
    assert "corpus rows" not in msg
    assert "cluster sibling" not in msg


def test_handle_message_larger_cluster_K_equals_2_siblings(tmp_path: Path):
    """K=2 siblings: item 1 has cluster of 3 records; one correction → 3 rows.

    Beyond the original friction case (K=1) — exercise K=2 to confirm
    the sibling-count math is correct.
    """
    cfg = _config(tmp_path)
    _seed_batch(cfg, items=[
        _cluster_item(
            1, priority="medium",
            cluster_record_paths=[
                "note/A.md",
                "note/B.md",
                "note/C.md",
            ],
        ),
        _item(2, priority="low"),
    ], message_ids=[100])

    result = handle_daily_sync_reply(cfg, 100, "1 down, 2 ok")
    assert result is not None
    assert result["confirmed_count"] == 4  # 3 (cluster) + 1
    assert result["corrections_count"] == 2  # N = 2 items
    msg = result["message"]
    assert (
        "Calibration: applied 2 correction(s) "
        "(4 corpus rows, including 2 cluster sibling(s))." in msg
    )
