"""R4 — the voice-calibration learning loop's doors (capture → propose → apply).

The loop's whole justification is the self-correcting standard's guardrail:
*learn → propose → operator-approves*, never silent unsupervised mutation. So the
headline tests here are STRUCTURAL — a consumer census over the real source tree
asserting that the vault writer has exactly one caller and that nothing applies
on a timer — rather than behavioural tests that could pass while a second,
unguarded door existed somewhere else in the tree.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from alfred.telegram import calibration, calibration_store
from alfred.telegram.calibration import Proposal
from alfred.telegram.config import CalibrationConfig


SRC_ROOT = Path(calibration.__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# A. The guardrail — structural consumer census
# ---------------------------------------------------------------------------


def _call_sites(func_name: str) -> list[tuple[str, str]]:
    """Every CALL of ``func_name`` in ``src/alfred``, as (relpath, enclosing def).

    AST-based rather than grep-based on purpose: the question is "what CALLS
    this", and a text search cannot tell a call from a mention in a docstring —
    which matters here because several modules discuss ``apply_proposals`` at
    length in prose. Bound the instrument to the structure you actually mean.
    """
    out: list[tuple[str, str]] = []
    for py in sorted(SRC_ROOT.rglob("*.py")):
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):  # pragma: no cover - defensive
            continue
        parents: dict[ast.AST, str] = {}

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for child in ast.walk(node):
                    parents.setdefault(child, node.name)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = (
                fn.attr if isinstance(fn, ast.Attribute)
                else fn.id if isinstance(fn, ast.Name)
                else ""
            )
            if name == func_name:
                out.append((
                    str(py.relative_to(SRC_ROOT)),
                    parents.get(node, "<module>"),
                ))
    return out


def test_apply_proposals_has_exactly_one_production_caller() -> None:
    """THE GUARDRAIL. Nothing writes calibration without crossing a thumb.

    ``calibration.apply_proposals`` is the only function in the tree that mutates
    the operator's calibration block. This asserts its ENTIRE production call set
    is ``calibration_store.approve_proposal`` — the function whose first guard
    refuses a blank operator. A second caller anywhere (a daemon sweep, a reply
    dispatcher, a future feed-card handler wired without reading this) is a
    silent-mutation path, and it reds this pin the moment it is written.

    Mutation that reds this: add an ``apply_proposals(...)`` call in any other
    module or function.
    """
    sites = _call_sites("apply_proposals")

    # POSITIVE CONTROL, asserted first and deliberately: the census must be
    # capable of FINDING a call at all. Without this, deleting the writer
    # entirely — or an AST walker that silently matched nothing — would satisfy
    # the equality below with an empty set and read as maximum safety.
    assert sites, "census found no apply_proposals call at all — the instrument is broken"

    assert set(sites) == {
        ("telegram/calibration_store.py", "approve_proposal"),
    }, f"unexpected calibration writer call sites: {sites}"


def test_the_only_apply_door_refuses_a_blank_operator_at_the_core() -> None:
    """The census above is only worth its assertion if the ONE caller guards.

    Pairs with it deliberately: "exactly one caller" plus "that caller refuses
    anonymous writes" is the actual guarantee. Either alone is decorative.
    """
    src = (SRC_ROOT / "telegram" / "calibration_store.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "approve_proposal"
    )
    # ``operator`` is KEYWORD-ONLY — it cannot be supplied positionally by
    # accident, so a caller must name it.
    assert "operator" in [a.arg for a in fn.args.kwonlyargs]


def test_nothing_in_the_calibration_loop_applies_on_a_timer() -> None:
    """The attribution-audit flow auto-confirms after 24h; this loop must NOT.

    THE EXPECTATION COMES FROM AN INDEPENDENT SOURCE. Asserting only "the
    calibration store has no timeout symbol" would be satisfied by a store that
    imported one, and it would also pass if the attribution constant were
    renamed and the whole comparison became meaningless. So this pins the
    ``AUTO_CONFIRM_AFTER_HOURS`` premise against ``attribution_section`` itself
    — the module this design deliberately refused to reuse — and then asserts
    the calibration path shares none of it.

    If that constant ever moves or is retired, this test fails LOUDLY and the
    next reader re-derives the contrast rather than inheriting a stale one.
    """
    from alfred.daily_sync import attribution_section

    # The premise: the flow we refused to reuse really is timeout-driven.
    assert attribution_section.AUTO_CONFIRM_AFTER_HOURS == 24

    # The claim: no part of the calibration loop REFERENCES that constant in
    # code. Bound to the AST rather than to the text, and the distinction is
    # load-bearing here rather than pedantic: ``calibration_store``'s module
    # docstring DISCUSSES ``AUTO_CONFIRM_AFTER_HOURS`` at length — explaining
    # why this design refused to reuse it — so a substring search reds on the
    # very prose that documents the guarantee. Same prose-is-not-a-call
    # distinction ``_call_sites`` above is built on.
    for rel in (
        "telegram/calibration_store.py",
        "telegram/calibration_capture.py",
        "daily_sync/calibration_section.py",
    ):
        tree = ast.parse((SRC_ROOT / rel).read_text(encoding="utf-8"))
        referenced = {
            n.id for n in ast.walk(tree) if isinstance(n, ast.Name)
        } | {
            n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
        }
        assert "AUTO_CONFIRM_AFTER_HOURS" not in referenced, (
            f"{rel} references an auto-confirm timeout in CODE"
        )

    # POSITIVE CONTROL for the loop above: the same instrument, pointed at the
    # module that DOES reference the constant, must find it. Without this, an
    # AST walker that silently collected nothing would pass all three.
    attr_tree = ast.parse(
        (SRC_ROOT / "daily_sync" / "attribution_section.py").read_text("utf-8")
    )
    attr_names = {n.id for n in ast.walk(attr_tree) if isinstance(n, ast.Name)}
    assert "AUTO_CONFIRM_AFTER_HOURS" in attr_names

    # And structurally: approve_proposal takes no age/cutoff/now parameter, so
    # there is no seam a timer could be threaded through without a signature
    # change that shows up in review.
    tree = ast.parse((SRC_ROOT / "telegram" / "calibration_store.py").read_text("utf-8"))
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "approve_proposal"
    )
    arg_names = {a.arg for a in fn.args.args} | {a.arg for a in fn.args.kwonlyargs}
    assert not (arg_names & {"now", "cutoff", "age_hours", "auto_confirm_after_hours"})


def test_the_review_section_never_imports_the_writer() -> None:
    """The Daily Sync surface is READ-ONLY structurally, not by promise.

    It may import ``calibration_store`` (for ``open_proposals``) but must never
    import ``calibration`` — the module holding ``apply_proposals``.
    """
    section = SRC_ROOT / "daily_sync" / "calibration_section.py"
    tree = ast.parse(section.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                imported.add(f"{node.module}.{alias.name}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name)

    assert "alfred.telegram.calibration_store.calibration_store" not in imported
    # The writer module itself, under either spelling.
    assert not any(
        m.endswith(".calibration") or m == "alfred.telegram.calibration"
        for m in imported
    ), f"review section imports the writer: {sorted(imported)}"
    # POSITIVE CONTROL: it really does import the STORE, so the negative above
    # is about a specific absence rather than a module that imports nothing.
    assert any("calibration_store" in m for m in imported)


# ---------------------------------------------------------------------------
# B. Refusal pins — each asserts WHY it refused, each with a positive control
# ---------------------------------------------------------------------------


@pytest.fixture
def cal_config(tmp_path: Path) -> CalibrationConfig:
    return CalibrationConfig(
        capture_enabled=True,
        pending_path=str(tmp_path / "pending.jsonl"),
        decided_path=str(tmp_path / "decided.jsonl"),
    )


def _vault_with_calibration_block(tmp_path: Path) -> tuple[Path, str]:
    """A vault carrying a person record with an empty calibration block.

    Sampled from the SHAPE production writes: the marker pair plus a
    ``## Communication Style`` subsection, which is what
    ``_insert_into_block`` appends into.
    """
    vault = tmp_path / "vault"
    (vault / "person").mkdir(parents=True)
    rel = "person/Andrew Newton"
    (vault / f"{rel}.md").write_text(
        "---\ntype: person\nname: Andrew Newton\n---\n\n"
        "# Andrew Newton\n\n"
        f"{calibration.CALIBRATION_MARKER_START}\n"
        "## Communication Style\n\n"
        "- existing line\n\n"
        f"{calibration.CALIBRATION_MARKER_END}\n",
        encoding="utf-8",
    )
    return vault, rel


def _seed_one(cal_config: CalibrationConfig) -> str:
    rows = calibration_store.record_proposals(
        cal_config.pending_path,
        cal_config.decided_path,
        [Proposal("Communication Style", "Prefers short answers.", 0.9, "session/S")],
    )
    return rows[0].proposal_id


def test_approve_without_operator_refuses_and_names_the_reason(
    tmp_path: Path, cal_config: CalibrationConfig
) -> None:
    """A blank operator refuses, the REASON says so, and the record is untouched.

    Asserting only ``"error" in result`` would pass identically against an
    unknown-id refusal or a missing-record refusal — three different denials
    wearing one shape. The reason string is what distinguishes the guard firing
    from an unrelated failure, so it is asserted.
    """
    vault, rel = _vault_with_calibration_block(tmp_path)
    pid = _seed_one(cal_config)
    before = (vault / f"{rel}.md").read_text(encoding="utf-8")

    res = calibration_store.approve_proposal(
        vault, cal_config, pid, operator="", user_rel_path=rel,
    )

    assert "error" in res
    assert "operator" in res["error"], res["error"]
    assert (vault / f"{rel}.md").read_text(encoding="utf-8") == before
    # No decision recorded — the proposal must remain approvable.
    assert calibration_store.decided_ids(cal_config.decided_path) == set()

    # POSITIVE CONTROL — the nearest admissible neighbour. The SAME call with a
    # named operator writes, which proves the refusal above was about the
    # operator and not about a broken fixture, an unwritable vault, or a
    # proposal that could never have applied.
    ok = calibration_store.approve_proposal(
        vault, cal_config, pid, operator="andrew", user_rel_path=rel,
    )
    assert "error" not in ok, ok
    assert "Prefers short answers." in (vault / f"{rel}.md").read_text(encoding="utf-8")


def test_approve_without_a_target_record_refuses_and_names_the_reason(
    tmp_path: Path, cal_config: CalibrationConfig
) -> None:
    """No ``primary_users`` → refuse rather than guess a person record."""
    vault, rel = _vault_with_calibration_block(tmp_path)
    pid = _seed_one(cal_config)

    res = calibration_store.approve_proposal(
        vault, cal_config, pid, operator="andrew", user_rel_path="",
    )
    assert "error" in res
    assert "primary_users" in res["error"], res["error"]
    assert calibration_store.decided_ids(cal_config.decided_path) == set()

    # Positive control: same call, real target → applies.
    ok = calibration_store.approve_proposal(
        vault, cal_config, pid, operator="andrew", user_rel_path=rel,
    )
    assert "error" not in ok, ok


def test_reject_records_the_verdict_and_touches_no_vault(
    tmp_path: Path, cal_config: CalibrationConfig
) -> None:
    """Reject is a decision, not a write."""
    vault, rel = _vault_with_calibration_block(tmp_path)
    pid = _seed_one(cal_config)
    before = (vault / f"{rel}.md").read_text(encoding="utf-8")

    res = calibration_store.reject_proposal(cal_config, pid, operator="andrew")
    assert res.get("rejected") == pid
    assert (vault / f"{rel}.md").read_text(encoding="utf-8") == before

    decisions = calibration_store.load_decisions(cal_config.decided_path)
    assert len(decisions) == 1
    assert decisions[0].decision == calibration_store.DECISION_REJECT
    assert decisions[0].operator == "andrew"

    # POSITIVE CONTROL that the vault was writable all along — otherwise
    # "touched no vault" is indistinguishable from "could not have written".
    other = calibration_store.record_proposals(
        cal_config.pending_path, cal_config.decided_path,
        [Proposal("Communication Style", "Another observation.", 0.9, "session/S")],
    )[0].proposal_id
    calibration_store.approve_proposal(
        vault, cal_config, other, operator="andrew", user_rel_path=rel,
    )
    assert (vault / f"{rel}.md").read_text(encoding="utf-8") != before


def test_reject_without_operator_refuses_and_names_the_reason(
    tmp_path: Path, cal_config: CalibrationConfig
) -> None:
    pid = _seed_one(cal_config)
    res = calibration_store.reject_proposal(cal_config, pid, operator="  ")
    assert "error" in res
    assert "operator" in res["error"]
    assert calibration_store.decided_ids(cal_config.decided_path) == set()

    ok = calibration_store.reject_proposal(cal_config, pid, operator="andrew")
    assert "error" not in ok, ok


def test_an_already_decided_proposal_cannot_be_applied_twice(
    tmp_path: Path, cal_config: CalibrationConfig
) -> None:
    """A double-tap must not append the bullet twice."""
    vault, rel = _vault_with_calibration_block(tmp_path)
    pid = _seed_one(cal_config)

    first = calibration_store.approve_proposal(
        vault, cal_config, pid, operator="andrew", user_rel_path=rel,
    )
    assert "error" not in first
    body = (vault / f"{rel}.md").read_text(encoding="utf-8")
    assert body.count("Prefers short answers.") == 1

    second = calibration_store.approve_proposal(
        vault, cal_config, pid, operator="andrew", user_rel_path=rel,
    )
    assert "error" in second
    assert "already decided" in second["error"]
    assert (vault / f"{rel}.md").read_text(encoding="utf-8").count(
        "Prefers short answers."
    ) == 1


def test_a_failed_vault_write_leaves_the_proposal_pending(
    tmp_path: Path, cal_config: CalibrationConfig
) -> None:
    """A write that does not land must NOT burn the proposal.

    Recording the decision anyway would permanently exclude the observation
    from the review list with nothing applied — silently losing it.
    """
    vault = tmp_path / "vault"
    (vault / "person").mkdir(parents=True)
    # A record with NO calibration block: apply_proposals' rewriter no-ops.
    (vault / "person" / "Andrew Newton.md").write_text(
        "---\ntype: person\nname: Andrew Newton\n---\n\n# Andrew Newton\n",
        encoding="utf-8",
    )
    pid = _seed_one(cal_config)

    res = calibration_store.approve_proposal(
        vault, cal_config, pid, operator="andrew",
        user_rel_path="person/Andrew Newton",
    )

    # THE INVARIANT, and it is what this test is really for: a decision row
    # exists IF AND ONLY IF the bullet landed. This assertion caught a real
    # defect on its first run — ``apply_proposals`` reports ``written=True`` on
    # a record with no calibration block (the frontmatter write succeeds while
    # the body rewriter no-ops), so the first cut recorded a decision for a
    # bullet that never landed and dropped the observation forever.
    body = (vault / "person" / "Andrew Newton.md").read_text(encoding="utf-8")
    landed = "Prefers short answers." in body
    recorded = pid in calibration_store.decided_ids(cal_config.decided_path)
    assert landed == recorded, (
        f"decision/write disagree: landed={landed} recorded={recorded} — "
        "a recorded decision with no write silently loses the observation"
    )

    # And the refusal is NAMED, so the operator can act on it rather than
    # wondering why an approve did nothing.
    assert "error" in res
    assert "no calibration block" in res["error"], res["error"]
    assert not landed and not recorded

    # POSITIVE CONTROL — the nearest admissible neighbour: the SAME proposal,
    # against a record that DOES carry the block, applies and records. Without
    # this the refusal above would be indistinguishable from a proposal that
    # could never have applied at all.
    good_vault, good_rel = _vault_with_calibration_block(tmp_path / "ok")
    ok = calibration_store.approve_proposal(
        good_vault, cal_config, pid, operator="andrew", user_rel_path=good_rel,
    )
    assert "error" not in ok, ok
    assert pid in calibration_store.decided_ids(cal_config.decided_path)


# ---------------------------------------------------------------------------
# C. Store behaviour
# ---------------------------------------------------------------------------


def test_the_same_observation_twice_makes_one_pending_row(
    cal_config: CalibrationConfig,
) -> None:
    """Content-keyed ids — two sessions noticing the same thing is one review."""
    first = calibration_store.record_proposals(
        cal_config.pending_path, cal_config.decided_path,
        [Proposal("Communication Style", "Prefers short answers.", 0.9, "session/A")],
    )
    second = calibration_store.record_proposals(
        cal_config.pending_path, cal_config.decided_path,
        # Same sentence, different session, different whitespace + case.
        [Proposal("Communication Style", "prefers  short   answers.", 0.7, "session/B")],
    )
    assert len(first) == 1
    assert len(second) == 0
    assert len(calibration_store.open_proposals(
        cal_config.pending_path, cal_config.decided_path)) == 1


def test_a_rejected_observation_never_comes_back(
    cal_config: CalibrationConfig,
) -> None:
    """The decided set excludes a rejection from RE-CAPTURE, not just from display.

    Display-only exclusion would let the analyzer re-append the same sentence
    every session; the queue would look clean and grow forever underneath.
    """
    pid = _seed_one(cal_config)
    calibration_store.reject_proposal(cal_config, pid, operator="andrew")
    assert calibration_store.open_proposals(
        cal_config.pending_path, cal_config.decided_path) == []

    again = calibration_store.record_proposals(
        cal_config.pending_path, cal_config.decided_path,
        [Proposal("Communication Style", "Prefers short answers.", 0.9, "session/C")],
    )
    assert again == []
    # And nothing was appended to the pending file either.
    assert len(calibration_store.load_pending(cal_config.pending_path)) == 1


def test_a_corrupt_store_row_is_skipped_not_fatal(
    cal_config: CalibrationConfig,
) -> None:
    """One bad line must not cost the operator the whole review."""
    _seed_one(cal_config)
    with open(cal_config.pending_path, "a", encoding="utf-8") as f:
        f.write("{not json at all\n")
        f.write(json.dumps(["a list, not an object"]) + "\n")
    assert len(calibration_store.open_proposals(
        cal_config.pending_path, cal_config.decided_path)) == 1


def test_capture_logs_an_explicit_nothing_new_signal(
    cal_config: CalibrationConfig,
) -> None:
    """ILB — a capture that drafted nothing NEW is the steady state and must be
    distinguishable from a capture that never ran.

    Log-emission pinned per the standing discipline: a future refactor that
    drops the line stays green on behaviour alone while the operator's grep goes
    dark.
    """
    import structlog

    _seed_one(cal_config)
    with structlog.testing.capture_logs() as captured:
        calibration_store.record_proposals(
            cal_config.pending_path, cal_config.decided_path,
            [Proposal("Communication Style", "Prefers short answers.", 0.9, "session/D")],
        )
    events = [c for c in captured if c.get("event") == "talker.calibration.capture_recorded"]
    assert len(events) == 1
    assert events[0]["appended"] == 0
    assert events[0]["skipped_duplicate"] == 1
    assert events[0]["drafted"] == 1
