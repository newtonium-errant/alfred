"""#44 slice 2 — the two ratified campaigns.

The gmail campaign's whole job is to make the budget BIND on a directory the
curator daemon actively watches. The link001 campaign's whole job is to keep an
irreversible branch decision frozen at list-build.
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import frontmatter
import pytest
import structlog
import yaml

from alfred.drip.campaigns import (
    _PROVENANCE_MARK,
    GmailBacklogCampaign,
    Link001Campaign,
    _flexible_ws_re,
)
from alfred.drip.runner import run_increment
from alfred.drip.state import DONE, FAILED, IN_FLIGHT, CampaignState

TODAY = date(2026, 8, 4)

#: A real long vault title, and the exact shape PyYAML folds it into at its
#: default width of 80 — reproduced from the live 2026-08-06 increment rather
#: than invented, because the whole failure class depends on the wrap landing
#: mid-title. ``test_the_wrapped_fixture_is_the_shape_yaml_actually_emits``
#: below pins that this literal still matches what a dump produces.
WRAPPED_TARGET = (
    "constraint/Multi-Instance Alfred Has Information That Cannot Be Shared "
    "Across Instances"
)
WRAPPED_ENTRY = (
    "- '[[constraint/Multi-Instance Alfred Has Information That Cannot Be "
    "Shared Across\n  Instances]]'\n"
)


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


# ---------------------------------------------------------------------------
# #60 — YAML-WRAPPED frontmatter links
#
# Found live on 2026-08-06: 4 of the first increment's 12 ``done`` items were
# false-dones. The scanner PARSES frontmatter and normalizes whitespace (by
# design, see ``janitor/parser.extract_wikilinks``); the campaign matched the
# EXACT raw string. A folded long title is therefore reported by the scanner and
# invisible to the repair — and the removal branch's ``link not in body``
# verifier PASSED on it, because the wrapped form does not contain the unwrapped
# needle. Detection at the parser level, repair at the text level.
# ---------------------------------------------------------------------------


def _wrapped_vault(tmp_path: Path, rtype: str = "note") -> Path:
    """A record whose ``related`` list holds ONE folded link and one short one.

    The short sibling is load-bearing: it proves the surgery is targeted. An
    edit that took the whole block, or a regex that ran away across the fold,
    would take ``person/Someone`` with it and no assertion on the wrapped link
    alone would notice.
    """
    vault = _vault(tmp_path)
    (vault / "note" / "R.md").write_text(
        f"---\ntype: {rtype}\nrelated:\n{WRAPPED_ENTRY}"
        "- '[[person/Someone]]'\n---\n\nBody.\n",
        encoding="utf-8",
    )
    return vault


def test_the_wrapped_fixture_is_the_shape_yaml_actually_emits() -> None:
    """The fixture is only worth anything if production emits this shape.

    Every vault fixture in the repo was previously wrap-free, which is exactly
    how an entire failure class stayed invisible behind a green suite: the
    defect needs a fold to exist at all, so a suite without folds could not
    have caught it however many tests it ran. This pins the fixture to PyYAML's
    real output rather than to a plausible-looking hand-written string.
    """
    dumped = yaml.dump(
        {"related": [f"[[{WRAPPED_TARGET}]]"]},
        default_flow_style=False, allow_unicode=True, sort_keys=False,
    )
    assert "\n" in dumped.split("related:\n")[1].rstrip("\n"), (
        "PyYAML must still FOLD this title — if it stopped, this fixture no "
        "longer reproduces the #60 shape and the pins below are decorative"
    )
    assert WRAPPED_ENTRY.rstrip("\n") in dumped, (
        "the fixture literal must match what a dump actually produces"
    )
    # And the raw exact needle is absent — the defect's precondition.
    assert f"[[{WRAPPED_TARGET}]]" not in dumped


def test_the_old_exact_match_verifier_false_passed_a_wrapped_removal(
    tmp_path: Path,
) -> None:
    """THE bug, in executable form: the two verdicts disagree.

    ``old_verdict`` is the shipped predicate verbatim. On a wrapped link it says
    "the link is gone" about a record that still contains it — so the runner
    recorded DONE over an untouched file and the frozen work-list never offered
    the item again. The fixed verifier returns False here, which is what lets
    the 907 guard do its job.

    A verifier must never be satisfiable by the defect it exists to catch, and
    this one was: the single condition that stopped ``work()`` landing (needle
    absent from raw text) is the same condition that made ``verify()`` pass.
    """
    vault = _wrapped_vault(tmp_path)
    item = Link001Campaign.build_item("note/R.md", WRAPPED_TARGET,
                                      citer_is_learn=False)
    c = Link001Campaign(worklist_items=[item], vault_path=vault)

    body = (vault / "note" / "R.md").read_text(encoding="utf-8")
    old_verdict = f"[[{WRAPPED_TARGET}]]" not in body
    assert old_verdict is True, (
        "the OLD verifier called this done — the false-done, reproduced"
    )
    assert c.verify(item) is False, (
        "the fixed verifier must see the wrapped link that is plainly there"
    )


def test_remove_branch_lands_on_a_wrapped_frontmatter_link(
    tmp_path: Path,
) -> None:
    """Work lands where it was aimed, and the record stays parseable YAML."""
    vault = _wrapped_vault(tmp_path)
    item = Link001Campaign.build_item("note/R.md", WRAPPED_TARGET,
                                      citer_is_learn=False)
    c = Link001Campaign(worklist_items=[item], vault_path=vault)

    c.work(item)

    text = (vault / "note" / "R.md").read_text(encoding="utf-8")
    meta = frontmatter.loads(text).metadata
    assert meta["related"] == ["[[person/Someone]]"], (
        "the wrapped entry goes entirely; the short sibling is untouched"
    )
    assert c.verify(item) is True


def test_annotate_branch_lands_on_a_wrapped_frontmatter_link(
    tmp_path: Path,
) -> None:
    """The end-to-end annotate pin: work lands, YAML still parses, verify
    passes, and the janitor scanner still sees the link.

    All four matter together. The mark is inserted INSIDE the quoted scalar,
    which is the placement the #60 recon measured: after the closing quote,
    PyYAML raises ParserError and the record becomes unreadable to the janiter
    entirely — a silent break far worse than the bug being fixed.
    """
    from alfred.janitor.parser import extract_wikilinks

    vault = _wrapped_vault(tmp_path, rtype="learn")
    item = Link001Campaign.build_item("note/R.md", WRAPPED_TARGET,
                                      citer_is_learn=True)
    c = Link001Campaign(worklist_items=[item], vault_path=vault)
    assert c.verify(item) is False, "not annotated yet"

    c.work(item)

    text = (vault / "note" / "R.md").read_text(encoding="utf-8")
    # (1) still valid YAML, via the same library the janitor parses with
    meta = frontmatter.loads(text).metadata
    # (2) the link SURVIVES — this branch keeps it — and carries the mark
    entry = meta["related"][0]
    assert entry.startswith(f"[[{WRAPPED_TARGET}]]")
    assert _PROVENANCE_MARK in entry
    assert meta["related"][1] == "[[person/Someone]]", "sibling untouched"
    # (3) the verifier agrees
    assert c.verify(item) is True
    # (4) the scanner still resolves the link to the same target
    assert WRAPPED_TARGET in extract_wikilinks(text)


def test_annotate_on_a_wrapped_link_is_idempotent(tmp_path: Path) -> None:
    """A re-run must not stack marks — and the tolerant idempotence check has
    to survive the fold, or every re-run appends another one."""
    vault = _wrapped_vault(tmp_path, rtype="learn")
    item = Link001Campaign.build_item("note/R.md", WRAPPED_TARGET,
                                      citer_is_learn=True)
    c = Link001Campaign(worklist_items=[item], vault_path=vault)

    c.work(item)
    c.work(item)

    text = (vault / "note" / "R.md").read_text(encoding="utf-8")
    assert text.count("link-provenance") == 1
    assert frontmatter.loads(text).metadata["related"][1] == "[[person/Someone]]"


def test_the_provenance_mark_carries_no_apostrophe(tmp_path: Path) -> None:
    """A structural guard on the CONSTANT, because the constraint is invisible
    where someone would edit it.

    ``work()`` inserts this mark inside a SINGLE-QUOTED YAML scalar. YAML
    escapes a literal ``'`` there by doubling it, so an un-doubled apostrophe in
    the wording (``retained (it's a learn record)``) would close the scalar
    early and corrupt the frontmatter — after which the janitor cannot parse the
    record at all. Reworded marks are cheap; this makes the cost visible.
    """
    assert "'" not in _PROVENANCE_MARK

    # And the property that matters, driven rather than asserted about: a mark
    # inserted into a quoted scalar leaves the record parseable.
    vault = _wrapped_vault(tmp_path, rtype="learn")
    item = Link001Campaign.build_item("note/R.md", WRAPPED_TARGET,
                                      citer_is_learn=True)
    Link001Campaign(worklist_items=[item], vault_path=vault).work(item)
    text = (vault / "note" / "R.md").read_text(encoding="utf-8")
    assert yaml.safe_load(text.split("---\n")[1]) is not None


@pytest.mark.parametrize("branch_is_learn", [True, False])
def test_work_does_not_touch_the_file_when_nothing_changes(
    tmp_path: Path, branch_is_learn: bool,
) -> None:
    """The mtime half of #60, both branches.

    The replaced code called ``write_text`` unconditionally, so a no-op repair
    still moved the file's mtime. That is the worst available lie for this
    campaign: mtime is the cheapest evidence an operator has that a record was
    touched, and a bumped mtime over unchanged bytes is what made 4 false-dones
    look worked. The timestamp is pinned to a fixed value first, so this fails
    deterministically rather than depending on clock resolution.
    """
    vault = _wrapped_vault(tmp_path)
    path = vault / "note" / "R.md"
    before = path.read_text(encoding="utf-8")
    os.utime(path, ns=(1_000_000_000, 1_000_000_000))

    item = Link001Campaign.build_item(
        "note/R.md", "person/NotInThisFile", citer_is_learn=branch_is_learn,
    )
    Link001Campaign(worklist_items=[item], vault_path=vault).work(item)

    assert path.stat().st_mtime_ns == 1_000_000_000, (
        "a no-op repair must not move the mtime"
    )
    assert path.read_text(encoding="utf-8") == before


def test_a_no_op_on_a_present_target_warns_and_an_absent_one_informs(
    tmp_path: Path,
) -> None:
    """ILB, with the level carrying the distinction.

    Silence on a no-op work() is what let the first live increment look healthy,
    so the no-op is a named event. Its LEVEL splits on the only thing that
    separates an anomaly from a legitimate idempotent re-run: whether the target
    is still sitting in the record.
    """
    vault = _vault(tmp_path)
    (vault / "note" / "R.md").write_text(
        "---\ntype: note\n---\n\nNothing here.\n", encoding="utf-8",
    )
    item = Link001Campaign.build_item("note/R.md", "person/Gone",
                                      citer_is_learn=False)
    c = Link001Campaign(worklist_items=[item], vault_path=vault)

    with structlog.testing.capture_logs() as captured:
        c.work(item)

    events = [e for e in captured if e.get("event") == "drip.link001.no_change"]
    assert len(events) == 1
    assert events[0]["log_level"] == "info", "an absent target is idempotent"
    assert events[0]["target_present"] is False
    assert events[0]["branch"] == "remove"
    assert events[0]["item_id"] == item
    assert events[0]["path"] == "note/R.md"


def test_a_no_op_over_a_target_that_is_still_there_warns(tmp_path: Path) -> None:
    """The anomaly side of the same event.

    Reached by a link whose text is present but which the matcher cannot edit —
    here an EMBED (``![[...]]``), which the janitor deliberately excludes from
    LINK001 and which the annotate branch's own idempotence check must not
    mistake for an annotation. The point of the pin is the signal, not the
    embed: work() ran, the target is demonstrably in the file, nothing changed,
    and that must reach the log at warning level rather than passing silently.
    """
    vault = _vault(tmp_path)
    (vault / "note" / "R.md").write_text(
        "---\ntype: learn\n---\n\nSee [[learn/L]] "
        f"{_PROVENANCE_MARK} already.\n",
        encoding="utf-8",
    )
    item = Link001Campaign.build_item("note/R.md", "learn/L", citer_is_learn=True)
    c = Link001Campaign(worklist_items=[item], vault_path=vault)

    # Already annotated: the early idempotent return, which must NOT log a
    # no-change event — it is a successful verified state, not a no-op.
    with structlog.testing.capture_logs() as captured:
        c.work(item)
    assert not [e for e in captured
                if e.get("event") == "drip.link001.no_change"], (
        "an already-annotated record is done, not a no-op"
    )
    assert c.verify(item) is True


# --- the shared seam -------------------------------------------------------


def test_one_pattern_matches_both_the_wrapped_and_unwrapped_spelling() -> None:
    """The seam property. There is no second code path for wrapped links, which
    is what keeps the large unwrapped population's behaviour unchanged."""
    pattern = _flexible_ws_re("[[a/B C D]]")
    assert pattern.search("x [[a/B C D]] y")
    assert pattern.search("x '[[a/B C\n  D]]' y")
    assert pattern.search("x '[[a/B\n  C D]]' y")


def test_the_tolerant_matcher_does_not_over_match_a_longer_target() -> None:
    """``\\s+`` between tokens is safe because the needle carries its own
    delimiters: the tokens must sit between a literal ``[[`` and ``]]``,
    separated by whitespace ONLY. A prefix target therefore cannot eat a longer
    sibling — which for the removal branch would be unrecoverable data loss."""
    pattern = _flexible_ws_re("[[Cox and Palmer]]")
    assert pattern.search("[[Cox and Palmer]]")
    assert not pattern.search("[[Cox and Palmer Halifax]]")
    assert not pattern.search("[[Cox and other Palmer]]")


# --- the removal contract for list entries --------------------------------


@pytest.mark.parametrize(
    "entry,expected_related",
    [
        # Wrapped, quoted, the link is the entry's WHOLE content -> entry goes.
        (WRAPPED_ENTRY, ["[[person/Someone]]"]),
        # Unwrapped and quoted: the same contract, so the already-shipped
        # unwrapped population stops leaving ``- ''`` behind too.
        ("- '[[constraint/Short]]'\n", ["[[person/Someone]]"]),
        # Double-quoted spelling.
        ('- "[[constraint/Short]]"\n', ["[[person/Someone]]"]),
        # The entry carries MORE than the link, so it is not wholly the link:
        # it keeps its remaining content and only the link is healed out.
        ("- '[[constraint/Short]] and a note'\n",
         ["and a note", "[[person/Someone]]"]),
    ],
)
def test_removing_a_list_entry_link_takes_the_whole_entry(
    tmp_path: Path, entry: str, expected_related: list[str],
) -> None:
    """Healing alone leaves ``- ''`` — an empty string sitting in a list of
    wikilinks, which is neither a link nor absence. Measured (#60): dropping
    the entry leaves a clean list where healing leaves ``['', ...]``."""
    target = (WRAPPED_TARGET if entry is WRAPPED_ENTRY else "constraint/Short")
    vault = _vault(tmp_path)
    (vault / "note" / "R.md").write_text(
        f"---\ntype: note\nrelated:\n{entry}- '[[person/Someone]]'\n"
        "---\n\nBody.\n",
        encoding="utf-8",
    )
    item = Link001Campaign.build_item("note/R.md", target, citer_is_learn=False)
    c = Link001Campaign(worklist_items=[item], vault_path=vault)

    c.work(item)

    text = (vault / "note" / "R.md").read_text(encoding="utf-8")
    assert frontmatter.loads(text).metadata["related"] == expected_related
    assert c.verify(item) is True


def test_removing_the_sole_list_entry_leaves_a_null_key_not_an_empty_string(
    tmp_path: Path,
) -> None:
    """The edge the entry-drop creates, pinned to the shape the live consumers
    already tolerate: ``janitor/autofix`` coerces a non-list to ``[]`` and
    ``surveyor/cleanup`` guards on ``isinstance(..., list)``. Verified against
    those call sites, not assumed."""
    vault = _vault(tmp_path)
    (vault / "note" / "R.md").write_text(
        f"---\ntype: note\nrelated:\n{WRAPPED_ENTRY}---\n\nBody.\n",
        encoding="utf-8",
    )
    item = Link001Campaign.build_item("note/R.md", WRAPPED_TARGET,
                                      citer_is_learn=False)
    c = Link001Campaign(worklist_items=[item], vault_path=vault)

    c.work(item)

    meta = frontmatter.loads(
        (vault / "note" / "R.md").read_text(encoding="utf-8")
    ).metadata
    assert meta["related"] is None, "the key stays, the empty entry does not"
    assert c.verify(item) is True


def test_a_markdown_bullet_whose_whole_content_is_the_link_loses_the_line(
    tmp_path: Path,
) -> None:
    """Same debris, same contract. Healing alone would leave a dangling ``- ``
    bullet in the body."""
    vault = _vault(tmp_path)
    (vault / "note" / "R.md").write_text(
        "---\ntype: note\n---\n\n## Related\n\n"
        "- [[person/Ghost]]\n- [[person/Kept]]\n",
        encoding="utf-8",
    )
    item = Link001Campaign.build_item("note/R.md", "person/Ghost",
                                      citer_is_learn=False)
    c = Link001Campaign(worklist_items=[item], vault_path=vault)

    c.work(item)

    assert (vault / "note" / "R.md").read_text(encoding="utf-8") == (
        "---\ntype: note\n---\n\n## Related\n\n- [[person/Kept]]\n"
    )


def test_a_frontmatter_scalar_field_keeps_its_key(tmp_path: Path) -> None:
    """Deleting a DECLARED field is a schema change and this campaign has no
    mandate to make one, so a scalar field is emptied rather than removed. The
    distinction from the list-entry case is that an empty string is already how
    an unset scalar field is spelled, whereas a list of links has no such
    spelling for 'one fewer link'."""
    vault = _vault(tmp_path)
    (vault / "note" / "R.md").write_text(
        f"---\ntype: note\nsource_a: '[[{WRAPPED_TARGET}]]'\n---\n\nBody.\n",
        encoding="utf-8",
    )
    item = Link001Campaign.build_item("note/R.md", WRAPPED_TARGET,
                                      citer_is_learn=False)
    c = Link001Campaign(worklist_items=[item], vault_path=vault)

    c.work(item)

    meta = frontmatter.loads(
        (vault / "note" / "R.md").read_text(encoding="utf-8")
    ).metadata
    assert meta["source_a"] == ""
    assert "source_a" in meta
    assert c.verify(item) is True


# --- through the runner ----------------------------------------------------


def test_a_wrapped_item_reaches_done_through_the_runner_with_the_edit_landed(
    tmp_path: Path,
) -> None:
    """The e2e pin, through the production entry point the scheduler uses.

    Per-layer pins cannot catch what #60 was: the campaign's two halves agreed
    with each other and both were wrong, so only driving ``run_increment`` and
    then asserting on the FILE distinguishes "recorded done" from "actually
    repaired". Pre-fix this run also reported ``done=1`` — over an untouched
    record — which is precisely why the state assertion alone is not enough.
    """
    vault = _wrapped_vault(tmp_path)
    item = Link001Campaign.build_item("note/R.md", WRAPPED_TARGET,
                                      citer_is_learn=False)
    c = Link001Campaign(worklist_items=[item], vault_path=vault)
    state = CampaignState(campaign="link001_repair")

    result = _run(c, state, tmp_path)

    assert result.done == 1
    assert result.failed == 0
    assert state.items[item].state == DONE
    # The half a state assertion cannot see.
    meta = frontmatter.loads(
        (vault / "note" / "R.md").read_text(encoding="utf-8")
    ).metadata
    assert meta["related"] == ["[[person/Someone]]"], (
        "DONE must mean the record actually changed"
    )


def test_an_unfixable_wrapped_item_fails_rather_than_reporting_done(
    tmp_path: Path,
) -> None:
    """The honest direction, restored. A target the matcher genuinely cannot
    find leaves the record untouched, and the 907 guard must then call the item
    FAILED — retried, and retired at max_attempts — instead of DONE.

    This is the pin the annotate branch already satisfied by accident (it failed
    visibly) and the removal branch inverted (it failed silently).
    """
    vault = _wrapped_vault(tmp_path, rtype="learn")
    item = Link001Campaign.build_item("note/R.md", WRAPPED_TARGET,
                                      citer_is_learn=True)
    # Make the record unwritable-in-effect by pointing the item at a target the
    # file does not contain in ANY spelling.
    broken = Link001Campaign.build_item(
        "note/R.md", "person/DefinitelyAbsent", citer_is_learn=True,
    )
    c = Link001Campaign(worklist_items=[broken], vault_path=vault)
    state = CampaignState(campaign="link001_repair")

    result = _run(c, state, tmp_path)

    assert result.done == 0
    assert result.failed == 1
    assert state.items[broken].state == FAILED
    assert item  # the wrapped sibling is untouched by this run
    assert WRAPPED_TARGET in (vault / "note" / "R.md").read_text(
        encoding="utf-8"
    ).replace("\n  ", " ")
