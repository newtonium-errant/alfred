"""#64 stage C — the propose-close loop through its REAL surfaces.

Separate from test_capture_close_proposals.py and test_capture_close_scan.py on
purpose. Those pin the matcher, the queue and the scan; every one of them stays
green if the daemon never persists the batch or the reply dispatcher never
routes the verb. A propose-then-approve feature is made of parts that only fail
TOGETHER at the seams:

    vault → scan → trigger → numbered item → PERSISTED BATCH → reply verb
          → task closed + corpus row

THE FAILURE THESE EXIST TO CATCH is the missed touchpoint. reply_dispatch is
3,500 lines and a new item kind has to be threaded through eighteen places; miss
one and the card renders while ``N confirm`` silently does nothing. The operator
sees "I confirmed it and it's still open" — accepted-then-ignored, and every
per-layer unit test still green.

So these drive ``fire_once`` (the real daemon entry) and
``handle_daily_sync_reply`` (the real dispatcher) rather than hand-writing the
state file. A hand-written ``last_batch`` would define the key on both sides of
the seam and pass against a daemon that never stashed anything.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import frontmatter
import pytest
import structlog

from alfred.daily_sync import assembler, capture_close_section
from alfred.daily_sync.capture_close_match import (
    VERDICT_CONFIRMED,
    VERDICT_REJECTED,
    iter_corpus,
)
from alfred.daily_sync.capture_close_proposals import (
    STATE_ACCEPTED,
    STATE_PENDING,
    STATE_REJECTED,
    cooldown_until,
    iter_proposals,
    list_pending,
)
from alfred.daily_sync.confidence import load_state
from alfred.daily_sync.config import DailySyncConfig
from alfred.daily_sync.daemon import fire_once
from alfred.daily_sync.reply_dispatch import handle_daily_sync_reply

TODAY = date(2026, 8, 11)
TASK_DAY = date(2026, 8, 9)
PROMISE = "Attach some screenshots of workout plans"
EVIDENCE = "Louka Workout Plan"
TASK_REL = "task/attach-screenshots.md"


@pytest.fixture(autouse=True)
def _clean_registry():
    assembler.clear_providers()
    capture_close_section.consume_last_batch()
    yield
    assembler.clear_providers()
    capture_close_section.consume_last_batch()


def _vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    (vault / "task").mkdir(parents=True)
    (vault / "note").mkdir(parents=True)
    (vault / TASK_REL).write_text(
        "---\n"
        "type: task\n"
        f'name: "{PROMISE}"\n'
        "status: todo\n"
        "created_by_capture: true\n"
        f"created: {TASK_DAY.isoformat()}\n"
        "---\n\nThe promise, as capture filed it.\n",
        encoding="utf-8",
    )
    (vault / "note" / f"{EVIDENCE}.md").write_text(
        "---\n"
        "type: note\n"
        f'name: "{EVIDENCE}"\n'
        f"created: {(TASK_DAY + timedelta(days=1)).isoformat()}\n"
        "---\n\nThe plan itself.\n",
        encoding="utf-8",
    )
    return vault


def _config(tmp_path: Path, **over: Any) -> DailySyncConfig:
    cfg = DailySyncConfig(enabled=True, batch_size=5)
    cfg.corpus.path = str(tmp_path / "email_corpus.jsonl")
    cfg.state.path = str(tmp_path / "state.json")
    cfg.attribution.corpus_path = str(tmp_path / "attr_corpus.jsonl")
    cc = cfg.capture_close
    cc.enabled = True
    cc.queue_path = str(tmp_path / "store" / "queue.jsonl")
    cc.corpus_path = str(tmp_path / "store" / "corpus.jsonl")
    cc.pending_path = str(tmp_path / "store" / "pending.jsonl")
    for k, v in over.items():
        setattr(cc, k, v)
    return cfg


def _patch_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_send_batch(
        user_id: int, chunks: list[str], *,
        dedupe_key: str | None = None, client_name: str | None = None,
    ) -> dict[str, Any]:
        return {"telegram_message_ids": [9001]}

    import alfred.transport.client as client_mod
    monkeypatch.setattr(client_mod, "send_outbound_batch", _fake_send_batch)


def _status(vault: Path, rel: str = TASK_REL) -> str:
    return str(frontmatter.load(str(vault / rel)).metadata.get("status"))


def _events(captured: list[dict], event: str) -> list[dict]:
    return [c for c in captured if c.get("event") == event]


async def _fire(cfg: DailySyncConfig, vault: Path) -> dict[str, Any]:
    return await fire_once(cfg, vault, user_id=42, today=TODAY)


# ===========================================================================
# The seam: fire_once must PERSIST the batch the dispatcher reads
# ===========================================================================

async def test_fire_once_persists_capture_close_items(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = _vault(tmp_path)
    cfg = _config(tmp_path)
    _patch_transport(monkeypatch)

    result = await _fire(cfg, vault)

    assert result["capture_close_items_count"] == 1
    items = load_state(cfg.state.path)["last_batch"]["capture_close_items"]
    assert len(items) == 1
    assert items[0]["task_path"] == TASK_REL
    assert items[0]["evidence_name"] == EVIDENCE
    assert isinstance(items[0]["item_number"], int)
    assert items[0]["item_number"] >= 1
    # The card was rendered into the body the operator actually receives.
    assert PROMISE in result["body"]


async def test_fire_once_omits_the_key_when_nothing_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = _vault(tmp_path)
    (vault / "note" / f"{EVIDENCE}.md").unlink()  # no evidence → no proposal
    cfg = _config(tmp_path)
    _patch_transport(monkeypatch)

    result = await _fire(cfg, vault)

    assert result["capture_close_items_count"] == 0
    batch = load_state(cfg.state.path).get("last_batch") or {}
    assert "capture_close_items" not in batch


# ===========================================================================
# THE ACCEPTANCE TEST — the whole path, both verdicts
# ===========================================================================

async def test_confirm_closes_the_task_and_writes_a_positive_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The operator case, end to end.

    A promise goes stale; the plan that fulfilled it arrives; the morning card
    asks; he says ``N confirm``; the task is DONE and the matcher has learned
    the pairing. Every step through production code.
    """
    vault = _vault(tmp_path)
    cfg = _config(tmp_path)
    _patch_transport(monkeypatch)

    fired = await _fire(cfg, vault)
    item_number = load_state(cfg.state.path)[
        "last_batch"]["capture_close_items"][0]["item_number"]
    assert _status(vault) == "todo"

    result = handle_daily_sync_reply(
        cfg, 9001, f"{item_number} confirm", vault_path=vault,
    )

    assert result is not None
    assert result["capture_close_count"] == 1
    assert result["confirmed_count"] == 1
    assert result["execution_errors"] == []
    # 1. THE TASK IS CLOSED — the thing he asked for.
    assert _status(vault) == "done"
    # 2. The corpus learned a POSITIVE pair — the correction signal.
    rows = iter_corpus(cfg.capture_close.corpus_path)
    assert len(rows) == 1
    assert rows[0].verdict == VERDICT_CONFIRMED
    assert rows[0].evidence_name == EVIDENCE
    assert rows[0].task_text == PROMISE
    # 3. The queue row is marked, so the question is not re-asked.
    proposals = iter_proposals(cfg.capture_close.queue_path)
    assert [p.state for p in proposals] == [STATE_ACCEPTED]
    assert proposals[0].resolved_at
    # 4. The operator is TOLD, by name rather than by number alone.
    assert PROMISE in result["message"]


async def test_reject_leaves_it_open_writes_negative_row_and_starts_cooldown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = _vault(tmp_path)
    cfg = _config(tmp_path)
    _patch_transport(monkeypatch)

    await _fire(cfg, vault)
    item_number = load_state(cfg.state.path)[
        "last_batch"]["capture_close_items"][0]["item_number"]

    result = handle_daily_sync_reply(
        cfg, 9001, f"{item_number} reject", vault_path=vault,
    )

    assert result is not None
    assert result["capture_close_count"] == 1
    # 1. The task stays open — a rejection is not a close.
    assert _status(vault) == "todo"
    # 2. A NEGATIVE row: this pair is now excluded from future matching.
    rows = iter_corpus(cfg.capture_close.corpus_path)
    assert len(rows) == 1
    assert rows[0].verdict == VERDICT_REJECTED
    # 3. The cooldown clock started, per task.
    proposals = iter_proposals(cfg.capture_close.queue_path)
    assert [p.state for p in proposals] == [STATE_REJECTED]
    until = cooldown_until(cfg.capture_close.queue_path, TASK_REL, 14)
    assert until is not None and until > datetime.now(timezone.utc)


async def test_a_rejected_pair_is_not_re_proposed_on_the_next_fire(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The learning loop closing: reject once, and tomorrow's pass is quiet.

    Two mechanisms both hold here — the glossary excludes the pair outright and
    the per-task cooldown suppresses the task — and that redundancy is the
    design, not an accident.
    """
    vault = _vault(tmp_path)
    cfg = _config(tmp_path)
    _patch_transport(monkeypatch)

    await _fire(cfg, vault)
    item_number = load_state(cfg.state.path)[
        "last_batch"]["capture_close_items"][0]["item_number"]
    handle_daily_sync_reply(cfg, 9001, f"{item_number} reject", vault_path=vault)

    second = await _fire(cfg, vault)

    assert second["capture_close_items_count"] == 0
    assert list_pending(cfg.capture_close.queue_path) == []


async def test_a_confirmed_task_is_not_asked_about_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = _vault(tmp_path)
    cfg = _config(tmp_path)
    _patch_transport(monkeypatch)

    await _fire(cfg, vault)
    item_number = load_state(cfg.state.path)[
        "last_batch"]["capture_close_items"][0]["item_number"]
    handle_daily_sync_reply(cfg, 9001, f"{item_number} confirm", vault_path=vault)

    second = await _fire(cfg, vault)

    assert second["capture_close_items_count"] == 0


# ===========================================================================
# The refusals — each must fail for its OWN reason, visibly
# ===========================================================================

async def test_a_bare_ack_never_closes_a_captured_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The evidence is a fuzzy match. A bare "ok" acknowledging a batch must
    never be read as agreeing that a promise was kept — a wrong close silently
    deletes work he still meant to do."""
    vault = _vault(tmp_path)
    cfg = _config(tmp_path)
    _patch_transport(monkeypatch)
    await _fire(cfg, vault)

    with structlog.testing.capture_logs() as captured:
        result = handle_daily_sync_reply(cfg, 9001, "ok", vault_path=vault)

    assert result is not None
    assert result["capture_close_count"] == 0
    assert _status(vault) == "todo"
    skipped = _events(captured, "daily_sync.capture_close.all_ok_skipped")
    assert len(skipped) == 1
    assert skipped[0]["count"] == 1
    # Still pending, so tomorrow's card asks again rather than losing it.
    assert len(list_pending(cfg.capture_close.queue_path)) == 1


async def test_a_pending_only_verb_is_refused_for_its_own_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``N noted`` is a pending-items verb. Refusing it must be distinguishable
    from refusing for any other cause — the logged reason is what separates the
    guard firing from an unrelated denial."""
    vault = _vault(tmp_path)
    cfg = _config(tmp_path)
    _patch_transport(monkeypatch)
    await _fire(cfg, vault)
    item_number = load_state(cfg.state.path)[
        "last_batch"]["capture_close_items"][0]["item_number"]

    with structlog.testing.capture_logs() as captured:
        result = handle_daily_sync_reply(
            cfg, 9001, f"{item_number} noted", vault_path=vault,
        )

    assert result is not None
    assert result["capture_close_count"] == 0
    assert _status(vault) == "todo"
    refusals = _events(
        captured, "daily_sync.capture_close.correction_refused")
    assert len(refusals) == 1
    assert refusals[0]["reason"] == "pending_only_verb"
    assert iter_corpus(cfg.capture_close.corpus_path) == []


async def test_a_failed_close_is_reported_not_swallowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the vault write fails, nothing else happens and he is TOLD. Reporting
    "closed" over a task still sitting todo is the exact lie this feature
    exists to stop telling."""
    vault = _vault(tmp_path)
    cfg = _config(tmp_path)
    _patch_transport(monkeypatch)
    await _fire(cfg, vault)
    item_number = load_state(cfg.state.path)[
        "last_batch"]["capture_close_items"][0]["item_number"]
    # The task vanishes between the card and the reply.
    (vault / TASK_REL).unlink()

    with structlog.testing.capture_logs() as captured:
        result = handle_daily_sync_reply(
            cfg, 9001, f"{item_number} confirm", vault_path=vault,
        )

    assert result is not None
    assert result["capture_close_count"] == 0
    assert result["execution_errors"], "the failure must reach the operator"
    assert len(_events(captured, "daily_sync.capture_close.close_failed")) == 1
    # No corpus row: nothing was learned from a verdict that did not apply.
    assert iter_corpus(cfg.capture_close.corpus_path) == []
    # The queue row is untouched, so the question survives to be re-asked.
    assert [p.state for p in iter_proposals(cfg.capture_close.queue_path)] == [
        STATE_PENDING]


async def test_disabled_instance_renders_and_routes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = _vault(tmp_path)
    cfg = _config(tmp_path, enabled=False)
    _patch_transport(monkeypatch)

    result = await _fire(cfg, vault)

    assert result["capture_close_items_count"] == 0
    assert PROMISE not in result["body"]
    assert not Path(cfg.capture_close.queue_path).exists()


# ===========================================================================
# Item numbering across sections
# ===========================================================================

async def test_the_item_number_the_card_shows_is_the_one_that_routes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The rendered number and the persisted ``item_number`` must be the same
    integer. If they drift, he answers the number he can see and the dispatcher
    resolves a different item — or none."""
    vault = _vault(tmp_path)
    cfg = _config(tmp_path)
    _patch_transport(monkeypatch)

    fired = await _fire(cfg, vault)
    item = load_state(cfg.state.path)["last_batch"]["capture_close_items"][0]

    assert f'{item["item_number"]}. Done with "{PROMISE}"?' in fired["body"]
