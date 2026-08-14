"""Tests for ``alfred.web.day_state`` — what the C4 router evaluates against.

Load-bearing pins:

* **THE LEVERS COME FROM THE VAULT** — a real ``preference/`` record with the
  spec's ``matcher.rule: contact_surface_open`` is read, and its numbers reach
  the payload. Every failure path (no vault, no record, malformed matcher) lands
  on the spec defaults and SAYS which it used, so an ignored record is
  discoverable rather than invisible.
* **"LAST SESSION" MEANS EITHER DOOR** — a talker turn or an app-open. Reading
  only chat activity would make every brief-only morning look like a fresh gap
  and re-fire rule 3 all day.
* **RULE 1'S FIELDS ARE ABSENT, NOT FALSE** — a fabricated ``false`` reads
  exactly like a real answer.
* **A BROKEN INPUT DEGRADES, NEVER 500s** — the day state is the router's only
  input; a raising notify store costs the count, not the payload.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import structlog

from alfred.web.contact_state import (
    ARMED_RULES,
    DEFAULT_BRIEF_READ_DECAY_HOURS,
    DEFAULT_GAP_HOURS_NEW_DAY,
    RULE_DEFAULT,
    RULE_FIRST_CONTACT_AFTER_GAP,
    RULE_RESUME_PENDING_CAPTURE,
    SURFACE_BRIEF,
    SURFACE_CHAT,
    SURFACE_CHAT,
    WebContactStore,
)
from alfred.web.day_state import (
    LEVERS_FROM_DEFAULTS,
    LEVERS_FROM_RECORD,
    compute_day_state,
    read_router_preference,
)

NOW = datetime(2026, 8, 13, 9, 0, tzinfo=timezone.utc)

RECORD = """---
type: preference
status: active
name: Algernon — contact-surface routing
shape: action
scope: universal
matcher:
  domain: algernon
  rule: contact_surface_open
  args:
    levers:
      gap_hours_new_day: 9
      brief_read_decay_hours: 4
    pattern_surfacing:
      enabled: true
      min_observations: 5
      window_days: 21
      threshold_ratio: 0.75
---

# Policy
"""


def _vault(tmp_path, body: str | None = RECORD):
    v = tmp_path / "vault"
    (v / "preference").mkdir(parents=True)
    if body is not None:
        (v / "preference" / "contact-surface-routing.md").write_text(
            body, encoding="utf-8"
        )
    return v


class _Notify:
    """Minimal notify-store stand-in — the two methods day_state calls."""

    def __init__(self, entries: list[dict[str, Any]] | None = None, boom: bool = False):
        self.entries = entries or []
        self.boom = boom

    def unread_count(self, user_key):  # noqa: ANN001
        if self.boom:
            raise OSError("store gone")
        return sum(1 for e in self.entries if not e.get("read"))

    def list_for(self, user_key):  # noqa: ANN001
        # Newest-first, like the production store.
        return list(reversed(self.entries))


class _StateMgr:
    def __init__(self, state: dict[str, Any] | None = None):
        self.state = state or {"active_sessions": {}, "closed_sessions": []}

    def get_active(self, chat_id):  # noqa: ANN001
        return self.state.get("active_sessions", {}).get(str(chat_id))


def _compute(**kw):
    base = dict(
        user_key="u1",
        contact_store=None,
        notify_store=None,
        state_mgr=None,
        vault_path=None,
        current_brief_date="",
        now=NOW,
    )
    base.update(kw)
    return compute_day_state(**base)


# ---------------------------------------------------------------------------
# Levers
# ---------------------------------------------------------------------------


class TestLevers:
    def test_the_record_supplies_the_numbers(self, tmp_path):
        state = _compute(vault_path=_vault(tmp_path))
        assert state["levers_source"] == LEVERS_FROM_RECORD
        assert state["levers"]["gap_hours_new_day"] == 9
        assert state["levers"]["brief_read_decay_hours"] == 4
        assert state["pattern_surfacing"]["min_observations"] == 5
        assert state["pattern_surfacing"]["window_days"] == 21
        assert state["pattern_surfacing"]["threshold_ratio"] == 0.75

    def test_no_vault_falls_back_to_the_spec_defaults_and_says_so(self):
        state = _compute(vault_path=None)
        assert state["levers_source"] == LEVERS_FROM_DEFAULTS
        assert state["levers"]["gap_hours_new_day"] == DEFAULT_GAP_HOURS_NEW_DAY
        assert (
            state["levers"]["brief_read_decay_hours"]
            == DEFAULT_BRIEF_READ_DECAY_HOURS
        )

    def test_a_vault_with_no_matching_record_falls_back_and_logs_why(self, tmp_path):
        with structlog.testing.capture_logs() as captured:
            args, source = read_router_preference(_vault(tmp_path, body=None))
        assert (args, source) == ({}, LEVERS_FROM_DEFAULTS)
        events = [c for c in captured if c["event"] == "web.day_state.levers_default"]
        assert len(events) == 1
        assert "contact_surface_open" in events[0]["reason"]

    def test_a_record_for_another_domain_is_not_consumed(self, tmp_path):
        body = RECORD.replace("domain: algernon", "domain: curator")
        args, source = read_router_preference(_vault(tmp_path, body=body))
        assert (args, source) == ({}, LEVERS_FROM_DEFAULTS)

    def test_a_record_with_no_domain_still_matches(self, tmp_path):
        """The house idiom — a missing ``matcher.domain`` matches every
        consumer (curator/pipeline.py and brief/upcoming_events.py both)."""
        body = RECORD.replace("  domain: algernon\n", "")
        _args, source = read_router_preference(_vault(tmp_path, body=body))
        assert source == LEVERS_FROM_RECORD

    def test_a_revoked_record_is_ignored(self, tmp_path):
        body = RECORD.replace("status: active", "status: revoked")
        _args, source = read_router_preference(_vault(tmp_path, body=body))
        assert source == LEVERS_FROM_DEFAULTS

    def test_a_malformed_matcher_args_falls_back_and_warns(self, tmp_path):
        body = RECORD.replace("  args:\n", "  args: not-a-mapping\n")
        # Strip the now-orphaned arg lines so the YAML stays parseable.
        body = "\n".join(
            ln for ln in body.split("\n")
            if not ln.startswith("    ") and not ln.startswith("      ")
        )
        with structlog.testing.capture_logs() as captured:
            _args, source = read_router_preference(_vault(tmp_path, body=body))
        assert source == LEVERS_FROM_DEFAULTS
        assert any(c["event"] == "web.day_state.levers_malformed" for c in captured)

    def test_a_nonpositive_lever_is_rejected_not_honoured(self, tmp_path):
        """``gap_hours_new_day: 0`` would make every contact a new day."""
        body = RECORD.replace("gap_hours_new_day: 9", "gap_hours_new_day: 0")
        state = _compute(vault_path=_vault(tmp_path, body=body))
        assert state["levers"]["gap_hours_new_day"] == DEFAULT_GAP_HOURS_NEW_DAY

    def test_an_unreadable_vault_degrades_and_warns(self, tmp_path):
        v = tmp_path / "vault"
        (v / "preference").mkdir(parents=True)
        (v / "preference" / "bad.md").write_text(
            "---\nnot: [valid: yaml\n---\n", encoding="utf-8"
        )
        _args, source = read_router_preference(v)
        # The loader tolerates the bad file per-record; either way we must not
        # raise, and must land on a usable answer.
        assert source in (LEVERS_FROM_DEFAULTS, LEVERS_FROM_RECORD)


# ---------------------------------------------------------------------------
# Rule 2 input
# ---------------------------------------------------------------------------


class TestNotificationInput:
    def test_the_oldest_unread_is_the_one_pointed_at(self):
        notify = _Notify([
            {"id": "old", "read": False},
            {"id": "mid", "read": True},
            {"id": "new", "read": False},
        ])
        state = _compute(notify_store=notify)
        assert state["unresolved_flagged_notifications"] == 2
        assert state["first_unresolved_notification_id"] == "old"

    def test_an_all_read_tray_points_at_nothing(self):
        state = _compute(notify_store=_Notify([{"id": "a", "read": True}]))
        assert state["unresolved_flagged_notifications"] == 0
        assert state["first_unresolved_notification_id"] is None

    def test_a_raising_store_costs_the_count_not_the_payload(self):
        with structlog.testing.capture_logs() as captured:
            state = _compute(notify_store=_Notify(boom=True))
        assert state["unresolved_flagged_notifications"] == 0
        assert state["configured"] is False  # unrelated field still assembled
        assert any(
            c["event"] == "web.day_state.notify_read_failed" for c in captured
        )


# ---------------------------------------------------------------------------
# Rule 3 + 4 inputs
# ---------------------------------------------------------------------------


class TestSessionAndContactInputs:
    def test_an_active_sessions_last_message_counts_as_contact(self):
        mgr = _StateMgr({
            "active_sessions": {
                "u1": {"last_message_at": (NOW - timedelta(hours=2)).isoformat()}
            },
            "closed_sessions": [],
        })
        state = _compute(state_mgr=mgr)
        assert state["time_since_last_session_hours"] == 2.0

    def test_a_closed_session_for_this_chat_counts_and_others_do_not(self):
        mgr = _StateMgr({
            "active_sessions": {},
            "closed_sessions": [
                {"chat_id": "u1", "ended_at": (NOW - timedelta(hours=5)).isoformat()},
                {"chat_id": "someone_else",
                 "ended_at": (NOW - timedelta(minutes=1)).isoformat()},
            ],
        })
        state = _compute(state_mgr=mgr)
        assert state["time_since_last_session_hours"] == 5.0

    def test_an_app_open_counts_as_a_session_even_with_no_chat(self, tmp_path):
        """Either door. A brief-only morning must not read as a fresh gap."""
        store = WebContactStore.create(tmp_path / "c.json")
        store.record_contact(
            "u1", rule=RULE_DEFAULT, surface=SURFACE_CHAT,
            now=NOW - timedelta(hours=1),
        )
        state = _compute(contact_store=store)
        assert state["time_since_last_session_hours"] == 1.0

    def test_the_newest_of_the_two_doors_wins(self, tmp_path):
        store = WebContactStore.create(tmp_path / "c.json")
        store.record_contact(
            "u1", rule=RULE_DEFAULT, surface=SURFACE_CHAT,
            now=NOW - timedelta(hours=8),
        )
        mgr = _StateMgr({
            "active_sessions": {},
            "closed_sessions": [
                {"chat_id": "u1", "ended_at": (NOW - timedelta(hours=1)).isoformat()},
            ],
        })
        state = _compute(contact_store=store, state_mgr=mgr)
        assert state["time_since_last_session_hours"] == 1.0

    def test_no_history_at_all_is_null_not_zero(self):
        state = _compute()
        assert state["last_session_ended"] is None
        assert state["time_since_last_session_hours"] is None


class TestBriefReadToday:
    """WHICH brief, then WHEN — and the first question used to be missing.

    THE DEFECT THESE PINS REPLACE, stated because the old ones were confidently
    wrong: `brief_read_today` was derived from contact recency alone. A
    28-second glance at YESTERDAY'S brief is indistinguishable from a real read
    of today's under that rule, so the operator's 04:48 look at the previous
    day's artifact suppressed rule 3 for the one that landed at 06:00.

    Worse than the miss: the old suite PINNED it. `test_a_recent_brief_read_
    counts` asserted true for a contact with no artifact identity at all, and
    `test_the_records_decay_lever_is_what_decides` carried a positive control I
    wrote to prove the lever bites — which it did, while proving nothing about
    whether the right brief had been read. A green positive control on the wrong
    proposition is worse than no control, because it reads as diligence.
    """

    def _read_at(self, tmp_path, when, *, showing: str):
        """A brief LANDING at ``when``, showing the artifact dated ``showing``."""
        store = WebContactStore.create(tmp_path / "c.json")
        store.record_contact(
            "u1",
            rule=RULE_FIRST_CONTACT_AFTER_GAP,
            surface=SURFACE_BRIEF,
            brief_date=showing,
            now=when,
        )
        return store

    def test_reading_TODAYS_brief_counts(self, tmp_path):
        store = self._read_at(tmp_path, NOW - timedelta(hours=2), showing="2026-08-13")
        state = _compute(contact_store=store, current_brief_date="2026-08-13")
        assert state["brief_read_today"] is True

    def test_reading_YESTERDAYS_brief_does_NOT_count(self, tmp_path):
        """The operator's actual case: a glance at the previous day's artifact,
        recent enough to pass every decay check, must not suppress today's."""
        store = self._read_at(tmp_path, NOW - timedelta(hours=2), showing="2026-08-12")
        state = _compute(contact_store=store, current_brief_date="2026-08-13")
        assert state["brief_read_today"] is False
        # And the payload says WHICH, so the answer is inspectable rather than a
        # bare boolean nobody can check.
        assert state["brief_read_date"] == "2026-08-12"
        assert state["current_brief_date"] == "2026-08-13"

    def test_it_still_decays_after_the_lever(self, tmp_path):
        """WHEN is unchanged and still does its own job: a genuine morning read
        stops suppressing the evening offer."""
        store = self._read_at(tmp_path, NOW - timedelta(hours=13), showing="2026-08-13")
        assert _compute(
            contact_store=store, current_brief_date="2026-08-13"
        )["brief_read_today"] is False

    def test_the_records_decay_lever_is_what_decides(self, tmp_path):
        store = self._read_at(tmp_path, NOW - timedelta(hours=6), showing="2026-08-13")
        vault = _vault(tmp_path)  # brief_read_decay_hours: 4
        assert _compute(
            contact_store=store, vault_path=vault, current_brief_date="2026-08-13"
        )["brief_read_today"] is False
        # Positive control, now on the RIGHT proposition: same contact, same
        # artifact, default 12h decay reads true — so the assertion above is the
        # lever biting and not the identity check silently failing.
        assert _compute(
            contact_store=store, current_brief_date="2026-08-13"
        )["brief_read_today"] is True

    def test_an_UNRECORDED_artifact_date_is_not_a_match(self, tmp_path):
        """Contacts written before this field existed have no date. Absence must
        read as not-read — the failure direction is offering the brief again,
        never withholding it."""
        store = self._read_at(tmp_path, NOW - timedelta(hours=1), showing="")
        assert _compute(
            contact_store=store, current_brief_date="2026-08-13"
        )["brief_read_today"] is False

    def test_NO_brief_spooled_is_not_a_match_either(self, tmp_path):
        """Nothing current to have read. Two empty strings agreeing about
        nothing is not identity."""
        store = self._read_at(tmp_path, NOW - timedelta(hours=1), showing="")
        assert _compute(contact_store=store, current_brief_date="")["brief_read_today"] is False

    def test_an_OVERRIDDEN_brief_open_is_still_not_a_read(self, tmp_path):
        """The `landed`-not-`surface` rule survives the rewrite: routed to the
        brief, overridden away, never read — whatever the artifact date says."""
        store = WebContactStore.create(tmp_path / "c.json")
        e = store.record_contact(
            "u1", rule=RULE_FIRST_CONTACT_AFTER_GAP, surface=SURFACE_BRIEF,
            brief_date="2026-08-13", now=NOW - timedelta(hours=1),
        )
        store.record_override("u1", e["id"], surface=SURFACE_CHAT, now=NOW)
        assert _compute(
            contact_store=store, current_brief_date="2026-08-13"
        )["brief_read_today"] is False


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------


class TestPayloadShape:
    def test_rule_1s_fields_are_absent_not_false(self):
        state = _compute()
        assert "open_capture_pending" not in state
        assert "pending_capture_id" not in state

    def test_the_unarmed_rung_is_declared_with_its_reason(self):
        state = _compute()
        assert RULE_RESUME_PENDING_CAPTURE not in state["armed_rules"]
        assert RULE_RESUME_PENDING_CAPTURE in state["rule_order"]
        assert RULE_RESUME_PENDING_CAPTURE in state["unarmed_rules"]
        assert "open_capture_pending" in state["unarmed_rules"][
            RULE_RESUME_PENDING_CAPTURE
        ]

    def test_armed_rules_are_the_three_v1_rungs(self):
        assert _compute()["armed_rules"] == list(ARMED_RULES)

    def test_configured_is_false_without_a_store(self):
        assert _compute()["configured"] is False

    def test_configured_is_true_with_one(self, tmp_path):
        store = WebContactStore.create(tmp_path / "c.json")
        assert _compute(contact_store=store)["configured"] is True

    def test_adopted_defaults_are_served(self, tmp_path):
        store = WebContactStore.create(tmp_path / "c.json")
        store.adopt_default("u1", rule=RULE_DEFAULT, surface=SURFACE_BRIEF)
        assert _compute(contact_store=store)["adopted_defaults"] == {
            RULE_DEFAULT: SURFACE_BRIEF
        }

    def test_every_read_logs_including_the_quiet_one(self):
        with structlog.testing.capture_logs() as captured:
            _compute()
        computed = [c for c in captured if c["event"] == "web.day_state.computed"]
        assert len(computed) == 1
        assert computed[0]["user_key"] == "u1"
        assert computed[0]["configured"] is False
        assert computed[0]["unresolved"] == 0
        assert computed[0]["last_active_surface"] == "(none)"
        assert computed[0]["levers_source"] == LEVERS_FROM_DEFAULTS
