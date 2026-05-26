"""Tests for the direct-CLI audit-log fallback path.

Issue #64 (2026-05-10): ``alfred --config <c> vault create/edit/move/
delete/retype`` mutations weren't appearing in ``vault_audit.log``
when invoked directly (no agent backend involvement). The audit log
is the canonical "who changed what when" trail; direct CLI bypass
silently lost ~10 days of operator workflow mutations.

Root cause: ``vault/cli.py`` called ``log_mutation(_session(), ...)``
which early-returned on missing ``ALFRED_VAULT_SESSION`` (set only by
agent backends).

Fix: ``cmd_vault`` dispatcher wires ``ALFRED_VAULT_AUDIT_LOG`` env
var, ``_log_or_audit`` helper falls through to ``append_to_audit_log``
when no session is active.

These pins lock the four-state contract:
  - direct CLI + audit-log path set → audit log gets entry tool="cli"
  - direct CLI + audit-log path UNSET → silent no-op (legacy behavior
    preserved for standalone test invocations outside the dispatcher)
  - agent context (session set) + audit-log path also set → session
    path wins; audit log NOT double-written (preserves the
    agent-backend's deferred-flush contract)
  - move/retype writes both delete and create rows (mirrors
    ``read_mutations``)

Plus per-instance dispatcher resolution pins (Salem ./data, KAL-LE
/home/andrew/.alfred/kalle/data).

Tests run unconditionally per
``feedback_regression_pin_unconditional.md``.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_audit_log(path: Path) -> list[dict]:
    """Read a JSONL audit log file as a list of decoded dicts."""
    if not path.is_file():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        out.append(json.loads(line))
    return out


# ---------------------------------------------------------------------------
# _single_mutation_dict — the bucket-shape mirror for read_mutations
# ---------------------------------------------------------------------------


class TestSingleMutationDict:
    def test_create_writes_created_bucket(self) -> None:
        from alfred.vault.cli import _single_mutation_dict
        result = _single_mutation_dict("create", "note/test.md")
        assert result == {
            "files_created": ["note/test.md"],
            "files_modified": [],
            "files_deleted": [],
        }

    def test_edit_writes_modified_bucket(self) -> None:
        from alfred.vault.cli import _single_mutation_dict
        result = _single_mutation_dict("edit", "person/Jane.md")
        assert result["files_modified"] == ["person/Jane.md"]
        assert result["files_created"] == []
        assert result["files_deleted"] == []

    def test_delete_writes_deleted_bucket(self) -> None:
        from alfred.vault.cli import _single_mutation_dict
        result = _single_mutation_dict("delete", "task/Old.md")
        assert result["files_deleted"] == ["task/Old.md"]
        assert result["files_created"] == []
        assert result["files_modified"] == []

    def test_move_writes_both_delete_and_create(self) -> None:
        # Move semantics mirror ``read_mutations`` lines 67-72: a move
        # produces one delete (old path) + one create (new path).
        # Pinning this contract since the two code paths (CLI fallback
        # + agent-backend flush) need to stay in lockstep.
        from alfred.vault.cli import _single_mutation_dict
        result = _single_mutation_dict(
            "move", "old/path.md", to="new/path.md",
        )
        assert result["files_deleted"] == ["old/path.md"]
        assert result["files_created"] == ["new/path.md"]
        assert result["files_modified"] == []

    def test_move_without_to_omits_create_side(self) -> None:
        # Defensive against a malformed mutation entry — the create
        # side is skipped if ``to`` is empty rather than producing a
        # garbage ``files_created: [""]`` entry.
        from alfred.vault.cli import _single_mutation_dict
        result = _single_mutation_dict("move", "old/path.md")
        assert result["files_deleted"] == ["old/path.md"]
        assert result["files_created"] == []

    def test_retype_writes_both_delete_and_create(self) -> None:
        # Retype is composite (create new target + delete source).
        # CLI-only op; ``read_mutations`` doesn't handle it because
        # agent backends don't issue retype. Pin so the divergence
        # stays explicit.
        from alfred.vault.cli import _single_mutation_dict
        result = _single_mutation_dict(
            "retype", "person/John.md", target="org/Acme.md",
        )
        assert result["files_deleted"] == ["person/John.md"]
        assert result["files_created"] == ["org/Acme.md"]

    def test_unknown_op_produces_empty_buckets(self) -> None:
        # Defensive: an unknown op-string (e.g., a future op added to
        # session files but not yet handled in the CLI fallback)
        # produces no entries rather than crashing.
        from alfred.vault.cli import _single_mutation_dict
        result = _single_mutation_dict("unknown_future_op", "x.md")
        assert result["files_created"] == []
        assert result["files_modified"] == []
        assert result["files_deleted"] == []


# ---------------------------------------------------------------------------
# WARN-3 cure (2026-05-11): canonical lift to mutation_log.py. The same
# op→bucket-dict contract previously private to vault/cli.py is now
# importable as ``build_audit_mutations`` from the canonical home
# (next to ``append_to_audit_log``). Three call sites consume it:
# this file (via the thin ``_single_mutation_dict`` wrapper),
# ``distiller/cli.py::cmd_promote_proposal``, and
# ``distiller/cli.py::cmd_discard_proposal``.
#
# These pins cover the canonical helper directly + the two new
# op-strings (``promote``, ``discard``) added when lifting. The
# existing ``TestSingleMutationDict`` pins above still pass — they
# exercise the same logic transparently via the wrapper.
# ---------------------------------------------------------------------------


class TestBuildAuditMutationsCanonical:
    def test_imported_from_mutation_log_module(self) -> None:
        # Pin that the helper is exported from the canonical
        # location. Catches a regression that moves it back to
        # ``vault/cli.py`` or splits it across modules.
        from alfred.vault.mutation_log import build_audit_mutations
        assert callable(build_audit_mutations)

    def test_wrapper_delegates_to_canonical(self) -> None:
        # Pin that ``vault/cli.py::_single_mutation_dict`` is a thin
        # wrapper — same outputs across the two import paths so a
        # future refactor that breaks delegation surfaces here.
        from alfred.vault.cli import _single_mutation_dict
        from alfred.vault.mutation_log import build_audit_mutations
        # Cover the 5 ops the wrapper used to handle plus the 2 new
        # ones; both paths must produce identical dicts.
        for op, kwargs in [
            ("create", {}),
            ("edit", {}),
            ("delete", {}),
            ("move", {"to": "new/path.md"}),
            ("retype", {"target": "org/Acme.md"}),
            ("promote", {"target": "architecture/x.md"}),
            ("discard", {}),
        ]:
            wrapper_result = _single_mutation_dict(op, "x.md", **kwargs)
            direct_result = build_audit_mutations(op, "x.md", **kwargs)
            assert wrapper_result == direct_result, (
                f"wrapper vs direct diverged for op={op!r}: "
                f"wrapper={wrapper_result} direct={direct_result}"
            )

    def test_promote_op_writes_both_delete_and_create(self) -> None:
        # New op-string lifted as part of WARN-3 cure. Distiller's
        # cmd_promote_proposal calls this with target=<canonical>.
        # Same bucket-dict shape as ``retype`` since both are
        # "convert-via-create-plus-delete" composites — kept as
        # separate op-strings so the audit-log row detail can
        # distinguish operator intent (promote-from-inbox vs
        # retype-via-vault-cli).
        from alfred.vault.mutation_log import build_audit_mutations
        result = build_audit_mutations(
            "promote",
            "inbox/proposed-canonical/topic-x.md",
            target="architecture/topic-x.md",
        )
        assert result["files_deleted"] == [
            "inbox/proposed-canonical/topic-x.md",
        ]
        assert result["files_created"] == ["architecture/topic-x.md"]
        assert result["files_modified"] == []

    def test_promote_op_without_target_omits_create_side(self) -> None:
        # Defensive against a malformed promote call (no target).
        # Same defensive shape as move-without-to.
        from alfred.vault.mutation_log import build_audit_mutations
        result = build_audit_mutations(
            "promote", "inbox/proposed-canonical/x.md",
        )
        assert result["files_deleted"] == [
            "inbox/proposed-canonical/x.md",
        ]
        assert result["files_created"] == []

    def test_discard_op_writes_delete_only(self) -> None:
        # New op-string lifted as part of WARN-3 cure. Distiller's
        # cmd_discard_proposal calls this with no target — only the
        # inbox file gets deleted (no canonical replacement).
        from alfred.vault.mutation_log import build_audit_mutations
        result = build_audit_mutations(
            "discard", "inbox/proposed-canonical/x.md",
        )
        assert result["files_deleted"] == [
            "inbox/proposed-canonical/x.md",
        ]
        assert result["files_created"] == []
        assert result["files_modified"] == []

    def test_canonical_helper_handles_all_existing_ops(self) -> None:
        # Sanity coverage that lifting didn't accidentally drop one
        # of the 5 pre-existing ops the private helper handled. Pin
        # each op produces non-empty output for at least one bucket.
        from alfred.vault.mutation_log import build_audit_mutations
        assert build_audit_mutations("create", "a.md")["files_created"]
        assert build_audit_mutations("edit", "a.md")["files_modified"]
        assert build_audit_mutations("delete", "a.md")["files_deleted"]
        move_result = build_audit_mutations(
            "move", "a.md", to="b.md",
        )
        assert move_result["files_deleted"] and move_result["files_created"]
        retype_result = build_audit_mutations(
            "retype", "a.md", target="b.md",
        )
        assert retype_result["files_deleted"] and retype_result["files_created"]


# ---------------------------------------------------------------------------
# _log_or_audit — the four-state contract
# ---------------------------------------------------------------------------


class TestLogOrAudit:
    def test_session_path_takes_precedence(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # State 3 from the dispatch: both env vars set → session
        # file wins, audit log NOT double-written. Preserves the
        # agent-backend's deferred-flush contract (it'll write to
        # the audit log at wrap-up time).
        from alfred.vault.cli import _log_or_audit
        session_file = tmp_path / "session.jsonl"
        audit_file = tmp_path / "vault_audit.log"
        monkeypatch.setenv("ALFRED_VAULT_SESSION", str(session_file))
        monkeypatch.setenv("ALFRED_VAULT_AUDIT_LOG", str(audit_file))

        _log_or_audit("create", "note/test.md")

        # Session file got the entry.
        assert session_file.is_file()
        session_entries = [
            json.loads(ln)
            for ln in session_file.read_text(encoding="utf-8").splitlines()
            if ln.strip()
        ]
        assert len(session_entries) == 1
        assert session_entries[0]["op"] == "create"
        assert session_entries[0]["path"] == "note/test.md"

        # Audit log was NOT written (no double-counting).
        assert not audit_file.exists()

    def test_no_session_with_audit_path_writes_to_audit_log(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # State 1: the bug-of-record path. Direct CLI invocation now
        # writes to the audit log when ALFRED_VAULT_AUDIT_LOG is set.
        from alfred.vault.cli import _log_or_audit
        audit_file = tmp_path / "vault_audit.log"
        monkeypatch.delenv("ALFRED_VAULT_SESSION", raising=False)
        monkeypatch.setenv("ALFRED_VAULT_AUDIT_LOG", str(audit_file))

        _log_or_audit("create", "note/test.md")

        entries = _read_audit_log(audit_file)
        assert len(entries) == 1
        e = entries[0]
        assert e["tool"] == "cli"
        assert e["op"] == "create"
        assert e["path"] == "note/test.md"
        # ``detail`` carries the op for grep-by-detail workflows.
        assert "vault create via CLI" in e["detail"]
        # Audit-log entries carry a UTC timestamp; pin the field is
        # present (don't pin the value, it's wall-clock).
        assert "ts" in e

    def test_no_session_no_audit_path_silent_noop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # State 2: neither env var set → silent no-op. Preserves
        # legacy behavior for standalone test invocations or other
        # callers outside the dispatcher.
        from alfred.vault.cli import _log_or_audit
        monkeypatch.delenv("ALFRED_VAULT_SESSION", raising=False)
        monkeypatch.delenv("ALFRED_VAULT_AUDIT_LOG", raising=False)

        # Must not raise and must not create any file (no audit path
        # to create, no session path to create).
        _log_or_audit("create", "note/test.md")

        # tmp_path stays empty — neither file got created.
        assert list(tmp_path.iterdir()) == []

    def test_move_writes_two_audit_rows(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # State 4: move semantics. The agent-backend ``read_mutations``
        # path produces 2 rows for a move (delete old + create new).
        # The CLI fallback must produce the same shape — otherwise a
        # dashboard tailing the audit log would see different patterns
        # depending on whether the mutation came from CLI or agent.
        from alfred.vault.cli import _log_or_audit
        audit_file = tmp_path / "vault_audit.log"
        monkeypatch.delenv("ALFRED_VAULT_SESSION", raising=False)
        monkeypatch.setenv("ALFRED_VAULT_AUDIT_LOG", str(audit_file))

        _log_or_audit("move", "old/loc.md", to="new/loc.md")

        entries = _read_audit_log(audit_file)
        # Two rows: one delete, one create.
        assert len(entries) == 2
        ops = sorted(e["op"] for e in entries)
        assert ops == ["create", "delete"]
        # Both carry tool="cli".
        assert all(e["tool"] == "cli" for e in entries)
        # Path mapping correct.
        delete_row = next(e for e in entries if e["op"] == "delete")
        create_row = next(e for e in entries if e["op"] == "create")
        assert delete_row["path"] == "old/loc.md"
        assert create_row["path"] == "new/loc.md"

    def test_retype_writes_two_audit_rows(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Retype produces both sides too. CLI-only path so this is
        # the only place the audit log can see this op-shape today.
        from alfred.vault.cli import _log_or_audit
        audit_file = tmp_path / "vault_audit.log"
        monkeypatch.delenv("ALFRED_VAULT_SESSION", raising=False)
        monkeypatch.setenv("ALFRED_VAULT_AUDIT_LOG", str(audit_file))

        _log_or_audit(
            "retype", "person/John.md",
            target="org/Acme.md",
            target_type="org",
        )

        entries = _read_audit_log(audit_file)
        assert len(entries) == 2
        ops = sorted(e["op"] for e in entries)
        assert ops == ["create", "delete"]

    def test_list_extra_values_filtered_for_audit_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # ``edit`` passes ``fields=<list>`` to log diagnostic info into
        # the session file. The audit log doesn't carry list-shaped
        # extras (its row schema is flat). The helper must filter them
        # rather than crash on ``str(list_value)`` shenanigans or leak
        # a stringified list into a ``str``-typed audit field.
        from alfred.vault.cli import _log_or_audit
        audit_file = tmp_path / "vault_audit.log"
        monkeypatch.delenv("ALFRED_VAULT_SESSION", raising=False)
        monkeypatch.setenv("ALFRED_VAULT_AUDIT_LOG", str(audit_file))

        _log_or_audit("edit", "x.md", fields=["status", "due"])

        entries = _read_audit_log(audit_file)
        assert len(entries) == 1
        e = entries[0]
        assert e["op"] == "modify"  # read_mutations maps edit → modify
        assert e["path"] == "x.md"
        # The list-typed ``fields`` extra didn't crash + didn't leak.

    def test_creates_parent_dir_for_audit_log(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Mirrors ``append_to_audit_log`` line 107: parent dir auto-
        # created. Pin so a fresh-install scenario (data/ doesn't
        # exist yet) doesn't lose the first CLI mutation.
        from alfred.vault.cli import _log_or_audit
        audit_file = tmp_path / "nested" / "dir" / "vault_audit.log"
        monkeypatch.delenv("ALFRED_VAULT_SESSION", raising=False)
        monkeypatch.setenv("ALFRED_VAULT_AUDIT_LOG", str(audit_file))

        _log_or_audit("create", "x.md")

        assert audit_file.is_file()


# ---------------------------------------------------------------------------
# cmd_vault dispatcher — wires ALFRED_VAULT_AUDIT_LOG from config
# ---------------------------------------------------------------------------


class TestCmdVaultDispatcherWiring:
    """End-to-end tests that the top-level ``cmd_vault`` dispatcher
    sets ``ALFRED_VAULT_AUDIT_LOG`` to the per-instance-correct path
    before delegating to ``handle_vault_command``.

    The actual vault command (``handle_vault_command``) is stubbed
    out so these tests pin ONLY the dispatcher's env-wiring contract,
    not vault-op behavior. Stubbing keeps the test surface tight to
    the issue #64 fix (env var resolution) without re-exercising
    vault ops the other test suites already cover.
    """

    def _make_args(self, config_path: Path) -> argparse.Namespace:
        return argparse.Namespace(
            config=str(config_path),
            vault_cmd="context",
        )

    def _write_config(
        self,
        path: Path,
        *,
        log_dir: str,
        vault_path: Path,
    ) -> None:
        path.write_text(
            f"vault:\n"
            f"  path: {vault_path}\n"
            f"logging:\n"
            f"  dir: {log_dir}\n",
            encoding="utf-8",
        )

    def _stub_handle(
        self, monkeypatch: pytest.MonkeyPatch, captured: dict,
    ) -> None:
        """Replace ``handle_vault_command`` with a stub that captures
        ``ALFRED_VAULT_AUDIT_LOG`` at call time. The dispatcher's
        contract is "set the env var BEFORE delegating," so capturing
        from inside the stub pins the right ordering.

        Stub accepts ``**kwargs`` so the V1 ``vault_context`` typed
        thread-through doesn't break the env-var-only contract pin —
        these tests pin the V1 backward-compat mirror to env, not the
        VaultContext kwarg shape. The VaultContext path is pinned by
        ``test_vault_context.py``.
        """
        def _stub(_args: argparse.Namespace, **_kwargs) -> None:
            captured["audit_log"] = os.environ.get("ALFRED_VAULT_AUDIT_LOG")
            # Also capture the V1 vault_context kwarg shape so future
            # regressions in dispatcher resolution surface here too.
            captured["vault_context"] = _kwargs.get("vault_context")
        monkeypatch.setattr(
            "alfred.vault.cli.handle_vault_command", _stub,
        )

    def test_salem_style_logging_dir_resolves_to_data_subdir(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Salem convention: logging.dir = "./data" → audit log at
        # "./data/vault_audit.log". Pin so the dispatcher's path
        # resolution stays in sync with cmd_exec's precedent
        # (cli.py:942).
        from alfred.cli import cmd_vault

        vault = tmp_path / "vault"
        vault.mkdir()
        log_dir = tmp_path / "data"
        config = tmp_path / "config.yaml"
        self._write_config(config, log_dir=str(log_dir), vault_path=vault)

        monkeypatch.delenv("ALFRED_VAULT_AUDIT_LOG", raising=False)
        captured: dict = {}
        self._stub_handle(monkeypatch, captured)

        cmd_vault(self._make_args(config))

        assert captured["audit_log"] == str(log_dir / "vault_audit.log")

    def test_kalle_style_per_instance_logging_dir_resolves_correctly(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # KAL-LE convention: logging.dir =
        # "/home/andrew/.alfred/kalle/data" → audit log at
        # "/home/andrew/.alfred/kalle/data/vault_audit.log". Per-
        # instance scope discipline: the dispatcher MUST NOT
        # hardcode Salem's "./data" — the resolution comes from
        # the config block. Simulate with a tmp_path-shaped
        # per-instance dir.
        from alfred.cli import cmd_vault

        vault = tmp_path / "kalle-vault"
        vault.mkdir()
        # Simulate KAL-LE's distinct data dir layout. The actual
        # /home/andrew/.alfred/kalle/data path isn't used here —
        # the per-instance VARIATION is what we pin.
        log_dir = tmp_path / "instance-specific" / "data"
        log_dir.mkdir(parents=True)
        config = tmp_path / "config.kalle.yaml"
        self._write_config(config, log_dir=str(log_dir), vault_path=vault)

        monkeypatch.delenv("ALFRED_VAULT_AUDIT_LOG", raising=False)
        captured: dict = {}
        self._stub_handle(monkeypatch, captured)

        cmd_vault(self._make_args(config))

        # Audit-log path resolved to the per-instance logging.dir,
        # not "./data" (which would be Salem's).
        assert captured["audit_log"] == str(log_dir / "vault_audit.log")
        # Sanity: it's NOT the Salem default.
        assert captured["audit_log"] != "./data/vault_audit.log"

    def test_dispatcher_respects_caller_override(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # If the caller has already set ALFRED_VAULT_AUDIT_LOG (e.g.
        # a test harness pointing at a tmp_path), the dispatcher
        # MUST NOT overwrite it. Mirrors the standard
        # ALFRED_VAULT_PATH / ALFRED_VAULT_SCOPE precedence rules.
        from alfred.cli import cmd_vault

        vault = tmp_path / "vault"
        vault.mkdir()
        log_dir = tmp_path / "data"
        config = tmp_path / "config.yaml"
        self._write_config(config, log_dir=str(log_dir), vault_path=vault)

        override = str(tmp_path / "test-only-audit.log")
        monkeypatch.setenv("ALFRED_VAULT_AUDIT_LOG", override)
        captured: dict = {}
        self._stub_handle(monkeypatch, captured)

        cmd_vault(self._make_args(config))

        # Override preserved; dispatcher didn't clobber.
        assert captured["audit_log"] == override

    def test_dispatcher_threads_vault_context_kwarg(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """V1 contract: ``cmd_vault`` builds a ``VaultContext`` and
        passes it to ``handle_vault_command(args, vault_context=...)``.

        Pins the typed thread-through alongside the legacy env-var
        mirror. Both must work in V1; V2 drops the env-var mirror
        once consumer migration tail closes.
        """
        from alfred.cli import cmd_vault
        from alfred.vault.context import VaultContext

        vault = tmp_path / "vault"
        vault.mkdir()
        log_dir = tmp_path / "data"
        config = tmp_path / "config.yaml"
        self._write_config(config, log_dir=str(log_dir), vault_path=vault)

        monkeypatch.delenv("ALFRED_VAULT_AUDIT_LOG", raising=False)
        captured: dict = {}
        self._stub_handle(monkeypatch, captured)

        cmd_vault(self._make_args(config))

        # V1: vault_context kwarg is present AND its audit_log_path
        # field matches what env-var carries (the two paths agree
        # during V1's backward-compat overlap).
        ctx = captured["vault_context"]
        assert isinstance(ctx, VaultContext)
        assert ctx.audit_log_path == str(log_dir / "vault_audit.log")
        assert ctx.audit_log_path == captured["audit_log"]


