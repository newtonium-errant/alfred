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
from alfred.telegram.captured_tasks_view import (
    CAPTURED_TASKS_VIEW_REL,
    regenerate_captured_tasks_view,
)

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


# ===========================================================================
# PY-D — the registry the close leaves behind
# ===========================================================================
#
# Closing the task and MAINTAINING THE VIEW OF IT are two writes, and only the
# first happened here. ``process/Captured Tasks.md`` had exactly one writer —
# capture time — so a task closed by the confirm resolver kept sitting under
# "Open" until some later, unrelated capture regenerated the file. That is the
# "25 open, 2 done" staleness: a view whose numbers disagree with the records
# it is a view OF, in the direction that makes finished work look outstanding.


def _view_text(vault: Path) -> str:
    return (vault / CAPTURED_TASKS_VIEW_REL).read_text(encoding="utf-8")


def _seed_view(vault: Path) -> None:
    """Put the view in the state capture leaves it in — the task listed OPEN."""
    regenerate_captured_tasks_view(vault)


async def test_confirm_moves_the_task_out_of_the_views_open_section(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE BUG, end to end through production code.

    Mutation that reds this: delete the ``regenerate_captured_tasks_view`` call
    from the confirm branch of ``_resolve_capture_close_correction``.
    """
    vault = _vault(tmp_path)
    cfg = _config(tmp_path)
    _patch_transport(monkeypatch)

    _seed_view(vault)
    # PRE-STATE, asserted rather than assumed: the view starts out listing it
    # open, which is what makes the post-state meaningful.
    before = _view_text(vault)
    assert "## Open" in before
    assert PROMISE in before.split("## Open")[1].split("## Closed")[0]

    fired = await _fire(cfg, vault)
    item_number = load_state(cfg.state.path)[
        "last_batch"]["capture_close_items"][0]["item_number"]
    result = handle_daily_sync_reply(
        cfg, 9001, f"{item_number} confirm", vault_path=vault,
    )
    assert result is not None and result["confirmed_count"] == 1
    assert _status(vault) == "done"

    after = _view_text(vault)
    assert "## Closed" in after
    # It is under Closed...
    assert PROMISE in after.split("## Closed")[1]
    # ...and NOT still under Open, which is the half that was broken.
    open_block = after.split("## Open")[1].split("## Closed")[0]
    assert PROMISE not in open_block
    assert "(none)" in open_block


async def test_a_view_write_fault_does_not_fail_the_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The belt, driven. Everything after the close is bookkeeping, and this
    function's rule is that bookkeeping must not turn a successful close into a
    reported failure — reporting an error over a task that IS done is the same
    species of lie as reporting success over one that is not."""
    vault = _vault(tmp_path)
    cfg = _config(tmp_path)
    _patch_transport(monkeypatch)
    _seed_view(vault)

    import alfred.telegram.captured_tasks_view as ctv

    def _boom(_vault_path):  # type: ignore[no-untyped-def]
        raise RuntimeError("view generator exploded")

    monkeypatch.setattr(ctv, "regenerate_captured_tasks_view", _boom)

    fired = await _fire(cfg, vault)
    item_number = load_state(cfg.state.path)[
        "last_batch"]["capture_close_items"][0]["item_number"]
    with structlog.testing.capture_logs() as captured:
        result = handle_daily_sync_reply(
            cfg, 9001, f"{item_number} confirm", vault_path=vault,
        )

    # The close stands and is reported as applied.
    assert result is not None
    assert result["confirmed_count"] == 1
    assert result["execution_errors"] == []
    assert _status(vault) == "done"
    # ...and the fault is LOUD, not swallowed.
    matches = _events(
        captured, "daily_sync.capture_close.captured_tasks_view_unhandled",
    )
    assert len(matches) == 1
    assert matches[0]["error_type"] == "RuntimeError"


async def test_reject_does_not_touch_the_view(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POSITIVE CONTROL for the pin above, in the opposite direction: a reject
    closes nothing, so the task must still be listed OPEN afterwards. Without
    this, "the view is regenerated" would be equally satisfied by a change that
    moved rows to Closed on any reply at all.

    Asserted on CONTENT, not on bytes. A byte-equality version of this test
    fails for a reason that has nothing to do with the property: ``_fire``
    legitimately reconciles the view (the daily belt), so ``generated_at``
    advances by a few milliseconds even though no row moved. That timestamp is
    the file honestly recording when it was rebuilt — comparing bytes would be
    pinning the clock, not the behaviour."""
    vault = _vault(tmp_path)
    cfg = _config(tmp_path)
    _patch_transport(monkeypatch)
    _seed_view(vault)

    fired = await _fire(cfg, vault)
    item_number = load_state(cfg.state.path)[
        "last_batch"]["capture_close_items"][0]["item_number"]
    handle_daily_sync_reply(
        cfg, 9001, f"{item_number} reject", vault_path=vault,
    )

    assert _status(vault) == "todo"
    after = _view_text(vault)
    assert PROMISE in after.split("## Open")[1]
    assert "## Closed" not in after


# --- the daily belt: any out-of-band close eventually reconciles -------------
#
# The confirm resolver is one writer among many — the operator editing the
# record, a slot ``done``, the janitor, the talker. The per-close regen cannot
# cover writers it does not know about, so the daily fire reconciles the view.


async def test_the_daily_fire_reconciles_a_view_gone_stale_out_of_band(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Close the task WITHOUT going through any capture-close path, then fire.

    Mutation that reds this: delete the reconcile block from ``fire_once``.
    """
    vault = _vault(tmp_path)
    cfg = _config(tmp_path)
    _patch_transport(monkeypatch)
    _seed_view(vault)
    assert PROMISE in _view_text(vault).split("## Open")[1]

    # An out-of-band close: rewrite the record directly, as an operator editing
    # the file in Obsidian would. Nothing tells the view.
    task = vault / TASK_REL
    task.write_text(
        task.read_text(encoding="utf-8").replace(
            "status: todo", "status: done",
        ),
        encoding="utf-8",
    )
    assert _status(vault) == "done"
    assert PROMISE in _view_text(vault).split("## Open")[1]  # still stale

    with structlog.testing.capture_logs() as captured:
        await _fire(cfg, vault)

    after = _view_text(vault)
    assert "## Closed" in after
    assert PROMISE in after.split("## Closed")[1]
    assert PROMISE not in after.split("## Open")[1].split("## Closed")[0]
    matches = _events(captured, "daily_sync.captured_tasks_view.reconciled")
    assert len(matches) == 1
    assert matches[0]["written"] is True


async def test_the_daily_fire_never_CREATES_the_view(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE GATE, and the reason it exists.

    Capture is what brings ``process/Captured Tasks.md`` into being. Without
    this gate a daily fire would mint the file — reading "(none)" — in EVERY
    instance's vault, including ones that have never captured anything and did
    not ask for it. The gate also respects a deliberate deletion: removing the
    view is a legible way to say you do not want it, and a daily job that put
    it back would be overruling the operator.

    Mutation that reds this: drop the ``if view_path.exists()`` guard.
    """
    vault = _vault(tmp_path)
    cfg = _config(tmp_path)
    _patch_transport(monkeypatch)
    view = vault / CAPTURED_TASKS_VIEW_REL
    assert not view.exists()  # premise, asserted

    await _fire(cfg, vault)

    assert not view.exists(), (
        "the daily fire created a Captured Tasks view in a vault that never "
        "had one — the gate is gone"
    )


async def test_a_reconcile_fault_does_not_sink_the_daily_fire(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Decoration must never take the Daily Sync down — the same belt the
    attribution sweep above it carries, for the same reason."""
    vault = _vault(tmp_path)
    cfg = _config(tmp_path)
    _patch_transport(monkeypatch)
    _seed_view(vault)

    import alfred.daily_sync.daemon as daemon_mod

    def _boom(_vault_path):  # type: ignore[no-untyped-def]
        raise RuntimeError("reconcile exploded")

    monkeypatch.setattr(daemon_mod, "regenerate_captured_tasks_view", _boom)

    with structlog.testing.capture_logs() as captured:
        fired = await _fire(cfg, vault)

    assert fired["ok"] is True  # the fire completed
    matches = _events(
        captured, "daily_sync.captured_tasks_view.reconcile_failed",
    )
    assert len(matches) == 1
    assert matches[0]["error_type"] == "RuntimeError"
