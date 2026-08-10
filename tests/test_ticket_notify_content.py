"""#76 — the ticket notification carries the ticket's CONTENT, so the PWA can read it.

Operator ruling (option 2): the notification card expands to show the issue's
content, fetched box-side. The tunnel was explicitly REJECTED — forgejo's
unreachability from outside stays the security boundary, so #63b's box-local
label remains the link fallback and `public_base_url` stays empty.

WHY ENRICH-AT-NOTIFY RATHER THAN LIVE-FETCH. The instance that RENDERS the card
cannot reach forgejo. The client is registered only alongside `ticket_intake`
(`transport/server.py`), which is KAL-LE's block; Salem hosts the PWA tray and
has no forgejo client at all. A live status fetch would therefore be Salem → a
new peer-pinned route on KAL-LE → forgejo, a cross-instance read path with its
own token and error surface. Enriching the notify payload — which is composed on
KAL-LE, where the content and the client already are — needs no new route, no
new auth, and offers no SSRF surface, because nothing takes a URL or an id from
the client at all.

The content is composed by the SAME function that builds the real issue body,
with the machine markers switched off. Composing it twice would let the card and
the tracker drift into disagreeing about what the ticket says.
"""

from __future__ import annotations

from typing import Any

import pytest

from alfred.integrations.github_ops import issue_marker
from alfred.transport.peer_handlers import _build_issue_body
from alfred.web.notify_state import NOTIFY_BODY_MAX_CHARS, WebNotifyStore

TICKET_UID = "tkt-20260810-abc123"
FM = {
    "reporter": "Ben",
    "area": "checkout",
    "priority": "high",
    "created": "2026-08-10",
}
BODY = "## Repro\n1. Click Submit Payroll\n2. Empty JSON response\n"


def _issue_body(**over: Any) -> str:
    kwargs: dict[str, Any] = dict(
        fm=FM, relpath="ticket/Payroll.md", auth_peer="vera",
        body=BODY, ticket_uid=TICKET_UID,
    )
    kwargs.update(over)
    return _build_issue_body(**kwargs)


# --- the display body is the issue body, minus the plumbing -----------------


def test_the_real_issue_body_still_carries_the_dedupe_marker() -> None:
    """LOAD-BEARING regression pin, and it comes first on purpose.

    The dedupe marker is how a VERA re-push finds its existing issue instead of
    filing a duplicate. #76 adds a marker-FREE variant for display, and the way
    that goes wrong is by accidentally stripping markers from the real issue
    too. If this fails, dedupe is broken and #76 broke it.
    """
    assert issue_marker(TICKET_UID) in _issue_body()


def test_the_display_body_drops_the_markers(tmp_path) -> None:
    display = _issue_body(include_markers=False)
    assert issue_marker(TICKET_UID) not in display
    assert "algernon-ticket" not in display


def test_the_display_body_keeps_the_header_and_the_ticket_text() -> None:
    """Dropping markers must not drop the content the operator came to read."""
    display = _issue_body(include_markers=False)
    assert "Reported by: Ben" in display
    assert "Priority: high" in display
    assert "Click Submit Payroll" in display


def test_display_and_real_bodies_agree_on_everything_but_the_markers() -> None:
    """The anti-drift pin. Both come from ONE composer, so the card cannot
    start describing the ticket differently from the tracker. A second
    hand-rolled composition is exactly how that drift begins."""
    real = _issue_body()
    display = _issue_body(include_markers=False)
    assert real.startswith(display.rstrip("\n"))


def test_a_project_slug_marker_is_also_display_stripped() -> None:
    display = _issue_body(include_markers=False, project_slug="bug-intake")
    assert "algernon-project" not in display
    assert "bug-intake" not in display


# --- the store carries it end to end ----------------------------------------


def test_the_store_round_trips_the_new_fields(tmp_path) -> None:
    store = WebNotifyStore.create(tmp_path / "notify.json")
    entry = store.enqueue(
        1, text="New ticket", ticket_uid=TICKET_UID, issue_url="",
        ticket_body="Reported by: Ben\n\nthe body", issue_number=9,
    )
    assert entry["ticket_body"] == "Reported by: Ben\n\nthe body"
    assert entry["issue_number"] == 9

    reloaded = WebNotifyStore.create(tmp_path / "notify.json")
    reloaded.load()
    row = reloaded.list_for(1)[0]
    assert row["ticket_body"] == "Reported by: Ben\n\nthe body"
    assert row["issue_number"] == 9


def test_an_entry_written_before_76_still_loads(tmp_path) -> None:
    """Schema tolerance, backward. A tray already holding pre-#76 entries must
    keep rendering them — they simply have no expandable content."""
    import json

    path = tmp_path / "notify.json"
    path.write_text(json.dumps({
        "version": 1,
        "notifications": {"1": [{
            "id": "n1", "text": "old", "precedence": "R", "source": "kal-le",
            "ticket_uid": TICKET_UID, "issue_url": "", "ts": "2026-08-01T00:00:00Z",
            "read": False,
        }]},
    }), encoding="utf-8")

    store = WebNotifyStore.create(path)
    store.load()
    row = store.list_for(1)[0]
    assert row["text"] == "old"
    assert row.get("ticket_body", "") == ""


def test_an_unknown_future_field_does_not_break_the_loader(tmp_path) -> None:
    """Schema tolerance, forward."""
    import json

    path = tmp_path / "notify.json"
    path.write_text(json.dumps({
        "version": 1,
        "notifications": {"1": [{
            "id": "n1", "text": "x", "precedence": "R", "source": "s",
            "ts": "2026-08-01T00:00:00Z", "read": False,
            "something_from_2027": {"nested": True},
        }]},
    }), encoding="utf-8")
    store = WebNotifyStore.create(path)
    store.load()
    assert store.list_for(1)[0]["text"] == "x"


def test_the_body_is_bounded(tmp_path) -> None:
    """A ticket body is operator-authored text of unbounded length; it crosses
    the peer protocol and lands in a phone's tray. Cap it at the source rather
    than hoping the renderer copes."""
    store = WebNotifyStore.create(tmp_path / "notify.json")
    entry = store.enqueue(
        1, text="t", ticket_body="x" * (NOTIFY_BODY_MAX_CHARS + 5_000),
    )
    assert len(entry["ticket_body"]) <= NOTIFY_BODY_MAX_CHARS
    assert entry["ticket_body_truncated"] is True


def test_a_short_body_is_not_flagged_truncated(tmp_path) -> None:
    store = WebNotifyStore.create(tmp_path / "notify.json")
    entry = store.enqueue(1, text="t", ticket_body="short")
    assert entry["ticket_body_truncated"] is False


def test_a_notice_with_no_ticket_body_stays_empty_not_absent(tmp_path) -> None:
    """ILB at the data layer: the field is always PRESENT, so the renderer can
    tell 'this notice has no content' from 'this notice predates the field'."""
    store = WebNotifyStore.create(tmp_path / "notify.json")
    entry = store.enqueue(1, text="a plain notice")
    assert entry["ticket_body"] == ""
    assert entry["issue_number"] == 0


# --- the SINK: the bridge the mutation round proved was untested -------------
#
# Found by mutating, not by reading. Dropping `ticket_body` from the sink left
# all 46 pins green: the intake's e2e pin proves the payload LEAVES KAL-LE, and
# the store pins prove enqueue ACCEPTS the field — but nothing exercised the
# receiving bridge in between, which is Salem-side and is exactly where a
# silently-emptied card would come from.
#
# This is the "threaded at every production call site" trap one layer further
# along than the intake: two correct endpoints with an untested wire between.


def _sink_for(tmp_path) -> tuple[Any, Any]:
    """The REAL sink over a real store. `WebUser`/`WebConfig` are the actual
    config types, not stand-ins — the sink resolves its recipient through them
    and a namespace would quietly excuse a lookup that had broken."""
    from alfred.web.config import WebConfig, WebUser
    from alfred.web.notify_state import build_web_notify_sink

    store = WebNotifyStore.create(tmp_path / "notify.json")
    store.load()
    sink = build_web_notify_sink(store, WebConfig(users=[WebUser(name="andrew")]))
    return store, sink


def test_the_sink_carries_the_ticket_content_into_the_store(tmp_path) -> None:
    from alfred.web.identity import synthetic_chat_id

    store, sink = _sink_for(tmp_path)
    sink(
        payload={
            "text": "New ticket [bug] Payroll",
            "ticket_uid": TICKET_UID,
            "ticket_body": "Reported by: Ben\n\n## Repro\n1. Click Submit",
            "issue_number": 9,
            "web_notify": True,
        },
        from_peer="kal-le",
    )

    row = store.list_for(synthetic_chat_id("andrew"))[0]
    assert "Click Submit" in row["ticket_body"], (
        "the sink dropped the ticket body — the card would expand to nothing"
    )
    assert row["issue_number"] == 9


def test_the_sink_leaves_a_plain_notice_with_empty_content(tmp_path) -> None:
    """A non-ticket notice must not acquire phantom content, and must still
    land — the sink is shared by every peer notice, not just tickets."""
    from alfred.web.identity import synthetic_chat_id

    store, sink = _sink_for(tmp_path)
    sink(payload={"text": "just a notice"}, from_peer="kal-le")

    row = store.list_for(synthetic_chat_id("andrew"))[0]
    assert row["text"] == "just a notice"
    assert row["ticket_body"] == ""
