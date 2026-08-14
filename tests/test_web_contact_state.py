"""Tests for ``alfred.web.contact_state`` — the C4 contact-router store.

Load-bearing pins:

* **PATH RESOLUTION IS ONE PARSE** — explicit config wins; otherwise the path
  derives from the instance's own ``logging.dir``; and an UNANCHORED config
  returns ``None`` rather than a cwd-relative guess (the #74 cross-instance
  pollution class). Both consumers (the web config layer and the feed-act
  dispatcher) call this function, so a drift here is a drift everywhere.
* **``landed`` vs ``surface``** — a contact the router sent to the brief and the
  operator immediately overrode away from is NOT a brief read. The two fields
  exist to keep that distinction, and ``last_brief_contact_ts`` reads the right
  one.
* **SUPPRESSION FAILS OPEN** — an unparseable ``until`` reads as not-suppressed:
  a pattern the operator can never be told about again is worse than one they
  are told about twice.
* **SCHEMA TOLERANCE** — a file from another build (extra keys, malformed rows)
  loads without raising and keeps what it can.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import structlog

from alfred.web.contact_state import (
    CONTACT_CAP,
    CONTACT_STATE_FILENAME,
    SURFACE_BRIEF,
    SURFACE_CHAT,
    SURFACE_FEED,
    RULE_DEFAULT,
    RULE_FIRST_CONTACT_AFTER_GAP,
    WebContactStore,
    parse_ts,
    resolve_contact_state_path,
)

NOW = datetime(2026, 8, 13, 9, 0, tzinfo=timezone.utc)


def _store(tmp_path) -> WebContactStore:
    return WebContactStore.create(tmp_path / "contact.json")


# ---------------------------------------------------------------------------
# Path resolution — the shared parse
# ---------------------------------------------------------------------------


class TestPathResolution:
    def test_explicit_config_wins_over_the_derived_default(self):
        raw = {
            "logging": {"dir": "/data/salem"},
            "web": {"contact_router": {"state_path": "/elsewhere/contacts.json"}},
        }
        assert resolve_contact_state_path(raw) == "/elsewhere/contacts.json"

    def test_it_derives_from_the_instances_own_data_dir(self):
        raw = {"logging": {"dir": "/data/salem"}}
        assert resolve_contact_state_path(raw) == (
            f"/data/salem/{CONTACT_STATE_FILENAME}"
        )

    def test_an_unanchored_config_resolves_to_None_not_a_cwd_guess(self):
        """The #74 pin. A cwd-relative fallback is how KAL-LE's writer landed in
        Salem's store; the honest answer to 'no data dir' is 'not wired'."""
        assert resolve_contact_state_path({}) is None
        assert resolve_contact_state_path({"web": {}}) is None
        assert resolve_contact_state_path(None) is None
        # Positive control: the same call with an anchor DOES resolve, so the
        # None results above are the guard firing rather than the function
        # being broken for every input.
        assert resolve_contact_state_path({"logging": {"dir": "/d"}}) is not None

    def test_the_trailing_slash_does_not_double(self):
        raw = {"logging": {"dir": "/data/salem/"}}
        assert resolve_contact_state_path(raw) == (
            f"/data/salem/{CONTACT_STATE_FILENAME}"
        )


# ---------------------------------------------------------------------------
# Timestamps
# ---------------------------------------------------------------------------


class TestParseTs:
    def test_a_naive_stamp_is_read_as_utc(self):
        assert parse_ts("2026-08-13T09:00:00") == NOW

    def test_unusable_values_are_None_never_a_guess(self):
        for bad in (None, "", "   ", "not-a-date", 17, []):
            assert parse_ts(bad) is None


# ---------------------------------------------------------------------------
# The contact log
# ---------------------------------------------------------------------------


class TestContactLog:
    def test_a_contact_starts_landed_where_it_opened(self, tmp_path):
        s = _store(tmp_path)
        e = s.record_contact("u1", rule=RULE_DEFAULT, surface=SURFACE_CHAT, now=NOW)
        assert e["surface"] == SURFACE_CHAT
        assert e["landed"] == SURFACE_CHAT
        assert e["overridden"] is False
        assert e["ts"] == NOW.isoformat()
        assert e["id"]

    def test_an_override_moves_landed_and_leaves_surface_alone(self, tmp_path):
        s = _store(tmp_path)
        e = s.record_contact("u1", rule=RULE_DEFAULT, surface=SURFACE_CHAT, now=NOW)
        got = s.record_override("u1", e["id"], surface=SURFACE_FEED, now=NOW)
        assert got is not None
        assert got["surface"] == SURFACE_CHAT   # what the router opened
        assert got["landed"] == SURFACE_FEED    # where the operator went
        assert got["overridden"] is True
        assert got["overridden_at"] == NOW.isoformat()

    def test_an_override_for_an_unknown_contact_returns_None(self, tmp_path):
        s = _store(tmp_path)
        s.record_contact("u1", rule=RULE_DEFAULT, surface=SURFACE_CHAT, now=NOW)
        assert s.record_override("u1", "nope", surface=SURFACE_FEED) is None

    def test_an_override_cannot_reach_another_users_contact(self, tmp_path):
        s = _store(tmp_path)
        e = s.record_contact("u1", rule=RULE_DEFAULT, surface=SURFACE_CHAT, now=NOW)
        assert s.record_override("u2", e["id"], surface=SURFACE_FEED) is None
        # Positive control: the SAME id under its own user does resolve.
        assert s.record_override("u1", e["id"], surface=SURFACE_FEED) is not None

    def test_the_log_is_bounded_and_evicts_oldest_first(self, tmp_path):
        s = _store(tmp_path)
        for i in range(CONTACT_CAP + 5):
            s.record_contact(
                "u1", rule=RULE_DEFAULT, surface=SURFACE_CHAT,
                now=NOW + timedelta(minutes=i),
            )
        kept = s.contacts_for("u1")
        assert len(kept) == CONTACT_CAP
        # The survivors are the NEWEST ones.
        assert kept[-1]["ts"] == (NOW + timedelta(minutes=CONTACT_CAP + 4)).isoformat()

    def test_last_contact_ts_is_the_newest_datable_one(self, tmp_path):
        s = _store(tmp_path)
        s.record_contact("u1", rule=RULE_DEFAULT, surface=SURFACE_CHAT, now=NOW)
        s.record_contact(
            "u1", rule=RULE_DEFAULT, surface=SURFACE_CHAT,
            now=NOW + timedelta(hours=3),
        )
        assert s.last_contact_ts("u1") == NOW + timedelta(hours=3)
        assert s.last_contact_ts("nobody") is None


class TestLandedIsTheAuthority:
    def test_a_brief_open_the_operator_overrode_away_is_not_a_brief_read(
        self, tmp_path
    ):
        """The whole reason ``landed`` is a separate field."""
        s = _store(tmp_path)
        e = s.record_contact(
            "u1", rule=RULE_FIRST_CONTACT_AFTER_GAP, surface=SURFACE_BRIEF, now=NOW,
        )
        assert s.last_brief_contact_ts("u1") == NOW  # before the override
        s.record_override("u1", e["id"], surface=SURFACE_CHAT, now=NOW)
        assert s.last_brief_contact_ts("u1") is None  # after: never read

    def test_last_landed_surface_skips_a_surface_this_build_cannot_route_to(
        self, tmp_path
    ):
        """A store written by a build with a wider vocabulary must not hand back
        a surface this one has no path for — that is a dead navigation."""
        s = _store(tmp_path)
        s.record_contact("u1", rule=RULE_DEFAULT, surface=SURFACE_FEED, now=NOW)
        s.contacts["u1"].append({"id": "x", "ts": NOW.isoformat(), "landed": "hologram"})
        assert s.last_landed_surface("u1") == SURFACE_FEED

    def test_no_contacts_is_an_empty_string_not_a_default_surface(self, tmp_path):
        assert _store(tmp_path).last_landed_surface("u1") == ""


# ---------------------------------------------------------------------------
# Operator-approved adjustments
# ---------------------------------------------------------------------------


class TestAdoptAndSuppress:
    def test_adopt_records_a_per_rule_surface(self, tmp_path):
        s = _store(tmp_path)
        s.adopt_default("u1", rule=RULE_DEFAULT, surface=SURFACE_FEED)
        assert s.adopted_for("u1") == {RULE_DEFAULT: SURFACE_FEED}
        assert s.adopted_for("u2") == {}

    def test_suppression_holds_inside_the_window_and_lapses_after(self, tmp_path):
        s = _store(tmp_path)
        s.suppress_pattern("u1", "r->s", days=14, now=NOW)
        assert s.is_pattern_suppressed("u1", "r->s", now=NOW + timedelta(days=13))
        assert not s.is_pattern_suppressed("u1", "r->s", now=NOW + timedelta(days=15))

    def test_an_unknown_pattern_is_not_suppressed(self, tmp_path):
        s = _store(tmp_path)
        assert not s.is_pattern_suppressed("u1", "never-seen", now=NOW)

    def test_a_corrupt_until_fails_OPEN(self, tmp_path):
        """Failing closed would silence a pattern permanently on a bad
        timestamp — unreachable advice is worse than repeated advice."""
        s = _store(tmp_path)
        s.suppressed["u1"] = {"r->s": "garbage"}
        assert not s.is_pattern_suppressed("u1", "r->s", now=NOW)

    def test_a_zero_day_window_is_a_day_not_forever_and_not_expired(self, tmp_path):
        s = _store(tmp_path)
        until = s.suppress_pattern("u1", "r->s", days=0, now=NOW)
        assert until == (NOW + timedelta(days=1)).isoformat()


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


class TestPersistence:
    def test_round_trip_through_disk(self, tmp_path):
        p = tmp_path / "c.json"
        s = WebContactStore.create(p)
        e = s.record_contact("u1", rule=RULE_DEFAULT, surface=SURFACE_CHAT, now=NOW)
        s.adopt_default("u1", rule=RULE_DEFAULT, surface=SURFACE_FEED)
        s.suppress_pattern("u1", "r->s", days=3, now=NOW)

        again = WebContactStore.create(p)
        again.load()
        assert [c["id"] for c in again.contacts_for("u1")] == [e["id"]]
        assert again.adopted_for("u1") == {RULE_DEFAULT: SURFACE_FEED}
        assert again.is_pattern_suppressed("u1", "r->s", now=NOW)

    def test_a_missing_file_loads_empty_and_says_so(self, tmp_path):
        s = WebContactStore.create(tmp_path / "absent.json")
        with structlog.testing.capture_logs() as captured:
            s.load()
        events = [c["event"] for c in captured]
        assert "web.contact_state.no_existing_state" in events
        assert s.contacts == {}

    def test_a_corrupt_file_is_tolerated_and_logged(self, tmp_path):
        p = tmp_path / "c.json"
        p.write_text("{not json", encoding="utf-8")
        s = WebContactStore.create(p)
        with structlog.testing.capture_logs() as captured:
            s.load()
        failures = [c for c in captured if c["event"] == "web.contact_state.load_failed"]
        assert len(failures) == 1
        assert failures[0]["path"] == str(p)
        assert s.contacts == {}

    def test_unknown_entry_keys_survive_and_malformed_rows_are_dropped(self, tmp_path):
        p = tmp_path / "c.json"
        p.write_text(
            json.dumps({
                "version": 1,
                "contacts": {
                    "u1": [
                        {"id": "keep", "ts": NOW.isoformat(), "from_the_future": 1},
                        {"no_id": True},
                        "not even a dict",
                    ],
                    "u2": "not a list",
                },
                "adopted": {"u1": {"default": "feed"}},
                "suppressed": {"u1": {"a->b": "2026-09-01T00:00:00+00:00"}},
            }),
            encoding="utf-8",
        )
        s = WebContactStore.create(p)
        s.load()
        kept = s.contacts_for("u1")
        assert [c["id"] for c in kept] == ["keep"]
        assert kept[0]["from_the_future"] == 1
        assert "u2" not in s.contacts
        assert s.adopted_for("u1") == {"default": "feed"}

    def test_a_load_logs_its_counts(self, tmp_path):
        p = tmp_path / "c.json"
        s = WebContactStore.create(p)
        s.record_contact("u1", rule=RULE_DEFAULT, surface=SURFACE_CHAT, now=NOW)
        again = WebContactStore.create(p)
        with structlog.testing.capture_logs() as captured:
            again.load()
        loaded = [c for c in captured if c["event"] == "web.contact_state.loaded"]
        assert len(loaded) == 1
        assert loaded[0]["users"] == 1
        assert loaded[0]["contacts"] == 1
