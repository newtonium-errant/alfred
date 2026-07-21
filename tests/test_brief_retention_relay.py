"""Tests for the brief's STAY-C Retention Review Relay section (task #13 §4 / C3).

Load-bearing property: the brief renders ONLY the PHI-free ``review_due`` count + the OPAQUE oldest
encounter_id — never encounter labels/bodies (the brief transits Telegram; STAY-C uses none).
Covers the intentionally-left-blank states (disabled → omitted; absent / unreadable / stale →
explicit visible line) + the config loader. The spool format is the cross-component contract the
retention sweep's ``_write_review_spool`` produces (pinned there in test_scribe_retention_sweep.py).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from alfred.brief.config import StaycRetentionRelayConfig, load_from_unified
from alfred.brief.stayc_relay import render_stayc_retention_relay_section

_GEN_AT = "2026-07-18T03:20:10Z"
_NOW = datetime(2026, 7, 18, 4, 0, 0, tzinfo=timezone.utc)  # 40 min after gen


def _cfg(spool_path: str, *, enabled: bool = True, staleness_hours: float = 25.0):
    return StaycRetentionRelayConfig(
        enabled=enabled, spool_path=spool_path, staleness_hours=staleness_hours)


def _spool(tmp_path, *, review_due, oldest="enc-0123456789abcdef", generated_at=_GEN_AT):
    p = tmp_path / "retention_review.spool"
    p.write_text(
        "# STAY-C retention review — relay snapshot\n"
        f"generated_at: {generated_at}\n"
        f"review_due: {review_due}\n"
        f"oldest_encounter_id: {oldest}\n",
        encoding="utf-8")
    return p


def test_disabled_returns_empty(tmp_path):
    assert render_stayc_retention_relay_section(_cfg("", enabled=False), _NOW) == ""


def test_enabled_but_no_path_renders_not_configured(tmp_path):
    out = render_stayc_retention_relay_section(_cfg("", enabled=True), _NOW)
    assert "not configured" in out


def test_fresh_review_due_renders_count_and_opaque_oldest(tmp_path):
    spool = _spool(tmp_path, review_due=2, oldest="enc-abc123")
    out = render_stayc_retention_relay_section(_cfg(str(spool)), _NOW)
    assert "2 encounters over the s.50 review window" in out
    assert "enc-abc123" in out                               # opaque id only
    assert "destroy playbook" in out


def test_fresh_singular(tmp_path):
    out = render_stayc_retention_relay_section(_cfg(str(_spool(tmp_path, review_due=1))), _NOW)
    assert "1 encounter over the s.50 review window" in out


def test_fresh_zero_renders_none_line(tmp_path):
    out = render_stayc_retention_relay_section(_cfg(str(_spool(tmp_path, review_due=0, oldest=""))), _NOW)
    assert out == "STAY-C retention: no encounters over the s.50 review window."


def test_render_never_leaks_phi(tmp_path):
    # even if a (contract-violating) label somehow reached the body, the reader parses ONLY the header
    # fields — a body line is never rendered.
    spool = _spool(tmp_path, review_due=1, oldest="enc-x")
    spool.write_text(spool.read_text() + "\nnote: Jane Doe DOB 1990 chest pain\n", encoding="utf-8")
    out = render_stayc_retention_relay_section(_cfg(str(spool)), _NOW)
    assert "jane" not in out.lower() and "doe" not in out.lower()


def test_absent_spool_renders_no_data(tmp_path):
    out = render_stayc_retention_relay_section(_cfg(str(tmp_path / "nope.spool")), _NOW)
    assert "no data" in out and "not found" in out


def test_stale_spool_is_visible(tmp_path):
    spool = _spool(tmp_path, review_due=3, generated_at="2026-07-15T03:00:00Z")   # ~73h before _NOW
    out = render_stayc_retention_relay_section(_cfg(str(spool), staleness_hours=25.0), _NOW)
    assert "stale" in out


def test_unparseable_header_renders_no_data(tmp_path):
    p = tmp_path / "retention_review.spool"
    p.write_text("garbage with no header fields\n", encoding="utf-8")
    out = render_stayc_retention_relay_section(_cfg(str(p)), _NOW)
    assert "no data" in out


def test_config_loader_parses_retention_relay():
    cfg = load_from_unified({"brief": {"stayc_retention_relay": {
        "enabled": True, "spool_path": "/data/retention_review.spool", "staleness_hours": 30}}})
    r = cfg.stayc_retention_relay
    assert r.enabled is True and r.spool_path == "/data/retention_review.spool"
    assert r.staleness_hours == 30.0


def test_config_loader_defaults_off():
    cfg = load_from_unified({"brief": {}})
    assert cfg.stayc_retention_relay.enabled is False
    assert cfg.stayc_retention_relay.spool_path == ""
