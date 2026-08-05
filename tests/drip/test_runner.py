"""#44 drip-drain runner — the 907 guard first, everything else after.

Spec: ``~/reports/drip-drain-design-2026-08-04.md`` (operator ratified
2026-08-04). The design's own §9 names the risk: the verifier will look
redundant and get dropped, because every test passes without it when the test
worker actually works. So the first pins here use a **liar worker** — one that
returns success having done nothing, which is precisely
``files_created=[]`` returning cleanly on 2026-07-26.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
import structlog

from alfred.drip.runner import (
    STOP_BUDGET_EXHAUSTED,
    STOP_CIRCUIT_BREAKER,
    STOP_QUOTA_BLOCKED,
    STOP_REASONS,
    STOP_WORKLIST_EMPTY,
    RunResult,
    run_increment,
)
from alfred.drip.state import (
    BLOCKED,
    DONE,
    FAILED,
    IN_FLIGHT,
    CampaignState,
    ItemState,
    campaign_state_path,
    load_state,
    save_state,
)

TODAY = date(2026, 8, 4)


class FakeCampaign:
    """A campaign whose effects are observable in a dict — so `verify` can be
    honest about whether work landed, exactly as a real one checks the vault."""

    def __init__(
        self,
        items: list[str],
        *,
        spends: bool = True,
        liar: bool = False,
        raises: dict[str, Exception] | None = None,
    ) -> None:
        self.name = "fake"
        self._items = items
        self._spends = spends
        self._liar = liar          # returns success, produces no effect
        self._raises = raises or {}
        self.effects: set[str] = set()
        self.worked: list[str] = []

    def worklist(self) -> list[str]:
        return list(self._items)

    def work(self, item_id: str) -> None:
        self.worked.append(item_id)
        if item_id in self._raises:
            raise self._raises[item_id]
        if not self._liar:
            self.effects.add(item_id)

    def verify(self, item_id: str) -> bool:
        # Keyed on the ITEM ID — an "any effect exists?" verifier would be
        # satisfied by a previous item's output. See the dedicated pin below.
        return item_id in self.effects

    def spends_quota(self) -> bool:
        return self._spends


def _run(campaign, state, tmp_path, **kw):
    defaults = dict(
        state_path=tmp_path / "s.json",
        max_items_per_run=12,          # D1 as ratified (halved from my draft)
        max_items_per_week=60,
        max_attempts=3,
        max_failures_per_run=5,
        today=TODAY,
        run_id="run-1",
    )
    defaults.update(kw)
    return run_increment(campaign, state, **defaults)


# ---------------------------------------------------------------------------
# THE 907 GUARD — non-negotiable per the ratified build shape
# ---------------------------------------------------------------------------


def test_worker_that_returns_success_with_no_effect_leaves_the_item_FAILED(
    tmp_path: Path,
) -> None:
    """THE pin. A worker returning cleanly is NOT evidence it did anything —
    ``mark_processed(files_created=[])`` returned cleanly too, 907 times.

    Mutation that must redden this: make ``verify()`` return True
    unconditionally.
    """
    campaign = FakeCampaign(["a", "b"], liar=True)
    state = CampaignState(campaign="fake")

    result = _run(campaign, state, tmp_path)

    assert campaign.worked == ["a", "b"], "the worker did run"
    assert result.done == 0, "nothing may be marked done without an effect"
    assert result.failed == 2
    assert state.items["a"].state == FAILED
    assert state.items["b"].state == FAILED
    assert "not observable" in state.items["a"].last_error.lower(), (
        "the recorded reason must name WHY — a bare FAILED reads as a "
        "transient error rather than as the 907 signature"
    )


def test_the_unverified_effect_is_logged_with_its_lineage(
    tmp_path: Path,
) -> None:
    """Observability half: the operator's grep for the 907 signature must find
    this. A silent FAILED is a campaign that looks merely slow."""
    campaign = FakeCampaign(["a"], liar=True)
    with structlog.testing.capture_logs() as cap:
        _run(campaign, CampaignState(campaign="fake"), tmp_path)
    events = [c for c in cap if c.get("event") == "drip.unverified_effect"]
    assert len(events) == 1
    assert events[0]["item_id"] == "a"


def test_verify_must_key_on_the_item_id_not_on_any_effect(
    tmp_path: Path,
) -> None:
    """The contract the design states and types cannot enforce.

    A verifier asking "did ANY effect appear?" is satisfied by an earlier
    item's output and marks an untouched item done — the 907 again, wearing the
    verifier's own uniform. This pin drives that exact mistake and requires it
    to fail.
    """

    class AnyEffectCampaign(FakeCampaign):
        def verify(self, item_id: str) -> bool:
            return bool(self.effects)   # ← the wrong question

    good = FakeCampaign(["a", "b"])
    bad = AnyEffectCampaign(["a", "b"])
    for c in (good, bad):
        c.effects.add("a")              # only 'a' ever landed

    # The distinction, asserted on the VERIFIERS rather than on runner state.
    # Pinning the runner's downstream behaviour here would cement an outcome the
    # runner cannot prevent (the verifier is the campaign's to supply) and would
    # go red if a future runner ever DID add a defence — failing for a good
    # reason. What must stay true is the property itself:
    assert good.verify("b") is False, "an item-keyed verifier knows 'b' is absent"
    assert bad.verify("b") is True, (
        "an any-effect verifier greenlights 'b' on 'a''s output — the 907 "
        "again, wearing the verifier's own uniform"
    )
    assert bad.verify("never-in-the-worklist") is True, (
        "and it greenlights an item that was never even attempted"
    )


# ---------------------------------------------------------------------------
# Crash recovery — verify-first-on-requeue (the 907 inverted)
# ---------------------------------------------------------------------------


def test_requeued_in_flight_item_is_verified_not_rerun(tmp_path: Path) -> None:
    """A crash AFTER the effect and BEFORE the state write leaves a genuinely
    done item ``in_flight``. Re-running duplicates the work — for gmail, a
    second vault record for one email. Verify-first settles it."""
    campaign = FakeCampaign(["a"])
    campaign.effects.add("a")            # the effect landed pre-crash
    state = CampaignState(campaign="fake")
    state.items["a"] = ItemState(
        item_id="a", state=IN_FLIGHT, claimed_by="run-0",  # a PRIOR run
    )

    result = _run(campaign, state, tmp_path)

    assert campaign.worked == [], "a verified item must NOT be re-worked"
    assert result.skipped_verified == 1
    assert state.items["a"].state == DONE


def test_requeued_in_flight_item_that_did_NOT_land_is_reworked(
    tmp_path: Path,
) -> None:
    """The other half: an in_flight row whose effect never landed is genuine
    unfinished work and must be retried, not assumed done."""
    campaign = FakeCampaign(["a"])
    state = CampaignState(campaign="fake")
    state.items["a"] = ItemState(item_id="a", state=IN_FLIGHT, claimed_by="run-0")

    result = _run(campaign, state, tmp_path)

    assert campaign.worked == ["a"]
    assert result.done == 1
    assert state.items["a"].state == DONE


def test_item_is_claimed_in_flight_before_the_work_runs(tmp_path: Path) -> None:
    """A kill mid-item must leave a VISIBLE in_flight, never a silent skip.
    Asserted by observing the persisted state from inside the worker."""
    path = tmp_path / "s.json"
    seen: dict = {}

    class ObservingCampaign(FakeCampaign):
        def work(self, item_id: str) -> None:
            seen[item_id] = load_state(path, "fake").items[item_id].state
            super().work(item_id)

    campaign = ObservingCampaign(["a"])
    _run(campaign, CampaignState(campaign="fake"), tmp_path, state_path=path)

    assert seen["a"] == IN_FLIGHT, (
        "the claim must be PERSISTED before the work, or a crash loses the item"
    )


# ---------------------------------------------------------------------------
# Budget — D1 as ratified (12/run, 60/week)
# ---------------------------------------------------------------------------


def test_per_run_cap_bounds_the_increment(tmp_path: Path) -> None:
    campaign = FakeCampaign([f"i{n}" for n in range(50)])
    state = CampaignState(campaign="fake")

    result = _run(campaign, state, tmp_path, max_items_per_run=12)

    assert result.attempted == 12
    assert result.done == 12
    assert result.stop_reason == STOP_BUDGET_EXHAUSTED
    assert result.remaining == 38


def test_weekly_cap_binds_across_runs(tmp_path: Path) -> None:
    """The cap that actually bit on 2026-07-26 is WEEKLY. A daily-only limit
    still burns a week's allowance in four days."""
    campaign = FakeCampaign([f"i{n}" for n in range(100)])
    state = CampaignState(campaign="fake")

    for run in range(5):
        _run(campaign, state, tmp_path, max_items_per_run=20,
             max_items_per_week=60, run_id=f"run-{run}")

    assert state.spend_in_week_ending(TODAY) == 60
    assert sum(1 for i in state.items.values() if i.state == DONE) == 60


def test_budget_exhausted_is_the_boring_expected_stop(tmp_path: Path) -> None:
    """Running out of budget is the SUCCESS path most days — it must be a
    normal stop_reason, not an error, per the operator's own framing."""
    campaign = FakeCampaign([f"i{n}" for n in range(20)])
    state = CampaignState(campaign="fake")
    state.spend_by_day[TODAY.isoformat()] = 60      # week already spent

    result = _run(campaign, state, tmp_path, max_items_per_week=60)

    assert result.attempted == 0
    assert result.stop_reason == STOP_BUDGET_EXHAUSTED
    assert result.errors == []


def test_non_quota_campaign_does_not_consume_the_weekly_budget(
    tmp_path: Path,
) -> None:
    """link001 does no LLM work, so it must not eat gmail's weekly allowance —
    the per-campaign asymmetry D4 relies on."""
    campaign = FakeCampaign([f"i{n}" for n in range(30)], spends=False)
    state = CampaignState(campaign="fake")

    _run(campaign, state, tmp_path, max_items_per_run=30, max_items_per_week=5)

    assert state.spend_in_week_ending(TODAY) == 0
    assert sum(1 for i in state.items.values() if i.state == DONE) == 30


# ---------------------------------------------------------------------------
# Quota — blocked ≠ failed
# ---------------------------------------------------------------------------


def _quota_error() -> Exception:
    # The real message shape classify_agent_failure was built to recognize.
    return RuntimeError(
        "You've hit your weekly limit · resets 4am (UTC)"
    )


def test_quota_block_stops_the_run_immediately(tmp_path: Path) -> None:
    """Continuing would fail every remaining item identically — that is how 907
    failures happened in one sitting."""
    campaign = FakeCampaign(
        ["a", "b", "c"], raises={"a": _quota_error()},
    )
    state = CampaignState(campaign="fake")

    result = _run(campaign, state, tmp_path)

    assert result.stop_reason == STOP_QUOTA_BLOCKED
    assert campaign.worked == ["a"], "must not keep hammering a blocked account"
    assert state.items["a"].state == BLOCKED


def test_blocked_burns_no_attempt_and_records_no_spend(tmp_path: Path) -> None:
    """A quota block is a PAUSE, not a verdict about the item. After a
    multi-day outage the campaign must resume where it stopped rather than
    discover it burned three retries per item on the operator's billing cycle.
    """
    campaign = FakeCampaign(["a"], raises={"a": _quota_error()})
    state = CampaignState(campaign="fake")

    _run(campaign, state, tmp_path)

    assert state.items["a"].attempts == 0, "blocked must not consume an attempt"
    assert state.spend_in_week_ending(TODAY) == 0, "blocked spent nothing"


def test_blocked_item_is_retried_on_the_next_run(tmp_path: Path) -> None:
    campaign = FakeCampaign(["a", "b"], raises={"a": _quota_error()})
    state = CampaignState(campaign="fake")
    _run(campaign, state, tmp_path)
    assert state.items["a"].state == BLOCKED

    campaign._raises = {}                     # quota reset
    _run(campaign, state, tmp_path, run_id="run-2")

    assert state.items["a"].state == DONE


def test_ordinary_failure_consumes_an_attempt_and_retires_at_max(
    tmp_path: Path,
) -> None:
    campaign = FakeCampaign(["a"], raises={"a": ValueError("malformed record")})
    state = CampaignState(campaign="fake")

    for run in range(4):
        _run(campaign, state, tmp_path, run_id=f"run-{run}")

    assert state.items["a"].attempts == 3, "capped at max_attempts, not 4"
    assert state.items["a"].state == FAILED


def test_circuit_breaker_ends_a_run_that_is_failing_everything(
    tmp_path: Path,
) -> None:
    """A campaign failing every item should stop after five, not after two
    hundred."""
    items = [f"i{n}" for n in range(20)]
    campaign = FakeCampaign(
        items, raises={i: ValueError("boom") for i in items},
    )
    state = CampaignState(campaign="fake")

    result = _run(campaign, state, tmp_path, max_failures_per_run=5)

    assert result.stop_reason == STOP_CIRCUIT_BREAKER
    assert result.failed == 5


# ---------------------------------------------------------------------------
# Dry run — the preview must describe the same operation as the apply
# ---------------------------------------------------------------------------


def test_dry_run_touches_nothing_and_previews_the_same_selection(
    tmp_path: Path,
) -> None:
    """Three times in the #40/#40b arc a wrong predicate was caught by
    previewing rather than by reasoning. The preview is a first-class mode: it
    must report what the apply WOULD do, and write nothing."""
    campaign = FakeCampaign([f"i{n}" for n in range(30)])
    state = CampaignState(campaign="fake")
    path = tmp_path / "s.json"

    preview = _run(campaign, state, tmp_path, state_path=path, apply=False)

    assert preview.attempted == 12, "same budget the apply would use"
    assert preview.done == 0 and campaign.worked == []
    assert not path.exists(), "a dry run must not write state"

    applied = _run(campaign, state, tmp_path, state_path=path, apply=True)
    assert applied.attempted == preview.attempted


# ---------------------------------------------------------------------------
# ILB — every increment reports, including the no-ops
# ---------------------------------------------------------------------------


def test_every_run_emits_the_coverage_line_including_a_no_op(
    tmp_path: Path,
) -> None:
    """"Ran and did nothing" and "did not run" must be different lines. A
    background campaign is the easiest place in this system for silent death to
    hide, because nobody waits on any individual increment."""
    campaign = FakeCampaign([])
    with structlog.testing.capture_logs() as cap:
        result = _run(campaign, CampaignState(campaign="fake"), tmp_path)

    events = [c for c in cap if c.get("event") == "drip.campaign.run"]
    assert len(events) == 1
    assert events[0]["attempted"] == 0
    assert events[0]["stop_reason"] == STOP_WORKLIST_EMPTY
    assert result.stop_reason in STOP_REASONS


def test_eta_is_none_rather_than_guessed_when_it_cannot_be_computed() -> None:
    """D2's headline must be honest. A guessed ETA is worse than an absent one:
    the number exists to make "a few weeks" an informed acceptance."""
    r = RunResult(remaining=100)
    assert r.eta_days(per_run=12) == 9      # ceil(100/12)
    assert r.eta_days(per_run=0) is None    # no budget → no honest estimate
    assert RunResult(remaining=0).eta_days(per_run=12) == 0


def test_pct_complete_is_100_on_an_empty_campaign() -> None:
    """Nothing outstanding ⇒ complete. 0% would read as total failure on a
    campaign that simply has no work."""
    assert RunResult(total=0, remaining=0).pct_complete == 100.0


# ---------------------------------------------------------------------------
# State path — the feed-store lesson
# ---------------------------------------------------------------------------


def test_state_path_is_tool_and_instance_scoped(tmp_path: Path) -> None:
    salem = campaign_state_path(tmp_path, "Salem", "gmail_backlog")
    kalle = campaign_state_path(tmp_path, "KAL-LE", "gmail_backlog")
    assert salem != kalle, (
        "on the box all instances share one WorkingDirectory — an unscoped "
        "path is ONE shared cursor, and a shared cursor is a silent-loss "
        "generator (each instance advances the other past unprocessed items)"
    )
    assert "drip" in salem.parts and "salem" in salem.parts


def test_missing_instance_fails_loud(tmp_path: Path) -> None:
    """Fail-loud beats a shared default — that default IS the incident."""
    with pytest.raises(ValueError, match="instance"):
        campaign_state_path(tmp_path, "", "gmail_backlog")


def test_state_survives_a_round_trip_and_tolerates_unknown_fields(
    tmp_path: Path,
) -> None:
    """The load() schema-tolerance contract: a campaign that cannot be resumed
    across a version change is not resumable in the sense this needs."""
    import json

    path = tmp_path / "s.json"
    state = CampaignState(campaign="c")
    state.items["a"] = ItemState(item_id="a", state=DONE, attempts=1)
    state.spend_by_day["2026-08-04"] = 3
    save_state(path, state)

    raw = json.loads(path.read_text())
    raw["future_field"] = "from a newer runner"
    raw["items"]["a"]["future_item_field"] = 1
    path.write_text(json.dumps(raw))

    reloaded = load_state(path, "c")
    assert reloaded.items["a"].state == DONE
    assert reloaded.items["a"].attempts == 1
    assert reloaded.spend_by_day["2026-08-04"] == 3


def test_corrupt_state_starts_fresh_but_says_so(tmp_path: Path) -> None:
    """Silently starting from zero would re-run an entire campaign — for a
    worklist with irreversible items that is the expensive direction."""
    path = tmp_path / "s.json"
    path.write_text("{ not json")
    with structlog.testing.capture_logs() as cap:
        state = load_state(path, "c")
    assert state.items == {}
    assert [c for c in cap if c.get("event") == "drip.state.load_failed"]
