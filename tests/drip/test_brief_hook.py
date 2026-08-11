"""The ops-brief Campaigns section (#44b) — driven through the REAL assembler.

These pins go through ``generate_brief`` rather than the section renderer,
because the two failures that matter here are both assembler-level and a
renderer test cannot see either:

* **Deploy-inert.** An instance with no ``drip:`` block must gain no section.
  Pinning ``render_drip_body([]) is None`` proves nothing about whether the
  brief actually consults it.
* **The header seam.** ``brief/renderer.render_brief`` emits ``## {name}``
  from the section NAME, while ``render_drip_section`` returns a body that
  already carries its own ``## Campaigns``. Handing the assembler the wrong one
  prints the header twice — visible in the operator's brief, invisible to every
  unit test of either function.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from alfred.brief.config import load_from_unified as load_brief_config
from alfred.brief.state import StateManager
from alfred.drip.state import DONE, CampaignState, ItemState, save_state


def _patch_weather(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub BOTH weather paths — the daemon's module-bound name and the
    narration module's call-time import. Per the 2026-08-04 live-network
    incident, patching only the first leaves a real request to
    aviationweather.gov on every run."""
    async def _fake_weather(_cfg):
        # COLLECTING form: (markdown, parsed TAFs).
        return "*Weather: fixed for the test.*", []

    async def _fake_metars(_cfg):
        return []

    monkeypatch.setattr("alfred.brief.daemon.fetch_and_format_collect", _fake_weather)
    monkeypatch.setattr("alfred.brief.weather.fetch_metars", _fake_metars)


def _raw(tmp_path: Path, drip_block: dict | None = None) -> dict:
    vault = tmp_path / "vault"
    vault.mkdir(parents=True, exist_ok=True)
    raw: dict = {
        "vault": {"path": str(vault)},
        "logging": {"dir": str(tmp_path / "data")},
        "telegram": {"instance": {"name": "Salem"}},
        "brief": {"state": {"path": str(tmp_path / "data" / "brief_state.json")}},
    }
    if drip_block is not None:
        raw["drip"] = drip_block
    return raw


async def _render(tmp_path: Path, monkeypatch, drip_block: dict | None) -> str:
    from alfred.brief.daemon import generate_brief

    _patch_weather(monkeypatch)
    cfg = load_brief_config(_raw(tmp_path, drip_block))
    cfg.primary_telegram_user_id = None      # no push
    rel = await generate_brief(cfg, StateManager(cfg.state.path))
    return (Path(cfg.vault_path) / rel).read_text(encoding="utf-8")


async def test_brief_has_no_campaigns_section_when_drip_is_unconfigured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deploy-inert, end to end. This is the property that makes merging the
    feature safe for every instance that has not opted in."""
    body = await _render(tmp_path, monkeypatch, None)
    assert "Campaigns" not in body


async def test_configured_campaign_renders_exactly_one_header(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The header seam. Two ``## Campaigns`` lines is the specific defect that
    appears if the assembler is handed ``render_drip_section``'s output (which
    embeds its own header) instead of the headerless body."""
    wl = tmp_path / "wl.txt"
    wl.write_text("note/A.md::person/Gone::remove\n", encoding="utf-8")
    body = await _render(tmp_path, monkeypatch, {"campaigns": {
        "link001_repair": {"enabled": True, "worklist_path": str(wl)},
    }})

    assert body.count("## Campaigns") == 1
    assert "**link001_repair:**" in body


async def test_a_configured_campaign_that_never_ran_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ILB: 'configured, not yet run' is a scheduling problem and must not read
    as 'nothing to do'. Same silence, opposite meanings."""
    wl = tmp_path / "wl.txt"
    wl.write_text("note/A.md::person/Gone::remove\n", encoding="utf-8")
    body = await _render(tmp_path, monkeypatch, {"campaigns": {
        "link001_repair": {"enabled": True, "worklist_path": str(wl)},
    }})
    assert "not yet run" in body


async def test_a_disabled_campaign_still_gets_a_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A campaign someone turned off is exactly what an operator needs to see
    when they wonder why a backlog is not moving — so it is rendered, not
    omitted."""
    wl = tmp_path / "wl.txt"
    wl.write_text("note/A.md::person/Gone::remove\n", encoding="utf-8")
    body = await _render(tmp_path, monkeypatch, {"campaigns": {
        "link001_repair": {"enabled": False, "worklist_path": str(wl)},
    }})
    assert "disabled" in body


async def test_a_misconfigured_campaign_does_not_break_the_brief(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The morning brief is the operator's primary surface. A campaign missing
    its required path degrades to a missing line, never a missing brief."""
    body = await _render(tmp_path, monkeypatch, {"campaigns": {
        "gmail_backlog": {"enabled": True},      # no staging_dir
    }})
    assert "# " in body, "the brief still rendered"


async def test_the_brief_reports_persisted_progress_without_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A brief render reads the cursor; it must never advance it.

    Pinned by byte-comparing the state file across the render. A brief that
    could trigger real work would make the morning read a side effect, and a
    campaign that only advances when someone looks at it is not a scheduled
    drain.
    """
    wl = tmp_path / "wl.txt"
    wl.write_text(
        "note/A.md::person/Gone::remove\nnote/B.md::person/Gone::remove\n",
        encoding="utf-8",
    )
    state_path = tmp_path / "data" / "drip" / "salem" / "link001_repair_state.json"
    state = CampaignState(campaign="link001_repair")
    state.items["note/A.md::person/Gone::remove"] = ItemState(
        item_id="note/A.md::person/Gone::remove", state=DONE,
    )
    state.last_run_at = "2026-08-04T09:00:00+00:00"
    state.last_stop_reason = "budget_exhausted"
    save_state(state_path, state)
    before = state_path.read_bytes()

    body = await _render(tmp_path, monkeypatch, {"campaigns": {
        "link001_repair": {"enabled": True, "worklist_path": str(wl)},
    }})

    assert state_path.read_bytes() == before, "the render must not touch the cursor"
    assert "1 left" in body, "one of the two items is already done"
    assert "on budget" in body, "the persisted stop reason is reported"


async def test_a_misconfigured_campaign_renders_a_visible_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken campaign must be VISIBLE, not merely non-fatal.

    Dropping the line makes "this campaign is broken" and "this campaign was
    never configured" identical on the operator's primary surface. Not
    hypothetical: enabling link001_repair before its frozen work-list exists
    (task #50) produces exactly that silence on day one.
    """
    body = await _render(tmp_path, monkeypatch, {"campaigns": {
        "gmail_backlog": {"enabled": True},      # no staging_dir
    }})

    assert "## Campaigns" in body, "the section renders rather than vanishing"
    assert "**gmail_backlog:** ⚠ misconfigured" in body
    assert "staging_dir" in body, "the line names the key the operator must fix"


async def test_a_broken_campaign_does_not_cost_its_sibling_a_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both facts survive: one campaign's misconfiguration is reported AND the
    healthy campaign still reports its own progress."""
    wl = tmp_path / "wl.txt"
    wl.write_text("note/A.md::person/Gone::remove\n", encoding="utf-8")
    body = await _render(tmp_path, monkeypatch, {"campaigns": {
        "gmail_backlog": {"enabled": True},                       # broken
        "link001_repair": {"enabled": True, "worklist_path": str(wl)},
    }})

    assert "**gmail_backlog:** ⚠ misconfigured" in body
    assert "**link001_repair:**" in body
    assert "not yet run" in body
