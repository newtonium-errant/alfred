"""#60 contract point 3 — the state-repair pass.

Written CONTRACT-FIRST from task #60, before reading the parked WIP.

## Why a repair pass exists at all

The #60 verifier bug wrote DONE onto items nothing had been done to: a folded
frontmatter link defeated ``work()``'s matcher AND satisfied ``verify()``'s
``link not in body`` check. Fixing the verifier stops NEW false-dones; it does
nothing for the rows already on disk. Those rows are indistinguishable from
genuine successes by inspection — the campaign will simply never revisit them,
so the links they claim to have repaired stay broken forever, invisibly.

So the fix has two halves, and this is the second: re-audit every state row
marked ``done`` with the FIXED verifier and demote the ones that fail back to
``pending``, where the drain will pick them up again.

## The three rules the tests below hold

1. **Only ``done`` rows are audited.** ``pending``/``failed``/``in_flight`` are
   already going to be revisited; touching them would reset attempt counters
   and un-fail items the operator may be triaging.
2. **Demotion is the only mutation, and only downward.** The pass never marks
   anything done. A repair tool that can promote is a second way to
   manufacture the exact bug it was written to clean up.
3. **Unverifiable rows are REPORTED, never demoted.** If ``verify()`` raises,
   the pass does not know — and guessing in either direction is worse than a
   number the operator can see.

Idempotence falls out of rule 1: a demoted row is ``pending`` on the next pass
and is no longer audited, so a second run demotes zero.
"""

from __future__ import annotations

from pathlib import Path

import structlog
import yaml

from alfred.drip.campaigns import Link001Campaign
from alfred.drip.cli import cmd_repair_verify
from alfred.drip.config import CampaignConfig, DripConfig
from alfred.drip.repair import repair_false_dones
from alfred.drip.state import (
    BLOCKED,
    DONE,
    FAILED,
    IN_FLIGHT,
    PENDING,
    CampaignState,
    ItemState,
    campaign_state_path,
    load_state,
    save_state,
)

LONG_TARGET = (
    "note/2026-07-14 Weekly Operations Review Meeting Notes And Follow Up "
    "Actions For The Northern Pulp Remediation Programme"
)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _vault(tmp_path: Path) -> Path:
    v = tmp_path / "vault"
    (v / "note").mkdir(parents=True, exist_ok=True)
    return v


def _write_folded(vault: Path, name: str, target: str = LONG_TARGET) -> str:
    """A record whose frontmatter link is FOLDED — the false-done shape."""
    fm = yaml.safe_dump(
        {"sources": [f"[[{target}]]"], "type": "constraint"},
        default_flow_style=False, sort_keys=True,
    )
    text = f"---\n{fm}---\n\nBody.\n"
    assert f"[[{target}]]" not in text, "FIXTURE INTEGRITY: not actually folded"
    (vault / "note" / name).write_text(text, encoding="utf-8")
    return f"note/{name}"


def _write_clean(vault: Path, name: str) -> str:
    """A record the campaign genuinely repaired — the link really is gone."""
    (vault / "note" / name).write_text(
        "---\ntype: constraint\n---\n\nBody with no links.\n", encoding="utf-8",
    )
    return f"note/{name}"


def _state_with(campaign: str, rows: dict[str, str]) -> CampaignState:
    st = CampaignState(campaign=campaign)
    for item_id, state_name in rows.items():
        st.items[item_id] = ItemState(item_id=item_id, state=state_name)
    return st


# ---------------------------------------------------------------------------
# the headline: a false-done is found and demoted
# ---------------------------------------------------------------------------


def test_a_false_done_is_demoted_to_pending(tmp_path: Path) -> None:
    """The 4-of-12 population, in miniature: state says done, the link is
    still there, and only the fixed verifier can tell."""
    vault = _vault(tmp_path)
    rel = _write_folded(vault, "R.md")
    item = Link001Campaign.build_item(rel, LONG_TARGET, citer_is_learn=False)
    campaign = Link001Campaign(worklist_items=[item], vault_path=vault)
    state = _state_with("link001_repair", {item: DONE})

    result = repair_false_dones(campaign, state, apply=True)

    assert state.items[item].state == PENDING
    assert result.audited == 1
    assert result.demoted == 1
    assert result.confirmed == 0


def test_a_genuine_done_is_confirmed_and_left_alone(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    rel = _write_clean(vault, "R.md")
    item = Link001Campaign.build_item(rel, LONG_TARGET, citer_is_learn=False)
    campaign = Link001Campaign(worklist_items=[item], vault_path=vault)
    state = _state_with("link001_repair", {item: DONE})

    result = repair_false_dones(campaign, state, apply=True)

    assert state.items[item].state == DONE
    assert (result.audited, result.confirmed, result.demoted) == (1, 1, 0)


def test_a_mixed_batch_splits_correctly(tmp_path: Path) -> None:
    """Both populations in one pass — the shape the box will actually see."""
    vault = _vault(tmp_path)
    false_items = []
    for n in range(4):
        rel = _write_folded(vault, f"F{n}.md")
        false_items.append(
            Link001Campaign.build_item(rel, LONG_TARGET, citer_is_learn=False)
        )
    true_items = []
    for n in range(8):
        rel = _write_clean(vault, f"T{n}.md")
        true_items.append(
            Link001Campaign.build_item(rel, LONG_TARGET, citer_is_learn=False)
        )
    all_items = false_items + true_items
    campaign = Link001Campaign(worklist_items=all_items, vault_path=vault)
    state = _state_with("link001_repair", {i: DONE for i in all_items})

    result = repair_false_dones(campaign, state, apply=True)

    assert (result.audited, result.confirmed, result.demoted) == (12, 8, 4)
    assert all(state.items[i].state == PENDING for i in false_items)
    assert all(state.items[i].state == DONE for i in true_items)


# ---------------------------------------------------------------------------
# rule 1 — only done rows are audited
# ---------------------------------------------------------------------------


def test_non_done_rows_are_not_audited_or_touched(tmp_path: Path) -> None:
    """Rule 1. These rows are already going to be revisited by the drain;
    rewriting them would reset attempt counters and un-fail items the operator
    may be triaging."""
    vault = _vault(tmp_path)
    rows = {}
    for name, st in (("P.md", PENDING), ("F.md", FAILED),
                     ("I.md", IN_FLIGHT), ("B.md", BLOCKED)):
        rel = _write_folded(vault, name)
        rows[Link001Campaign.build_item(rel, LONG_TARGET,
                                        citer_is_learn=False)] = st
    campaign = Link001Campaign(worklist_items=list(rows), vault_path=vault)
    state = _state_with("link001_repair", rows)

    result = repair_false_dones(campaign, state, apply=True)

    assert result.audited == 0, "nothing was marked done, so nothing to re-audit"
    assert result.demoted == 0
    for item_id, original in rows.items():
        assert state.items[item_id].state == original


def test_the_pass_never_promotes(tmp_path: Path) -> None:
    """Rule 2. A repair tool that can write DONE is a second way to
    manufacture the bug it exists to clean up."""
    vault = _vault(tmp_path)
    rel = _write_clean(vault, "R.md")      # genuinely repaired…
    item = Link001Campaign.build_item(rel, LONG_TARGET, citer_is_learn=False)
    campaign = Link001Campaign(worklist_items=[item], vault_path=vault)
    state = _state_with("link001_repair", {item: FAILED})   # …but marked failed

    repair_false_dones(campaign, state, apply=True)

    assert state.items[item].state == FAILED, "the pass must not promote it"


# ---------------------------------------------------------------------------
# rule 3 — unverifiable rows are reported, not guessed at
# ---------------------------------------------------------------------------


def test_an_unverifiable_row_is_reported_and_left_done(tmp_path: Path) -> None:
    """A vanished record makes verify() unable to answer. Demoting would
    re-run irreversible work on a guess; silently confirming would hide it.
    So: leave it, and give the operator a number."""
    vault = _vault(tmp_path)
    item = Link001Campaign.build_item(
        "note/GONE.md", LONG_TARGET, citer_is_learn=False,
    )
    campaign = Link001Campaign(worklist_items=[item], vault_path=vault)
    state = _state_with("link001_repair", {item: DONE})

    class _Raising(Link001Campaign):
        def verify(self, item_id: str) -> bool:
            raise OSError("record unreadable")

    raising = _Raising(worklist_items=[item], vault_path=vault)
    result = repair_false_dones(raising, state, apply=True)

    assert result.unverifiable == 1
    assert result.demoted == 0
    assert state.items[item].state == DONE
    assert campaign.name == "link001_repair"


# ---------------------------------------------------------------------------
# idempotence + dry run
# ---------------------------------------------------------------------------


def test_the_pass_is_idempotent(tmp_path: Path) -> None:
    """Second run demotes zero. Falls out of rule 1 — a demoted row is
    ``pending`` and is no longer audited."""
    vault = _vault(tmp_path)
    rel = _write_folded(vault, "R.md")
    item = Link001Campaign.build_item(rel, LONG_TARGET, citer_is_learn=False)
    campaign = Link001Campaign(worklist_items=[item], vault_path=vault)
    state = _state_with("link001_repair", {item: DONE})

    first = repair_false_dones(campaign, state, apply=True)
    second = repair_false_dones(campaign, state, apply=True)

    assert first.demoted == 1
    assert second.demoted == 0
    assert second.audited == 0
    assert state.items[item].state == PENDING


def test_dry_run_reports_but_changes_nothing(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    rel = _write_folded(vault, "R.md")
    item = Link001Campaign.build_item(rel, LONG_TARGET, citer_is_learn=False)
    campaign = Link001Campaign(worklist_items=[item], vault_path=vault)
    state = _state_with("link001_repair", {item: DONE})

    result = repair_false_dones(campaign, state, apply=False)

    assert result.demoted == 1, "it still REPORTS what it would demote"
    assert state.items[item].state == DONE, "…but the row is untouched"


# ---------------------------------------------------------------------------
# ILB — "ran, N demoted" even when N is zero
# ---------------------------------------------------------------------------


def test_it_logs_ran_nothing_to_do_when_no_false_dones_exist(
    tmp_path: Path,
) -> None:
    """Intentionally-left-blank. A repair pass that prints nothing when it
    finds nothing is indistinguishable from a repair pass that did not run —
    and this one runs at deploy time, where that distinction is the point."""
    vault = _vault(tmp_path)
    rel = _write_clean(vault, "R.md")
    item = Link001Campaign.build_item(rel, LONG_TARGET, citer_is_learn=False)
    campaign = Link001Campaign(worklist_items=[item], vault_path=vault)
    state = _state_with("link001_repair", {item: DONE})

    with structlog.testing.capture_logs() as captured:
        result = repair_false_dones(campaign, state, apply=True)

    assert result.demoted == 0
    events = [e for e in captured if e.get("event") == "drip.repair_verify.summary"]
    assert len(events) == 1, "the completion signal fires even with nothing to do"
    assert events[0]["demoted"] == 0
    assert events[0]["audited"] == 1
    assert events[0]["confirmed"] == 1
    assert events[0]["campaign"] == "link001_repair"


def test_it_logs_the_completion_signal_when_it_did_demote(
    tmp_path: Path,
) -> None:
    """Same event, non-zero — so a grep for one string covers both outcomes."""
    vault = _vault(tmp_path)
    rel = _write_folded(vault, "R.md")
    item = Link001Campaign.build_item(rel, LONG_TARGET, citer_is_learn=False)
    campaign = Link001Campaign(worklist_items=[item], vault_path=vault)
    state = _state_with("link001_repair", {item: DONE})

    with structlog.testing.capture_logs() as captured:
        repair_false_dones(campaign, state, apply=True)

    events = [e for e in captured if e.get("event") == "drip.repair_verify.summary"]
    assert len(events) == 1
    assert events[0]["demoted"] == 1


def test_an_empty_state_is_a_legible_outcome_not_a_crash(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    campaign = Link001Campaign(worklist_items=[], vault_path=vault)
    state = CampaignState(campaign="link001_repair")

    with structlog.testing.capture_logs() as captured:
        result = repair_false_dones(campaign, state, apply=True)

    assert (result.audited, result.demoted) == (0, 0)
    events = [e for e in captured if e.get("event") == "drip.repair_verify.summary"]
    assert len(events) == 1, "an empty campaign still says it ran"


# ---------------------------------------------------------------------------
# the demoted row must be legible afterwards
# ---------------------------------------------------------------------------


def test_a_demoted_row_records_why_and_resets_its_attempt_count(
    tmp_path: Path,
) -> None:
    """The operator reading state after the deploy must be able to tell a
    #60 demotion from an ordinary pending item."""
    vault = _vault(tmp_path)
    rel = _write_folded(vault, "R.md")
    item = Link001Campaign.build_item(rel, LONG_TARGET, citer_is_learn=False)
    campaign = Link001Campaign(worklist_items=[item], vault_path=vault)
    state = _state_with("link001_repair", {item: DONE})
    state.items[item].attempts = 2
    state.items[item].claimed_by = "run-old"

    repair_false_dones(campaign, state, apply=True)

    row = state.items[item]
    assert row.state == PENDING
    assert "#60" in row.last_error, "the demotion reason is legible in state"
    assert row.attempts == 0, (
        "the retry budget is RESET, not preserved: those attempts were scored "
        "against a broken verifier, so an item returning to the queue with them "
        "already spent (max_attempts defaults to 3) would retire as FAILED "
        "without ever having been genuinely tried once"
    )
    assert row.claimed_by == "", "the stale claim is cleared for the re-drain"
    assert row.updated_at, "the row records when the repair touched it"


# ---------------------------------------------------------------------------
# the OPERATOR-FACING path — `alfred drip repair-verify`
# ---------------------------------------------------------------------------
#
# The pins above drive ``repair_false_dones`` directly. These drive the command
# the box will actually run, because a repair that works only when called from a
# test is the standing trap: every layer green, the production entry point never
# threading what it needs. The deploy step is one invocation of THIS, so this is
# the surface that has to be right.


def _cli_setup(tmp_path: Path, items: list[str]) -> tuple[DripConfig, Path]:
    """A configured link001 campaign plus the path its state will live at."""
    worklist = tmp_path / "worklist.txt"
    worklist.write_text("\n".join(items) + "\n", encoding="utf-8")
    config = DripConfig(
        vault_path=str(tmp_path / "vault"),
        data_dir=str(tmp_path / "data"),
        instance="salem",
        campaigns={
            "link001_repair": CampaignConfig(
                kind="link001_repair", enabled=True,
                worklist_path=str(worklist),
            ),
        },
    )
    return config, campaign_state_path(
        config.data_dir, config.instance, "link001_repair",
    )


def test_cli_apply_demotes_the_false_done_and_PERSISTS_it(
    tmp_path: Path, capsys,
) -> None:
    """The deploy step, end to end: a false-done on disk comes back pending on
    disk. Asserting the reloaded FILE, not the in-memory object — the whole
    point of the command is that the demotion outlives the process."""
    vault = _vault(tmp_path)
    rel = _write_folded(vault, "R.md")
    item = Link001Campaign.build_item(rel, LONG_TARGET, citer_is_learn=False)
    config, state_path = _cli_setup(tmp_path, [item])
    save_state(state_path, _state_with("link001_repair", {item: DONE}))

    code = cmd_repair_verify(config, apply=True)

    assert code == 0
    reloaded = load_state(state_path, "link001_repair")
    assert reloaded.items[item].state == PENDING
    assert "#60" in reloaded.items[item].last_error
    assert "1 demoted" in capsys.readouterr().out


def test_cli_dry_run_is_the_DEFAULT_and_writes_nothing(
    tmp_path: Path, capsys,
) -> None:
    """Dry-run by default, inverting ``run`` and matching ``build-worklist``.
    Called with no ``apply`` argument at all, so the DEFAULT is what is pinned —
    a default that flipped to apply would make the preview command a writer."""
    vault = _vault(tmp_path)
    rel = _write_folded(vault, "R.md")
    item = Link001Campaign.build_item(rel, LONG_TARGET, citer_is_learn=False)
    config, state_path = _cli_setup(tmp_path, [item])
    save_state(state_path, _state_with("link001_repair", {item: DONE}))
    before = state_path.read_text(encoding="utf-8")

    cmd_repair_verify(config)

    assert state_path.read_text(encoding="utf-8") == before, "byte-identical"
    out = capsys.readouterr().out
    assert "[dry run]" in out
    assert "1 demoted" in out, "it still REPORTS what it would reopen"


def test_cli_repairs_a_campaign_that_is_DISABLED(tmp_path: Path) -> None:
    """State repair is wanted precisely when a campaign is PARKED — #60's
    containment stopped the link001 timers the moment the false-dones were
    found. An enabled-only selector would refuse the one command that makes the
    campaign safe to switch back on."""
    vault = _vault(tmp_path)
    rel = _write_folded(vault, "R.md")
    item = Link001Campaign.build_item(rel, LONG_TARGET, citer_is_learn=False)
    config, state_path = _cli_setup(tmp_path, [item])
    config.campaigns["link001_repair"].enabled = False
    save_state(state_path, _state_with("link001_repair", {item: DONE}))

    cmd_repair_verify(config, apply=True)

    assert load_state(state_path, "link001_repair").items[item].state == PENDING


def test_cli_says_it_ran_when_there_is_nothing_to_re_verify(
    tmp_path: Path, capsys,
) -> None:
    """ILB at the operator surface. This runs once, by hand, at deploy — a
    command that prints nothing is indistinguishable from one that did not
    run, which is the exact ambiguity the deploy step cannot afford."""
    vault = _vault(tmp_path)
    rel = _write_clean(vault, "R.md")
    item = Link001Campaign.build_item(rel, LONG_TARGET, citer_is_learn=False)
    config, state_path = _cli_setup(tmp_path, [item])
    save_state(state_path, _state_with("link001_repair", {item: PENDING}))

    cmd_repair_verify(config, apply=True)

    assert "no done items to re-verify" in capsys.readouterr().out


def test_cli_reports_zero_demotions_explicitly(tmp_path: Path, capsys) -> None:
    """The N=0 case the contract names: 'ran, 0 demoted' as a sentence."""
    vault = _vault(tmp_path)
    rel = _write_clean(vault, "R.md")
    item = Link001Campaign.build_item(rel, LONG_TARGET, citer_is_learn=False)
    config, state_path = _cli_setup(tmp_path, [item])
    save_state(state_path, _state_with("link001_repair", {item: DONE}))

    cmd_repair_verify(config, apply=True)

    out = capsys.readouterr().out
    assert "ran, 0 demoted" in out
    assert "1 confirmed" in out


def test_cli_with_no_campaigns_configured_is_legible(capsys) -> None:
    cmd_repair_verify(DripConfig(vault_path="/tmp/v", instance="salem"))
    assert "nothing to check" in capsys.readouterr().out


def test_cli_is_idempotent_across_two_apply_runs(tmp_path: Path) -> None:
    """Running the deploy step twice must not compound. The second pass sees
    the demoted row as pending and leaves it there."""
    vault = _vault(tmp_path)
    rel = _write_folded(vault, "R.md")
    item = Link001Campaign.build_item(rel, LONG_TARGET, citer_is_learn=False)
    config, state_path = _cli_setup(tmp_path, [item])
    save_state(state_path, _state_with("link001_repair", {item: DONE}))

    cmd_repair_verify(config, apply=True)
    after_first = state_path.read_text(encoding="utf-8")
    cmd_repair_verify(config, apply=True)

    assert state_path.read_text(encoding="utf-8") == after_first
    assert load_state(state_path, "link001_repair").items[item].state == PENDING


# ---------------------------------------------------------------------------
# argparse wiring — the flag has to reach the handler
# ---------------------------------------------------------------------------


def test_the_subcommand_is_registered_and_apply_defaults_to_off() -> None:
    """A ``--apply`` that never reaches the handler is the trap where every
    unit pin is green and the box writes nothing. Parsed from the real parser."""
    from alfred.cli import build_parser

    args = build_parser().parse_args(["drip", "repair-verify"])
    assert args.drip_cmd == "repair-verify"
    assert args.apply is False, "dry run unless asked"
    assert args.campaign is None

    args = build_parser().parse_args(
        ["drip", "repair-verify", "--apply", "--campaign", "link001_repair"],
    )
    assert args.apply is True
    assert args.campaign == "link001_repair"
