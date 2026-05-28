"""CLI integration tests for ``alfred vault edit --unset <field>``.

Sister file to ``test_vault_edit_unset.py`` (library-surface tests).
This file pins the CLI surface — argparse plumbing, cmd_edit handler
behaviour, dual session-log emission on combined ops, and the
audit-log integration end-to-end.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest
import structlog

from alfred.vault.ops import vault_create, vault_read


def _read_audit_log(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        out.append(json.loads(line))
    return out


def _read_session_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        out.append(json.loads(line))
    return out


def _make_edit_args(
    path: str,
    *,
    set_args: list[str] | None = None,
    append_args: list[str] | None = None,
    unset_args: list[str] | None = None,
    body_append: str | None = None,
    body_stdin: bool = False,
) -> argparse.Namespace:
    """Build an argparse.Namespace mirroring the argparse defaults
    for ``alfred vault edit``."""
    return argparse.Namespace(
        path=path,
        set=set_args,
        append=append_args,
        unset=unset_args,
        body_append=body_append,
        body_stdin=body_stdin,
    )


# ---------------------------------------------------------------------------
# Argparse plumbing
# ---------------------------------------------------------------------------


class TestUnsetArgparsePlumbing:
    def test_unset_flag_registered_in_edit_parser(self) -> None:
        """``vault edit --unset`` must accept a string argument and
        be repeatable. Build the parser and inspect the action."""
        from alfred.vault.cli import build_vault_parser
        root = argparse.ArgumentParser()
        subparsers = root.add_subparsers(dest="cmd")
        build_vault_parser(subparsers)

        # Parse two --unset flags + one --set flag end-to-end.
        ns = root.parse_args([
            "vault", "edit", "task/X.md",
            "--unset", "priority",
            "--unset", "due",
            "--set", "status=todo",
        ])
        assert ns.unset == ["priority", "due"]
        assert ns.set == ["status=todo"]

    def test_unset_absent_defaults_to_none(self) -> None:
        """When --unset is not supplied, ``args.unset`` is None
        (the argparse default for ``action='append'``)."""
        from alfred.vault.cli import build_vault_parser
        root = argparse.ArgumentParser()
        subparsers = root.add_subparsers(dest="cmd")
        build_vault_parser(subparsers)

        ns = root.parse_args([
            "vault", "edit", "task/X.md",
            "--set", "status=todo",
        ])
        assert ns.unset is None


# ---------------------------------------------------------------------------
# cmd_edit handler behaviour
# ---------------------------------------------------------------------------


class TestCmdEditUnset:
    def test_unset_only_removes_field(
        self, tmp_vault: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """End-to-end CLI: --unset alone removes the field."""
        from alfred.vault.cli import cmd_edit
        vault_create(
            tmp_vault, "task", "CLI Unset Only",
            set_fields={"status": "todo", "priority": "low"},
        )
        monkeypatch.setenv("ALFRED_VAULT_PATH", str(tmp_vault))
        monkeypatch.delenv("ALFRED_VAULT_SCOPE", raising=False)
        monkeypatch.delenv("ALFRED_VAULT_SESSION", raising=False)
        monkeypatch.delenv("ALFRED_VAULT_AUDIT_LOG", raising=False)

        cmd_edit(_make_edit_args(
            "task/CLI Unset Only.md",
            unset_args=["priority"],
        ))

        post = vault_read(tmp_vault, "task/CLI Unset Only.md")
        assert "priority" not in post["frontmatter"]
        assert post["frontmatter"]["status"] == "todo"

    def test_unset_combined_with_set(
        self, tmp_vault: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Single CLI call with both --set and --unset: both apply."""
        from alfred.vault.cli import cmd_edit
        vault_create(
            tmp_vault, "task", "CLI Combined",
            set_fields={"status": "todo", "priority": "low"},
        )
        monkeypatch.setenv("ALFRED_VAULT_PATH", str(tmp_vault))
        monkeypatch.delenv("ALFRED_VAULT_SCOPE", raising=False)
        monkeypatch.delenv("ALFRED_VAULT_SESSION", raising=False)
        monkeypatch.delenv("ALFRED_VAULT_AUDIT_LOG", raising=False)

        cmd_edit(_make_edit_args(
            "task/CLI Combined.md",
            set_args=["status=active"],
            unset_args=["priority"],
        ))

        post = vault_read(tmp_vault, "task/CLI Combined.md")
        assert post["frontmatter"]["status"] == "active"
        assert "priority" not in post["frontmatter"]

    def test_unset_required_field_exits_with_actionable_error(
        self,
        tmp_vault: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """Layer 1 ops-layer refusal surfaces to operator as a non-zero
        CLI exit with the required-field error message.

        Wrapped in ``structlog.testing.capture_logs()`` because
        ``cmd_edit`` calls ``_vault_path()`` → ``_ctx()`` →
        ``VaultContext.from_env`` which emits a structured
        ``vault_context.env_fallback`` log line. Default structlog
        config (no setup_logging called in isolated test runs) routes
        that line to stdout, polluting the JSON payload that ``_error``
        prints. ``capture_logs`` intercepts structlog emissions before
        the rendering layer so only the JSON output reaches stdout.
        Pattern matches ``feedback_structlog_assertion_patterns.md``:
        for tests that parse stdout from a CLI handler, capture all
        structlog noise so the parse target is unambiguous.
        """
        from alfred.vault.cli import cmd_edit
        vault_create(
            tmp_vault, "task", "CLI Required Guard",
            set_fields={"status": "todo"},
        )
        monkeypatch.setenv("ALFRED_VAULT_PATH", str(tmp_vault))
        monkeypatch.delenv("ALFRED_VAULT_SCOPE", raising=False)
        monkeypatch.delenv("ALFRED_VAULT_SESSION", raising=False)
        monkeypatch.delenv("ALFRED_VAULT_AUDIT_LOG", raising=False)

        with structlog.testing.capture_logs():
            with pytest.raises(SystemExit) as exc_info:
                cmd_edit(_make_edit_args(
                    "task/CLI Required Guard.md",
                    unset_args=["created"],
                ))
        assert exc_info.value.code == 1
        out = capsys.readouterr().out
        payload = json.loads(out)
        assert "required field" in payload["error"].lower()
        assert "created" in payload["error"]

    def test_unset_at_migration_scope_succeeds(
        self, tmp_vault: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Migration scope permits unset (edit: True, no allowlist)."""
        from alfred.vault.cli import cmd_edit
        vault_create(
            tmp_vault, "task", "Migration Scope CLI",
            set_fields={"status": "todo", "priority": "low"},
        )
        monkeypatch.setenv("ALFRED_VAULT_PATH", str(tmp_vault))
        monkeypatch.setenv("ALFRED_VAULT_SCOPE", "migration")
        monkeypatch.delenv("ALFRED_VAULT_SESSION", raising=False)
        monkeypatch.delenv("ALFRED_VAULT_AUDIT_LOG", raising=False)

        cmd_edit(_make_edit_args(
            "task/Migration Scope CLI.md",
            unset_args=["priority"],
        ))
        post = vault_read(tmp_vault, "task/Migration Scope CLI.md")
        assert "priority" not in post["frontmatter"]


# ---------------------------------------------------------------------------
# Session-log + audit-log integration
# ---------------------------------------------------------------------------


class TestCmdEditUnsetAuditLog:
    def test_unset_only_writes_single_session_unset_entry(
        self, tmp_vault: Path, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When ONLY unset is supplied, the session log emits exactly
        one entry with op="unset" — not "edit"."""
        from alfred.vault.cli import cmd_edit
        vault_create(
            tmp_vault, "task", "Session Unset Solo",
            set_fields={"status": "todo", "priority": "low"},
        )
        session_file = tmp_path / "session.jsonl"
        monkeypatch.setenv("ALFRED_VAULT_PATH", str(tmp_vault))
        monkeypatch.setenv("ALFRED_VAULT_SESSION", str(session_file))
        monkeypatch.delenv("ALFRED_VAULT_AUDIT_LOG", raising=False)
        monkeypatch.delenv("ALFRED_VAULT_SCOPE", raising=False)

        cmd_edit(_make_edit_args(
            "task/Session Unset Solo.md",
            unset_args=["priority"],
        ))

        entries = _read_session_jsonl(session_file)
        assert len(entries) == 1
        assert entries[0]["op"] == "unset"
        assert entries[0]["path"] == "task/Session Unset Solo.md"

    def test_combined_set_and_unset_writes_two_session_entries(
        self, tmp_vault: Path, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Combined --set + --unset → TWO session entries, one per
        operator intent. Audit-log readers + KAL-LE distiller-radar
        can attribute each field-mutation to the right op-string."""
        from alfred.vault.cli import cmd_edit
        vault_create(
            tmp_vault, "task", "Session Combined",
            set_fields={"status": "todo", "priority": "low"},
        )
        session_file = tmp_path / "session.jsonl"
        monkeypatch.setenv("ALFRED_VAULT_PATH", str(tmp_vault))
        monkeypatch.setenv("ALFRED_VAULT_SESSION", str(session_file))
        monkeypatch.delenv("ALFRED_VAULT_AUDIT_LOG", raising=False)
        monkeypatch.delenv("ALFRED_VAULT_SCOPE", raising=False)

        cmd_edit(_make_edit_args(
            "task/Session Combined.md",
            set_args=["status=active"],
            unset_args=["priority"],
        ))

        entries = _read_session_jsonl(session_file)
        # Two entries: one "unset" + one "edit". Order is "unset
        # first" per cmd_edit's implementation (the actually-unset
        # filter pass runs before the write-side log). Operators who
        # rely on temporal order can re-sort by ts; what we pin here
        # is that BOTH ops surface in the log.
        ops = sorted(e["op"] for e in entries)
        assert ops == ["edit", "unset"]
        # Both name the same path.
        for e in entries:
            assert e["path"] == "task/Session Combined.md"

    def test_already_absent_unset_does_not_emit_session_entry(
        self, tmp_vault: Path, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When --unset names a field that's already absent, the
        ops layer logs the no-op (vault.edit.unset_no_op) but the
        CLI does NOT emit a session "unset" entry — that would
        falsely claim a mutation happened.

        Specifically pinned because the audit log is the operator's
        primary "what changed" surface; emitting unset entries for
        no-op calls would clutter the trail with phantom mutations
        and break the operator-grep-by-path workflow."""
        from alfred.vault.cli import cmd_edit
        vault_create(
            tmp_vault, "task", "Session Unset No-Op",
            set_fields={"status": "todo"},
        )
        session_file = tmp_path / "session.jsonl"
        monkeypatch.setenv("ALFRED_VAULT_PATH", str(tmp_vault))
        monkeypatch.setenv("ALFRED_VAULT_SESSION", str(session_file))
        monkeypatch.delenv("ALFRED_VAULT_AUDIT_LOG", raising=False)
        monkeypatch.delenv("ALFRED_VAULT_SCOPE", raising=False)

        # Unset a field that's NOT present + a set so the no-op gate
        # doesn't fire.
        cmd_edit(_make_edit_args(
            "task/Session Unset No-Op.md",
            set_args=["status=active"],
            unset_args=["nonexistent_field"],
        ))

        entries = _read_session_jsonl(session_file)
        # ONLY the edit entry — no phantom unset.
        assert len(entries) == 1
        assert entries[0]["op"] == "edit"

    def test_audit_log_fallback_writes_modify_row_with_unset_detail(
        self, tmp_vault: Path, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No session set + audit-log path set → direct CLI fallback
        writes a single "modify" row (bucket assignment) with
        detail="vault unset via CLI" so operators grep on detail."""
        from alfred.vault.cli import cmd_edit
        vault_create(
            tmp_vault, "task", "Audit Unset",
            set_fields={"status": "todo", "priority": "low"},
        )
        audit_file = tmp_path / "vault_audit.log"
        monkeypatch.setenv("ALFRED_VAULT_PATH", str(tmp_vault))
        monkeypatch.delenv("ALFRED_VAULT_SESSION", raising=False)
        monkeypatch.setenv("ALFRED_VAULT_AUDIT_LOG", str(audit_file))
        monkeypatch.delenv("ALFRED_VAULT_SCOPE", raising=False)

        cmd_edit(_make_edit_args(
            "task/Audit Unset.md",
            unset_args=["priority"],
        ))

        entries = _read_audit_log(audit_file)
        assert len(entries) == 1
        e = entries[0]
        # File-state-change bucket is "modify" (same as edit).
        assert e["op"] == "modify"
        assert e["path"] == "task/Audit Unset.md"
        # The detail field is the operator's grep target for intent.
        assert "vault unset via CLI" in e["detail"]
        assert e["tool"] == "cli"
