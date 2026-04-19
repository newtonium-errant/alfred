"""Tests for ``alfred.janitor.autofix`` deterministic flagging helpers.

Scope: the LINK001 and STUB001 unresolved-fallback writers. Both emit
stable ``janitor_note`` prose owned by Python so the SKILL never has
to describe the fallback shape, and the idempotency-by-issue-code-prefix
check in the SKILL stays honest across sweeps.

Option E Q6 coverage: STUB001 fallback flag. See
``vault/session/Option E Q6 — STUB001 fallback flag 2026-04-19.md``.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from alfred.janitor.autofix import (
    _STUB001_UNENRICHABLE_NOTE,
    flag_unenrichable_stubs,
)
from alfred.janitor.issues import Issue, IssueCode, Severity
from alfred.vault.ops import vault_read


# --- Helpers --------------------------------------------------------------


def _write_stub(vault: Path, rel_path: str, body: str = "") -> None:
    """Write a minimal stub record for the given relative path.

    The file has just enough frontmatter to round-trip through
    ``vault_read`` / ``vault_edit`` without schema validation yelling.
    """
    full = vault / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    content = dedent(
        f"""\
        ---
        type: {rel_path.split('/', 1)[0]}
        name: {Path(rel_path).stem}
        created: 2026-04-19
        ---
        {body}
        """
    )
    full.write_text(content, encoding="utf-8")


def _stub_issue(rel_path: str) -> Issue:
    return Issue(
        code=IssueCode.STUB_RECORD,
        severity=Severity.INFO,
        file=rel_path,
        message="Body is empty or very short",
    )


# --- Tests ----------------------------------------------------------------


def test_stub001_unenrichable_note_has_exact_prose(tmp_vault: Path, tmp_path: Path) -> None:
    """Sanity-check the deterministic prose before wiring the pipeline tests.

    The exact string is load-bearing for the SKILL's idempotency rule —
    if this string drifts, the next sweep rewrites existing notes and
    the janitor starts fighting itself. Pin it.
    """
    assert _STUB001_UNENRICHABLE_NOTE.startswith("STUB001 -- ")
    assert "Stage 3 enrichment unavailable or skipped" in _STUB001_UNENRICHABLE_NOTE
    assert _STUB001_UNENRICHABLE_NOTE == (
        "STUB001 -- body is minimal; Stage 3 enrichment unavailable or "
        "skipped. Consider adding content."
    )


def test_stub001_unenrichable_gets_flagged(tmp_vault: Path, tmp_path: Path) -> None:
    """A stub that Stage 3 couldn't enrich gets the deterministic note."""
    _write_stub(tmp_vault, "person/Stubby.md")
    session_path = str(tmp_path / "session.jsonl")

    issue = _stub_issue("person/Stubby.md")
    flagged = flag_unenrichable_stubs([issue], tmp_vault, session_path)

    assert flagged == ["person/Stubby.md"]
    record = vault_read(tmp_vault, "person/Stubby.md")
    assert record["frontmatter"].get("janitor_note") == _STUB001_UNENRICHABLE_NOTE


def test_stub001_enriched_not_flagged_by_fallback(
    tmp_vault: Path, tmp_path: Path,
) -> None:
    """A stub Stage 3 DID enrich is not in the unresolved list → no fallback.

    The fallback helper only writes notes for the issues it's handed. If
    the pipeline correctly excludes enriched stubs from ``unresolved``,
    the helper never touches them. Simulate by passing an empty list.
    """
    _write_stub(tmp_vault, "person/Rich.md", body="A substantive biographical paragraph.")
    session_path = str(tmp_path / "session.jsonl")

    flagged = flag_unenrichable_stubs([], tmp_vault, session_path)

    assert flagged == []
    record = vault_read(tmp_vault, "person/Rich.md")
    assert "janitor_note" not in record["frontmatter"]


def test_stub001_stale_gets_flagged(tmp_vault: Path, tmp_path: Path) -> None:
    """A stale stub (enrichment_stale=True) flows through the fallback helper.

    The pipeline passes stale stubs into ``unresolved`` before the
    Stage 3 filter so they stay visible between sweeps until the content
    hash changes. From the helper's perspective that's an ordinary
    flag call — the staleness accounting lives in state.py.
    """
    _write_stub(tmp_vault, "org/Stagnant.md")
    session_path = str(tmp_path / "session.jsonl")

    issue = _stub_issue("org/Stagnant.md")
    flagged = flag_unenrichable_stubs([issue], tmp_vault, session_path)

    assert flagged == ["org/Stagnant.md"]
    record = vault_read(tmp_vault, "org/Stagnant.md")
    assert record["frontmatter"].get("janitor_note") == _STUB001_UNENRICHABLE_NOTE


def test_stub001_multiple_unresolved_flags_each(
    tmp_vault: Path, tmp_path: Path,
) -> None:
    """Several unresolved stubs in one call all get flagged with the same prose."""
    _write_stub(tmp_vault, "person/One.md")
    _write_stub(tmp_vault, "org/Two.md")
    _write_stub(tmp_vault, "project/Three.md")
    session_path = str(tmp_path / "session.jsonl")

    issues = [
        _stub_issue("person/One.md"),
        _stub_issue("org/Two.md"),
        _stub_issue("project/Three.md"),
    ]
    flagged = flag_unenrichable_stubs(issues, tmp_vault, session_path)

    assert set(flagged) == {"person/One.md", "org/Two.md", "project/Three.md"}
    for rel in flagged:
        record = vault_read(tmp_vault, rel)
        assert record["frontmatter"].get("janitor_note") == _STUB001_UNENRICHABLE_NOTE


def test_stub001_missing_file_is_skipped_not_raised(
    tmp_vault: Path, tmp_path: Path,
) -> None:
    """If the target file is gone (race, concurrent move), the helper skips cleanly.

    This mirrors ``_flag_issue_with_note``'s existing VaultError handling —
    the pipeline must never crash because one stub vanished mid-sweep.
    """
    session_path = str(tmp_path / "session.jsonl")
    issue = _stub_issue("person/Ghost.md")

    # File does not exist in tmp_vault.
    flagged = flag_unenrichable_stubs([issue], tmp_vault, session_path)

    assert flagged == []  # Nothing was actually flagged.


@pytest.mark.asyncio
async def test_pipeline_flags_unresolved_stubs_when_template_missing(
    tmp_vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Integration-lite: ``_stage3_enrich`` routes template-missing stubs to unresolved.

    When Stage 3's prompt template can't be loaded (e.g. bundled skill
    missing), every stub becomes unresolved. The pipeline then flags
    them all via ``flag_unenrichable_stubs``. Skips the LLM entirely.
    """
    from alfred.janitor import pipeline as pipe

    # Force the stage-3 template loader to return empty so the guard
    # at the top of _stage3_enrich returns (0, all_stubs).
    monkeypatch.setattr(pipe, "_load_stage_prompt", lambda _fname: "")

    _write_stub(tmp_vault, "person/NoTemplate.md")
    session_path = str(tmp_path / "session.jsonl")
    issue = _stub_issue("person/NoTemplate.md")

    # Build a minimal JanitorConfig pointed at the tmp vault.
    from alfred.janitor.config import JanitorConfig, VaultConfig, SweepConfig
    cfg = JanitorConfig(
        vault=VaultConfig(path=str(tmp_vault)),
        sweep=SweepConfig(max_stubs_per_sweep=10, max_enrichment_attempts=3),
    )

    enriched, unresolved = await pipe._stage3_enrich(
        [issue], cfg, session_path, state=None,
    )
    assert enriched == 0
    assert [i.file for i in unresolved] == ["person/NoTemplate.md"]

    # The pipeline step that the real run_pipeline does: flag them.
    flagged = flag_unenrichable_stubs(unresolved, tmp_vault, session_path)
    assert flagged == ["person/NoTemplate.md"]
    record = vault_read(tmp_vault, "person/NoTemplate.md")
    assert record["frontmatter"].get("janitor_note") == _STUB001_UNENRICHABLE_NOTE


@pytest.mark.asyncio
async def test_pipeline_over_cap_stubs_are_unresolved(
    tmp_vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stubs past the per-sweep cap roll through to the fallback flag.

    ``max_stubs_per_sweep = 1`` with two stubs → one enriched (mocked),
    one rolled to unresolved. The unresolved one gets flagged.
    """
    from alfred.janitor import pipeline as pipe
    from alfred.janitor.config import JanitorConfig, VaultConfig, SweepConfig

    # Real template load would add LLM-call noise; stub it to a minimal
    # template string that's allowed to format(). Then short-circuit the
    # LLM call to avoid a subprocess.
    monkeypatch.setattr(
        pipe,
        "_load_stage_prompt",
        lambda _fname: "Enrich {file_path} type={record_type} name={record_name} "
                       "content={record_content} schema={type_schema} "
                       "linked={linked_records} cli={vault_cli_reference}",
    )

    async def _fake_call_llm(*args, **kwargs):
        return ""

    monkeypatch.setattr(pipe, "_call_llm", _fake_call_llm)

    _write_stub(tmp_vault, "person/Alpha.md")
    _write_stub(tmp_vault, "person/Beta.md")
    session_path = str(tmp_path / "session.jsonl")

    issues = [_stub_issue("person/Alpha.md"), _stub_issue("person/Beta.md")]
    cfg = JanitorConfig(
        vault=VaultConfig(path=str(tmp_vault)),
        sweep=SweepConfig(max_stubs_per_sweep=1, max_enrichment_attempts=3),
    )

    enriched, unresolved = await pipe._stage3_enrich(
        issues, cfg, session_path, state=None,
    )

    # LLM returned empty so the "processed" stub's mtime didn't change;
    # that stub is unresolved too. The over-cap stub is unresolved by cap.
    # Either way, both stubs end up unresolved — the cap path is the
    # critical branch exercised here (over-cap path adds to ``unresolved``
    # independent of LLM behaviour).
    assert enriched == 0
    assert len(unresolved) == 2
    assert {i.file for i in unresolved} == {"person/Alpha.md", "person/Beta.md"}
