"""Tests for the ``pattern_surfaced`` act path — C4's operator-approval end.

``adopt`` is the only tap in the system that changes what the contact router
opens to. The pins that matter:

* **THE WRITER AND THE READER RESOLVE ONE PATH** — the dispatcher reads the
  store from the instance's own config file through the SAME
  ``resolve_contact_state_path`` the ``/day/*`` routes get theirs from. This
  test writes a real config file and asserts the tap lands in the file the web
  layer would have read — the read/write split that BLOCKed the board snooze at
  gate, pinned here before it can happen again.
* **AN UNWIRED INSTANCE REFUSES, IT DOES NOT PRETEND** — a tap that writes
  nowhere must not flip the card.
* **ADOPT TOUCHES ONLY THE CONTACT STORE** — no vault op, no preference-record
  edit. Adopting a routing habit must not be able to touch a record.
* **THE CEILING IS CLOSED AT TWO VERBS**, and the generic defer is excluded so
  the card carries exactly one set-aside mechanism.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import structlog
import yaml

from alfred.daily_sync.action_router import (
    DEFER_ACTIONS,
    DEFER_EXCLUDED_KINDS,
    FEED_ACTIONS,
    PATTERN_ADOPT,
    PATTERN_IGNORE,
    PATTERN_KIND,
    act,
    actions_for,
)
from alfred.daily_sync.config import DailySyncConfig
from alfred.feed.model import KIND_DEFAULTS, KINDS, MODE_DECIDE
from alfred.feed.store import FeedStore
from alfred.web.contact_patterns import SurfacedPattern, build_pattern_item
from alfred.web.contact_state import (
    CONTACT_STATE_FILENAME,
    RULE_DEFAULT,
    SURFACE_FEED,
    WebContactStore,
    resolve_contact_state_path,
)

USER = "u1"


def _pattern() -> SurfacedPattern:
    return SurfacedPattern(
        rule=RULE_DEFAULT, surface=SURFACE_FEED,
        overrides=3, observations=4, window_days=14,
    )


def _instance_config(tmp_path: Path, *, anchored: bool = True) -> Path:
    """A real instance config file — the dispatcher re-reads this, as production
    does, rather than being handed a path."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    raw: dict[str, Any] = {"web": {"enabled": True}}
    if anchored:
        raw["logging"] = {"dir": str(data_dir)}
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return p


def _cfg(config_path: Path) -> DailySyncConfig:
    cfg = DailySyncConfig(enabled=True, batch_size=5)
    cfg.config_path = str(config_path)
    return cfg


def _seed(tmp_path: Path) -> tuple[FeedStore, str]:
    store = FeedStore(str(tmp_path / "feed.jsonl"))
    item = build_pattern_item(_pattern(), instance="Salem", user_key=USER)
    store.upsert(item)
    return store, item.id


def _call(store: FeedStore, cfg: DailySyncConfig, feed_id: str, action_id: str):
    return act(
        feed_id, action_id,
        feed_store=store, config=cfg, vault_path=None,
        instance_name="salem", instance_scope="talker", raw_config=None,
    )


# ---------------------------------------------------------------------------
# The ceiling
# ---------------------------------------------------------------------------


class TestTheCeiling:
    def test_the_kind_is_registered_in_both_places(self):
        """``FeedItem.create`` silently degrades an unknown kind to fyi/fyi."""
        assert PATTERN_KIND in KINDS
        assert KIND_DEFAULTS[PATTERN_KIND][0] == MODE_DECIDE

    def test_exactly_two_verbs_are_served(self):
        verbs = [a["verb"] for a in actions_for(PATTERN_KIND)]
        assert verbs == [PATTERN_ADOPT, PATTERN_IGNORE]

    def test_the_deck_can_deal_it(self):
        """A decide card whose served verbs carry no affirm/reject gesture is
        never dealt at all — `isDeckDealt` drops it."""
        gestures = {a.get("gesture") for a in actions_for(PATTERN_KIND)}
        assert {"affirm", "reject"} <= gestures

    def test_adopt_is_heavy_and_declares_its_consequence(self):
        adopt = next(a for a in actions_for(PATTERN_KIND) if a["verb"] == PATTERN_ADOPT)
        assert adopt["weight"] == "heavy"
        assert adopt["note"]

    def test_the_generic_defer_is_excluded(self):
        """Two set-aside mechanisms on one card is the thing being avoided —
        and the detector consults only ``ignore``'s suppression."""
        assert PATTERN_KIND in DEFER_EXCLUDED_KINDS
        assert not any(v in FEED_ACTIONS[PATTERN_KIND] for v in DEFER_ACTIONS)


# ---------------------------------------------------------------------------
# adopt / ignore
# ---------------------------------------------------------------------------


class TestAdopt:
    def test_it_writes_the_default_where_the_web_layer_reads_it(self, tmp_path):
        """The read/write split, pinned: the path is resolved from the config
        file by the SAME helper the web config layer calls."""
        config_path = _instance_config(tmp_path)
        store, feed_id = _seed(tmp_path)

        res = _call(store, _cfg(config_path), feed_id, PATTERN_ADOPT)
        assert res.ok and res.status == "acted"

        # Resolve the path the way the WEB side does, from the same config.
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        web_side_path = resolve_contact_state_path(raw)
        assert web_side_path is not None
        assert Path(web_side_path).name == CONTACT_STATE_FILENAME

        contacts = WebContactStore.create(web_side_path)
        contacts.load()
        assert contacts.adopted_for(USER) == {RULE_DEFAULT: SURFACE_FEED}

    def test_it_flips_the_card_with_the_verb_recorded(self, tmp_path):
        store, feed_id = _seed(tmp_path)
        _call(store, _cfg(_instance_config(tmp_path)), feed_id, PATTERN_ADOPT)
        item = store.load()[feed_id]
        assert item.state == "acted"
        assert item.acted_action == PATTERN_ADOPT

    def test_it_suppresses_nothing(self, tmp_path):
        """Adopt and ignore are different acts — adopting must not also silence
        the pattern, or a later un-adopt would have nothing to say."""
        config_path = _instance_config(tmp_path)
        store, feed_id = _seed(tmp_path)
        _call(store, _cfg(config_path), feed_id, PATTERN_ADOPT)
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        contacts = WebContactStore.create(resolve_contact_state_path(raw))
        contacts.load()
        assert contacts.suppressed == {}


class TestIgnore:
    def test_it_suppresses_for_the_cards_own_window(self, tmp_path):
        config_path = _instance_config(tmp_path)
        store, feed_id = _seed(tmp_path)

        res = _call(store, _cfg(config_path), feed_id, PATTERN_IGNORE)
        assert res.ok
        assert "14 days" in res.detail

        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        contacts = WebContactStore.create(resolve_contact_state_path(raw))
        contacts.load()
        assert contacts.is_pattern_suppressed(
            USER, f"{RULE_DEFAULT}->{SURFACE_FEED}"
        )

    def test_it_adopts_nothing(self, tmp_path):
        config_path = _instance_config(tmp_path)
        store, feed_id = _seed(tmp_path)
        _call(store, _cfg(config_path), feed_id, PATTERN_IGNORE)
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        contacts = WebContactStore.create(resolve_contact_state_path(raw))
        contacts.load()
        assert contacts.adopted_for(USER) == {}

    def test_it_records_IGNORE_as_the_verb_not_merely_a_verb(self, tmp_path):
        """VERB FIDELITY, which the site pin above cannot reach.

        ``TestAdopt.test_it_flips_the_card_with_the_verb_recorded`` proves the
        stamp EXISTS — revert `action=action_id` in `_dispatch_contact_pattern`
        and it reds. What no pin covered is that the stamp is the verb the
        operator actually used: substituting the CONSTANT `PATTERN_ADOPT` for
        `action_id` there passes 1001 tests, so an `ignore` recorded as `adopt`
        was invisible. Both verbs flow through that one line, so only the second
        verb can catch a constant.

        Measured, not supposed — that substitution was run before this was
        written. Same reasoning as `test_a_different_verb_records_differently`
        on the `_act_locked` site; this is its missing sibling.
        """
        store, feed_id = _seed(tmp_path)
        _call(store, _cfg(_instance_config(tmp_path)), feed_id, PATTERN_IGNORE)
        item = store.load()[feed_id]
        assert item.state == "acted"
        assert item.acted_action == PATTERN_IGNORE
        assert item.acted_action != PATTERN_ADOPT


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


class TestRefusals:
    def test_an_unmapped_verb_is_invalid_action(self, tmp_path):
        store, feed_id = _seed(tmp_path)
        res = _call(store, _cfg(_instance_config(tmp_path)), feed_id, "confirm")
        assert not res.ok
        assert res.status == "invalid_action"
        assert store.load()[feed_id].state == "open"

    def test_an_unwired_instance_refuses_and_leaves_the_card_open(self, tmp_path):
        """A tap that writes nowhere must not flip the card."""
        config_path = _instance_config(tmp_path, anchored=False)
        store, feed_id = _seed(tmp_path)
        with structlog.testing.capture_logs() as captured:
            res = _call(store, _cfg(config_path), feed_id, PATTERN_ADOPT)
        assert not res.ok
        assert store.load()[feed_id].state == "open"
        assert any(c["event"] == "contact_router.act_not_wired" for c in captured)

    def test_a_card_missing_its_routing_key_is_refused(self, tmp_path):
        """An older card (or a hand-edited store) with no ``user_key`` must not
        be written to a guessed operator."""
        store = FeedStore(str(tmp_path / "feed.jsonl"))
        item = build_pattern_item(_pattern(), instance="Salem", user_key=USER)
        item.source_ref = {"producer": "contact_router"}  # key stripped
        store.upsert(item)
        with structlog.testing.capture_logs() as captured:
            res = _call(store, _cfg(_instance_config(tmp_path)), item.id, PATTERN_ADOPT)
        assert not res.ok
        assert store.load()[item.id].state == "open"
        denials = [
            c for c in captured if c["event"] == "contact_router.act_incomplete_card"
        ]
        assert len(denials) == 1
        assert denials[0]["has_user_key"] is False

    def test_an_already_acted_card_is_not_re_applied(self, tmp_path):
        config_path = _instance_config(tmp_path)
        store, feed_id = _seed(tmp_path)
        _call(store, _cfg(config_path), feed_id, PATTERN_ADOPT)
        res = _call(store, _cfg(config_path), feed_id, PATTERN_IGNORE)
        assert res.status == "already_acted"


class TestIsolation:
    def test_the_act_path_never_touches_the_vault(self, tmp_path):
        """`preference` is canonical-scope only; a talker-scope write would be
        gate-refused anyway. The pin is that it is never attempted."""
        vault = tmp_path / "vault"
        (vault / "preference").mkdir(parents=True)
        before = sorted(p.name for p in (vault / "preference").iterdir())

        store, feed_id = _seed(tmp_path)
        act(
            feed_id, PATTERN_ADOPT,
            feed_store=store, config=_cfg(_instance_config(tmp_path)),
            vault_path=vault, instance_name="salem", instance_scope="talker",
            raw_config=None,
        )
        assert sorted(p.name for p in (vault / "preference").iterdir()) == before
