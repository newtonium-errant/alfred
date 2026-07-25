"""#20 P5 B1 — recurrence→promote Daily Sync surface pins (PROPOSE-ONLY).

Covers the ``tier_recurrence`` section: omits (None) when disabled or when the vault path isn't wired;
the intentionally-left-blank sentinel when enabled-but-nothing-over-threshold; the numbered proposal
list (global start_index) when populated; and the load-bearing NEVER-AUTO-MUTATE property at the
section level (materialize-on-render writes ONLY the pending queue — no routine record).
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import structlog

from alfred.daily_sync import recurrence_section as rs
from alfred.daily_sync.config import DailySyncConfig, TierRecurrenceConfig
from alfred.tier import promote


_TODAY = date(2026, 5, 28)


def _cfg(tmp_path: Path, *, enabled: bool = True) -> DailySyncConfig:
    return DailySyncConfig(
        enabled=True,
        tier_recurrence=TierRecurrenceConfig(
            enabled=enabled,
            pending_path=str(tmp_path / "pending.jsonl"),
            decided_path=str(tmp_path / "decided.jsonl"),
            threshold_done_days=3,
            window_days=30,
        ),
    )


def _write_daily(vault: Path, date_str: str, item: str, source: str, done_at: str) -> None:
    daily = vault / "daily"
    daily.mkdir(parents=True, exist_ok=True)
    (daily / f"{date_str}.md").write_text(
        f"---\ntier_curation:\n  t3:\n    - item: {item}\n      source: {source}\n"
        f"      done_at: '{done_at}'\n---\n\nbody\n",
        encoding="utf-8",
    )


def _seed_recurring(vault: Path, item: str = "Rake leaves") -> None:
    for d in ("2026-05-10", "2026-05-15", "2026-05-20"):
        _write_daily(vault, d, item, "operator-adhoc", d)


def test_disabled_omits_section(tmp_path: Path) -> None:
    rs.set_vault_path(tmp_path / "vault")
    assert rs.recurrence_review_section(_cfg(tmp_path, enabled=False), _TODAY) is None


def test_vault_not_wired_omits_section(tmp_path: Path) -> None:
    rs._VAULT_PATH_HOLDER["path"] = None  # simulate a daemon that didn't wire set_vault_path
    with structlog.testing.capture_logs() as cap:
        out = rs.recurrence_review_section(_cfg(tmp_path), _TODAY)
    assert out is None
    assert [c for c in cap if c.get("event") == "tier_recurrence.vault_not_wired"]


def test_enabled_empty_emits_ilb_sentinel(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    (vault / "daily").mkdir(parents=True)   # a vault with no recurring chores
    rs.set_vault_path(vault)
    with structlog.testing.capture_logs() as cap:
        out = rs.recurrence_review_section(_cfg(tmp_path), _TODAY)
    assert out is not None
    assert out.startswith("## Recurring task review")
    assert "No recurring ad-hoc items to propose" in out
    assert [c for c in cap if c.get("event") == "tier_recurrence.no_proposals"]


def test_enabled_with_proposal_renders_numbered_list(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _seed_recurring(vault)
    rs.set_vault_path(vault)
    with structlog.testing.capture_logs() as cap:
        out = rs.recurrence_review_section(_cfg(tmp_path), _TODAY, start_index=5)
    assert out is not None
    assert out.startswith("## Recurring task review (1 item)")
    assert "5. “Rake leaves” — done 3 days in the last 30 → promote to a routine?" in out
    assert "Reply with `N approve` / `N reject`." in out
    assert [c for c in cap if c.get("event") == "tier_recurrence.surfaced"]
    # global numbering starts at start_index; batch captured for reply routing (B2)
    batch = rs.consume_last_batch()
    assert len(batch) == 1 and batch[0].item_number == 5
    assert batch[0].proposal_id == promote.proposal_id(promote.query_key("Rake leaves"))


def test_section_materialize_never_writes_a_routine_record(tmp_path: Path) -> None:
    """Section-level NEVER-AUTO-MUTATE: rendering the section materializes the pending queue but
    writes NO routine/ record and NO decided row."""
    vault = tmp_path / "vault"
    (vault / "routine").mkdir(parents=True)
    _seed_recurring(vault)
    rs.set_vault_path(vault)
    cfg = _cfg(tmp_path)
    rs.recurrence_review_section(cfg, _TODAY)
    assert len(promote.load_pending(cfg.tier_recurrence.pending_path)) == 1   # pending grew
    assert list((vault / "routine").glob("*.md")) == []                       # NO routine record
    assert not Path(cfg.tier_recurrence.decided_path).exists()                # NO decided row (B2 only)


def test_section_render_is_idempotent(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _seed_recurring(vault)
    rs.set_vault_path(vault)
    cfg = _cfg(tmp_path)
    rs.recurrence_review_section(cfg, _TODAY)
    rs.recurrence_review_section(cfg, _TODAY)   # re-render (re-assembly) → no duplicate pending row
    assert len(promote.load_pending(cfg.tier_recurrence.pending_path)) == 1
