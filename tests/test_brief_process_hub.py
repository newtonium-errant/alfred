"""LINK001 closure — the Morning Brief process hub note.

Every brief run record carries ``process: [[process/Morning Brief]]``
(renderer.py) but nothing ever created the hub note, so the janitor
flagged LINK001 daily and (no create scope) could never self-heal it.
The daemon now ensure-creates the hub writer-side, idempotently.

Identical defect-class and fix-shape to the BIT hub (commit 02ff294,
``tests/health/test_bit.py::TestProcessHub``) — these tests mirror that
suite deliberately.
"""

from __future__ import annotations

from pathlib import Path

import structlog

from alfred.brief import daemon as daemon_mod
from alfred.brief import weather as weather_mod
from alfred.brief.config import BriefConfig, OutputConfig, StateConfig
from alfred.brief.daemon import ensure_process_hub
from alfred.brief.renderer import (
    process_hub_name,
    render_brief,
    render_process_hub_record,
)
from alfred.brief.state import StateManager


class TestProcessHubName:
    """process_hub_name — pure hub-name derivation."""

    def test_default_template_derives_morning_brief(self) -> None:
        assert process_hub_name("Morning Brief {date}") == "Morning Brief"

    def test_custom_template_derives_matching_hub(self) -> None:
        assert process_hub_name("KAL-LE Brief {date}") == "KAL-LE Brief"

    def test_empty_template_falls_back(self) -> None:
        # The wikilink must never render as ``[[process/]]``.
        assert process_hub_name("") == "Morning Brief"

    def test_date_only_template_falls_back(self) -> None:
        assert process_hub_name("{date}") == "Morning Brief"


class TestRenderBriefProcessLink:
    """render_brief ``process`` field pins."""

    def test_default_process_link_byte_identical(self) -> None:
        """Regression pin: default config emits the historical link."""
        cfg = BriefConfig(vault_path="/tmp/unused")
        fm, _ = render_brief("2026-06-12", [("Weather", "x")], cfg)
        assert fm["process"] == "[[process/Morning Brief]]"

    def test_custom_template_process_link_matches_hub(self) -> None:
        cfg = BriefConfig(
            vault_path="/tmp/unused",
            output=OutputConfig(name_template="KAL-LE Brief {date}"),
        )
        fm, _ = render_brief("2026-06-12", [("Weather", "x")], cfg)
        assert fm["process"] == "[[process/KAL-LE Brief]]"


class TestRenderProcessHubRecord:
    def test_frontmatter_shape(self) -> None:
        fm, body = render_process_hub_record("Morning Brief", "2026-06-12")
        assert fm["type"] == "process"
        assert fm["status"] == "active"
        assert fm["name"] == "Morning Brief"
        assert fm["created"] == "2026-06-12"
        assert "# Morning Brief" in body


class TestEnsureProcessHub:
    def test_first_call_creates_hub(self, tmp_path: Path) -> None:
        vault = tmp_path / "vault"
        vault.mkdir()
        cfg = BriefConfig(vault_path=str(vault))
        created = ensure_process_hub(vault, cfg, "2026-06-12")
        assert created is True
        hub = vault / "process" / "Morning Brief.md"
        assert hub.exists()
        content = hub.read_text(encoding="utf-8")
        assert "type: process" in content
        assert "status: active" in content
        assert "name: Morning Brief" in content

    def test_second_call_is_idempotent(self, tmp_path: Path) -> None:
        vault = tmp_path / "vault"
        vault.mkdir()
        cfg = BriefConfig(vault_path=str(vault))
        assert ensure_process_hub(vault, cfg, "2026-06-12") is True
        hub = vault / "process" / "Morning Brief.md"
        first_content = hub.read_text(encoding="utf-8")
        assert ensure_process_hub(vault, cfg, "2026-06-13") is False
        assert hub.read_text(encoding="utf-8") == first_content

    def test_create_logs_once_then_silent(self, tmp_path: Path) -> None:
        """capture_logs pin: CREATE logs exactly once; existing hub none."""
        vault = tmp_path / "vault"
        vault.mkdir()
        cfg = BriefConfig(vault_path=str(vault))
        with structlog.testing.capture_logs() as captured:
            ensure_process_hub(vault, cfg, "2026-06-12")
        created_events = [
            c for c in captured
            if c.get("event") == "brief.process_hub_created"
        ]
        assert len(created_events) == 1
        assert created_events[0]["path"].endswith("process/Morning Brief.md")

        with structlog.testing.capture_logs() as captured_2nd:
            ensure_process_hub(vault, cfg, "2026-06-13")
        assert [
            c for c in captured_2nd
            if c.get("event") == "brief.process_hub_created"
        ] == []

    def test_create_failure_is_loud_not_fatal(self, tmp_path: Path) -> None:
        """A FILE at vault/process makes mkdir raise — warn, don't raise."""
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "process").write_text("not a directory", encoding="utf-8")
        cfg = BriefConfig(vault_path=str(vault))
        with structlog.testing.capture_logs() as captured:
            created = ensure_process_hub(vault, cfg, "2026-06-12")
        assert created is False
        failed_events = [
            c for c in captured
            if c.get("event") == "brief.process_hub_create_failed"
        ]
        assert len(failed_events) == 1
        assert failed_events[0]["error_type"]
        assert failed_events[0]["error"]
        assert failed_events[0]["path"].endswith("process/Morning Brief.md")


class TestGenerateBriefCreatesHub:
    async def test_generate_brief_creates_hub_and_matching_link(
        self, tmp_path, monkeypatch
    ) -> None:
        """End-to-end LINK001 closure: brief record + hub, link targets hub.

        Weather fetchers stubbed to empty responses — network isolation
        only; render and vault write run for real (same fixture shape as
        ``test_generate_brief_empty_vault_smoke``).
        """
        vault = tmp_path / "vault"
        vault.mkdir()
        data_dir = tmp_path / "data"
        data_dir.mkdir()

        async def _no_metars(config):  # type: ignore[no-untyped-def]
            return []

        async def _no_tafs(config):  # type: ignore[no-untyped-def]
            return []

        monkeypatch.setattr(weather_mod, "fetch_metars", _no_metars)
        monkeypatch.setattr(weather_mod, "fetch_tafs", _no_tafs)

        config = BriefConfig(
            vault_path=str(vault),
            state=StateConfig(path=str(data_dir / "brief_state.json")),
        )
        state_mgr = StateManager(config.state.path)

        rel_path = await daemon_mod.generate_brief(config, state_mgr)

        assert rel_path is not None
        record = vault / rel_path
        assert record.exists()
        hub = vault / "process" / "Morning Brief.md"
        assert hub.exists()
        # The record's ``process`` link target matches the hub filename —
        # the janitor's LINK001 resolves against this exact pair.
        record_content = record.read_text(encoding="utf-8")
        assert f"[[process/{hub.stem}]]" in record_content

    async def test_update_weather_create_branch_creates_hub(
        self, tmp_path, monkeypatch
    ) -> None:
        """update_weather's no-brief-yet branch also writes a run record
        carrying the hub link — it must ensure the hub too."""
        vault = tmp_path / "vault"
        vault.mkdir()
        data_dir = tmp_path / "data"
        data_dir.mkdir()

        async def _no_metars(config):  # type: ignore[no-untyped-def]
            return []

        async def _no_tafs(config):  # type: ignore[no-untyped-def]
            return []

        monkeypatch.setattr(weather_mod, "fetch_metars", _no_metars)
        monkeypatch.setattr(weather_mod, "fetch_tafs", _no_tafs)

        config = BriefConfig(
            vault_path=str(vault),
            state=StateConfig(path=str(data_dir / "brief_state.json")),
        )

        rel_path = await daemon_mod.update_weather(config)

        assert rel_path is not None
        assert (vault / rel_path).exists()
        assert (vault / "process" / "Morning Brief.md").exists()
