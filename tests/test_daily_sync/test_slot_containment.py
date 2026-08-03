"""Arc #18 M1 — board-writer path containment (the ``/feed/act`` lanes).

The audit (2026-08-03) proved the board writers compose a vault path from
``evidence["path"]`` with no containment. The mitigation on record was
"non-client-reachable": ``POST /feed/act`` accepts only ``{id, action_id}``, so
no client string reaches the composition. That is TRUE of the HTTP boundary and
insufficient, because ``evidence["path"]`` is not always scan-derived:

    LLM tool arg  ->  tier_confirm persists it into vault/daily/<date>.md
                  ->  compute._curated_to_tier_entry interpolates
                      path=f"routine/{record}.md"
                  ->  feed_producer stamps it as evidence["path"]
                  ->  operator taps a normal-looking card
                  ->  Path(vault) / rel  ==  outside the vault

``test_chain_*`` below is the centerpiece: it drives that whole path with a
hostile record name and asserts the write is contained. Every assertion is
OUTPUT-BOUND — the outside file's bytes, not a call count — per
``feedback_identity_pin_output_binding``. A containment that merely "returns an
error" while the file changed would pass a call-count pin and fail these.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pytest
import structlog

from alfred.brief.feed_producer import slot_suggestion_feed_items
from alfred.daily_sync import action_router as arouter
from alfred.daily_sync.action_router import act
from alfred.daily_sync.config import DailySyncConfig
from alfred.feed import FeedStore
from alfred.feed.model import STATE_OPEN, FeedItem
from alfred.tier.daily_curation import DailyCuration, T1T2Entry, save_tier_curation
from alfred.tier.task_completion import (
    TASK_DONE_KIND_UNKNOWN_RECORD,
    mark_task_done,
)

NOW = datetime(2026, 7, 22, 13, 0, 0, tzinfo=timezone.utc)
TODAY = date(2026, 7, 22)
TODAY_ISO = "2026-07-22"

#: The canary the escape must never touch. Written OUTSIDE the vault, with a
#: ``.md`` suffix and ``type: task`` frontmatter so it satisfies every gate the
#: writers apply APART from containment — if containment is the only thing
#: standing between the board and this file, these pins prove it.
CANARY_BODY = "---\ntype: task\nstatus: todo\nname: Untouchable\n---\n\n# do not write here\n"


@pytest.fixture(autouse=True)
def _pin_router_today(monkeypatch):
    monkeypatch.setattr(arouter, "_today_for", lambda config: TODAY)


@pytest.fixture()
def vault(tmp_path: Path) -> Path:
    v = tmp_path / "vault"
    for d in ("routine", "daily", "task"):
        (v / d).mkdir(parents=True, exist_ok=True)
    return v


@pytest.fixture()
def canary(tmp_path: Path) -> Path:
    """An existing ``.md`` file OUTSIDE the vault, one level up."""
    p = tmp_path / "outside.md"
    p.write_text(CANARY_BODY, encoding="utf-8")
    return p


def _ds_config(tmp_path: Path) -> DailySyncConfig:
    cfg = DailySyncConfig(enabled=True)
    cfg.state.path = str(tmp_path / "state.json")
    cfg.schedule.timezone = "UTC"
    return cfg


def _routine_selfcare(vault: Path, *, item: str = "Meditate") -> Path:
    p = vault / "routine" / "Self Care.md"
    p.write_text(
        "---\ntype: routine\nstatus: active\nname: Self Care\n"
        "cadence:\n  type: daily\n"
        f"items:\n- text: {item}\n  priority: aspirational\n  self_care: true\n"
        "---\n\n# Self Care\n",
        encoding="utf-8",
    )
    return p


def _act(store: FeedStore, cfg: DailySyncConfig, vault: Path, fid: str, action: str) -> Any:
    return act(
        fid, action, feed_store=store, config=cfg, vault_path=vault,
        instance_name="salem", instance_scope="talker", raw_config=None,
    )


#: The record-name fragment that escapes the vault when ``compute`` interpolates
#: it as ``f"routine/{record}.md"``. TWO levels: one climbs out of ``routine/``
#: to the vault root, the second out of the vault itself. ``../outside`` would
#: land on ``<vault>/outside.md`` — still INSIDE, and correctly allowed; getting
#: this wrong is how a containment pin passes for the wrong reason.
HOSTILE_RECORD_PREFIX = "../.."


def _escapes(canary: Path) -> str:
    """The vault-relative spelling that actually reaches ``canary``."""
    return f"routine/{HOSTILE_RECORD_PREFIX}/{canary.name}"


def _denials(cap: list[dict], event: str) -> list[dict]:
    return [c for c in cap if c.get("event") == event]


# ---------------------------------------------------------------------------
# The chain pin — the audit's model -> vault -> evidence -> tap -> write path
# ---------------------------------------------------------------------------


def test_chain_hostile_curated_record_cannot_escape_on_done(
    vault: Path, canary: Path, tmp_path: Path,
) -> None:
    """THE arc pin. A hostile ``routine_record`` persisted into the daily file
    (as ``tier_confirm`` would from an LLM tool arg) is interpolated by the REAL
    producer into ``evidence["path"]`` and tapped DONE on the board.

    No mocking of the producer or the writer — the whole chain runs. The pin is
    the canary's BYTES.
    """
    hostile = f"{HOSTILE_RECORD_PREFIX}/{canary.stem}"  # -> "routine/../../outside.md"
    save_tier_curation(
        vault, TODAY,
        DailyCuration(t1=[T1T2Entry(
            routine_item={"record": hostile, "text": "Meditate"},
            source="operator",
        )]),
    )

    items = slot_suggestion_feed_items(vault, NOW, None, instance="salem") or []
    hostile_items = [i for i in items if ".." in str(i.evidence.get("path") or "")]
    assert hostile_items, (
        "precondition: the producer must actually interpolate the hostile record "
        f"into evidence.path — got {[i.evidence.get('path') for i in items]}"
    )
    item = hostile_items[0]
    assert item.evidence["path"] == f"routine/{hostile}.md"

    store = FeedStore(str(tmp_path / "feed.jsonl"))
    store.upsert(item)
    cfg = _ds_config(tmp_path)

    with structlog.testing.capture_logs() as cap:
        res = _act(store, cfg, vault, item.id, "done")

    assert res.ok is False
    assert canary.read_text(encoding="utf-8") == CANARY_BODY  # output-bound
    assert len(_denials(cap, "feed.act.slot.path_escape_denied")) == 1
    assert store.load()[item.id].state == STATE_OPEN  # not marked acted


def test_chain_leaves_no_tmp_or_lock_debris_outside_the_vault(
    vault: Path, canary: Path, tmp_path: Path,
) -> None:
    """Containment must fire BEFORE the lock: ``file_rmw_lock`` does
    ``lock_path.parent.mkdir(parents=True)`` and the writers write a ``.tmp``
    sidecar NEXT TO the target. A gate placed after the lock would leave
    ``outside.lock`` / ``outside.md.tmp`` debris even on a refusal."""
    hostile = f"{HOSTILE_RECORD_PREFIX}/{canary.stem}"
    save_tier_curation(
        vault, TODAY,
        DailyCuration(t1=[T1T2Entry(
            routine_item={"record": hostile, "text": "Meditate"}, source="operator",
        )]),
    )
    items = slot_suggestion_feed_items(vault, NOW, None, instance="salem") or []
    item = next(i for i in items if ".." in str(i.evidence.get("path") or ""))
    store = FeedStore(str(tmp_path / "feed.jsonl"))
    store.upsert(item)
    _act(store, _ds_config(tmp_path), vault, item.id, "done")

    # Assert on debris named after the CANARY specifically. A blanket
    # "no new .lock/.tmp under tmp_path" would also catch the harness's own
    # feed-store sidecar (``feed.lock``) and fail for the wrong reason.
    debris = [
        p.name for p in tmp_path.iterdir()
        if p.name.startswith(canary.stem) and p.name != canary.name
    ]
    assert debris == [], (
        f"containment ran too late — writer debris beside the canary: {debris}"
    )
    assert not (tmp_path / f"{canary.stem}.lock").exists()
    assert not (tmp_path / f"{canary.name}.tmp").exists()


# ---------------------------------------------------------------------------
# Per-lane escape pins (evidence tampered directly — a stale/poisoned store row)
# ---------------------------------------------------------------------------


def _tampered_routine_item(
    vault: Path, store: FeedStore, canary: Path, *, done: bool,
) -> FeedItem:
    """Publish a REAL routine slot item, then poison its stamped path — the
    shape a store row written by an earlier poisoned producer emit would have."""
    _routine_selfcare(vault)
    items = slot_suggestion_feed_items(vault, NOW, None, instance="salem") or []
    item = next(i for i in items if i.evidence.get("routine_record"))
    item.evidence["path"] = _escapes(canary)
    if done:
        item.evidence["done"] = True
    store.upsert(item)
    return item


def test_routine_done_refuses_escaping_evidence_path(
    vault: Path, canary: Path, tmp_path: Path,
) -> None:
    store = FeedStore(str(tmp_path / "feed.jsonl"))
    item = _tampered_routine_item(vault, store, canary, done=False)

    with structlog.testing.capture_logs() as cap:
        res = _act(store, _ds_config(tmp_path), vault, item.id, "done")

    assert res.ok is False
    assert canary.read_text(encoding="utf-8") == CANARY_BODY
    denials = _denials(cap, "feed.act.slot.path_escape_denied")
    assert len(denials) == 1
    assert denials[0]["lane"] == "routine"
    assert denials[0]["action"] == "done"
    assert store.load()[item.id].state == STATE_OPEN


def test_routine_undo_refuses_escaping_evidence_path(
    vault: Path, canary: Path, tmp_path: Path,
) -> None:
    """The undo lane composes the same way and needs its own gate — a pin on
    ``done`` alone would leave ``undo_done`` open."""
    store = FeedStore(str(tmp_path / "feed.jsonl"))
    item = _tampered_routine_item(vault, store, canary, done=True)

    with structlog.testing.capture_logs() as cap:
        res = _act(store, _ds_config(tmp_path), vault, item.id, "undo_done")

    assert res.ok is False
    assert canary.read_text(encoding="utf-8") == CANARY_BODY
    denials = _denials(cap, "feed.act.slot.path_escape_denied")
    assert len(denials) == 1
    assert denials[0]["lane"] == "routine"
    assert denials[0]["action"] == "undo_done"


# ---------------------------------------------------------------------------
# Task lane — gated inside the writer (it composes; the router only delegates)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "task_path",
    [
        "../outside.md",
        "task/../../outside.md",
        "routine/../../outside.md",
        "/etc/passwd",
        "..",
    ],
)
def test_mark_task_done_refuses_escaping_path(
    vault: Path, canary: Path, task_path: str,
) -> None:
    """``mark_task_done`` composes ``vault_path / task_path`` itself, so the
    gate lives in the writer. The canary is a REAL ``type: task`` record, so it
    clears the writer's type-gate — only containment stops the write."""
    with structlog.testing.capture_logs() as cap:
        res = mark_task_done(vault, task_path, TODAY_ISO)

    assert res.kind == TASK_DONE_KIND_UNKNOWN_RECORD
    assert res.changed is False
    assert canary.read_text(encoding="utf-8") == CANARY_BODY
    assert len(_denials(cap, "tier.task_done.path_escape_denied")) == 1


def test_mark_task_done_still_completes_a_real_in_vault_task(vault: Path) -> None:
    """The gate must not break the happy path — a normal relative task path
    still completes. (Guards against a containment fix that over-rejects.)"""
    (vault / "task" / "Interview.md").write_text(
        f"---\ntype: task\nstatus: todo\nname: Interview\ndue: {TODAY_ISO}\n---\n\n# Interview\n",
        encoding="utf-8",
    )
    res = mark_task_done(vault, "task/Interview.md", TODAY_ISO)
    assert res.kind == "success"
    assert res.changed is True


def test_mark_task_done_accepts_a_name_with_a_slash(vault: Path) -> None:
    """Blocklist-regression guard at the writer. Real vault records carry ``/``
    in their names (5 in the production vault), which creates a nested dir —
    still inside the vault, so it must WORK."""
    nested = vault / "task" / "Verify FedEx Duty"
    nested.mkdir(parents=True)
    (nested / "Tax Charge.md").write_text(
        "---\ntype: task\nstatus: todo\nname: Verify FedEx Duty/Tax Charge\n---\n\n# x\n",
        encoding="utf-8",
    )
    res = mark_task_done(vault, "task/Verify FedEx Duty/Tax Charge.md", TODAY_ISO)
    assert res.kind == "success"
    assert res.changed is True


# ---------------------------------------------------------------------------
# Non-regression — the ordinary board DONE still works end to end
# ---------------------------------------------------------------------------


def test_ordinary_routine_done_still_writes(vault: Path, tmp_path: Path) -> None:
    """The gate is transparent to legitimate traffic: a normal slot card still
    completes and flips the feed item. Without this, an over-broad containment
    fix would look green on every escape pin while breaking the product."""
    import frontmatter

    record = _routine_selfcare(vault)
    store = FeedStore(str(tmp_path / "feed.jsonl"))
    items = slot_suggestion_feed_items(vault, NOW, None, instance="salem") or []
    item = next(i for i in items if i.evidence.get("routine_record"))
    store.upsert(item)

    res = _act(store, _ds_config(tmp_path), vault, item.id, "done")

    assert res.ok is True
    log = dict(frontmatter.load(str(record)).metadata.get("completion_log") or {})
    assert log.get("Meditate") == [TODAY_ISO]
