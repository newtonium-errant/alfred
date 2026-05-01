"""Regression tests for the ``dont_scan_dirs`` / ``dont_index_dirs`` split.

Pre-2026-05-01 the vault config had a single ``ignore_dirs`` field that
served two semantically distinct purposes:

1. Outbound scan exclusion — directories whose records should NOT be
   validated for issues by janitor's structural scan.
2. Valid-link-target index exclusion — directories whose records
   should NOT contribute to the wikilink-resolution stem index.

Conflating the two meant that putting ``session/`` in ``ignore_dirs``
(legitimate scan exclusion: voice transcripts aren't records to
validate) silently made every wikilink TO a session record report
LINK001, even when the session file existed on disk and Obsidian
resolved the wikilink correctly.

These tests pin the split:

- ``dont_scan_dirs`` — outbound scan exclusion (legacy ``ignore_dirs``).
- ``dont_index_dirs`` — index exclusion. Default empty, meaning every
  on-disk record is a valid wikilink target unless the operator
  explicitly opts out.

Plus the back-compat path: a config with only the legacy ``ignore_dirs``
key still loads, logs a one-time deprecation warning, and behaves as
``dont_scan_dirs`` with empty ``dont_index_dirs`` (i.e. the bug fix
takes effect even on un-migrated configs).
"""

from __future__ import annotations

import logging
from pathlib import Path
from textwrap import dedent

import pytest

from alfred.janitor.config import (
    JanitorConfig,
    StateConfig,
    SweepConfig,
    VaultConfig,
    load_from_unified as load_janitor_unified,
)
from alfred.janitor.issues import IssueCode
from alfred.janitor.scanner import run_structural_scan
from alfred.janitor.state import JanitorState
from alfred.vault.config_helpers import normalize_vault_block, reset_deprecation_log


# --- normalize_vault_block ---------------------------------------------------


class TestNormalizeVaultBlock:
    def setup_method(self) -> None:
        reset_deprecation_log()

    def test_new_keys_pass_through_and_mirror_to_ignore_dirs(self) -> None:
        # New-shape config: dont_scan_dirs is the source of truth, but
        # ignore_dirs is mirrored from it for back-compat with every
        # existing call site that still reads config.vault.ignore_dirs.
        out = normalize_vault_block({
            "dont_scan_dirs": ["session", "view"],
            "dont_index_dirs": ["archive"],
        })
        assert out["dont_scan_dirs"] == ["session", "view"]
        assert out["dont_index_dirs"] == ["archive"]
        assert out["ignore_dirs"] == ["session", "view"]

    def test_legacy_key_alone_logs_deprecation_once(self, caplog: pytest.LogCaptureFixture) -> None:
        # A legacy-only config (only ignore_dirs set) still loads and
        # works, but logs a one-time deprecation hint per process.
        # Multiple normalize calls on the same dict must not re-fire.
        with caplog.at_level(logging.WARNING, logger="alfred.vault.config"):
            raw = {"ignore_dirs": ["session"]}
            normalize_vault_block(raw)
            normalize_vault_block(raw)
            normalize_vault_block(raw)

        deprecations = [r for r in caplog.records if "ignore_dirs_deprecated" in r.getMessage()]
        assert len(deprecations) == 1, (
            f"expected exactly 1 deprecation warning, got {len(deprecations)}"
        )

    def test_legacy_key_logs_deprecation_once_across_tool_loaders(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Real-world dedup contract: loading the same legacy unified
        # config dict through ≥3 tools' ``load_from_unified`` in one
        # process must produce exactly ONE deprecation warning.
        #
        # Pre-fix this fired N times: each tool calls ``_substitute_env``
        # which deep-copies the dict tree, so ``raw["vault"]`` had a
        # different ``id()`` per tool. The id-keyed set dedup mis-fired.
        # Module-level bool fixes it.
        from alfred.curator.config import load_from_unified as load_curator
        from alfred.distiller.config import load_from_unified as load_distiller
        from alfred.janitor.config import load_from_unified as load_janitor

        raw = {
            "vault": {
                "path": str(tmp_path / "vault"),
                "ignore_dirs": ["_templates", "session", "view"],
            },
            "logging": {"dir": str(tmp_path / "data")},
            "curator": {},
            "janitor": {"state": {"path": str(tmp_path / "janitor_state.json")}},
            "distiller": {"state": {"path": str(tmp_path / "distiller_state.json")}},
        }

        with caplog.at_level(logging.WARNING, logger="alfred.vault.config"):
            load_curator(raw)
            load_janitor(raw)
            load_distiller(raw)

        deprecations = [r for r in caplog.records if "ignore_dirs_deprecated" in r.getMessage()]
        assert len(deprecations) == 1, (
            f"expected exactly 1 deprecation warning across 3 tool loads, "
            f"got {len(deprecations)}"
        )

    def test_legacy_key_alone_yields_empty_dont_index(self) -> None:
        # The bug fix: a legacy-only config gets dont_index_dirs=[],
        # NOT a copy of ignore_dirs. Configs that listed `session` in
        # ignore_dirs (legitimate scan exclusion) now have session/ as
        # a valid wikilink target — fixing the false-positive LINK001s.
        out = normalize_vault_block({"ignore_dirs": ["session", "view"]})
        assert out["dont_index_dirs"] == []
        assert out["ignore_dirs"] == ["session", "view"]

    def test_new_key_wins_when_both_present(self) -> None:
        # If a config has both legacy ignore_dirs AND new dont_scan_dirs,
        # the new key wins. ignore_dirs gets overwritten to match.
        out = normalize_vault_block({
            "ignore_dirs": ["legacy_only"],
            "dont_scan_dirs": ["new_winner"],
        })
        assert out["ignore_dirs"] == ["new_winner"]
        assert out["dont_scan_dirs"] == ["new_winner"]

    def test_empty_dict_yields_default_dont_index(self) -> None:
        # Bare config (neither key) leaves ignore_dirs absent (so the
        # dataclass default applies) and sets dont_index_dirs to [].
        out = normalize_vault_block({})
        assert out == {"dont_index_dirs": []}

    def test_non_dict_returns_empty(self) -> None:
        # Defensive: YAML may produce None or a non-dict at the vault
        # key. The helper returns empty so the dataclass falls back to
        # its defaults rather than crashing.
        assert normalize_vault_block(None) == {}  # type: ignore[arg-type]


# --- end-to-end: link to session/ no longer reports LINK001 -----------------


def _write_record(vault: Path, rel: str, frontmatter: str, body: str = "") -> None:
    """Write a markdown record with the given raw frontmatter text."""
    full = vault / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(f"---\n{frontmatter}\n---\n{body}\n", encoding="utf-8")


class TestLinkToSessionNoLongerLink001:
    """Lock in the headline bug fix.

    A record under ``session/`` is excluded from outbound scanning
    (``dont_scan_dirs`` includes ``session``) but is a valid wikilink
    target. Pre-fix, a wikilink TO that session record reported LINK001
    because the stem index excluded ``session/``. Post-fix, with
    ``dont_index_dirs: []``, the session record IS in the index and the
    link resolves cleanly.
    """

    def test_link_to_session_resolves_when_session_in_dont_scan_only(
        self, tmp_vault: Path, tmp_path: Path
    ) -> None:
        # Seed a session record (the link target) and a note that links
        # to it. ``session/`` is in dont_scan_dirs (the realistic Salem
        # config) but NOT in dont_index_dirs.
        _write_record(
            tmp_vault,
            "session/Voice Capture 2026-04-30.md",
            dedent(
                """\
                type: session
                name: Voice Capture 2026-04-30
                created: 2026-04-30
                """
            ),
        )
        _write_record(
            tmp_vault,
            "note/Talking About Capture.md",
            dedent(
                """\
                type: note
                name: Talking About Capture
                created: 2026-05-01
                related:
                - "[[session/Voice Capture 2026-04-30]]"
                """
            ),
            body="See [[session/Voice Capture 2026-04-30]] for context.",
        )

        config = JanitorConfig(
            vault=VaultConfig(
                path=str(tmp_vault),
                ignore_dirs=[".obsidian", "session"],
                # Critical: empty dont_index_dirs — session/ is excluded
                # from outbound scans but IS indexed as a valid target.
                dont_index_dirs=[],
                ignore_files=[".gitkeep"],
            ),
            sweep=SweepConfig(),
            state=StateConfig(path=str(tmp_path / "janitor_state.json")),
        )
        state = JanitorState(state_path=str(tmp_path / "janitor_state.json"))

        issues = run_structural_scan(config, state)

        # No LINK001 should fire on the wikilink to the session record.
        link001_against_session = [
            i for i in issues
            if i.code == IssueCode.BROKEN_WIKILINK
            and "Voice Capture 2026-04-30" in i.message
        ]
        assert link001_against_session == [], (
            f"expected no LINK001 on session/ wikilink, got: "
            f"{[i.message for i in link001_against_session]}"
        )

    def test_link_to_session_DOES_report_link001_when_explicitly_dont_indexed(
        self, tmp_vault: Path, tmp_path: Path
    ) -> None:
        # Symmetric assertion: if the operator DOES want session/
        # excluded from the link-target index (e.g. they treat session
        # records as transient and don't want references rotting),
        # adding it to dont_index_dirs MUST flag wikilinks to them.
        # Otherwise dont_index_dirs is a dead config knob.
        _write_record(
            tmp_vault,
            "session/Voice Capture 2026-04-30.md",
            dedent(
                """\
                type: session
                name: Voice Capture 2026-04-30
                created: 2026-04-30
                """
            ),
        )
        _write_record(
            tmp_vault,
            "note/Talking About Capture.md",
            dedent(
                """\
                type: note
                name: Talking About Capture
                created: 2026-05-01
                related:
                - "[[session/Voice Capture 2026-04-30]]"
                """
            ),
        )

        config = JanitorConfig(
            vault=VaultConfig(
                path=str(tmp_vault),
                ignore_dirs=[".obsidian", "session"],
                # Operator opts in to session/ index exclusion.
                dont_index_dirs=["session"],
                ignore_files=[".gitkeep"],
            ),
            sweep=SweepConfig(),
            state=StateConfig(path=str(tmp_path / "janitor_state.json")),
        )
        state = JanitorState(state_path=str(tmp_path / "janitor_state.json"))

        issues = run_structural_scan(config, state)

        link001_against_session = [
            i for i in issues
            if i.code == IssueCode.BROKEN_WIKILINK
            and "Voice Capture 2026-04-30" in i.message
        ]
        assert link001_against_session, (
            "expected LINK001 when session/ is explicitly in dont_index_dirs"
        )


# --- back-compat: legacy-only config still works ----------------------------


class TestLegacyConfigBackCompat:
    def setup_method(self) -> None:
        reset_deprecation_log()

    def test_legacy_ignore_dirs_only_loads_via_load_from_unified(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # A unified config with only the legacy key still loads,
        # populates ignore_dirs, leaves dont_index_dirs empty, and
        # fires the deprecation warning exactly once.
        raw = {
            "vault": {
                "path": "/tmp/some-vault",
                "ignore_dirs": ["_templates", "session", "view"],
            },
            "logging": {"dir": "/tmp/data"},
            "janitor": {},
        }

        with caplog.at_level(logging.WARNING, logger="alfred.vault.config"):
            cfg = load_janitor_unified(raw)

        assert cfg.vault.ignore_dirs == ["_templates", "session", "view"]
        assert cfg.vault.dont_index_dirs == []
        deprecations = [r for r in caplog.records if "ignore_dirs_deprecated" in r.getMessage()]
        assert len(deprecations) == 1

    def test_legacy_only_config_fixes_the_bug(self, tmp_vault: Path, tmp_path: Path) -> None:
        # End-to-end: a config with only the legacy key, listing
        # ``session`` in ignore_dirs, MUST NOT report LINK001 on
        # wikilinks to session records. This is the un-migrated-config
        # path — operators who don't update their YAML still get the fix.
        _write_record(
            tmp_vault,
            "session/Old Capture.md",
            dedent(
                """\
                type: session
                name: Old Capture
                created: 2026-04-15
                """
            ),
        )
        _write_record(
            tmp_vault,
            "note/Linking Note.md",
            dedent(
                """\
                type: note
                name: Linking Note
                created: 2026-05-01
                related:
                - "[[session/Old Capture]]"
                """
            ),
        )

        raw = {
            "vault": {
                "path": str(tmp_vault),
                # Only the legacy key — no migration. Should still fix the bug.
                "ignore_dirs": [".obsidian", "session"],
                "ignore_files": [".gitkeep"],
            },
            "logging": {"dir": str(tmp_path)},
            "janitor": {
                "state": {"path": str(tmp_path / "janitor_state.json")},
            },
        }

        cfg = load_janitor_unified(raw)
        state = JanitorState(state_path=str(tmp_path / "janitor_state.json"))
        issues = run_structural_scan(cfg, state)

        link001 = [
            i for i in issues
            if i.code == IssueCode.BROKEN_WIKILINK
            and "Old Capture" in i.message
        ]
        assert link001 == [], (
            f"legacy-only config should still see session/ as a valid "
            f"link target. Got: {[i.message for i in link001]}"
        )


# --- per-tool configs all gain the new fields -------------------------------


class TestAllToolConfigsHaveSplitFields:
    """Every per-tool VaultConfig must expose ``dont_scan_dirs`` (back-compat
    shim) and ``dont_index_dirs`` so a unified config flows through every
    tool's loader without losing the new keys."""

    def test_janitor_vault_config_has_split_fields(self) -> None:
        from alfred.janitor.config import VaultConfig as JanitorVault
        cfg = JanitorVault()
        assert hasattr(cfg, "dont_scan_dirs")
        assert hasattr(cfg, "dont_index_dirs")
        assert cfg.dont_index_dirs == []

    def test_curator_vault_config_has_split_fields(self) -> None:
        from alfred.curator.config import VaultConfig as CuratorVault
        cfg = CuratorVault()
        assert hasattr(cfg, "dont_scan_dirs")
        assert hasattr(cfg, "dont_index_dirs")
        assert cfg.dont_index_dirs == []

    def test_distiller_vault_config_has_split_fields(self) -> None:
        from alfred.distiller.config import VaultConfig as DistillerVault
        cfg = DistillerVault()
        assert hasattr(cfg, "dont_scan_dirs")
        assert hasattr(cfg, "dont_index_dirs")
        assert cfg.dont_index_dirs == []

    def test_surveyor_vault_config_has_split_fields(self) -> None:
        from alfred.surveyor.config import VaultConfig as SurveyorVault
        cfg = SurveyorVault(path=Path("/tmp/x"))
        assert hasattr(cfg, "dont_scan_dirs")
        assert hasattr(cfg, "dont_index_dirs")
        assert cfg.dont_index_dirs == []

    def test_telegram_vault_config_has_split_fields(self) -> None:
        from alfred.telegram.config import VaultConfig as TelegramVault
        cfg = TelegramVault()
        assert hasattr(cfg, "dont_scan_dirs")
        assert hasattr(cfg, "dont_index_dirs")
        assert cfg.dont_index_dirs == []

    def test_instructor_vault_config_has_split_fields(self) -> None:
        from alfred.instructor.config import VaultConfig as InstructorVault
        cfg = InstructorVault()
        assert hasattr(cfg, "dont_scan_dirs")
        assert hasattr(cfg, "dont_index_dirs")
        assert cfg.dont_index_dirs == []


# --- Stage 3 stub enrichment context bypasses inbox/processed --------------


class TestStageLookupNeverIndex:
    """Stage 2 candidate search and Stage 3 inbound-context lookup must
    skip ``inbox/processed/`` (raw email bodies — the curator's audit
    trail), ``_templates/`` (placeholder records), and other vault-system
    infrastructure dirs even when the operator's ``dont_index_dirs`` is
    empty (the new default).

    Pre-fix: switching pipeline.py from ``ignore_dirs`` to
    ``dont_index_dirs`` made the default-empty config let the
    Obsidian-CLI grep walk the whole vault, polluting Stage 3's
    ``linked records`` enrichment context with non-record content.
    """

    def test_stage3_inbound_context_skips_inbox_processed(
        self, tmp_vault: Path
    ) -> None:
        # A stub elsewhere in the vault has a stem name that also
        # appears verbatim in a raw consumed email under
        # ``inbox/processed/``. Stage 3's inbound-link grep MUST NOT
        # return the inbox/processed file as a "linked record."
        from alfred.janitor.pipeline import _collect_linked_records

        # The stub record we'd be enriching. The stem is intentionally
        # a single token without spaces so vault_search's grep
        # re.escape() round-trips cleanly (a pre-existing double-escape
        # quirk in vault_search drops space-containing stems; not what
        # this test is asserting).
        stub_dir = tmp_vault / "person"
        stub_dir.mkdir(exist_ok=True)
        stub_path = stub_dir / "AcmeMgr.md"
        stub_path.write_text(
            dedent(
                """\
                ---
                type: person
                name: AcmeMgr
                created: 2026-04-30
                ---
                """
            ),
            encoding="utf-8",
        )

        # A raw consumed-email body under inbox/processed/ that
        # mentions the stub's stem in plain prose. This file MUST be
        # ignored by the inbound-context lookup — it's not a record,
        # it's curator's audit trail.
        processed_dir = tmp_vault / "inbox" / "processed"
        processed_dir.mkdir(parents=True, exist_ok=True)
        (processed_dir / "email-2026-04-15.md").write_text(
            dedent(
                """\
                ---
                type: email
                from: nobody@example.com
                ---
                Hi Andrew, I'm forwarding this thread from AcmeMgr
                about the Q2 forecast. -Whoever
                """
            ),
            encoding="utf-8",
        )

        # An actual linker record under person/ — this SHOULD show up.
        (stub_dir / "Real-Linker.md").write_text(
            dedent(
                """\
                ---
                type: person
                name: Real-Linker
                created: 2026-04-29
                related:
                - "[[person/AcmeMgr]]"
                ---
                Worked with [[person/AcmeMgr]] on the rollout.
                """
            ),
            encoding="utf-8",
        )

        # Empty operator config — the bare default that exposed the bug.
        # The pipeline helper unions in STAGE_LOOKUP_NEVER_INDEX so
        # inbox/processed is excluded regardless.
        from alfred.janitor.pipeline import _stage_lookup_ignore_dirs
        from alfred.janitor.config import (
            JanitorConfig,
            StateConfig,
            SweepConfig,
            VaultConfig,
        )

        cfg = JanitorConfig(
            vault=VaultConfig(
                path=str(tmp_vault),
                ignore_dirs=[],
                dont_index_dirs=[],
                ignore_files=[".gitkeep"],
            ),
            sweep=SweepConfig(),
            state=StateConfig(),
        )
        ignore_dirs = _stage_lookup_ignore_dirs(cfg)

        out = _collect_linked_records(
            "person/AcmeMgr.md", tmp_vault, ignore_dirs
        )

        assert "inbox/processed/email-2026-04-15.md" not in out, (
            f"Stage 3 enrichment context leaked inbox/processed file: "
            f"{out!r}"
        )
        # Sanity check: the legitimate linker IS included.
        assert "person/Real-Linker.md" in out, (
            f"Stage 3 enrichment context dropped real linker: {out!r}"
        )

    def test_stage_lookup_constant_includes_system_dirs(self) -> None:
        # Lock the constant's contents — these are the dirs the pipeline
        # MUST always skip regardless of operator config.
        from alfred.janitor.pipeline import STAGE_LOOKUP_NEVER_INDEX

        assert "_templates" in STAGE_LOOKUP_NEVER_INDEX
        assert "_bases" in STAGE_LOOKUP_NEVER_INDEX
        assert "_docs" in STAGE_LOOKUP_NEVER_INDEX
        assert ".obsidian" in STAGE_LOOKUP_NEVER_INDEX
        assert "inbox/processed" in STAGE_LOOKUP_NEVER_INDEX

    def test_stage_lookup_helper_unions_with_operator_config(self) -> None:
        # Operator-config entries come first (preserved order); system
        # dirs are appended only if not already present. Idempotent if
        # the operator already listed one.
        from alfred.janitor.pipeline import (
            STAGE_LOOKUP_NEVER_INDEX,
            _stage_lookup_ignore_dirs,
        )
        from alfred.janitor.config import (
            JanitorConfig,
            StateConfig,
            SweepConfig,
            VaultConfig,
        )

        cfg_empty = JanitorConfig(
            vault=VaultConfig(path="/tmp/x", dont_index_dirs=[]),
            sweep=SweepConfig(),
            state=StateConfig(),
        )
        result_empty = _stage_lookup_ignore_dirs(cfg_empty)
        assert result_empty == list(STAGE_LOOKUP_NEVER_INDEX)

        cfg_overlap = JanitorConfig(
            vault=VaultConfig(
                path="/tmp/x",
                dont_index_dirs=["session", "_templates"],
            ),
            sweep=SweepConfig(),
            state=StateConfig(),
        )
        result_overlap = _stage_lookup_ignore_dirs(cfg_overlap)
        # Operator's "session" first, then operator's "_templates",
        # then the remaining system dirs (skipping the duplicate).
        assert result_overlap[0] == "session"
        assert result_overlap[1] == "_templates"
        # _templates is not duplicated.
        assert result_overlap.count("_templates") == 1
        assert "inbox/processed" in result_overlap
        assert ".obsidian" in result_overlap
