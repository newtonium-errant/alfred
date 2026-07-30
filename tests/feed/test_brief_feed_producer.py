"""Brief → feed extractor pins (Feed Phase A producer #2, 3 clean sections).

Tests run unconditionally per ``feedback_regression_pin_unconditional.md``.
"""
from __future__ import annotations

from pathlib import Path

from alfred.brief.feed_producer import (
    event_feed_items,
    health_feed_items,
    peer_digest_feed_items,
)


# --- health: one item per NON-OK tool ---------------------------------------


def _write_bit(vault: Path, date_str: str = "2026-07-30") -> None:
    run = vault / "run"
    run.mkdir(parents=True, exist_ok=True)
    (run / f"Alfred BIT {date_str}.md").write_text(
        "---\ntype: run\n---\n\n"
        "## Summary\n"
        "[OK] curator  (120 ms)\n"
        "[WARN] surveyor  (92 ms) — ollama 404\n"
        "[FAIL] janitor — boom\n"
        "## Detail\n",
        encoding="utf-8",
    )


def test_health_emits_only_non_ok_tools(tmp_path: Path) -> None:
    _write_bit(tmp_path)
    items = health_feed_items(tmp_path, instance="salem")
    by_id = {it.id: it for it in items}
    assert set(by_id) == {"health:surveyor", "health:janitor"}  # ok curator excluded
    assert by_id["health:surveyor"].mode == "fyi"  # awareness kind
    assert "ollama 404" in by_id["health:surveyor"].title
    assert by_id["health:janitor"].evidence["status"] == "fail"


def test_health_all_ok_or_absent_is_empty(tmp_path: Path) -> None:
    # No BIT record → [] so reconcile marks any stale warns acted.
    assert health_feed_items(tmp_path, instance="salem") == []


# --- event: stable key = date + name (translation-focused) ------------------


def test_event_feed_items_translation(tmp_path: Path, monkeypatch) -> None:
    from alfred.brief.upcoming_events import _UpcomingItem

    def _fake_collect(vault_path, today, max_days, prefs=None):
        return ([
            _UpcomingItem(date_iso="2026-08-01", name="Payroll due", location=None, description=None),
            _UpcomingItem(date_iso="2026-08-03", name="Vet", location=None, description=None, rec_type="task"),
        ], 0)

    monkeypatch.setattr("alfred.brief.upcoming_events._collect_items", _fake_collect)

    class _Cfg:
        max_days_ahead = 30

    items = event_feed_items(_Cfg(), tmp_path, __import__("datetime").date(2026, 7, 30), instance="salem")
    ids = {it.id for it in items}
    assert ids == {"event:2026-08-01|Payroll due", "event:2026-08-03|Vet"}
    assert all(it.kind == "event" and it.mode == "fyi" for it in items)


# --- peer_digest: stable key = peer + date ----------------------------------


def _write_peer_digest(vault: Path, peer: str, today_iso: str) -> None:
    run = vault / "run"
    run.mkdir(parents=True, exist_ok=True)
    (run / f"Peer Digest {peer}.md").write_text(
        f"---\ntype: run\nsource: peer\npeer: {peer}\ncreated: {today_iso}\nreceived_at: {today_iso}T09:00:00+00:00\n---\n\nbody\n",
        encoding="utf-8",
    )


def test_peer_digest_feed_items(tmp_path: Path) -> None:
    _write_peer_digest(tmp_path, "kalle", "2026-07-30")
    items = peer_digest_feed_items(tmp_path, "2026-07-30", instance="salem")
    assert len(items) == 1
    assert items[0].id == "peer_digest:kalle|2026-07-30"
    assert items[0].mode == "fyi"


def test_peer_digest_none_today_is_empty(tmp_path: Path) -> None:
    _write_peer_digest(tmp_path, "kalle", "2026-07-29")  # yesterday
    assert peer_digest_feed_items(tmp_path, "2026-07-30", instance="salem") == []
