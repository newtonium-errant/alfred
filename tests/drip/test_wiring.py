"""Drip config + ignition (#44b) — the wiring the machinery was missing.

WHY THIS EXISTS. #44 shipped a correct runner, two correct campaigns, and a
correct brief line, with ZERO importers: nothing built a campaign, nothing
called the runner, nothing rendered the line. This suite pins the layer that
turns config into a run, and it pins it at the two places that can silently
fail:

* **Budgets must travel from CONFIG to the runner.** The runner takes them as
  required keyword arguments, so a caller that types ``12`` inline satisfies the
  type checker, passes every unit test, and quietly ignores the operator. The
  threading pins below drive a PRODUCTION entry point and inspect what the
  runner actually received — a pin that called ``run_increment`` directly would
  be green against exactly the build this guards against.
* **Deploy-inert must stay inert.** An instance with no ``drip:`` block must
  gain no state files, no CLI work and no brief section.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
import structlog

from alfred.drip import cli as drip_cli
from alfred.drip.config import (
    DEFAULT_MAX_ITEMS_PER_RUN,
    DEFAULT_MAX_ITEMS_PER_WEEK,
    CampaignConfig,
    DripConfig,
    load_from_unified,
)
from alfred.drip.runner import STOP_DISABLED
from alfred.drip.state import DONE, IN_FLIGHT, CampaignState, ItemState
from alfred.drip.wiring import (
    DripConfigError,
    build_campaign,
    build_progress,
    load_worklist_file,
)

TODAY = date(2026, 8, 5)


def _raw(drip_block: dict | None = None, **extra) -> dict:
    """A unified config dict shaped like a real one."""
    raw: dict = {
        "vault": {"path": "/tmp/vault-not-touched"},
        "logging": {"dir": "/tmp/data-not-touched"},
        "telegram": {"instance": {"name": "Salem"}},
    }
    if drip_block is not None:
        raw["drip"] = drip_block
    raw.update(extra)
    return raw


# ---------------------------------------------------------------------------
# Config — deploy-inert, ruled defaults, schema tolerance
# ---------------------------------------------------------------------------


def test_absent_drip_block_is_deploy_inert() -> None:
    """No ``drip:`` block ⇒ nothing configured, nothing enabled.

    The load-bearing property for merging this at all: every instance that has
    not opted in must behave exactly as it did before.
    """
    cfg = load_from_unified(_raw())
    assert cfg.campaigns == {}
    assert cfg.enabled_campaigns() == {}
    assert cfg.enabled is False


def test_absent_block_emits_the_inert_signal() -> None:
    """ILB: 'no drip block' and 'the config blew up' must not look alike."""
    with structlog.testing.capture_logs() as captured:
        load_from_unified(_raw())
    matches = [c for c in captured if c.get("event") == "drip.config.absent"]
    assert len(matches) == 1
    assert matches[0]["instance"] == "Salem"


def test_configured_but_nothing_enabled_emits_its_own_signal() -> None:
    """A DIFFERENT fact from the absent case: the operator wrote a block and
    nothing in it will run. Same silence downstream, opposite diagnosis."""
    with structlog.testing.capture_logs() as captured:
        cfg = load_from_unified(_raw({"campaigns": {"link001_repair": {}}}))
    assert cfg.enabled_campaigns() == {}
    matches = [
        c for c in captured
        if c.get("event") == "drip.config.no_enabled_campaigns"
    ]
    assert len(matches) == 1
    assert matches[0]["configured"] == ["link001_repair"]


def test_ruled_budget_defaults_apply_when_omitted() -> None:
    """D1 as ratified: 12 per run, 60 per week. A campaign block that omits
    them still gets the ruled numbers rather than whatever a call site
    happened to pass."""
    cfg = load_from_unified(
        _raw({"campaigns": {"link001_repair": {"enabled": True}}})
    )
    c = cfg.campaigns["link001_repair"]
    assert c.max_items_per_run == DEFAULT_MAX_ITEMS_PER_RUN == 12
    assert c.max_items_per_week == DEFAULT_MAX_ITEMS_PER_WEEK == 60


def test_config_values_override_the_defaults() -> None:
    cfg = load_from_unified(
        _raw({"campaigns": {"link001_repair": {
            "enabled": True, "max_items_per_run": 3, "max_items_per_week": 7,
        }}})
    )
    c = cfg.campaigns["link001_repair"]
    assert (c.max_items_per_run, c.max_items_per_week) == (3, 7)


def test_kind_defaults_to_the_block_key() -> None:
    """``gmail_backlog:`` needs no redundant ``kind: gmail_backlog``."""
    cfg = load_from_unified(_raw({"campaigns": {"gmail_backlog": {}}}))
    assert cfg.campaigns["gmail_backlog"].kind == "gmail_backlog"


def test_unknown_keys_are_tolerated_not_fatal() -> None:
    """The load-time schema-tolerance contract: a block written by a newer
    version loads on rollback."""
    cfg = load_from_unified(
        _raw({"campaigns": {"link001_repair": {
            "enabled": True, "some_future_key": "whatever",
        }}})
    )
    assert cfg.campaigns["link001_repair"].enabled is True


def test_env_substitution_reaches_campaign_paths(monkeypatch) -> None:
    monkeypatch.setenv("DRIP_TEST_STAGING", "/tmp/staged-here")
    cfg = load_from_unified(
        _raw({"campaigns": {"gmail_backlog": {
            "enabled": True, "staging_dir": "${DRIP_TEST_STAGING}",
        }}})
    )
    assert cfg.campaigns["gmail_backlog"].staging_dir == "/tmp/staged-here"


# ---------------------------------------------------------------------------
# Wiring — required paths fail LOUD, never with a plausible default
# ---------------------------------------------------------------------------


def test_gmail_without_staging_dir_names_the_config_key() -> None:
    drip = DripConfig(vault_path="/tmp/v", instance="salem")
    with pytest.raises(DripConfigError) as exc:
        build_campaign("gmail_backlog", CampaignConfig(enabled=True), drip)
    assert "staging_dir" in str(exc.value)


def test_link001_without_worklist_path_names_the_config_key() -> None:
    drip = DripConfig(vault_path="/tmp/v", instance="salem")
    with pytest.raises(DripConfigError) as exc:
        build_campaign("link001_repair", CampaignConfig(enabled=True), drip)
    assert "worklist_path" in str(exc.value)


def test_unknown_kind_is_an_error() -> None:
    drip = DripConfig(vault_path="/tmp/v", instance="salem")
    with pytest.raises(DripConfigError):
        build_campaign("nope", CampaignConfig(kind="nope", enabled=True), drip)


def test_missing_worklist_file_is_an_error_not_an_empty_list(tmp_path: Path) -> None:
    """'I could not find your list' must never render as 'you are finished'.

    An empty work-list and an absent one are indistinguishable downstream, and
    for a campaign whose removal branch is irreversible that confusion is the
    expensive direction.
    """
    with pytest.raises(DripConfigError):
        load_worklist_file(tmp_path / "absent.txt")


def test_worklist_file_ignores_blanks_and_comments(tmp_path: Path) -> None:
    p = tmp_path / "wl.txt"
    p.write_text(
        "# frozen 2026-08-05\n"
        "note/A.md::person/Gone::remove\n"
        "\n"
        "   \n"
        "learn/B.md::person/Gone::annotate\n",
        encoding="utf-8",
    )
    assert load_worklist_file(p) == [
        "note/A.md::person/Gone::remove",
        "learn/B.md::person/Gone::annotate",
    ]


# ---------------------------------------------------------------------------
# Progress — the denominator must not shrink under the operator
# ---------------------------------------------------------------------------


class _FakeCampaign:
    def __init__(self, worklist: list[str], *, spends: bool) -> None:
        self._worklist = worklist
        self._spends = spends
        self.name = "fake"

    def worklist(self) -> list[str]:
        return list(self._worklist)

    def spends_quota(self) -> bool:
        return self._spends


def test_total_counts_dispatched_items_that_left_the_worklist() -> None:
    """``gmail_backlog``'s work() MOVES the file out of staging, so a
    dispatched item vanishes from ``worklist()``. Sizing from the work-list
    alone would shrink the denominator and re-base the percentage under the
    reader — progress that lies in the reassuring direction."""
    state = CampaignState(campaign="fake")
    state.items["gone.md"] = ItemState(item_id="gone.md", state=IN_FLIGHT)
    state.items["did.md"] = ItemState(item_id="did.md", state=DONE)
    campaign = _FakeCampaign(["still-staged.md"], spends=True)

    p = build_progress(
        "fake", campaign, state, CampaignConfig(enabled=True), today=TODAY,
    )
    assert p.total == 3, "worklist ∪ everything state has tracked"
    assert p.remaining == 2, "the DONE one is finished; the in-flight one is not"
    assert p.awaiting == 1


def test_disabled_campaign_reports_disabled_not_its_last_run() -> None:
    """A campaign turned off two weeks ago must not read as still draining —
    the runner never ran to record STOP_DISABLED, so progress supplies it."""
    state = CampaignState(campaign="fake")
    state.last_run_at = "2026-07-20T10:00:00+00:00"
    state.last_stop_reason = "budget_exhausted"
    p = build_progress(
        "fake", _FakeCampaign(["a"], spends=True), state,
        CampaignConfig(enabled=False), today=TODAY,
    )
    assert p.last_stop_reason == STOP_DISABLED


def test_week_cap_is_zero_for_a_campaign_that_spends_no_quota() -> None:
    """The weekly cap is a CREDIT guard and the runner only applies it to
    spending campaigns. Reporting a cap that cannot bind would invite the
    operator to tune a number with no effect."""
    p = build_progress(
        "fake", _FakeCampaign(["a"], spends=False), CampaignState(),
        CampaignConfig(enabled=True, max_items_per_week=60), today=TODAY,
    )
    assert p.week_cap == 0


# ---------------------------------------------------------------------------
# BUDGET THREADING — the D1 regression guard. The load-bearing pins.
# ---------------------------------------------------------------------------


def _spy_runner(monkeypatch) -> list[dict]:
    """Record what ``run_increment`` was actually CALLED with.

    Patched on ``alfred.drip.cli`` — the name the production handler resolves —
    so the spy observes the real call site rather than a re-implementation.
    """
    seen: list[dict] = []

    def _fake(campaign, state, **kwargs):
        seen.append(kwargs)
        from alfred.drip.runner import RunResult
        return RunResult(campaign=getattr(campaign, "name", "?"))

    monkeypatch.setattr(drip_cli, "run_increment", _fake)
    return seen


def _link001_config(tmp_path: Path, **campaign_overrides) -> DripConfig:
    wl = tmp_path / "wl.txt"
    wl.write_text("note/A.md::person/Gone::remove\n", encoding="utf-8")
    block = {"campaigns": {"link001_repair": {
        "enabled": True, "worklist_path": str(wl), **campaign_overrides,
    }}}
    raw = _raw(block)
    raw["vault"]["path"] = str(tmp_path / "vault")
    raw["logging"]["dir"] = str(tmp_path / "data")
    return load_from_unified(raw)


def test_config_budgets_reach_the_runner(tmp_path: Path, monkeypatch) -> None:
    """A CONFIG value change must change what the runner receives.

    The numbers here are deliberately not the ruled defaults: a build that
    ignored config and passed 12/60 would produce a perfectly plausible run and
    fail this assertion. That is the whole point — "the tests thread it,
    production never does" is the trap, and only an assertion on the
    NON-DEFAULT value can catch it.
    """
    cfg = _link001_config(
        tmp_path, max_items_per_run=3, max_items_per_week=7,
        max_attempts=9, max_failures_per_run=2, max_awaiting_runs=4,
    )
    seen = _spy_runner(monkeypatch)

    drip_cli.cmd_run(cfg, today=TODAY)

    assert len(seen) == 1
    kwargs = seen[0]
    assert kwargs["max_items_per_run"] == 3 != DEFAULT_MAX_ITEMS_PER_RUN
    assert kwargs["max_items_per_week"] == 7 != DEFAULT_MAX_ITEMS_PER_WEEK
    assert kwargs["max_attempts"] == 9
    assert kwargs["max_failures_per_run"] == 2
    assert kwargs["max_awaiting_runs"] == 4


def test_today_is_passed_explicitly_never_defaulted(
    tmp_path: Path, monkeypatch,
) -> None:
    """The runner annotates ``today: date = None`` and dereferences it
    (``today.isoformat()``) the first time an item records spend — verified by
    running it, not by reading it. A caller that lets it default works against
    an empty cursor and dies once the campaign costs something."""
    cfg = _link001_config(tmp_path)
    seen = _spy_runner(monkeypatch)

    drip_cli.cmd_run(cfg, today=TODAY)

    assert seen[0]["today"] == TODAY
    assert seen[0]["today"] is not None


def test_dry_run_reaches_the_runner_as_apply_false(
    tmp_path: Path, monkeypatch,
) -> None:
    cfg = _link001_config(tmp_path)
    seen = _spy_runner(monkeypatch)
    drip_cli.cmd_run(cfg, apply=False, today=TODAY)
    assert seen[0]["apply"] is False


def test_state_path_is_instance_scoped(tmp_path: Path, monkeypatch) -> None:
    """Two instances sharing one cursor would each advance the other past items
    neither processed. The path carries the instance for that reason."""
    cfg = _link001_config(tmp_path)
    seen = _spy_runner(monkeypatch)
    drip_cli.cmd_run(cfg, today=TODAY)
    assert "salem" in str(seen[0]["state_path"]).lower()


def test_budgets_thread_through_the_argparse_entry_point(
    tmp_path: Path, monkeypatch, capsys,
) -> None:
    """The e2e pin: ``alfred drip run`` itself, not just ``cmd_run``.

    A gate parameter threaded in the inner function but not at the real entry
    point is green on every unit pin and dead in the field. This drives
    ``cmd_drip`` — the function the top-level dispatcher actually calls.
    """
    import argparse

    from alfred import cli as top_cli

    wl = tmp_path / "wl.txt"
    wl.write_text("note/A.md::person/Gone::remove\n", encoding="utf-8")
    raw = _raw({"campaigns": {"link001_repair": {
        "enabled": True, "worklist_path": str(wl), "max_items_per_run": 5,
    }}})
    raw["vault"]["path"] = str(tmp_path / "vault")
    raw["logging"]["dir"] = str(tmp_path / "data")

    monkeypatch.setattr(top_cli, "_load_unified_config", lambda _p: raw)
    monkeypatch.setattr(top_cli, "_setup_logging_from_config", lambda *a, **k: None)
    seen = _spy_runner(monkeypatch)

    top_cli.cmd_drip(argparse.Namespace(
        config="ignored.yaml", drip_cmd="run", campaign=None, dry_run=False,
    ))

    assert len(seen) == 1, "the real entry point must reach the runner"
    assert seen[0]["max_items_per_run"] == 5


# ---------------------------------------------------------------------------
# CLI behaviour — ILB, and selection errors that name what IS configured
# ---------------------------------------------------------------------------


def test_run_with_nothing_enabled_says_so(capsys, monkeypatch) -> None:
    """ILB: an all-disabled instance is a steady state, not a crashed run."""
    cfg = load_from_unified(_raw({"campaigns": {"link001_repair": {}}}))
    seen = _spy_runner(monkeypatch)

    with structlog.testing.capture_logs() as captured:
        code = drip_cli.cmd_run(cfg, today=TODAY)

    assert code == 0
    assert seen == [], "nothing enabled ⇒ the runner is never called"
    assert "nothing to drain" in capsys.readouterr().out
    events = [c for c in captured if c.get("event") == "drip.run.no_enabled_campaigns"]
    assert len(events) == 1


def test_status_with_no_campaigns_says_so(capsys) -> None:
    cfg = load_from_unified(_raw())
    with structlog.testing.capture_logs() as captured:
        drip_cli.cmd_status(cfg, today=TODAY)
    assert "no campaigns configured" in capsys.readouterr().out.lower()
    assert [c for c in captured if c.get("event") == "drip.status.no_campaigns"]


def test_unknown_campaign_name_lists_what_is_configured(tmp_path: Path) -> None:
    cfg = _link001_config(tmp_path)
    with pytest.raises(DripConfigError) as exc:
        drip_cli.cmd_run(cfg, campaign_name="typo", today=TODAY)
    assert "link001_repair" in str(exc.value)


def test_selecting_a_disabled_campaign_says_how_to_enable_it(
    tmp_path: Path,
) -> None:
    cfg = _link001_config(tmp_path)
    cfg.campaigns["link001_repair"].enabled = False
    with pytest.raises(DripConfigError) as exc:
        drip_cli.cmd_run(cfg, campaign_name="link001_repair", today=TODAY)
    assert "enabled: true" in str(exc.value)


def test_one_unbuildable_campaign_does_not_stop_a_healthy_one(
    tmp_path: Path, monkeypatch, capsys,
) -> None:
    """A misconfigured campaign is loud and skipped; its sibling still drains."""
    wl = tmp_path / "wl.txt"
    wl.write_text("note/A.md::person/Gone::remove\n", encoding="utf-8")
    raw = _raw({"campaigns": {
        "link001_repair": {"enabled": True, "worklist_path": str(wl)},
        "gmail_backlog": {"enabled": True},        # missing staging_dir
    }})
    raw["vault"]["path"] = str(tmp_path / "vault")
    raw["logging"]["dir"] = str(tmp_path / "data")
    cfg = load_from_unified(raw)
    seen = _spy_runner(monkeypatch)

    code = drip_cli.cmd_run(cfg, today=TODAY)

    assert code == 1, "a misconfigured campaign is a nonzero exit"
    assert len(seen) == 1, "the healthy campaign still ran"
    assert "gmail_backlog" in capsys.readouterr().out
