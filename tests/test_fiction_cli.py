"""Tests for ``alfred fiction`` CLI subcommands.

Phase 2.5 follow-up to the ``/fiction`` slash-command ship. The CLI
is the entry point used by Hypatia's SKILL natural-language
scaffolding path: SKILL detects "let's start a fiction project
called X" and invokes ``bash: alfred fiction scaffold "X"``. The
JSON output gives the SKILL the slug + path + file list it confirms
to Andrew.

Coverage:

  * scaffold success → JSON shape matches the documented contract
  * scaffold idempotent (already_existed=true on second invocation)
  * scaffold missing vault → exit 1, stderr error message, no JSON
    on stdout (so SKILL parsing never sees half-broken output)
  * scaffold empty title → exit 2 (usage error)
  * slug → prints just the slug, no JSON
  * slug empty title → exit 2
  * slug uses the canonical NFKD-normalized helper (parity with
    slash command + scaffold path)
  * Top-level argparse: ``alfred fiction {scaffold,slug} "<title>"``
    parses correctly
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_json_payload(captured_stdout: str) -> dict:
    """Pull the JSON object out of captured stdout, ignoring leading log lines.

    ``fiction_cli`` emits ``log.info("fiction_cli.scaffold_invoked", ...)``
    via structlog with the default ``ConsoleRenderer`` sink, which lands
    on stdout — same sink as the ``--json`` output. ``json.loads`` chokes
    on the leading non-JSON content. Find the first line whose stripped
    form starts with ``{`` and parse from there. Per
    ``feedback_structlog_assertion_patterns.md``.
    """
    lines = captured_stdout.splitlines(keepends=True)
    for idx, line in enumerate(lines):
        if line.lstrip().startswith("{"):
            return json.loads("".join(lines[idx:]))
    raise AssertionError(f"no JSON object found in: {captured_stdout!r}")


def _make_raw(vault_path: Path) -> dict:
    return {"vault": {"path": str(vault_path)}}


def _make_vault(tmp_path: Path) -> Path:
    """Hypatia-shaped vault root: just the directory (the scaffolder
    creates draft/fiction/<slug>/ on demand)."""
    vault = tmp_path / "vault"
    vault.mkdir()
    return vault


# ---------------------------------------------------------------------------
# scaffold — happy path
# ---------------------------------------------------------------------------


def test_scaffold_success_returns_documented_json_shape(tmp_path, capsys):
    from alfred.telegram import fiction_cli

    vault = _make_vault(tmp_path)
    rc = fiction_cli.cmd_scaffold(_make_raw(vault), "The Glass Forest")
    assert rc == 0

    out = capsys.readouterr().out
    payload = _parse_json_payload(out)

    # The four documented contract fields, in exact shape.
    assert payload["slug"] == "the-glass-forest"
    assert payload["already_existed"] is False
    assert payload["path"] == str(
        vault / "draft" / "fiction" / "the-glass-forest"
    )
    # files_created uses BASENAMES (the SKILL contract), not full
    # vault-relative paths.
    assert sorted(payload["files_created"]) == sorted([
        "continuity.md",
        "story.md",
        "structure.md",
        "world.md",
        "voice.md",
        "characters/.gitkeep",
    ])

    # Files actually exist on disk.
    project_dir = vault / "draft" / "fiction" / "the-glass-forest"
    assert (project_dir / "continuity.md").is_file()
    assert (project_dir / "characters" / ".gitkeep").is_file()


def test_scaffold_idempotent_second_invocation(tmp_path, capsys):
    """Second call with same title returns already_existed=True with
    empty files_created — SKILL renders 'already exists, no changes'
    rather than re-confirming a fresh create."""
    from alfred.telegram import fiction_cli

    vault = _make_vault(tmp_path)
    rc1 = fiction_cli.cmd_scaffold(_make_raw(vault), "The Glass Forest")
    assert rc1 == 0
    capsys.readouterr()  # discard first output

    rc2 = fiction_cli.cmd_scaffold(_make_raw(vault), "The Glass Forest")
    assert rc2 == 0
    payload = _parse_json_payload(capsys.readouterr().out)
    assert payload["slug"] == "the-glass-forest"
    assert payload["already_existed"] is True
    assert payload["files_created"] == []


def test_scaffold_uses_nfkd_slug_parity(tmp_path, capsys):
    """Scaffold path uses the same slug helper as the slash command.

    Pinned because Phase 2.5 added NFKD normalization to fix the
    accented-Latin parity gap. If the CLI ever bypasses the helper
    (e.g. derives its own slug), this test fires."""
    from alfred.telegram import fiction_cli

    vault = _make_vault(tmp_path)
    rc = fiction_cli.cmd_scaffold(_make_raw(vault), "Café Society")
    assert rc == 0
    payload = _parse_json_payload(capsys.readouterr().out)
    # NFKD-normalized: "Café" → "Cafe" (the Phase 2.5 fix); pre-fix
    # this would have been "caf-society".
    assert payload["slug"] == "cafe-society"


# ---------------------------------------------------------------------------
# scaffold — error paths
# ---------------------------------------------------------------------------


def test_scaffold_missing_vault_exits_with_error(tmp_path, capsys):
    """Vault path doesn't exist → exit 1, stderr error, no stdout JSON.

    Critical: stdout must stay clean (no half-built JSON, no log
    leakage) so the SKILL's ``json.loads(stdout)`` never sees
    confusing partial output.
    """
    from alfred.telegram import fiction_cli

    nonexistent = tmp_path / "this-vault-does-not-exist"
    rc = fiction_cli.cmd_scaffold(_make_raw(nonexistent), "X")
    assert rc == 1
    captured = capsys.readouterr()
    assert captured.out == ""  # stdout clean
    assert "vault path not accessible" in captured.err


def test_scaffold_empty_title_exits_with_usage_error(tmp_path, capsys):
    from alfred.telegram import fiction_cli

    vault = _make_vault(tmp_path)
    rc = fiction_cli.cmd_scaffold(_make_raw(vault), "")
    assert rc == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "title is required" in captured.err


def test_scaffold_whitespace_only_title_exits_with_usage_error(tmp_path, capsys):
    from alfred.telegram import fiction_cli

    vault = _make_vault(tmp_path)
    rc = fiction_cli.cmd_scaffold(_make_raw(vault), "   ")
    assert rc == 2
    assert "title is required" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# slug subcommand
# ---------------------------------------------------------------------------


def test_slug_prints_canonical_slug(capsys):
    from alfred.telegram import fiction_cli

    rc = fiction_cli.cmd_slug("The Glass Forest")
    assert rc == 0
    out = capsys.readouterr().out
    # Trailing newline from print(); use .strip() to compare.
    assert out.strip() == "the-glass-forest"


def test_slug_uses_nfkd_normalization(capsys):
    """Same parity guarantee as scaffold — both go through
    slug_from_title."""
    from alfred.telegram import fiction_cli

    rc = fiction_cli.cmd_slug("Naïve résumé")
    assert rc == 0
    assert capsys.readouterr().out.strip() == "naive-resume"


def test_slug_empty_title_exits_with_usage_error(capsys):
    from alfred.telegram import fiction_cli

    rc = fiction_cli.cmd_slug("")
    assert rc == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "title is required" in captured.err


# ---------------------------------------------------------------------------
# Top-level CLI parser
# ---------------------------------------------------------------------------


def test_alfred_fiction_scaffold_subcommand_registered():
    from alfred.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(
        ["fiction", "scaffold", "The Glass Forest"],
    )
    assert args.command == "fiction"
    assert args.fiction_cmd == "scaffold"
    assert args.title == "The Glass Forest"


def test_alfred_fiction_slug_subcommand_registered():
    from alfred.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(["fiction", "slug", "The Glass Forest"])
    assert args.command == "fiction"
    assert args.fiction_cmd == "slug"
    assert args.title == "The Glass Forest"


def test_alfred_fiction_scaffold_requires_title():
    """argparse rejects ``alfred fiction scaffold`` with no title."""
    from alfred.cli import build_parser

    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["fiction", "scaffold"])


def test_alfred_fiction_slug_requires_title():
    from alfred.cli import build_parser

    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["fiction", "slug"])
