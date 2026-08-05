"""The frozen link001 work-list builder (#50).

WHY THIS EXISTS. #44b's campaign reads its work-list from a frozen file and
nothing generated one, so the campaign could not drain. D4a requires the
annotate-vs-remove branch to be decided ONCE, at build time, because the two
branches have opposite failure modes: a wrongly-annotated link is noise, a
wrongly-removed one is unrecoverable.

Two things these pins guard that nothing else can:

* **The branch is keyed on the CITER, not the target.** D2 rules on "inbound
  links FROM learn-records" and "non-learning CITERS" — the record HOLDING the
  link. The campaign's parameter is spelled ``is_learn_target``, which reads as
  the opposite; a future "fix" aligning the call site to the name would silently
  invert every decision in the file. Pinned both directions.
* **The #49 exclusion is INHERITED, not reimplemented.** A link quoted inside a
  janitor_note is annotation prose. If the builder saw those, the campaign would
  "repair" links that exist only inside explanatory sentences — editing records
  that were never broken.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest
import structlog

from alfred.drip.campaigns import Link001Campaign
from alfred.drip.worklist import (
    ITEM_SEP,
    LINK001_MESSAGE_RE,
    build_link001_worklist,
    render_worklist,
)
from alfred.janitor.config import (
    JanitorConfig,
    StateConfig,
    SweepConfig,
    VaultConfig,
)
from alfred.janitor.state import JanitorState


def _vault(tmp_path: Path, records: dict[str, str]) -> JanitorConfig:
    vault = tmp_path / "vault"
    for rel, body in records.items():
        p = vault / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    vault.mkdir(parents=True, exist_ok=True)
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    return JanitorConfig(
        vault=VaultConfig(
            path=str(vault),
            ignore_dirs=[".obsidian", "_templates", "_bases"],
            ignore_files=[".gitkeep"],
        ),
        sweep=SweepConfig(),
        state=StateConfig(path=str(data / "janitor_state.json")),
    )


def _rec(rec_type: str, name: str, body: str, note: str = "") -> str:
    note_line = f"janitor_note: {note}\n" if note else ""
    return dedent(
        f"""\
        ---
        type: {rec_type}
        name: {name}
        status: active
        created: 2026-01-01
        tags: []
        {note_line}---

        {body}
        """
    )


def _build(cfg: JanitorConfig):
    return build_link001_worklist(
        cfg, JanitorState(cfg.state.path, cfg.state.max_sweep_history),
    )


# ---------------------------------------------------------------------------
# The branch decision — keyed on the CITER
# ---------------------------------------------------------------------------


def test_a_learn_record_citer_annotates(tmp_path: Path) -> None:
    """D2: a learning that cites a since-deleted source keeps its provenance.

    The record HOLDING the link is the learn record; the broken target is the
    deleted source and is not a learn record at all — a missing file has no
    type. That asymmetry is the whole ruling.
    """
    cfg = _vault(tmp_path, {
        "decision/D.md": _rec("decision", "D", "Based on [[note/DeletedSpam]]."),
    })
    build = _build(cfg)
    assert build.items == ["decision/D.md::note/DeletedSpam::annotate"]
    assert (build.annotate, build.remove) == (1, 0)


def test_a_non_learn_citer_removes(tmp_path: Path) -> None:
    cfg = _vault(tmp_path, {
        "note/N.md": _rec("note", "N", "See [[person/Ghost]]."),
    })
    build = _build(cfg)
    assert build.items == ["note/N.md::person/Ghost::remove"]
    assert (build.annotate, build.remove) == (0, 1)


def test_the_branch_follows_the_citer_not_the_target(tmp_path: Path) -> None:
    """THE inversion guard.

    A non-learn record pointing at a learn-shaped target must REMOVE, and a
    learn record pointing at a non-learn-shaped target must ANNOTATE. A build
    that keyed on the target — which is what the campaign's ``is_learn_target``
    parameter name suggests — produces exactly the opposite pair here.
    """
    cfg = _vault(tmp_path, {
        "note/N.md": _rec("note", "N", "Cites [[decision/Gone]]."),
        "constraint/C.md": _rec("constraint", "C", "Cites [[person/Gone]]."),
    })
    build = _build(cfg)
    assert sorted(build.items) == [
        "constraint/C.md::person/Gone::annotate",
        "note/N.md::decision/Gone::remove",
    ]


def test_frontmatter_type_beats_the_directory(tmp_path: Path) -> None:
    """A record filed in the wrong directory is exactly the population DIR001
    reports, so the directory cannot be trusted as the type."""
    cfg = _vault(tmp_path, {
        # A decision (learn type) misfiled under note/.
        "note/Misfiled.md": _rec("decision", "Misfiled", "Cites [[person/Gone]]."),
    })
    build = _build(cfg)
    assert build.items == ["note/Misfiled.md::person/Gone::annotate"]


# ---------------------------------------------------------------------------
# The #49 inheritance — the cross-task contract
# ---------------------------------------------------------------------------


def test_a_link_quoted_only_in_a_janitor_note_is_not_work(tmp_path: Path) -> None:
    """The coupling to #49, pinned rather than assumed.

    A janitor_note quoting ``[[person/QuotedOnly]]`` is prose explaining a
    break, not a reference. If it reached the work-list the campaign would edit
    a record whose body was never broken.
    """
    cfg = _vault(tmp_path, {
        "note/Q.md": _rec(
            "note", "Q", "Clean body, no links.",
            note="LINK001 — broken wikilink [[person/QuotedOnly]]",
        ),
    })
    build = _build(cfg)
    assert build.items == []
    assert build.total == 0


def test_a_real_break_survives_alongside_its_own_note(tmp_path: Path) -> None:
    """PRESERVED DETECTION, paired with the pin above: a record whose BODY is
    genuinely broken still yields exactly one item even when its note quotes the
    same target. A builder that dropped both would silently shrink the campaign."""
    cfg = _vault(tmp_path, {
        "note/R.md": _rec(
            "note", "R", "See [[person/Ghost]] here.",
            note="LINK001 — broken wikilink [[person/Ghost]]",
        ),
    })
    build = _build(cfg)
    assert build.items == ["note/R.md::person/Ghost::remove"]


# ---------------------------------------------------------------------------
# Shape, dedupe, and the refusals
# ---------------------------------------------------------------------------


def test_one_item_per_record_and_target(tmp_path: Path) -> None:
    """``work()`` replaces EVERY occurrence in the file, so a target linked
    twice is ONE unit of work. Emitting it twice would make the second attempt a
    verified no-op and inflate the campaign's denominator."""
    cfg = _vault(tmp_path, {
        "note/N.md": _rec("note", "N", "[[person/Ghost]] and [[person/Ghost]]."),
    })
    build = _build(cfg)
    assert build.items == ["note/N.md::person/Ghost::remove"]


def test_a_target_containing_the_separator_is_refused_not_encoded(
    tmp_path: Path,
) -> None:
    """The separator is structural: an item the campaign's regex splits into the
    wrong fields could delete the wrong link. Refused, and NAMED — a reference
    the builder declines is work the campaign will never do."""
    cfg = _vault(tmp_path, {
        "note/N.md": _rec("note", "N", f"See [[person/Gh{ITEM_SEP}ost]]."),
    })
    build = _build(cfg)
    assert build.items == []
    assert len(build.skipped) == 1
    assert ITEM_SEP in build.skipped[0][2] or "separator" in build.skipped[0][2]


def test_every_built_item_round_trips_through_the_campaign(
    tmp_path: Path,
) -> None:
    """The builder's output is the campaign's input. Pinned by parsing every
    item with the campaign's OWN parser rather than a copy of its regex."""
    cfg = _vault(tmp_path, {
        "note/N.md": _rec("note", "N", "See [[person/Ghost]]."),
        "decision/D.md": _rec("decision", "D", "From [[note/Gone]]."),
    })
    build = _build(cfg)
    assert build.total == 2
    for item in build.items:
        path, target, branch = Link001Campaign.parse_item(item)
        assert path and target
        assert branch in {"annotate", "remove"}


def test_missing_vault_fails_loud(tmp_path: Path) -> None:
    cfg = _vault(tmp_path, {})
    cfg.vault.path = str(tmp_path / "does-not-exist")
    with pytest.raises(FileNotFoundError):
        _build(cfg)


def test_the_build_signal_is_always_emitted(tmp_path: Path) -> None:
    """ILB: a build that found nothing still says it ran, with its counts."""
    cfg = _vault(tmp_path, {"note/N.md": _rec("note", "N", "No links here.")})
    with structlog.testing.capture_logs() as captured:
        build = _build(cfg)
    assert build.total == 0
    events = [c for c in captured if c.get("event") == "drip.worklist.built"]
    assert len(events) == 1
    assert events[0]["total"] == 0
    assert events[0]["campaign"] == "link001_repair"


# ---------------------------------------------------------------------------
# The scanner-message contract — the builder's one fragile coupling
# ---------------------------------------------------------------------------


def test_the_target_regex_matches_the_REAL_scanner_message(
    tmp_path: Path,
) -> None:
    """``Issue`` carries the broken target only inside its human-readable
    message, so the builder parses it. That makes the message a CROSS-MODULE
    CONTRACT: a reworded scanner message yields a silently empty work-list.

    Pinned against a message the SCANNER actually produced, not a hand-written
    string — so this fails when the scanner changes rather than when someone
    edits a fixture to match.
    """
    from alfred.janitor.issues import IssueCode
    from alfred.janitor.scanner import run_structural_scan

    cfg = _vault(tmp_path, {
        "note/N.md": _rec("note", "N", "See [[person/Ghost]]."),
    })
    issues = run_structural_scan(
        cfg, JanitorState(cfg.state.path, cfg.state.max_sweep_history),
    )
    link001 = [i for i in issues if i.code is IssueCode.BROKEN_WIKILINK]
    assert link001, "fixture must produce a LINK001 for this pin to mean anything"

    match = LINK001_MESSAGE_RE.search(link001[0].message)
    assert match, (
        f"the scanner's LINK001 message no longer parses: "
        f"{link001[0].message!r} — the work-list builder would go silently empty"
    )
    assert match.group("target") == "person/Ghost"


# ---------------------------------------------------------------------------
# The rendered file
# ---------------------------------------------------------------------------


def test_rendered_file_loads_back_through_the_campaign_loader(
    tmp_path: Path,
) -> None:
    """Round-trip through the REAL consumer: the header must be ignorable and
    every item must survive ``load_worklist_file``."""
    from alfred.drip.wiring import load_worklist_file

    cfg = _vault(tmp_path, {
        "note/N.md": _rec("note", "N", "See [[person/Ghost]]."),
        "decision/D.md": _rec("decision", "D", "From [[note/Gone]]."),
    })
    build = _build(cfg)
    out = tmp_path / "wl.txt"
    out.write_text(render_worklist(build, source="test"), encoding="utf-8")

    assert load_worklist_file(out) == build.items


def test_rendered_header_records_the_freeze_and_the_counts(
    tmp_path: Path,
) -> None:
    """The file drives irreversible edits weeks after it is written, so "where
    did this come from" must be answerable from the artifact itself."""
    cfg = _vault(tmp_path, {
        "note/N.md": _rec("note", "N", "See [[person/Ghost]]."),
    })
    text = render_worklist(_build(cfg), source="alfred drip build-worklist link001")
    assert "FROZEN at build time" in text
    assert "alfred drip build-worklist link001" in text
    assert "1 remove" in text


# ---------------------------------------------------------------------------
# The CLI surface — dry-run default, and ERROR-never-empty
# ---------------------------------------------------------------------------


def test_dry_run_is_the_default_and_writes_nothing(
    tmp_path: Path, capsys,
) -> None:
    """Freezing a branch decision is a commitment and the removal branch is
    unrecoverable, so the preview is the default — the opposite of ``drip run``
    on purpose."""
    from alfred.drip import cli as dcli

    cfg = _vault(tmp_path, {
        "note/N.md": _rec("note", "N", "See [[person/Ghost]]."),
    })
    out = tmp_path / "wl.txt"
    code = dcli.cmd_build_worklist(cfg, out_path=str(out))

    assert code == 0
    assert not out.exists(), "the default must not write the frozen file"
    printed = capsys.readouterr().out
    assert "dry run" in printed
    assert "--apply" in printed, "the preview must say how to commit it"


def test_apply_writes_the_file(tmp_path: Path) -> None:
    from alfred.drip import cli as dcli

    cfg = _vault(tmp_path, {
        "note/N.md": _rec("note", "N", "See [[person/Ghost]]."),
    })
    out = tmp_path / "nested" / "wl.txt"
    assert dcli.cmd_build_worklist(cfg, out_path=str(out), apply=True) == 0
    assert out.exists()
    assert "note/N.md::person/Ghost::remove" in out.read_text(encoding="utf-8")


def test_zero_items_is_an_ERROR_and_writes_no_file(
    tmp_path: Path, capsys,
) -> None:
    """ERROR-never-empty. An empty work-list and an absent one are
    indistinguishable to the campaign — ``load_worklist_file`` already refuses a
    missing file for exactly that reason — so writing an empty one would
    manufacture the "backlog is drained" signal it must never receive by
    accident."""
    from alfred.drip import cli as dcli

    cfg = _vault(tmp_path, {"note/N.md": _rec("note", "N", "No links.")})
    out = tmp_path / "wl.txt"

    with structlog.testing.capture_logs() as captured:
        code = dcli.cmd_build_worklist(cfg, out_path=str(out), apply=True)

    assert code == 1, "a zero-item build is a nonzero exit, not a quiet success"
    assert not out.exists(), "no empty file may be written"
    assert "Refusing to write an empty work-list" in capsys.readouterr().out
    assert [c for c in captured if c.get("event") == "drip.worklist.empty"]
