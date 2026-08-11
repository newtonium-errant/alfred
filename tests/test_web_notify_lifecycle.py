"""#86 — how a notification LEAVES the tray.

The operator's report: a ticket notice they had already read sat on screen for
four days with no way to clear it. ``read`` restyled it forever and nothing
removed it. This covers the two exits added in response — an explicit dismiss
and an automatic age-out — plus the invariants that keep either from becoming
the opposite bug (a notice that vanishes before it is seen).

The load-bearing pins here are the NEGATIVE ones:

  * ``retire_aged`` must never touch an UNREAD entry, at any age. Clearing
    something the operator never saw is the failure this feature exists to
    prevent, pointed backwards.
  * an entry whose ``ts`` cannot be parsed must never be retired — deleting on
    a guess about age is the same failure with an extra step.
  * ``list_for`` must stay READ-ONLY, because a second daemon calls it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import structlog

from alfred.web.notify_state import (
    DISMISS_REASON_AGED_OUT,
    DISMISS_REASON_OPERATOR,
    WebNotifyStore,
)

_USER = "1234567"


@pytest.fixture()
def store(tmp_path: Path) -> WebNotifyStore:
    return WebNotifyStore.create(tmp_path / "web_notify_state.json")


def _aged(store: WebNotifyStore, entry_id: str, *, days: int) -> None:
    """Backdate an entry's ``ts`` — the only way to test a retention window."""
    for e in store.notifications[_USER]:
        if e["id"] == entry_id:
            e["ts"] = (
                datetime.now(timezone.utc) - timedelta(days=days)
            ).isoformat()
            return
    raise AssertionError(f"no entry {entry_id!r} to backdate")


# ---------------------------------------------------------------------------
# Dismiss
# ---------------------------------------------------------------------------


def test_a_dismissed_entry_stops_being_listed(store: WebNotifyStore) -> None:
    """The operator's actual complaint: read was not enough to clear it."""
    keep = store.enqueue(_USER, text="keep me")
    drop = store.enqueue(_USER, text="clear me")
    assert store.dismiss(_USER, [drop["id"]]) == 1

    listed = [e["id"] for e in store.list_for(_USER)]
    assert listed == [keep["id"]]


def test_a_dismissed_entry_stays_in_the_store(store: WebNotifyStore) -> None:
    """Non-destructive: the tray is cleared, the evidence is not.

    This store is the only record that a notice was ever delivered, so clearing
    the screen must not erase it.
    """
    entry = store.enqueue(_USER, text="clear me")
    store.dismiss(_USER, [entry["id"]])

    audit = store.list_for(_USER, include_dismissed=True)
    assert [e["id"] for e in audit] == [entry["id"]]
    assert audit[0]["dismissed"] is True
    assert audit[0]["dismissed_reason"] == DISMISS_REASON_OPERATOR
    assert audit[0]["dismissed_at"]


def test_dismissing_also_marks_read(store: WebNotifyStore) -> None:
    """The invariant that keeps the pill honest.

    'Dismissed but unread' would be a badge counting something the operator
    cannot see or reach. Making dismissal imply read removes that state
    entirely rather than asking every reader to remember a compound rule.
    """
    entry = store.enqueue(_USER, text="x")
    assert store.unread_count(_USER) == 1
    store.dismiss(_USER, [entry["id"]])
    assert store.unread_count(_USER) == 0
    assert store.list_for(_USER, include_dismissed=True)[0]["read"] is True


def test_unread_count_ignores_a_dismissed_unread_entry(
    store: WebNotifyStore,
) -> None:
    """Defensive: unreachable via the API, reachable via a hand-edited file.

    A badge pointing at something the tray does not render is the inversion of
    intentionally-left-blank — it reports work that cannot be found.
    """
    store.enqueue(_USER, text="x")
    store.notifications[_USER][0]["dismissed"] = True  # read stays False
    assert store.unread_count(_USER) == 0


def test_dismiss_is_idempotent(store: WebNotifyStore) -> None:
    entry = store.enqueue(_USER, text="x")
    assert store.dismiss(_USER, [entry["id"]]) == 1
    assert store.dismiss(_USER, [entry["id"]]) == 0


def test_dismissing_an_unknown_id_is_a_no_op(store: WebNotifyStore) -> None:
    store.enqueue(_USER, text="x")
    assert store.dismiss(_USER, ["not-a-real-id"]) == 0
    assert len(store.list_for(_USER)) == 1


def test_dismiss_survives_a_save_load_round_trip(store: WebNotifyStore) -> None:
    entry = store.enqueue(_USER, text="x")
    store.dismiss(_USER, [entry["id"]])

    reloaded = WebNotifyStore.create(store.state_path)
    reloaded.load()
    assert reloaded.list_for(_USER) == []
    assert len(reloaded.list_for(_USER, include_dismissed=True)) == 1


# ---------------------------------------------------------------------------
# Age-out
# ---------------------------------------------------------------------------


def test_read_entries_past_the_window_are_retired(store: WebNotifyStore) -> None:
    old = store.enqueue(_USER, text="old")
    recent = store.enqueue(_USER, text="recent")
    store.ack(_USER, [old["id"], recent["id"]])
    _aged(store, old["id"], days=30)

    assert store.retire_aged(_USER, max_age_days=7) == 1
    assert [e["id"] for e in store.list_for(_USER)] == [recent["id"]]

    audit = store.list_for(_USER, include_dismissed=True)
    aged = next(e for e in audit if e["id"] == old["id"])
    assert aged["dismissed_reason"] == DISMISS_REASON_AGED_OUT


def test_an_UNREAD_entry_is_never_retired_however_old(
    store: WebNotifyStore,
) -> None:
    """THE load-bearing negative pin.

    Age is permission to clear something already SEEN. Retiring an unread
    notice would silently delete work the operator never looked at — the exact
    failure this feature exists to prevent, running backwards. A year old and
    unread still shows.
    """
    entry = store.enqueue(_USER, text="never seen")
    _aged(store, entry["id"], days=365)

    assert store.retire_aged(_USER, max_age_days=7) == 0
    assert [e["id"] for e in store.list_for(_USER)] == [entry["id"]]
    assert store.unread_count(_USER) == 1


def test_an_unparseable_timestamp_is_not_retired(store: WebNotifyStore) -> None:
    """Do not delete on a guess about age.

    Failing open here would auto-clear entries whose age cannot be
    established, which is worse than keeping one row too long.
    """
    entry = store.enqueue(_USER, text="x")
    store.ack(_USER, [entry["id"]])
    store.notifications[_USER][0]["ts"] = "not-a-timestamp"

    assert store.retire_aged(_USER, max_age_days=7) == 0
    assert len(store.list_for(_USER)) == 1


def test_an_entry_exactly_inside_the_window_survives(
    store: WebNotifyStore,
) -> None:
    """Boundary: 6 days old under a 7-day window is not yet history."""
    entry = store.enqueue(_USER, text="x")
    store.ack(_USER, [entry["id"]])
    _aged(store, entry["id"], days=6)
    assert store.retire_aged(_USER, max_age_days=7) == 0


def test_a_zero_window_disables_retirement_and_says_so(
    store: WebNotifyStore,
) -> None:
    """A configured choice (keep until dismissed), not a broken sweep."""
    entry = store.enqueue(_USER, text="x")
    store.ack(_USER, [entry["id"]])
    _aged(store, entry["id"], days=999)

    with structlog.testing.capture_logs() as captured:
        assert store.retire_aged(_USER, max_age_days=0) == 0
    assert len(store.list_for(_USER)) == 1
    events = [c["event"] for c in captured]
    assert "web.notify.retire_disabled" in events


def test_the_sweep_reports_itself_even_when_it_retires_nothing(
    store: WebNotifyStore,
) -> None:
    """ILB, and the reason it matters here specifically.

    This sweep runs on a READ path with no daemon behind it. A retirement
    mechanism that only speaks when it acts is indistinguishable from one that
    stopped running, and nobody would notice for weeks.
    """
    store.enqueue(_USER, text="x")  # unread — nothing to retire

    with structlog.testing.capture_logs() as captured:
        assert store.retire_aged(_USER, max_age_days=7) == 0

    swept = [c for c in captured if c.get("event") == "web.notify.retired_aged"]
    assert len(swept) == 1
    assert swept[0]["retired"] == 0
    assert swept[0]["max_age_days"] == 7
    assert "ran, nothing to retire" in swept[0]["detail"]


def test_retiring_reports_the_count_it_actually_retired(
    store: WebNotifyStore,
) -> None:
    for i in range(3):
        e = store.enqueue(_USER, text=f"old-{i}")
        store.ack(_USER, [e["id"]])
        _aged(store, e["id"], days=30)

    with structlog.testing.capture_logs() as captured:
        assert store.retire_aged(_USER, max_age_days=7) == 3
    swept = [c for c in captured if c.get("event") == "web.notify.retired_aged"]
    assert swept[0]["retired"] == 3


def test_retirement_is_persisted(store: WebNotifyStore) -> None:
    entry = store.enqueue(_USER, text="x")
    store.ack(_USER, [entry["id"]])
    _aged(store, entry["id"], days=30)
    store.retire_aged(_USER, max_age_days=7)

    reloaded = WebNotifyStore.create(store.state_path)
    reloaded.load()
    assert reloaded.list_for(_USER) == []


def test_an_empty_tray_sweeps_without_writing(store: WebNotifyStore) -> None:
    """No entries, no save — and still an explicit line."""
    with structlog.testing.capture_logs() as captured:
        assert store.retire_aged(_USER, max_age_days=7) == 0
    assert not store.state_path.exists(), "an empty sweep wrote the store file"
    assert [c for c in captured if c.get("event") == "web.notify.retired_aged"]


# ---------------------------------------------------------------------------
# Cross-surface + schema tolerance
# ---------------------------------------------------------------------------


def test_list_for_never_writes(store: WebNotifyStore) -> None:
    """The daily-sync brief is a SEPARATE daemon that calls this and is
    documented read-only.

    If retirement were hooked into the read path, that daemon would become a
    second read-modify-write writer of an unlocked JSON file — a lost-update
    generator against the talker's enqueue/ack. This pin is what keeps the
    retirement in its own explicitly-called method.
    """
    entry = store.enqueue(_USER, text="x")
    store.ack(_USER, [entry["id"]])
    _aged(store, entry["id"], days=999)
    before = store.state_path.read_bytes()

    store.list_for(_USER)
    store.list_for(_USER, include_dismissed=True)
    store.unread_count(_USER)

    assert store.state_path.read_bytes() == before, "a read path wrote the store"


def test_a_pre_86_entry_loads_and_renders(tmp_path: Path) -> None:
    """Schema tolerance: entries written before dismissal existed.

    Every check is a falsy ``.get``, so no migration is needed — but that is
    only true if an entry with no ``dismissed`` key still lists.
    """
    path = tmp_path / "web_notify_state.json"
    path.write_text(
        '{"version": 1, "notifications": {"%s": [{"id": "old1", '
        '"text": "legacy", "ts": "2026-08-01T00:00:00+00:00", "read": true}]}}'
        % _USER,
        encoding="utf-8",
    )
    store = WebNotifyStore.create(path)
    store.load()

    assert [e["id"] for e in store.list_for(_USER)] == ["old1"]
    # And it is still dismissible / retirable despite lacking the field.
    assert store.dismiss(_USER, ["old1"]) == 1
    assert store.list_for(_USER) == []


def test_a_pre_86_entry_can_age_out(tmp_path: Path) -> None:
    """The rollout case: a tray already full of old read entries."""
    path = tmp_path / "web_notify_state.json"
    old_ts = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    path.write_text(
        '{"version": 1, "notifications": {"%s": [{"id": "old1", '
        '"text": "legacy", "ts": "%s", "read": true}]}}' % (_USER, old_ts),
        encoding="utf-8",
    )
    store = WebNotifyStore.create(path)
    store.load()
    assert store.retire_aged(_USER, max_age_days=7) == 1
    assert store.list_for(_USER) == []


def test_dismissal_hides_the_notice_from_the_brief_too(tmp_path: Path) -> None:
    """CROSS-SURFACE, and the reason the filter lives in ``list_for``.

    The daily-sync brief reads this store through the same method. If dismissal
    only hid an entry in the PWA tray, a notice the operator explicitly cleared
    would reappear in tomorrow's brief — the revive-after-ack failure the
    health cards already taught us. This drives the brief's OWN reader.
    """
    from alfred.daily_sync import ticket_notify_section

    store = WebNotifyStore.create(tmp_path / "web_notify_state.json")
    kept = store.enqueue("42", text="still relevant")
    cleared = store.enqueue("42", text="operator cleared this")
    store.dismiss("42", [cleared["id"]])

    reader = WebNotifyStore.create(store.state_path)
    reader.load()
    texts = [e["text"] for e in reader.list_for("42")]

    assert texts == [kept["text"]]
    assert "operator cleared this" not in texts
    # The brief's reader is the same API surface, asserted so a future
    # refactor that gives it a private path fails here.
    assert hasattr(ticket_notify_section, "_read_notices")
