"""#44 slice 2 — the two ratified campaigns.

The gmail campaign's whole job is to make the budget BIND on a directory the
curator daemon actively watches. The link001 campaign's whole job is to keep an
irreversible branch decision frozen at list-build.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from alfred.drip.campaigns import GmailBacklogCampaign, Link001Campaign
from alfred.drip.runner import run_increment
from alfred.drip.state import DONE, FAILED, IN_FLIGHT, CampaignState

TODAY = date(2026, 8, 4)


def _vault(tmp_path: Path) -> Path:
    v = tmp_path / "vault"
    (v / "note").mkdir(parents=True)
    (v / "inbox").mkdir()
    return v


def _gmail(tmp_path: Path) -> GmailBacklogCampaign:
    vault = _vault(tmp_path)
    staging = tmp_path / "staging"
    staging.mkdir()
    return GmailBacklogCampaign(
        staging_dir=staging, inbox_dir=vault / "inbox", vault_path=vault,
    )


def _run(campaign, state, tmp_path, **kw):
    d = dict(
        state_path=tmp_path / "s.json", max_items_per_run=12,
        max_items_per_week=60, max_attempts=3, max_failures_per_run=5,
        max_awaiting_runs=5, today=TODAY, run_id="run-1",
    )
    d.update(kw)
    return run_increment(campaign, state, **d)


# ---------------------------------------------------------------------------
# gmail — the budget must actually throttle the watched directory
# ---------------------------------------------------------------------------


def test_only_budgeted_items_reach_the_watched_inbox(tmp_path: Path) -> None:
    """THE point of the campaign. The curator watches vault/inbox/ and polls
    every 5s; at quota reset it would chew the whole cohort at full speed. A
    budget that doesn't change what the watcher SEES is decorative."""
    c = _gmail(tmp_path)
    for n in range(30):
        (c.staging_dir / f"email-{n}-recov.md").write_text("x")

    _run(c, CampaignState(campaign="gmail_backlog"), tmp_path, max_items_per_run=12)

    assert len(list(c.inbox_dir.glob("*.md"))) == 12, "budget must bind arrivals"
    assert len(list(c.staging_dir.glob("*.md"))) == 18, "the rest stay staged"


def test_work_moves_rather_than_copies(tmp_path: Path) -> None:
    """A copy would leave the item staged and the next run would dispatch it
    again — duplicating the curator's work, the 907 inverted."""
    c = _gmail(tmp_path)
    (c.staging_dir / "a-recov.md").write_text("x")

    c.work("a-recov.md")

    assert (c.inbox_dir / "a-recov.md").exists()
    assert not (c.staging_dir / "a-recov.md").exists()


def test_refuses_to_double_queue_an_item_already_in_the_inbox(
    tmp_path: Path,
) -> None:
    c = _gmail(tmp_path)
    (c.staging_dir / "a-recov.md").write_text("x")
    (c.inbox_dir / "a-recov.md").write_text("already here")

    with pytest.raises(FileExistsError):
        c.work("a-recov.md")


def test_dispatched_item_stays_in_flight_not_failed(tmp_path: Path) -> None:
    """The async gap, measured: the curator polls every 5s, so verify() right
    after the move is GUARANTEED False. Marking that FAILED would fail every
    item on the run that dispatched it."""
    c = _gmail(tmp_path)
    (c.staging_dir / "a-recov.md").write_text("x")
    state = CampaignState(campaign="gmail_backlog")

    result = _run(c, state, tmp_path)

    assert result.dispatched == 1
    assert result.failed == 0
    assert state.items["a-recov.md"].state == IN_FLIGHT
    assert state.items["a-recov.md"].attempts == 0, "dispatch is not a failure"


def test_a_later_run_resolves_the_dispatch_to_done(tmp_path: Path) -> None:
    """Verify-first-on-requeue closes the loop once the curator has acted."""
    c = _gmail(tmp_path)
    (c.staging_dir / "a-recov.md").write_text("x")
    state = CampaignState(campaign="gmail_backlog")
    _run(c, state, tmp_path)

    # The curator structures it (the -recov suffix does NOT survive into the
    # record name — it is a queueing artifact).
    (c.vault_path / "note" / "a.md").write_text("---\ntype: note\n---\n")

    _run(c, state, tmp_path, run_id="run-2")
    assert state.items["a-recov.md"].state == DONE


def test_a_dispatch_that_never_lands_eventually_fails(tmp_path: Path) -> None:
    """Bounded. An item dispatched into the void must surface — sitting
    in_flight forever is invisible work-not-done, the 907 shape with extra
    steps."""
    c = _gmail(tmp_path)
    (c.staging_dir / "a-recov.md").write_text("x")
    state = CampaignState(campaign="gmail_backlog")

    for n in range(8):
        _run(c, state, tmp_path, max_awaiting_runs=3, run_id=f"run-{n}")

    assert state.items["a-recov.md"].state == FAILED
    assert "never" in state.items["a-recov.md"].last_error.lower()


def test_spend_is_recorded_at_dispatch_not_at_verification(
    tmp_path: Path,
) -> None:
    """Otherwise one run dispatches the whole backlog with a recorded spend of
    zero — a budget that does not bind. The cost is incurred by the curator the
    moment the item is in its queue."""
    c = _gmail(tmp_path)
    for n in range(30):
        (c.staging_dir / f"e{n}-recov.md").write_text("x")
    state = CampaignState(campaign="gmail_backlog")

    _run(c, state, tmp_path, max_items_per_run=12)

    assert state.spend_in_week_ending(TODAY) == 12, (
        "nothing is verified yet, but 12 items are queued for the curator"
    )


def test_weekly_cap_binds_dispatch_across_runs(tmp_path: Path) -> None:
    c = _gmail(tmp_path)
    for n in range(200):
        (c.staging_dir / f"e{n:03d}-recov.md").write_text("x")
    state = CampaignState(campaign="gmail_backlog")

    for n in range(10):
        _run(c, state, tmp_path, max_items_per_run=12, max_items_per_week=60,
             run_id=f"run-{n}")

    assert len(list(c.inbox_dir.glob("*.md"))) == 60, "weekly cap holds the line"


def test_verify_is_item_keyed_not_any_note(tmp_path: Path) -> None:
    """An earlier item's record must not satisfy a later item's verify."""
    c = _gmail(tmp_path)
    (c.vault_path / "note" / "a.md").write_text("---\ntype: note\n---\n")
    assert c.verify("a-recov.md") is True
    assert c.verify("b-recov.md") is False, (
        "'a''s record must not greenlight 'b' — the 907 in the verifier's "
        "own uniform"
    )


def test_empty_staging_is_a_legible_state_not_a_crash(tmp_path: Path) -> None:
    c = _gmail(tmp_path)
    import shutil

    shutil.rmtree(c.staging_dir)
    assert c.worklist() == []


# ---------------------------------------------------------------------------
# link001 — the frozen branch
# ---------------------------------------------------------------------------


def _link_vault(tmp_path: Path, body: str) -> tuple[Path, str]:
    vault = _vault(tmp_path)
    (vault / "note" / "R.md").write_text(body, encoding="utf-8")
    return vault, "note/R.md"


def test_annotate_branch_keeps_the_link_and_marks_it(tmp_path: Path) -> None:
    vault, rel = _link_vault(tmp_path, "See [[learn/Some Learning]] here.\n")
    item = Link001Campaign.build_item(rel, "learn/Some Learning", citer_is_learn=True)
    c = Link001Campaign(worklist_items=[item], vault_path=vault)

    c.work(item)

    body = (vault / "note" / "R.md").read_text()
    assert "[[learn/Some Learning]]" in body, "the annotate branch KEEPS the link"
    assert "link-provenance" in body
    assert c.verify(item) is True


def test_remove_branch_deletes_the_link(tmp_path: Path) -> None:
    vault, rel = _link_vault(tmp_path, "See [[person/Ghost]] here.\n")
    item = Link001Campaign.build_item(rel, "person/Ghost", citer_is_learn=False)
    c = Link001Campaign(worklist_items=[item], vault_path=vault)

    c.work(item)

    assert "[[person/Ghost]]" not in (vault / "note" / "R.md").read_text()
    assert c.verify(item) is True


def test_a_uniform_link_is_gone_verifier_would_fail_every_annotation(
    tmp_path: Path,
) -> None:
    """Why verify() is branch-DEPENDENT. The annotate branch deliberately keeps
    the link, so a uniform "the link is gone" check marks every annotation
    FAILED — and the campaign would retry them forever."""
    vault, rel = _link_vault(tmp_path, "See [[learn/L]] here.\n")
    item = Link001Campaign.build_item(rel, "learn/L", citer_is_learn=True)
    c = Link001Campaign(worklist_items=[item], vault_path=vault)
    c.work(item)

    body = (vault / "note" / "R.md").read_text()
    assert "[[learn/L]]" in body, "a uniform gone-check would call this FAILED"
    assert c.verify(item) is True, "the branch-aware verifier calls it done"


def test_the_branch_is_frozen_in_the_item_id(tmp_path: Path) -> None:
    """D4a. Re-deriving per run would let the same item annotate on Monday and
    DELETE on Tuesday, depending on whether a learn/ record existed in between.
    Removal is irreversible, so the decision must be a function of the input."""
    item = Link001Campaign.build_item("note/R.md", "learn/L", citer_is_learn=True)
    assert item.endswith("::annotate")
    path, target, branch = Link001Campaign.parse_item(item)
    assert (path, target, branch) == ("note/R.md", "learn/L", "annotate")

    # The same target, decided the other way at build time, stays REMOVE — the
    # verifier reads the frozen branch, never the vault's current shape.
    other = Link001Campaign.build_item("note/R.md", "learn/L", citer_is_learn=False)
    assert other.endswith("::remove")


def test_malformed_item_fails_loud(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="malformed"):
        Link001Campaign.parse_item("note/R.md::learn/L")


def test_link001_does_not_spend_quota_and_verifies_synchronously() -> None:
    """The D4 asymmetry, and why the 907 guard stays full strength here."""
    c = Link001Campaign(worklist_items=[], vault_path=Path("/tmp"))
    assert c.spends_quota() is False
    assert c.verify_is_async() is False


def test_annotate_is_idempotent(tmp_path: Path) -> None:
    """A re-run after a crash must not double-annotate."""
    vault, rel = _link_vault(tmp_path, "See [[learn/L]] here.\n")
    item = Link001Campaign.build_item(rel, "learn/L", citer_is_learn=True)
    c = Link001Campaign(worklist_items=[item], vault_path=vault)
    c.work(item)
    c.work(item)
    assert (vault / "note" / "R.md").read_text().count("link-provenance") == 1


# ---------------------------------------------------------------------------
# #44b delta — removal must heal the whitespace it sat in
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "before,after",
    [
        # The reported defect: a plain str.replace left a double space.
        ("See [[person/Ghost]] here.\n", "See here.\n"),
        # Hugging punctuation — the space before the link goes with it, so the
        # sentence does not end with " ." dangling.
        ("Spoke to [[person/Ghost]].\n", "Spoke to.\n"),
        # Leading and trailing positions leave nothing behind.
        ("[[person/Ghost]] called.\n", "called.\n"),
        ("Called [[person/Ghost]]\n", "Called\n"),
        # Two on one line: each heals independently.
        (
            "A [[person/Ghost]] B [[person/Ghost]] C\n",
            "A B C\n",
        ),
        # A link alone on its line must NOT swallow the newline and join its
        # neighbours — the reason the pattern uses horizontal whitespace only.
        ("one\n[[person/Ghost]]\ntwo\n", "one\n\ntwo\n"),
    ],
)
def test_removal_heals_the_whitespace(
    tmp_path: Path, before: str, after: str,
) -> None:
    """Exact before/after through the PRODUCTION path (``work()``).

    One record it is a typo; across the ~2,000 this campaign drains it becomes
    a second cleanup campaign, which is why it is fixed at the removal site.
    """
    vault = _vault(tmp_path)
    rel = "note/R.md"
    (vault / rel).write_text(f"---\ntype: note\n---\n\n{before}", encoding="utf-8")

    item = Link001Campaign.build_item(rel, "person/Ghost", citer_is_learn=False)
    c = Link001Campaign(worklist_items=[item], vault_path=vault)
    c.work(item)

    body = (vault / rel).read_text(encoding="utf-8")
    assert body == f"---\ntype: note\n---\n\n{after}"
    assert c.verify(item) is True, "the link is gone, so the branch verifies"
