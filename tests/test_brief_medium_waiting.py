"""#27 slice 2 — the brief's "medium emails waiting" line (contract pins).

Medium emails surface as ``email_tier`` calibration cards; while unconfirmed they
sit OPEN in the feed store. The brief must stop being SILENT about that held set —
one ILB-honest line in Operations, with an explicit zero-state (a held-count of 0
is a real, tested state). The line reads the feed store (brief → feed leaf import)
and is threaded UNCONDITIONALLY by the daemon so the feed-parity golden gate stays
byte-identical.

Pins: count semantics (open-medium only; acted / non-medium / non-email_tier
excluded), explicit zero-state, singular/plural, read-error degradation + its ILB
log, and the format_operations_section threading (absent path → byte-identical for
existing callers; present path → the line rides in).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import structlog

from alfred.brief.operations import _medium_waiting_summary, format_operations_section
from alfred.feed import FeedItem, FeedStore


def _seed(
    store: FeedStore, *, kind: str, priority: str | None, state: str, key: str,
) -> str:
    ev: dict[str, Any] = {"record_path": key}
    if priority is not None:
        ev["classifier_priority"] = priority
    item = FeedItem.create(
        kind=kind, stable_key=key, instance="salem", title="t", evidence=ev,
    )
    store.upsert(item)
    if state != "open":
        store.set_state(item.id, state)
    return item.id


def _store(tmp_path: Path) -> FeedStore:
    return FeedStore(str(tmp_path / "feed.jsonl"))


# ---------------------------------------------------------------------------
# _medium_waiting_summary — count semantics
# ---------------------------------------------------------------------------


def test_counts_only_open_medium_email_tier(tmp_path: Path) -> None:
    """Open-medium email_tier are counted; acted-medium, open-high/low, and
    non-email_tier (even if medium) are all excluded."""
    store = _store(tmp_path)
    _seed(store, kind="email_tier", priority="medium", state="open", key="note/a.md")
    _seed(store, kind="email_tier", priority="medium", state="open", key="note/b.md")
    _seed(store, kind="email_tier", priority="medium", state="acted", key="note/c.md")  # confirmed
    _seed(store, kind="email_tier", priority="high", state="open", key="note/d.md")      # not medium
    _seed(store, kind="email_tier", priority="low", state="open", key="note/e.md")       # not medium
    _seed(store, kind="proposal", priority="medium", state="open", key="prop/f.md")      # not email_tier

    assert _medium_waiting_summary(str(tmp_path / "feed.jsonl")) == "📥 2 medium emails waiting"


def test_zero_state_is_explicit(tmp_path: Path) -> None:
    """Empty store → explicit zero-state line (ILB: held-count of 0 is not
    silence). A missing store file folds to {} without raising."""
    assert _medium_waiting_summary(str(tmp_path / "does_not_exist.jsonl")) == "📥 No medium emails waiting"


def test_singular_one_waiting(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed(store, kind="email_tier", priority="medium", state="open", key="note/solo.md")
    assert _medium_waiting_summary(str(tmp_path / "feed.jsonl")) == "📥 1 medium email waiting"


def test_reopened_medium_is_counted(tmp_path: Path) -> None:
    """An item acted then reopened (state back to open) counts again — the fold's
    last-write-wins state is what matters, not history."""
    store = _store(tmp_path)
    iid = _seed(store, kind="email_tier", priority="medium", state="acted", key="note/re.md")
    store.set_state(iid, "open")
    assert _medium_waiting_summary(str(tmp_path / "feed.jsonl")) == "📥 1 medium email waiting"


def test_read_error_degrades_and_logs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A fold error → explicit 'unavailable' line (never crashes the bare-called
    Operations render) + an ILB warning so the failure is greppable."""
    def _boom(self):  # noqa: ANN001
        raise RuntimeError("corrupt fold")

    monkeypatch.setattr("alfred.feed.store.FeedStore.load", _boom)

    with structlog.testing.capture_logs() as cap:
        line = _medium_waiting_summary(str(tmp_path / "feed.jsonl"))

    assert line == "📥 Medium-email count unavailable"
    fails = [c for c in cap if c.get("event") == "operations.medium_waiting_failed"]
    assert len(fails) == 1
    assert fails[0]["error_type"] == "RuntimeError"


# ---------------------------------------------------------------------------
# format_operations_section — threading (byte-parity for existing callers)
# ---------------------------------------------------------------------------


def _min_env(tmp_path: Path) -> tuple[str, str]:
    """A minimal data_dir + vault so format_operations_section renders."""
    data = tmp_path / "data"
    vault = tmp_path / "vault"
    data.mkdir(parents=True, exist_ok=True)
    vault.mkdir(parents=True, exist_ok=True)
    return str(data), str(vault)


def test_operations_omits_line_without_feed_path(tmp_path: Path) -> None:
    """Existing callers (no feed_store_path) → NO waiting line → byte-identical
    to the pre-#27 Operations render."""
    data_dir, vault = _min_env(tmp_path)
    md = format_operations_section(data_dir, vault, since="2026-08-01")
    assert "medium email" not in md
    assert "📥" not in md


def test_operations_includes_line_with_feed_path(tmp_path: Path) -> None:
    """When the daemon threads the feed store path, the line rides into the
    Operations section (beside the quarantine line)."""
    data_dir, vault = _min_env(tmp_path)
    store = FeedStore(str(tmp_path / "feed.jsonl"))
    _seed(store, kind="email_tier", priority="medium", state="open", key="note/x.md")
    _seed(store, kind="email_tier", priority="medium", state="open", key="note/y.md")
    _seed(store, kind="email_tier", priority="medium", state="open", key="note/z.md")

    md = format_operations_section(
        data_dir, vault, since="2026-08-01", feed_store_path=str(tmp_path / "feed.jsonl"),
    )
    assert "**📥 3 medium emails waiting**" in md
    # Sits alongside the quarantine line (both email-classifier surfaces).
    assert "Spam quarantine" in md


def test_operations_zero_state_line_with_feed_path(tmp_path: Path) -> None:
    """Threaded but empty store → the explicit zero-state line (never silent)."""
    data_dir, vault = _min_env(tmp_path)
    md = format_operations_section(
        data_dir, vault, since="2026-08-01", feed_store_path=str(tmp_path / "feed.jsonl"),
    )
    assert "**📥 No medium emails waiting**" in md


# ---------------------------------------------------------------------------
# end-to-end — the daemon threads config.feed.store_path into the brief
# ---------------------------------------------------------------------------
#
# The parity gate (feed-on == feed-off) can't catch a threading regression: if
# the daemon forgot to pass the store path, BOTH briefs would render the zero-
# state and parity would still hold. This pin proves a seeded store's real count
# rides into the FULL generate_brief output.


async def test_medium_waiting_line_rides_into_full_brief(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from alfred.brief.config import BriefConfig
    from alfred.brief.daemon import generate_brief
    from alfred.brief.state import StateManager
    from alfred.feed import FeedConfig

    vault = tmp_path / "vault"
    (vault / "run").mkdir(parents=True, exist_ok=True)
    (vault / "run" / "Alfred BIT 2026-08-01.md").write_text(
        "---\ntype: run\n---\n\n## Summary\n[OK] curator  (1 ms)\n## Detail\n",
        encoding="utf-8",
    )
    store_path = tmp_path / "data" / "feed.jsonl"
    store_path.parent.mkdir(parents=True, exist_ok=True)
    store = FeedStore(str(store_path))
    _seed(store, kind="email_tier", priority="medium", state="open", key="note/m1.md")
    _seed(store, kind="email_tier", priority="medium", state="open", key="note/m2.md")
    _seed(store, kind="email_tier", priority="medium", state="acted", key="note/m3.md")  # excluded
    _seed(store, kind="email_tier", priority="high", state="open", key="note/h1.md")      # excluded

    async def _fake_weather(_wc):
        # COLLECTING form: (markdown, parsed TAFs) — generate_brief takes this
        # one now so the feed reuses its fetch.
        return "*Weather: fixed.*", []

    async def _no_metars(_wc):
        return []

    monkeypatch.setattr("alfred.brief.daemon.fetch_and_format_collect", _fake_weather)
    # The NARRATION path reaches weather by a function-local import of
    # alfred.brief.weather.fetch_metars, which the daemon patch above cannot
    # cover — without this, generate_brief makes a live aviationweather.gov
    # call. Rationale: tests/feed/test_brief_feed_parity.py::_patch_weather.
    monkeypatch.setattr("alfred.brief.weather.fetch_metars", _no_metars)

    cfg = BriefConfig(vault_path=str(vault), instance_name="salem")
    cfg.state.path = str(tmp_path / "data" / "brief_state.json")
    cfg.primary_telegram_user_id = None
    cfg.feed = FeedConfig(enabled=True, store_path=str(store_path))

    rel = await generate_brief(cfg, StateManager(cfg.state.path))
    body = (vault / rel).read_text(encoding="utf-8")

    # The real open-medium count (2) rides into the brief — proves the daemon
    # threaded config.feed.store_path all the way through.
    assert "📥 2 medium emails waiting" in body
