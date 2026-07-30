"""Feed Phase A — the load-bearing BRIEF parity gate.

A full ``generate_brief`` run must produce BYTE-IDENTICAL output whether the feed
is enabled or disabled — the SectionResult seam unwraps to the same (name,
markdown) tuples the renderer always took, and the feed emission runs after the
render off the same section objects, so it can never perturb the markdown.

Tests run unconditionally per ``feedback_regression_pin_unconditional.md``.
"""
from __future__ import annotations

from pathlib import Path

import frontmatter  # type: ignore[import-untyped]
import pytest

from alfred.brief.config import BriefConfig
from alfred.brief.state import StateManager
from alfred.feed import FeedConfig, FeedStore


def _write_bit(vault: Path) -> None:
    run = vault / "run"
    run.mkdir(parents=True, exist_ok=True)
    (run / "Alfred BIT 2026-07-30.md").write_text(
        "---\ntype: run\n---\n\n## Summary\n[OK] curator  (1 ms)\n[WARN] surveyor — ollama 404\n## Detail\n",
        encoding="utf-8",
    )


def _config(tmp_path: Path, *, feed_enabled: bool) -> BriefConfig:
    vault = tmp_path / "vault"
    vault.mkdir(parents=True, exist_ok=True)
    _write_bit(vault)
    cfg = BriefConfig(vault_path=str(vault), instance_name="salem")
    cfg.state.path = str(tmp_path / "data" / "brief_state.json")
    cfg.primary_telegram_user_id = None  # no push
    cfg.feed = FeedConfig(enabled=feed_enabled, store_path=str(tmp_path / "data" / "feed.jsonl"))
    return cfg


def _patch_weather(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_weather(_weather_config):
        return "*Weather: fixed for the test.*"

    monkeypatch.setattr("alfred.brief.daemon.fetch_and_format", _fake_weather)


async def _run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, feed_enabled: bool) -> str:
    from alfred.brief.daemon import generate_brief

    _patch_weather(monkeypatch)
    cfg = _config(tmp_path, feed_enabled=feed_enabled)
    rel = await generate_brief(cfg, StateManager(cfg.state.path))
    return (Path(cfg.vault_path) / rel).read_text(encoding="utf-8")


async def test_brief_output_byte_identical_feed_on_vs_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    on = await _run(tmp_path / "on", monkeypatch, feed_enabled=True)
    off = await _run(tmp_path / "off", monkeypatch, feed_enabled=False)

    on_doc, off_doc = frontmatter.loads(on), frontmatter.loads(off)
    # The rendered markdown BODY is byte-identical (the render is what the
    # parity gate protects).
    assert on_doc.content == off_doc.content
    # Frontmatter is identical too, modulo the wall-clock ``started`` stamp
    # (per-run, orthogonal to the feed — same class as A2's fired_at).
    on_fm, off_fm = dict(on_doc.metadata), dict(off_doc.metadata)
    on_fm.pop("started", None)
    off_fm.pop("started", None)
    assert on_fm == off_fm

    # Non-vacuous: the feed actually ran when enabled (a health WARN item landed)
    # and did NOT when disabled (no store file).
    assert (tmp_path / "on" / "data" / "feed.jsonl").is_file()
    assert not (tmp_path / "off" / "data" / "feed.jsonl").exists()

    folded = FeedStore(tmp_path / "on" / "data" / "feed.jsonl").load()
    health = [it for it in folded.values() if it.kind == "health"]
    assert len(health) == 1
    assert health[0].id == "health:surveyor"
    assert health[0].state == "open"
