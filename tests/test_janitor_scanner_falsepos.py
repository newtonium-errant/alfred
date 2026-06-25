"""Regression pins for four janitor scanner false-positive bugs
(2026-06-23). All unconditional — no ``pytest.importorskip`` / optional-dep
gating (per ``feedback_regression_pin_unconditional.md``): these guard
production-breaking behavior in the structural scanner + the agent
issue-report routing.

The four bugs (all verified against the live vault before the fix):

  A. Agent issue-report had no scanner-code filter — FM*/DIR001/
     ORPHAN001/STUB001/SEM001-004 all reached the agent prompt, flooding
     it with false-positive busywork the SKILL says it should never see.
     Fix: ``AGENT_ACTIONABLE_CODES`` allowlist filter at the daemon, plus
     a ``sweep.no_agent_actionable`` "intentionally left blank" signal +
     agent-loop skip when a sweep finds only deterministically-handled
     issues.

  B. DIR001 did not exempt ``quarantine/`` — every ``quarantine/spam/
     <YYYY-MM>/*.md`` (type note) was flagged "move to note/", a latently
     dangerous suggestion (un-quarantining spam). Fix: exempt the
     ``quarantine/`` subtree from the wrong-directory check.

  C. LINK001 truncated filenames containing a literal ``#`` — the
     unconditional ``split("#")`` turned ``decision/... as a ## Health
     Section.md`` into a phantom broken link. Fix: full-name-first /
     anchor-strip-fallback resolution.

  D. ``daily`` (date-keyed, nameless, created-less) was double-flagged
     FM001 (missing ``created`` + missing name). Fix: per-type required
     fields are authoritative for the type's own timestamp (``date``
     substitutes for ``created`` on ``daily``); name check skipped for
     nameless-by-design types. The ~22 normal types are unchanged.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from textwrap import dedent

import structlog

from alfred.janitor.backends import (
    AGENT_ACTIONABLE_CODES,
    BackendResult,
    build_issue_report,
)
from alfred.janitor.config import (
    JanitorConfig,
    StateConfig,
    SweepConfig,
    VaultConfig,
)
from alfred.janitor.issues import Issue, IssueCode, Severity
from alfred.janitor.scanner import run_structural_scan
from alfred.janitor.state import JanitorState


# ---------------------------------------------------------------------------
# Shared helpers (mirror test_janitor_scanner.py shape)
# ---------------------------------------------------------------------------


def _build_scan_config(
    vault: Path, state_dir: Path, *, ignore_dirs: list[str] | None = None,
) -> JanitorConfig:
    return JanitorConfig(
        vault=VaultConfig(
            path=str(vault),
            ignore_dirs=ignore_dirs if ignore_dirs is not None else [
                ".obsidian", "_templates", "_bases",
            ],
            ignore_files=[".gitkeep"],
        ),
        sweep=SweepConfig(),
        state=StateConfig(path=str(state_dir / "janitor_state.json")),
    )


def _write_record(vault: Path, rel: str, frontmatter: str, body: str = "") -> None:
    full = vault / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(f"---\n{frontmatter}\n---\n{body}\n", encoding="utf-8")


def _issues_for(issues: list[Issue], rel: str, code: IssueCode) -> list[Issue]:
    return [i for i in issues if i.file == rel and i.code == code]


# ---------------------------------------------------------------------------
# BUG A — agent issue-report scanner-code filter
# ---------------------------------------------------------------------------


class TestAgentActionableFilter:
    """Only LINK001/DUP001/SEM005/SEM006 reach the agent; the deterministic
    scanner codes are dropped before the report is built."""

    def test_allowlist_contents_pinned(self) -> None:
        # Contract pin (SKILL.md §3): the agent-actionable set is exactly
        # these four codes. Widening this set is a deliberate signal —
        # bumping this assertion must be paired with a SKILL update.
        assert AGENT_ACTIONABLE_CODES == frozenset({
            IssueCode.BROKEN_WIKILINK,    # LINK001
            IssueCode.DUPLICATE_NAME,     # DUP001
            IssueCode.VAGUE_NOTE,         # SEM005
            IssueCode.DUPLICATE_SEMANTIC, # SEM006
        })

    def test_filter_drops_scanner_code_keeps_actionable(self) -> None:
        # A mixed list: one scanner-handled FM001 + one agent-actionable
        # LINK001. The daemon's filter (same predicate as here) must keep
        # LINK001 and drop FM001.
        fm001 = Issue(
            code=IssueCode.MISSING_REQUIRED_FIELD,
            severity=Severity.CRITICAL,
            file="person/Foo.md",
            message="Missing required field: created",
        )
        link001 = Issue(
            code=IssueCode.BROKEN_WIKILINK,
            severity=Severity.CRITICAL,
            file="note/Bar.md",
            message="Broken wikilink: [[project/Nope]]",
        )
        all_issues = [fm001, link001]
        agent_issues = [i for i in all_issues if i.code in AGENT_ACTIONABLE_CODES]

        assert agent_issues == [link001]

        # And the dumb formatter renders exactly what it is handed —
        # the FM001 file must NOT appear in the agent report.
        report = build_issue_report(agent_issues)
        assert "note/Bar.md" in report
        assert "FM001" not in report
        assert "person/Foo.md" not in report

    def test_every_deterministic_code_excluded(self) -> None:
        # Belt-and-braces: every code NOT in the allowlist is a
        # scanner-handled code that must never reach the agent.
        deterministic = {
            IssueCode.MISSING_REQUIRED_FIELD,   # FM001
            IssueCode.INVALID_TYPE_VALUE,       # FM002
            IssueCode.INVALID_STATUS_VALUE,     # FM003
            IssueCode.INVALID_FIELD_TYPE,       # FM004
            IssueCode.WRONG_DIRECTORY,          # DIR001
            IssueCode.UNLINKED_BODY_ENTITY,     # LINK002
            IssueCode.ORPHANED_RECORD,          # ORPHAN001
            IssueCode.STUB_RECORD,              # STUB001
            IssueCode.STALE_ACTIVE_PROJECT,     # SEM001
            IssueCode.STALE_TODO_TASK,          # SEM002
            IssueCode.STALE_ACTIVE_CONVERSATION,# SEM003
            IssueCode.STALE_ACTIVE_PERSON,      # SEM004
        }
        assert deterministic.isdisjoint(AGENT_ACTIONABLE_CODES)


class _RecordingBackend:
    """Fake backend that records whether ``process`` was ever called."""

    def __init__(self) -> None:
        self.called = False

    async def process(self, *args, **kwargs) -> BackendResult:
        self.called = True
        return BackendResult(success=True, summary="should not be called")


class TestAllDeterministicBatchSignal:
    """A fix-mode sweep that finds ONLY scanner-handled issues must emit
    the ``sweep.no_agent_actionable`` signal AND skip the agent loop
    entirely (no empty/near-empty report sent)."""

    def test_no_agent_actionable_emitted_and_agent_not_called(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        from alfred.janitor import daemon as daemon_mod

        vault = tmp_path / "vault"
        vault.mkdir()
        # A single record whose ONLY issue is FM001 (missing created).
        # No broken links, no dup names — nothing agent-actionable.
        _write_record(
            vault, "person/No Created.md",
            dedent(
                """\
                type: person
                name: No Created
                status: active
                tags: []
                related:
                - '[[person/No Created]]'
                """
            ).rstrip(),
        )

        # Minimal skills dir so ``_load_skill`` returns non-empty and the
        # filter branch is reached.
        skills_dir = tmp_path / "skills"
        (skills_dir / "vault-janitor").mkdir(parents=True)
        (skills_dir / "vault-janitor" / "SKILL.md").write_text(
            "# vault janitor test skill\n", encoding="utf-8",
        )

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        config = _build_scan_config(vault, state_dir)
        state = JanitorState(config.state.path, config.state.max_sweep_history)

        recording = _RecordingBackend()
        monkeypatch.setattr(daemon_mod, "_create_backend", lambda cfg: recording)

        with structlog.testing.capture_logs() as captured:
            result = asyncio.run(
                daemon_mod.run_sweep(
                    config, state, skills_dir,
                    structural_only=False, fix_mode=True,
                )
            )

        # The sweep DID find issues (FM001 + ORPHAN001 from the
        # self-referencing record), but none are agent-actionable.
        assert result.issues_found > 0

        # Explicit "ran, nothing routed to agent" signal fired exactly
        # once, with the load-bearing fields.
        matches = [
            c for c in captured
            if c.get("event") == "sweep.no_agent_actionable"
        ]
        assert len(matches) == 1, (
            f"expected one sweep.no_agent_actionable, got {len(matches)}"
        )
        assert matches[0]["routed_to_agent"] == 0
        assert matches[0]["deterministic_handled"] == result.issues_found

        # The agent backend was NEVER invoked — no empty report sent.
        assert recording.called is False
        assert result.agent_invoked is False


# ---------------------------------------------------------------------------
# BUG B — DIR001 quarantine exemption
# ---------------------------------------------------------------------------


class TestQuarantineExemption:
    """Type-note records living under ``quarantine/spam/<YYYY-MM>/`` are a
    legitimate convention — DIR001 must NOT flag them as misfiled."""

    def test_quarantined_note_no_dir001(
        self, tmp_path: Path,
    ) -> None:
        vault = tmp_path / "vault"
        vault.mkdir()
        rel = "quarantine/spam/2026-05/Some Spam Email.md"
        _write_record(
            vault, rel,
            dedent(
                """\
                type: note
                name: Some Spam Email
                status: active
                created: '2026-05-15'
                tags: []
                related: []
                """
            ).rstrip(),
        )

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        config = _build_scan_config(vault, state_dir)
        state = JanitorState(config.state.path, config.state.max_sweep_history)
        issues = run_structural_scan(config, state)

        assert _issues_for(issues, rel, IssueCode.WRONG_DIRECTORY) == [], (
            "Quarantined note must not fire DIR001 — acting on it would "
            "un-quarantine spam back into note/."
        )

    def test_misfiled_task_still_fires_dir001(
        self, tmp_path: Path,
    ) -> None:
        # Control: the exemption must be quarantine-specific, NOT a
        # blanket "any wrong directory is fine". A task misfiled under
        # project/ still fires DIR001.
        vault = tmp_path / "vault"
        vault.mkdir()
        rel = "project/Misfiled Task.md"
        _write_record(
            vault, rel,
            dedent(
                """\
                type: task
                name: Misfiled Task
                status: todo
                created: '2026-05-15'
                tags: []
                related: []
                """
            ).rstrip(),
        )

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        config = _build_scan_config(vault, state_dir)
        state = JanitorState(config.state.path, config.state.max_sweep_history)
        issues = run_structural_scan(config, state)

        assert len(_issues_for(issues, rel, IssueCode.WRONG_DIRECTORY)) == 1, (
            "A task misfiled under project/ must still fire DIR001."
        )


# ---------------------------------------------------------------------------
# BUG C — LINK001 literal-# filename resolution
# ---------------------------------------------------------------------------


class TestLiteralHashFilenameResolution:
    """A wikilink whose target filename contains a literal ``#`` resolves
    to the real file (no LINK001); a genuine ``[[file#anchor]]`` still
    strips to the base file via the fallback."""

    def test_literal_hash_filename_resolves(
        self, tmp_path: Path,
    ) -> None:
        vault = tmp_path / "vault"
        vault.mkdir()
        # The exact live-vault shape: a decision record whose NAME
        # contains '## Health Section'.
        target_name = (
            "Morning Brief Re-Renders Latest BIT Record as a "
            "## Health Section"
        )
        _write_record(
            vault, f"decision/{target_name}.md",
            dedent(
                f"""\
                type: decision
                name: {target_name}
                status: final
                created: '2026-04-25'
                tags: []
                """
            ).rstrip(),
        )
        # Source links to it by full name (including the '##').
        _write_record(
            vault, "decision/Linker.md",
            dedent(
                f"""\
                type: decision
                name: Linker
                status: final
                created: '2026-04-25'
                tags: []
                related:
                - '[[decision/{target_name}]]'
                """
            ).rstrip(),
        )

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        config = _build_scan_config(vault, state_dir)
        state = JanitorState(config.state.path, config.state.max_sweep_history)
        issues = run_structural_scan(config, state)

        assert _issues_for(
            issues, "decision/Linker.md", IssueCode.BROKEN_WIKILINK,
        ) == [], (
            "Wikilink to a filename containing a literal '#' must resolve "
            "by its full name, not be truncated at the first '#'."
        )

    def test_real_anchor_still_strips_to_base_file(
        self, tmp_path: Path,
    ) -> None:
        # A genuine [[file#anchor]] where file.md exists but the anchored
        # name does not — the fallback strip must still resolve it.
        vault = tmp_path / "vault"
        vault.mkdir()
        _write_record(
            vault, "project/Eagle Farm.md",
            dedent(
                """\
                type: project
                name: Eagle Farm
                status: active
                created: '2026-04-25'
                tags: []
                related: []
                """
            ).rstrip(),
            body="# Eagle Farm\n\n## Section\n\nNotes.",
        )
        _write_record(
            vault, "note/Source.md",
            dedent(
                """\
                type: note
                name: Source
                status: active
                created: '2026-04-25'
                tags: []
                """
            ).rstrip(),
            body="See [[project/Eagle Farm#Section]] for details.",
        )

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        config = _build_scan_config(vault, state_dir)
        state = JanitorState(config.state.path, config.state.max_sweep_history)
        issues = run_structural_scan(config, state)

        assert _issues_for(
            issues, "note/Source.md", IssueCode.BROKEN_WIKILINK,
        ) == [], (
            "A real [[file#anchor]] must still resolve to the base file "
            "via the anchor-strip fallback."
        )

    def test_genuinely_broken_hash_link_still_fires(
        self, tmp_path: Path,
    ) -> None:
        # The fix must not be so permissive that a real broken link with
        # a '#' in it goes silent — neither the full name nor the
        # anchor-stripped name exists.
        vault = tmp_path / "vault"
        vault.mkdir()
        _write_record(
            vault, "decision/Source.md",
            dedent(
                """\
                type: decision
                name: Source
                status: final
                created: '2026-04-25'
                tags: []
                related:
                - '[[decision/Nonexistent ## Thing]]'
                """
            ).rstrip(),
        )

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        config = _build_scan_config(vault, state_dir)
        state = JanitorState(config.state.path, config.state.max_sweep_history)
        issues = run_structural_scan(config, state)

        assert len(_issues_for(
            issues, "decision/Source.md", IssueCode.BROKEN_WIKILINK,
        )) == 1, (
            "A wikilink with a '#' whose target genuinely does not exist "
            "(full OR anchor-stripped) must still fire LINK001."
        )


# ---------------------------------------------------------------------------
# BUG D — daily required-field conflict
# ---------------------------------------------------------------------------


class TestDailyRequiredFields:
    """``daily`` records are date-keyed + nameless: ``date`` substitutes
    for ``created`` and no name is required. Normal types are unchanged."""

    def test_daily_with_date_no_created_no_name_no_fm001(
        self, tmp_path: Path,
    ) -> None:
        vault = tmp_path / "vault"
        vault.mkdir()
        rel = "daily/2026-06-15.md"
        _write_record(
            vault, rel,
            dedent(
                """\
                type: daily
                date: '2026-06-15'
                tags: []
                """
            ).rstrip(),
        )

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        config = _build_scan_config(vault, state_dir)
        state = JanitorState(config.state.path, config.state.max_sweep_history)
        issues = run_structural_scan(config, state)

        fm001 = _issues_for(issues, rel, IssueCode.MISSING_REQUIRED_FIELD)
        assert fm001 == [], (
            f"daily with date (no created, no name) must not fire FM001, "
            f"got: {[i.message for i in fm001]}"
        )

    def test_daily_missing_date_still_fires_fm001(
        self, tmp_path: Path,
    ) -> None:
        # The per-type requirement IS enforced: a daily missing its own
        # required ``date`` field still fires FM001.
        vault = tmp_path / "vault"
        vault.mkdir()
        rel = "daily/2026-06-16.md"
        _write_record(
            vault, rel,
            dedent(
                """\
                type: daily
                tags: []
                """
            ).rstrip(),
        )

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        config = _build_scan_config(vault, state_dir)
        state = JanitorState(config.state.path, config.state.max_sweep_history)
        issues = run_structural_scan(config, state)

        fm001 = _issues_for(issues, rel, IssueCode.MISSING_REQUIRED_FIELD)
        msgs = [i.message for i in fm001]
        assert any("date" in m for m in msgs), (
            f"daily missing its required 'date' must fire FM001, got: {msgs}"
        )

    def test_person_missing_created_still_fires_fm001(
        self, tmp_path: Path,
    ) -> None:
        # Regression guard: normal types (no per-type entry) still require
        # 'created'. The blast radius of the daily fix must not touch them.
        vault = tmp_path / "vault"
        vault.mkdir()
        rel = "person/No Created.md"
        _write_record(
            vault, rel,
            dedent(
                """\
                type: person
                name: No Created
                status: active
                tags: []
                related: []
                """
            ).rstrip(),
        )

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        config = _build_scan_config(vault, state_dir)
        state = JanitorState(config.state.path, config.state.max_sweep_history)
        issues = run_structural_scan(config, state)

        fm001 = _issues_for(issues, rel, IssueCode.MISSING_REQUIRED_FIELD)
        assert any("created" in i.message for i in fm001), (
            "person missing 'created' must STILL fire FM001 (no regression)."
        )

    def test_person_missing_name_still_fires_fm001(
        self, tmp_path: Path,
    ) -> None:
        # The nameless-by-design exemption must NOT leak to normal types.
        vault = tmp_path / "vault"
        vault.mkdir()
        rel = "person/Nameless.md"
        _write_record(
            vault, rel,
            dedent(
                """\
                type: person
                status: active
                created: '2026-06-15'
                tags: []
                related: []
                """
            ).rstrip(),
        )

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        config = _build_scan_config(vault, state_dir)
        state = JanitorState(config.state.path, config.state.max_sweep_history)
        issues = run_structural_scan(config, state)

        fm001 = _issues_for(issues, rel, IssueCode.MISSING_REQUIRED_FIELD)
        assert any("title field" in i.message for i in fm001), (
            "person with no name must STILL fire the missing-title FM001."
        )

    def test_routine_still_requires_created(
        self, tmp_path: Path,
    ) -> None:
        # Outcome (2) from the brief: routine declares per-type extras
        # (name/cadence/items) but genuinely carries 'created' on disk —
        # the daily fix must NOT let routine drop the 'created'
        # requirement. A routine missing 'created' still fires FM001.
        vault = tmp_path / "vault"
        vault.mkdir()
        rel = "routine/Core Daily.md"
        _write_record(
            vault, rel,
            dedent(
                """\
                type: routine
                name: Core Daily
                cadence: {type: daily}
                items:
                - text: Walk dog
                tags: []
                """
            ).rstrip(),
        )

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        config = _build_scan_config(vault, state_dir)
        state = JanitorState(config.state.path, config.state.max_sweep_history)
        issues = run_structural_scan(config, state)

        fm001 = _issues_for(issues, rel, IssueCode.MISSING_REQUIRED_FIELD)
        assert any("created" in i.message for i in fm001), (
            "routine missing 'created' must STILL fire FM001 — the daily "
            "exemption is date-keyed-type-specific, not all-per-type."
        )
