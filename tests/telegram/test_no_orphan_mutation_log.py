"""Tests for wk1-polish bug (c): no stray ``mutation_log.log_mutation`` call.

Wk1 called ``mutation_log.log_mutation(session.session_id, ...)`` in
``conversation._execute_tool``. The mutation-log module expects a JSONL
FILE PATH, not a UUID — passing a UUID caused ``open(session_id, "a")`` to
create a file literally named after the UUID at the CWD (typically repo
root). Artifact found on 2026-04-18: ``286921d8-*`` at the Alfred repo
root. The call is dropped entirely — vault ops are still tracked via
``session.vault_ops`` (→ session record frontmatter) and via
``data/vault_audit.log`` once that wiring lands.

Tests:
    1. ``conversation`` does not import ``mutation_log``.
    2. Running a ``vault_create`` through ``_execute_tool`` produces zero
       files of the ``<session_id>-*`` shape in the CWD.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from alfred.telegram import conversation
from alfred.telegram.session import Session


def test_conversation_does_not_call_mutation_log_log_mutation() -> None:
    """Static guard: the call site for the stray-file bug is gone.

    The explanatory comment in conversation.py legitimately contains the
    word ``mutation_log``, so we guard on the ACTUAL call signature
    (``mutation_log.log_mutation(``) and on the import form
    (``import mutation_log`` / ``from alfred.vault import mutation_log``).
    """
    source = Path(conversation.__file__).read_text(encoding="utf-8")
    assert "mutation_log.log_mutation(" not in source
    assert "import mutation_log" not in source
    # The ``from alfred.vault import ops, scope`` line is fine; the
    # previous wk1 shape was ``from alfred.vault import mutation_log, ops, scope``.
    for line in source.splitlines():
        if line.lstrip().startswith("from alfred.vault import"):
            assert "mutation_log" not in line, line


@pytest.mark.asyncio
async def test_execute_tool_does_not_create_stray_uuid_file(
    tmp_path, state_mgr, talker_config, monkeypatch
) -> None:
    """vault_create through ``_execute_tool`` leaves no stray file at CWD."""
    from datetime import datetime, timezone

    # Pin CWD so we can assert no stray file landed next to us.
    monkeypatch.chdir(tmp_path)
    # Make sure the vault is scaffold-safe for a ``note`` create.
    (Path(talker_config.vault.path) / "note").mkdir(exist_ok=True)

    session_id = "f00dface-0000-0000-0000-000000000000"
    sess = Session(
        session_id=session_id,
        chat_id=1,
        started_at=datetime.now(timezone.utc),
        last_message_at=datetime.now(timezone.utc),
        model="claude-sonnet-4-6",
    )
    state_mgr.set_active(1, sess.to_dict())

    result_json = await conversation._execute_tool(
        "vault_create",
        {
            "type": "note",
            "name": "Test Note From Polish Commit",
            "set_fields": {},
            "body": "body",
        },
        vault_path=talker_config.vault.path,
        state=state_mgr,
        session=sess,
    )

    # Vault op succeeded.
    assert '"path"' in result_json

    # No file literally named ``<session_id>*`` landed in CWD (the wk1 bug
    # signature). Also guard the session_id prefix exactly — a real temp
    # file could start with ``alfred_vault_`` which is harmless.
    stray = [p for p in Path.cwd().iterdir() if p.name.startswith(session_id[:8])]
    assert stray == [], f"stray mutation-log artifacts: {stray}"
