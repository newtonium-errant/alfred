"""The sort rotation's emit half — producer, daemon wiring, and the config gate.

The e2e-through-a-production-entry-point layer: cards are produced from a REAL
vault fixture through ``compute_today_view`` (the projection the board reads),
and the daemon path is driven through ``_emit_brief_feed`` — the same function
the brief fire calls — so a rotation that was never wired there could not stay
green here (the accepted-then-ignored trap this repo keeps closing).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import structlog

from alfred.brief import daemon as brief_daemon
from alfred.brief.config import BriefConfig, SortRotationConfig, load_from_unified
from alfred.feed import FeedConfig, FeedStore
from alfred.feed.model import KIND_SORT_SUGGESTION, STATE_DEFERRED, STATE_OPEN, STATE_RETIRED
from alfred.brief.feed_producer import sort_suggestion_feed_items
from alfred.tier.sort_proposal import (
    PROPOSE_RULE_DEFAULT,
    PROPOSE_RULE_LEARNED,
    corrections_path_for,
    record_ruling,
)
from alfred.tier.sort_rotation import DEFAULT_ROTATION_CAP

TODAY = date(2026, 8, 19)
NOW = datetime(2026, 8, 19, 6, 0, 0, tzinfo=timezone.utc)


# --- vault fixture ------------------------------------------------------------
# An UNDATED task reaches a tier lane only through operator curation (auto-T1 is
# deadline-driven), so the fixture writes the daily curation file the projection
# reads — the same production shape the board renders from.


def _vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    (vault / "task").mkdir(parents=True)
    (vault / "daily").mkdir(parents=True)
    return vault


def _task(vault: Path, name: str, extra_fm: str = "") -> None:
    (vault / "task" / f"{name}.md").write_text(
        f"---\ntype: task\nname: {name}\nstatus: todo\n{extra_fm}---\n\nBody.\n",
        encoding="utf-8",
    )


def _curate_t2(vault: Path, *names: str) -> None:
    # The REAL curation shape (mirrors tests/tier/test_sort_affordance.py's
    # helper) — the same production format the board's projection reads.
    rows = "".join(f"  - task: '[[task/{n}]]'\n    source: operator\n" for n in names)
    (vault / "daily" / f"{TODAY.isoformat()}.md").write_text(
        f"---\ntype: daily\ndate: '{TODAY.isoformat()}'\n"
        "tier_curation:\n  t1: []\n  t2:\n"
        f"{rows}"
        f"  t3: []\n  curated_at: '{TODAY.isoformat()}T07:00:00-03:00'\n"
        "---\n\n# daily\n",
        encoding="utf-8",
    )


def _items(vault: Path, *, tracked=None, cap=DEFAULT_ROTATION_CAP, corrections_path=None):
    return sort_suggestion_feed_items(
        vault, NOW, None, instance="salem",
        tracked=tracked or {}, cap=cap, corrections_path=corrections_path,
    )


# --- the producer -------------------------------------------------------------


def test_an_unsorted_curated_task_is_dealt_with_its_proposal(tmp_path: Path) -> None:
    """The operator's own 2026-08-19 shape: an undated curated task the
    classifier refused (rule 7). The card carries the writer's addressing
    fields AND the proposal triple — slot, rule, shape — stamped at deal time."""
    vault = _vault(tmp_path)
    _task(vault, "Fix shed door")
    _curate_t2(vault, "Fix shed door")

    items = _items(vault)

    assert items is not None and len(items) == 1
    card = items[0]
    assert card.kind == KIND_SORT_SUGGESTION
    assert card.id == f"{KIND_SORT_SUGGESTION}:task:task/Fix shed door.md"
    assert card.title == "Sort: Fix shed door"
    ev = card.evidence
    assert ev["origin"] == "task"
    assert ev["path"] == "task/Fix shed door.md"
    assert ev["slot"] == "unslotted"
    # The proposal: undated task → the default rule proposes Duty.
    assert ev["proposed_slot"] == "duty"
    assert ev["proposed_rule"] == PROPOSE_RULE_DEFAULT
    assert ev["proposal_shape"] == "task|due:n|t2"
    # Quiet kind — never rings the phone.
    assert (card.mode, card.attention) == ("fyi", "fyi")


def test_a_slotted_item_is_not_dealt_positive_control_in_same_scan(tmp_path: Path) -> None:
    """The exclusion pin WITH its positive control: the explicitly-slotted task
    produces no card while its unslotted sibling in the same projection does —
    so the zero is a filter, not a dead scan."""
    vault = _vault(tmp_path)
    _task(vault, "Sorted one", "slot: duty\n")
    _task(vault, "Unsorted one")
    _curate_t2(vault, "Sorted one", "Unsorted one")

    items = _items(vault)

    assert items is not None
    names = [i.evidence["name"] for i in items]
    assert names == ["Unsorted one"]


def test_a_dated_task_is_not_dealt_rule_six_already_answered(tmp_path: Path) -> None:
    """A dated task is Duty by classifier rule 6 — it never reaches the
    rotation. Same positive control shape."""
    vault = _vault(tmp_path)
    _task(vault, "Dated one", "due: 2026-08-21\n")
    _task(vault, "Undated one")
    _curate_t2(vault, "Dated one", "Undated one")

    items = _items(vault)

    assert items is not None
    assert [i.evidence["name"] for i in items] == ["Undated one"]


def test_the_cap_bounds_fresh_deals_and_a_deferred_card_rides_outside_it(tmp_path: Path) -> None:
    """The retirement trap, driven end to end: with cap=2 and one card already
    DEFERRED, the emit still contains the deferred card (else reconcile would
    retire it and destroy its window) plus two fresh ones."""
    vault = _vault(tmp_path)
    for n in ("Alpha", "Bravo", "Charlie", "Delta"):
        _task(vault, n)
    _curate_t2(vault, "Alpha", "Bravo", "Charlie", "Delta")

    deferred_id = f"{KIND_SORT_SUGGESTION}:task:task/Delta.md"
    items = _items(vault, tracked={deferred_id: STATE_DEFERRED}, cap=2)

    assert items is not None
    ids = [i.id for i in items]
    assert deferred_id in ids  # carried — outside the cap
    assert len(ids) == 3  # 2 visible + 1 held; Charlie is withheld


def test_empty_population_is_a_genuine_empty_list_not_none(tmp_path: Path) -> None:
    """Empty-fire-authoritative: a day with nothing unsorted returns [] so the
    reconcile can clear stale cards — the rotation's goal state."""
    vault = _vault(tmp_path)
    _task(vault, "Sorted one", "slot: rhythm\n")
    _curate_t2(vault, "Sorted one")
    assert _items(vault) == []


def test_a_learned_override_reaches_the_dealt_card(tmp_path: Path) -> None:
    """Part (2) of the standard, visible on the wire: three consistent fuel
    rulings on this shape flip the proposal the NEXT card carries."""
    vault = _vault(tmp_path)
    _task(vault, "Fix shed door")
    _curate_t2(vault, "Fix shed door")
    corrections = tmp_path / "sort_corrections.jsonl"
    for _ in range(3):
        record_ruling(corrections, shape="task|due:n|t2", proposed="duty", chosen="fuel")

    items = _items(vault, corrections_path=corrections)

    assert items is not None and len(items) == 1
    ev = items[0].evidence
    assert ev["proposed_slot"] == "fuel"
    assert ev["proposed_rule"] == PROPOSE_RULE_LEARNED


# --- the daemon wiring (driven through _emit_brief_feed, the production entry) --


def _cfg(tmp_path: Path, *, enabled: bool = True) -> tuple[BriefConfig, FeedStore]:
    store_path = tmp_path / "feed.jsonl"
    cfg = BriefConfig(vault_path=str(tmp_path / "vault"), instance_name="salem")
    cfg.feed = FeedConfig(enabled=True, store_path=str(store_path))
    cfg.sort_rotation = SortRotationConfig(enabled=enabled)
    return cfg, FeedStore(store_path)


def _emit(cfg: BriefConfig) -> list:
    with structlog.testing.capture_logs() as cap:
        brief_daemon._emit_brief_feed(cfg, [], TODAY, NOW)
    return cap


def test_the_brief_fire_deals_the_rotation(tmp_path: Path) -> None:
    """THE WIRING PIN — through ``_emit_brief_feed`` itself, with no sections at
    all, so the rotation demonstrably does not depend on any section extractor
    being configured. A card lands OPEN in the store."""
    vault = _vault(tmp_path)
    _task(vault, "Fix shed door")
    _curate_t2(vault, "Fix shed door")
    cfg, store = _cfg(tmp_path)

    _emit(cfg)

    stored = store.load()
    card = stored.get(f"{KIND_SORT_SUGGESTION}:task:task/Fix shed door.md")
    assert card is not None and card.state == STATE_OPEN
    assert card.evidence["proposed_slot"] == "duty"


def test_a_deferred_cards_window_survives_the_next_fire(tmp_path: Path) -> None:
    """The defer-window pin at the daemon layer: a card deferred TO A TIME
    stays deferred across a reconcile (held by ``_revival_suppressed``), its
    window intact — the emit carried it, so nothing retired it."""
    vault = _vault(tmp_path)
    _task(vault, "Fix shed door")
    _curate_t2(vault, "Fix shed door")
    cfg, store = _cfg(tmp_path)
    _emit(cfg)  # deal it
    card_id = f"{KIND_SORT_SUGGESTION}:task:task/Fix shed door.md"
    # DERIVED FROM WALL CLOCK, never a literal — and the clock named here is
    # the one that actually judges the window: defer revival is evaluated
    # against REAL time, not this file's fixture NOW (`FeedStore.reconcile`
    # passes `_now_iso()` into `_revival_suppressed` → `defer_window_open`;
    # store.py / model.py). The first draft hardcoded a future instant with a
    # comment naming the wrong clock ("beyond NOW"), which made this a dated
    # suite regression: the day real time crossed it, the reconcile would
    # rightly revive the card and this test would red over CORRECT production
    # behavior. Windows in wall-clock-predicate tests are derived, never
    # literal (gate rule, 2026-08-19).
    until = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()
    store.defer(card_id, until=until)

    _emit(cfg)  # the next morning's fire

    stored = store.load()[card_id]
    assert stored.state == STATE_DEFERRED
    assert stored.deferred_until == until


def test_a_next_render_defer_returns_on_the_next_fire(tmp_path: Path) -> None:
    """The other defer shape: no window means the very next fire IS the return
    — the card revives to open rather than being retired or stranded."""
    vault = _vault(tmp_path)
    _task(vault, "Fix shed door")
    _curate_t2(vault, "Fix shed door")
    cfg, store = _cfg(tmp_path)
    _emit(cfg)
    card_id = f"{KIND_SORT_SUGGESTION}:task:task/Fix shed door.md"
    store.defer(card_id, until=None)
    assert store.load()[card_id].state == STATE_DEFERRED

    _emit(cfg)

    assert store.load()[card_id].state == STATE_OPEN


def test_a_sorted_item_clears_from_the_store_on_the_next_fire(tmp_path: Path) -> None:
    """The lifecycle's happy ending: once the record carries a slot, the item
    leaves the population, and the next fire's authoritative-empty reconcile
    retires the card."""
    vault = _vault(tmp_path)
    _task(vault, "Fix shed door")
    _curate_t2(vault, "Fix shed door")
    cfg, store = _cfg(tmp_path)
    _emit(cfg)
    card_id = f"{KIND_SORT_SUGGESTION}:task:task/Fix shed door.md"
    assert store.load()[card_id].state == STATE_OPEN

    # The operator's ruling lands on the record (the writer's own path).
    p = tmp_path / "vault" / "task" / "Fix shed door.md"
    p.write_text(p.read_text(encoding="utf-8").replace("status: todo\n", "status: todo\nslot: fuel\n"), encoding="utf-8")

    _emit(cfg)

    assert store.load()[card_id].state == STATE_RETIRED


def test_disabled_rotation_says_so_and_touches_nothing(tmp_path: Path) -> None:
    """The config gate's ILB: off is a logged state, never a silence — and the
    store is untouched, so existing cards (and their windows) survive an
    operator turning the feature off."""
    vault = _vault(tmp_path)
    _task(vault, "Fix shed door")
    _curate_t2(vault, "Fix shed door")
    cfg, store = _cfg(tmp_path, enabled=False)

    cap = _emit(cfg)

    assert store.load() == {}
    lines = [c for c in cap if c.get("event") == "brief.sort_rotation_disabled"]
    assert len(lines) == 1 and lines[0]["instance"] == "salem"


def test_an_unreadable_store_skips_the_fire_rather_than_retiring(tmp_path: Path, monkeypatch) -> None:
    """A tracked-read failure must not reconcile: dealing over an unknown
    parked set would destroy exactly the windows the tracked read protects."""
    vault = _vault(tmp_path)
    _task(vault, "Fix shed door")
    _curate_t2(vault, "Fix shed door")
    cfg, store = _cfg(tmp_path)

    def _boom():
        raise OSError("disk gone")

    monkeypatch.setattr(FeedStore, "load", lambda self: _boom())
    cap = _emit(cfg)
    monkeypatch.undo()

    assert store.load() == {}  # nothing was written
    lines = [c for c in cap if c.get("event") == "brief.sort_rotation_store_unreadable"]
    assert len(lines) == 1


def test_the_correction_store_path_is_derived_from_the_feed_store(tmp_path: Path, monkeypatch) -> None:
    """The read side resolves the sidecar through the SAME helper the act
    router uses — asserted by observing the path the producer is actually
    handed, not by re-deriving it here."""
    vault = _vault(tmp_path)
    cfg, store = _cfg(tmp_path)
    seen: dict = {}

    import alfred.brief.daemon as daemon_mod

    def _spy(vault_path, now, tier_defaults, **kwargs):
        seen.update(kwargs)
        return []

    monkeypatch.setattr("alfred.brief.feed_producer.sort_suggestion_feed_items", _spy)
    _emit(cfg)

    assert seen["corrections_path"] == corrections_path_for(store.path)
    assert seen["cap"] == DEFAULT_ROTATION_CAP


# --- the config gate ----------------------------------------------------------


def test_sort_rotation_config_defaults_and_loader() -> None:
    """Missing block → enabled with the ratified cap; blank block → same;
    explicit values win; enabled=false loads off."""
    assert SortRotationConfig() == SortRotationConfig(enabled=True, cap=DEFAULT_ROTATION_CAP)

    absent = load_from_unified({"brief": {}})
    assert absent.sort_rotation.enabled is True
    assert absent.sort_rotation.cap == DEFAULT_ROTATION_CAP

    blank = load_from_unified({"brief": {"sort_rotation": None}})
    assert blank.sort_rotation.enabled is True

    explicit = load_from_unified(
        {"brief": {"sort_rotation": {"enabled": False, "cap": 5}}}
    )
    assert explicit.sort_rotation.enabled is False
    assert explicit.sort_rotation.cap == 5
