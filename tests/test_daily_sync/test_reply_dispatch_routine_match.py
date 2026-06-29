"""Self-correcting matcher Phase 2b — routine-match reply-routing pins.

Covers the operator-write half of the loop: a Daily Sync reply confirming /
rejecting a low-confidence routine match appends a verdict row to the learned
glossary corpus (the ONLY corpus-write path — the match/capture path writes the
pending sink only). Plus:
  - confirm → CORPUS_CONFIRM row (query_key + matched item).
  - reject → CORPUS_REJECT row.
  - modifier/tier on a routine-match item → unparsed (verb-mismatch hint).
  - all_ok ✅ confirms every routine-match item in one shot.
  - mixed reply: "1 down, 5 confirm" routes email + routine-match correctly.
  - no-silent-mutation guardrail: corpus unconfigured → execution error, no write.
  - round-trip CONFIRM: verdict is consultable by the matcher's glossary.
  - round-trip REJECT: the next cmd_done suppresses the false positive.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import frontmatter
import pytest
import structlog
import yaml

from alfred.daily_sync.config import DailySyncConfig, RoutineMatchConfig
from alfred.daily_sync.confidence import save_state
from alfred.daily_sync import reply_dispatch as rd
from alfred.daily_sync.reply_dispatch import handle_daily_sync_reply
from alfred.routine import match_calibration as mc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _config(tmp_path: Path) -> DailySyncConfig:
    cfg = DailySyncConfig(enabled=True, batch_size=5)
    cfg.corpus.path = str(tmp_path / "email_corpus.jsonl")
    cfg.state.path = str(tmp_path / "state.json")
    cfg.routine_match = RoutineMatchConfig(
        enabled=True, pending_path=str(tmp_path / "pending.jsonl"),
    )
    return cfg


def _routine_match_item(
    num: int,
    *,
    query: str,
    matched_to: str,
    record: str = "Daily",
    confidence: float = 0.4,
) -> dict:
    return {
        "item_number": num,
        "query": query,
        "matched_to": matched_to,
        "record": record,
        "confidence": confidence,
        "completion_date": "2026-06-28",
        "captured_at": "2026-06-28T09:00:00+00:00",
    }


def _email_item(num: int, *, priority: str) -> dict:
    return {
        "item_number": num,
        "record_path": f"note/Email{num}.md",
        "classifier_priority": priority,
        "classifier_action_hint": None,
        "classifier_reason": f"reason {num}",
        "sender": f"sender{num}@example.com",
        "subject": f"Subject {num}",
        "snippet": f"Snippet {num}",
    }


def _seed_state(
    cfg: DailySyncConfig,
    *,
    items: list[dict] | None = None,
    routine_match_items: list[dict] | None = None,
    message_ids: list[int] | None = None,
) -> None:
    payload: dict[str, Any] = {
        "date": "2026-06-28",
        "message_ids": message_ids or [100],
    }
    if items is not None:
        payload["items"] = items
    if routine_match_items is not None:
        payload["routine_match_items"] = routine_match_items
    save_state(cfg.state.path, {"last_batch": payload})


def _read_corpus(path: str | Path) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


@pytest.fixture
def corpus_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the dispatcher's corpus-path resolver at a tmp file.

    Mirrors test_proposal_merge.py's monkeypatch of
    ``_canonical_proposals_queue_path`` — avoids needing a real config.yaml
    on disk for the corpus-path round-trip.
    """
    path = tmp_path / "routine_match_corpus.jsonl"
    monkeypatch.setattr(
        rd, "_routine_match_corpus_path", lambda *a, **kw: str(path),
    )
    return path


# ---------------------------------------------------------------------------
# confirm / reject routing
# ---------------------------------------------------------------------------


def test_confirm_writes_corpus_confirm_row(tmp_path: Path, corpus_path: Path) -> None:
    cfg = _config(tmp_path)
    _seed_state(cfg, routine_match_items=[
        _routine_match_item(5, query="walk doggo", matched_to="Walk dog"),
    ])

    result = handle_daily_sync_reply(cfg, parent_message_id=100, reply_text="5 confirm")

    assert result is not None
    assert result["routine_match_count"] == 1
    assert result["email_count"] == 0
    rows = _read_corpus(corpus_path)
    assert len(rows) == 1
    assert rows[0]["type"] == mc.CORPUS_CONFIRM
    assert rows[0]["query_key"] == mc.query_key("walk doggo")
    assert rows[0]["item_text"] == "Walk dog"
    assert rows[0]["record"] == "Daily"


def test_confirm_emits_verdict_recorded_log(tmp_path: Path, corpus_path: Path) -> None:
    """Observability pin (feedback_log_emission_test_pattern): the corpus-write
    path emits ``daily_sync.routine_match.verdict_recorded`` with the verdict +
    query so the operator's grep workflow survives refactors."""
    cfg = _config(tmp_path)
    _seed_state(cfg, routine_match_items=[
        _routine_match_item(5, query="walk doggo", matched_to="Walk dog"),
    ])
    with structlog.testing.capture_logs() as cap:
        handle_daily_sync_reply(cfg, parent_message_id=100, reply_text="5 confirm")
    matches = [
        c for c in cap if c.get("event") == "daily_sync.routine_match.verdict_recorded"
    ]
    assert len(matches) == 1
    assert matches[0]["verdict"] == "confirm"
    assert matches[0]["query"] == "walk doggo"
    assert matches[0]["matched_to"] == "Walk dog"


def test_reject_writes_corpus_reject_row(tmp_path: Path, corpus_path: Path) -> None:
    cfg = _config(tmp_path)
    _seed_state(cfg, routine_match_items=[
        _routine_match_item(5, query="walk doggo", matched_to="Walk dog"),
    ])

    result = handle_daily_sync_reply(cfg, parent_message_id=100, reply_text="5 reject")

    assert result is not None
    assert result["routine_match_count"] == 1
    rows = _read_corpus(corpus_path)
    assert len(rows) == 1
    assert rows[0]["type"] == mc.CORPUS_REJECT
    assert rows[0]["query_key"] == mc.query_key("walk doggo")
    assert rows[0]["item_text"] == "Walk dog"


def test_modifier_on_routine_match_item_unparsed(
    tmp_path: Path, corpus_path: Path,
) -> None:
    """A tier/modifier verb ('down') makes no sense on a routine-match item →
    unparsed with the verb-mismatch hint, and NO corpus row."""
    cfg = _config(tmp_path)
    _seed_state(cfg, routine_match_items=[
        _routine_match_item(5, query="walk doggo", matched_to="Walk dog"),
    ])

    result = handle_daily_sync_reply(cfg, parent_message_id=100, reply_text="5 down")

    assert result is not None
    assert result["routine_match_count"] == 0
    assert any("routine matches only accept" in u for u in result["unparsed"])
    assert _read_corpus(corpus_path) == []


def test_all_ok_confirms_every_routine_match(tmp_path: Path, corpus_path: Path) -> None:
    cfg = _config(tmp_path)
    _seed_state(cfg, routine_match_items=[
        _routine_match_item(1, query="walk doggo", matched_to="Walk dog"),
        _routine_match_item(2, query="meds", matched_to="Take meds", record="Health"),
    ])

    result = handle_daily_sync_reply(cfg, parent_message_id=100, reply_text="✅")

    assert result is not None
    assert result["all_ok"] is True
    assert result["routine_match_count"] == 2
    rows = _read_corpus(corpus_path)
    assert len(rows) == 2
    assert all(r["type"] == mc.CORPUS_CONFIRM for r in rows)


def test_mixed_email_and_routine_match(tmp_path: Path, corpus_path: Path) -> None:
    """Same item-number space — dispatcher routes by which list claims N."""
    from alfred.daily_sync.corpus import iter_corrections

    cfg = _config(tmp_path)
    _seed_state(
        cfg,
        items=[_email_item(1, priority="medium")],
        routine_match_items=[
            _routine_match_item(5, query="walk doggo", matched_to="Walk dog"),
        ],
    )

    result = handle_daily_sync_reply(
        cfg, parent_message_id=100, reply_text="1 down, 5 confirm",
    )

    assert result is not None
    assert result["email_count"] == 1
    assert result["routine_match_count"] == 1
    email_rows = list(iter_corrections(cfg.corpus.path))
    assert len(email_rows) == 1 and email_rows[0].andrew_priority == "low"
    corpus_rows = _read_corpus(corpus_path)
    assert len(corpus_rows) == 1 and corpus_rows[0]["type"] == mc.CORPUS_CONFIRM


def test_corpus_unconfigured_is_execution_error_no_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """no-silent-mutation: if the corpus path can't be resolved, a confirm is
    bucketed as an execution failure rather than silently dropped — and nothing
    is written."""
    monkeypatch.setattr(rd, "_routine_match_corpus_path", lambda *a, **kw: None)
    cfg = _config(tmp_path)
    _seed_state(cfg, routine_match_items=[
        _routine_match_item(5, query="walk doggo", matched_to="Walk dog"),
    ])

    result = handle_daily_sync_reply(cfg, parent_message_id=100, reply_text="5 confirm")

    assert result is not None
    assert result["routine_match_count"] == 0
    assert any("corpus not configured" in e for e in result["execution_errors"])


# ---------------------------------------------------------------------------
# Round-trip — the full self-correcting loop (capture → confirm/reject → feedback)
# ---------------------------------------------------------------------------


def _routine_config(vault: Path, tmp_path: Path, corpus: Path):
    from alfred.routine.config import RoutineConfig

    rc = RoutineConfig(vault_path=str(vault), instance_name="salem")
    rc.state.path = str(tmp_path / "routine_state.json")
    rc.match_calibration.pending_path = str(tmp_path / "pending.jsonl")
    rc.match_calibration.corpus_path = str(corpus)
    return rc


def _write_routine(vault: Path, name: str, items: list[dict]) -> None:
    routine_dir = vault / "routine"
    routine_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "type": "routine", "name": name, "status": "active",
        "cadence": {"type": "daily"}, "items": items,
    }
    fm = yaml.dump(payload, default_flow_style=False, sort_keys=False)
    (routine_dir / f"{name}.md").write_text(f"---\n{fm}---\n\n# {name}\n", encoding="utf-8")


def test_roundtrip_confirm_is_consultable_by_matcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Confirm a captured low-conf match → the glossary the matcher loads now
    returns a confirm verdict for the pair (the operator-approved fast-path)."""
    from alfred.routine.cli import _matches_item, cmd_done

    vault = tmp_path / "vault"
    corpus = tmp_path / "corpus.jsonl"
    _write_routine(vault, "Daily", [{"text": "Walk the dog every morning before work"}])
    rcfg = _routine_config(vault, tmp_path, corpus)

    # Capture: a low-confidence vault-wide fuzzy match → pending row.
    assert cmd_done(rcfg, "", "walk dog", today_override="2026-06-28") == 0
    pending = mc.load_pending(rcfg.match_calibration.pending_path)
    assert len(pending) == 1

    # Operator confirms via the Daily Sync reply.
    monkeypatch.setattr(rd, "_routine_match_corpus_path", lambda *a, **kw: str(corpus))
    dcfg = _config(tmp_path)
    _seed_state(dcfg, routine_match_items=[
        _routine_match_item(
            1, query=pending[0].query, matched_to=pending[0].matched_to,
            record=pending[0].record, confidence=pending[0].confidence,
        ),
    ])
    result = handle_daily_sync_reply(dcfg, parent_message_id=100, reply_text="1 confirm")
    assert result["routine_match_count"] == 1

    # Feedback: the matcher's glossary now carries the confirm verdict.
    glossary = mc.load_glossary(rcfg.match_calibration.corpus_path)
    assert glossary.verdict(mc.query_key("walk dog"), "Walk the dog every morning before work") == "confirm"
    assert _matches_item("walk dog", "Walk the dog every morning before work", glossary) is True


def _no_match_item(
    num: int,
    *,
    query: str,
    matched_to: str,
    record: str = "Daily",
    confidence: float = 0.5,
) -> dict:
    item = _routine_match_item(
        num, query=query, matched_to=matched_to,
        record=record, confidence=confidence,
    )
    item["kind"] = mc.KIND_NO_MATCH
    return item


def test_no_match_confirm_writes_alias_row(tmp_path: Path, corpus_path: Path) -> None:
    """Phase 3: confirming a 'did you mean…' suggestion writes a CORPUS_ALIAS
    row (closes the false-negative), not a plain confirm."""
    cfg = _config(tmp_path)
    _seed_state(cfg, routine_match_items=[
        _no_match_item(5, query="feed the birds", matched_to="Feed the cat"),
    ])

    result = handle_daily_sync_reply(cfg, parent_message_id=100, reply_text="5 confirm")

    assert result["routine_match_count"] == 1
    rows = _read_corpus(corpus_path)
    assert len(rows) == 1
    assert rows[0]["type"] == mc.CORPUS_ALIAS
    assert rows[0]["query_key"] == mc.query_key("feed the birds")
    assert rows[0]["item_text"] == "Feed the cat"
    assert "aliased (now matches)" in result["message"]


def test_no_match_reject_writes_reject_row(tmp_path: Path, corpus_path: Path) -> None:
    """Rejecting a 'did you mean…' suggestion writes a CORPUS_REJECT row so the
    capture path doesn't re-ask it."""
    cfg = _config(tmp_path)
    _seed_state(cfg, routine_match_items=[
        _no_match_item(5, query="feed the birds", matched_to="Feed the cat"),
    ])

    result = handle_daily_sync_reply(cfg, parent_message_id=100, reply_text="5 reject")

    assert result["routine_match_count"] == 1
    rows = _read_corpus(corpus_path)
    assert len(rows) == 1
    assert rows[0]["type"] == mc.CORPUS_REJECT


def test_roundtrip_alias_matches_next_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Full false-negative loop: a completion that matched NOTHING is captured
    as a suggestion; the operator confirms the alias; the NEXT cmd_done now
    MATCHES that item."""
    from alfred.routine.cli import cmd_done

    vault = tmp_path / "vault"
    corpus = tmp_path / "corpus.jsonl"
    _write_routine(vault, "Daily", [{"text": "Feed the cat"}])
    rcfg = _routine_config(vault, tmp_path, corpus)

    # Capture: "feed the birds" matches nothing → no_match suggestion captured.
    assert cmd_done(rcfg, "", "feed the birds", today_override="2026-06-28") == 1
    pending = mc.load_pending(rcfg.match_calibration.pending_path)
    assert len(pending) == 1 and pending[0].kind == mc.KIND_NO_MATCH

    # Operator confirms the alias.
    monkeypatch.setattr(rd, "_routine_match_corpus_path", lambda *a, **kw: str(corpus))
    dcfg = _config(tmp_path)
    _seed_state(dcfg, routine_match_items=[
        _no_match_item(
            1, query=pending[0].query, matched_to=pending[0].matched_to,
            record=pending[0].record, confidence=pending[0].confidence,
        ),
    ])
    assert handle_daily_sync_reply(
        dcfg, parent_message_id=100, reply_text="1 confirm",
    )["routine_match_count"] == 1

    # Feedback: the same phrasing now MATCHES (exactly one) → success.
    assert cmd_done(rcfg, "", "feed the birds", today_override="2026-06-29") == 0
    post = frontmatter.load(str(vault / "routine" / "Daily.md"))
    log = post.metadata.get("completion_log") or {}
    assert "2026-06-29" in (log.get("Feed the cat") or [])


def test_roundtrip_no_match_reject_not_resurfaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject a 'did you mean…' suggestion → the next no-match completion does
    NOT re-capture it (recorded, not re-asked)."""
    from alfred.routine.cli import cmd_done

    vault = tmp_path / "vault"
    corpus = tmp_path / "corpus.jsonl"
    _write_routine(vault, "Daily", [{"text": "Feed the cat"}])
    rcfg = _routine_config(vault, tmp_path, corpus)

    assert cmd_done(rcfg, "", "feed the birds", today_override="2026-06-28") == 1
    pending = mc.load_pending(rcfg.match_calibration.pending_path)
    assert len(pending) == 1

    monkeypatch.setattr(rd, "_routine_match_corpus_path", lambda *a, **kw: str(corpus))
    dcfg = _config(tmp_path)
    _seed_state(dcfg, routine_match_items=[
        _no_match_item(
            1, query=pending[0].query, matched_to=pending[0].matched_to,
            record=pending[0].record, confidence=pending[0].confidence,
        ),
    ])
    assert handle_daily_sync_reply(
        dcfg, parent_message_id=100, reply_text="1 reject",
    )["routine_match_count"] == 1

    # Feedback: same completion no longer re-captured (still 1 pending row,
    # the original) AND still no match (reject ≠ alias).
    assert cmd_done(rcfg, "", "feed the birds", today_override="2026-06-29") == 1
    assert len(mc.load_pending(rcfg.match_calibration.pending_path)) == 1


def test_roundtrip_reject_suppresses_next_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject a captured low-conf match → the NEXT cmd_done no longer matches
    that item (the recurring false-positive is suppressed via the glossary)."""
    from alfred.routine.cli import DONE_KIND_UNKNOWN_ITEM, cmd_done

    vault = tmp_path / "vault"
    corpus = tmp_path / "corpus.jsonl"
    _write_routine(vault, "Daily", [{"text": "Walk the dog every morning before work"}])
    rcfg = _routine_config(vault, tmp_path, corpus)

    # Capture (low-conf match succeeds the first time).
    assert cmd_done(rcfg, "", "walk dog", today_override="2026-06-28") == 0
    pending = mc.load_pending(rcfg.match_calibration.pending_path)
    assert len(pending) == 1

    # Operator rejects it.
    monkeypatch.setattr(rd, "_routine_match_corpus_path", lambda *a, **kw: str(corpus))
    dcfg = _config(tmp_path)
    _seed_state(dcfg, routine_match_items=[
        _routine_match_item(
            1, query=pending[0].query, matched_to=pending[0].matched_to,
            record=pending[0].record, confidence=pending[0].confidence,
        ),
    ])
    assert handle_daily_sync_reply(
        dcfg, parent_message_id=100, reply_text="1 reject",
    )["routine_match_count"] == 1

    # Feedback: the same completion phrasing no longer matches → unknown_item.
    code = cmd_done(
        rcfg, "", "walk dog", today_override="2026-06-29", wants_json=True,
    )
    assert code == 1
    # the matcher consulted the glossary and suppressed the (now-rejected) pair.
    from alfred.routine.cli import _matches_item
    g = mc.load_glossary(corpus)
    assert _matches_item("walk dog", "Walk the dog every morning before work", g) is False
    _ = DONE_KIND_UNKNOWN_ITEM  # imported for intent; canary kind asserted via rc==1
