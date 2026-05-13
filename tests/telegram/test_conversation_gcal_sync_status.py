"""End-to-end dispatcher test for the ``gcal_sync`` tool_result surface
(shipped 2026-05-13).

The contract:

  1. The talker dispatcher (``conversation._execute_tool``) calls
     ``ops.vault_create`` / ``ops.vault_edit`` on event records.
  2. ``vault_create`` / ``vault_edit`` fires registered event hooks,
     collects dict-shaped hook return values, and surfaces a
     ``gcal_sync`` field in the result dict when something concrete
     was reported.
  3. The dispatcher JSON-serializes the result verbatim into the
     tool_result the LLM sees.

The lower-level pieces are pinned in ``test_vault_event_hooks_gcal_sync_surface.py``
(``_extract_gcal_sync_status``, ``_fire_*_hooks`` bubble-up, vault-op
return dicts). This file pins the end-to-end shape that lands as a
``tool_result`` block, with the dispatcher fully in the loop — the
test the original bug-report brief explicitly requested.

The fixture mocks ``sync_event_update_to_gcal`` to return the same
``auth_failed`` error shape the 2026-05-12 / 2026-05-13 production
incidents produced (per data/talker.log), then asserts that the
JSON-serialized tool_result the LLM would have seen carries the
``gcal_sync`` failure signal — so Salem can no longer narrate phantom
"GCal updated" success.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import frontmatter
import pytest

from alfred.telegram import conversation
from alfred.telegram.session import Session


def _make_session() -> Session:
    """Minimal session matching the talker test conventions."""
    now = datetime.now(timezone.utc)
    return Session(
        session_id="abc12345-0000-0000-0000-000000000000",
        chat_id=1,
        started_at=now,
        last_message_at=now,
        model="claude-sonnet-4-6",
    )


def _seed_event(vault_path: str, *, name: str, fields: dict) -> str:
    """Write a minimal event/<name>.md and return its rel_path."""
    fm = {"type": "event", "name": name, "title": name}
    fm.update(fields)
    rel_path = f"event/{name}.md"
    file_path = Path(vault_path) / rel_path
    file_path.parent.mkdir(exist_ok=True)
    post = frontmatter.Post("body\n", **fm)
    file_path.write_text(frontmatter.dumps(post) + "\n", encoding="utf-8")
    return rel_path


@pytest.fixture(autouse=True)
def _clean_hooks():
    """Clear hook registries before AND after each test (process-global state)."""
    from alfred.vault.ops import clear_event_hooks
    clear_event_hooks()
    yield
    clear_event_hooks()


# ---------------------------------------------------------------------------
# The smoking-gun pin: dispatcher surfaces auth_failed end-to-end
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatcher_vault_edit_surfaces_gcal_auth_failed_in_tool_result(
    state_mgr, talker_config,
) -> None:
    """The 2026-05-12 / 2026-05-13 production bug, pinned at the dispatcher.

    Before this fix: ``vault_edit`` succeeded, GCal sync failed with
    ``auth_failed``, but the dispatcher's tool_result said only
    ``{"path": "...", "fields_changed": [...]}`` — so Salem narrated
    "GCal updated" to Andrew. After: the tool_result carries
    ``gcal_sync: {status: failed, error_code: auth_failed, error: <msg>}``
    so the LLM has the signal to refuse the phantom-success narration.
    """
    from alfred.vault.ops import register_event_update_hook

    sess = _make_session()
    state_mgr.set_active(1, sess.to_dict())

    rel_path = _seed_event(
        talker_config.vault.path,
        name="Dentist Cleaning",
        fields={
            "start": "2026-05-19T10:30:00-03:00",
            "end": "2026-05-19T11:00:00-03:00",
            "gcal_event_id": "ev55futp8gbsqk0dtc5276d24o",
        },
    )

    # Mock the gcal sync hook to return the exact shape ``sync_event_update_to_gcal``
    # returns on expired OAuth refresh tokens — same shape grep'd from
    # data/talker.log on May 12 18:45 ADT.
    def _fake_update_hook(vault_path_, rel_path_, fm, fields_changed):
        return {
            "error": {
                "code": "auth_failed",
                "detail": (
                    "GCal token refresh failed: ('invalid_grant: Token "
                    "has been expired or revoked.', '{...}')"
                ),
            }
        }

    register_event_update_hook(_fake_update_hook)

    # Dispatch the same way the LLM tool_use loop does.
    result_json = await conversation._execute_tool(
        "vault_edit",
        {
            "path": rel_path,
            "set_fields": {
                "start": "2026-05-26T10:30:00-03:00",
                "end": "2026-05-26T11:00:00-03:00",
            },
        },
        vault_path=talker_config.vault.path,
        state=state_mgr,
        session=sess,
        config=talker_config,
    )

    # The JSON-serialized tool_result the LLM would see. Pin the
    # FULL shape so a future refactor that drops gcal_sync, renames a
    # field, or accidentally swallows the failure path fails CI.
    result = json.loads(result_json)
    assert result["path"] == rel_path
    assert "start" in result["fields_changed"]
    assert "gcal_sync" in result, (
        "Tool result must carry gcal_sync so the LLM sees the sync "
        "failure rather than narrating phantom success — this is the "
        "exact shape the May 12/13 production bug needed."
    )
    assert result["gcal_sync"]["status"] == "failed"
    assert result["gcal_sync"]["error_code"] == "auth_failed"
    assert "GCal token refresh failed" in result["gcal_sync"]["error"]


@pytest.mark.asyncio
async def test_dispatcher_vault_edit_surfaces_gcal_ok_in_tool_result(
    state_mgr, talker_config,
) -> None:
    """Happy path: successful sync surfaces ``gcal_sync: {status: ok}``.

    Pins the success-path shape so the LLM has explicit positive
    confirmation rather than inferring success from gcal_sync's absence.
    """
    from alfred.vault.ops import register_event_update_hook

    sess = _make_session()
    state_mgr.set_active(1, sess.to_dict())

    rel_path = _seed_event(
        talker_config.vault.path,
        name="Lunch with Marie",
        fields={
            "start": "2026-05-19T12:30:00-03:00",
            "end": "2026-05-19T13:30:00-03:00",
            "gcal_event_id": "ev_marie_lunch",
        },
    )

    def _fake_update_hook(vault_path_, rel_path_, fm, fields_changed):
        return {
            "event_id": "ev_marie_lunch",
            "calendar_label": "alfred",
        }

    register_event_update_hook(_fake_update_hook)

    result_json = await conversation._execute_tool(
        "vault_edit",
        {
            "path": rel_path,
            "set_fields": {"location": "Bedford Cafe"},
        },
        vault_path=talker_config.vault.path,
        state=state_mgr,
        session=sess,
        config=talker_config,
    )

    result = json.loads(result_json)
    assert result["gcal_sync"] == {"status": "ok"}


@pytest.mark.asyncio
async def test_dispatcher_vault_edit_omits_gcal_sync_when_no_gcal_action(
    state_mgr, talker_config,
) -> None:
    """No-op hook return → tool_result has no ``gcal_sync`` key.

    Distinguishes "we tried and it didn't work" (failed) from
    "nothing tried to sync" (absent). The SKILL teaches Salem to
    narrate calendar status only when the key is present.
    """
    from alfred.vault.ops import register_event_update_hook

    sess = _make_session()
    state_mgr.set_active(1, sess.to_dict())

    rel_path = _seed_event(
        talker_config.vault.path,
        name="Vault Only Event",
        fields={"date": "2026-05-19"},  # no start/end, no gcal_event_id
    )

    def _fake_update_hook(vault_path_, rel_path_, fm, fields_changed):
        # Mirror the daemon closure's "no_gcal_event_id_and_no_times"
        # branch — it returns ``None`` (which _fire_*_hooks filters out).
        return None

    register_event_update_hook(_fake_update_hook)

    result_json = await conversation._execute_tool(
        "vault_edit",
        {
            "path": rel_path,
            "set_fields": {"location": "Halifax"},
        },
        vault_path=talker_config.vault.path,
        state=state_mgr,
        session=sess,
        config=talker_config,
    )

    result = json.loads(result_json)
    assert "gcal_sync" not in result, (
        "When no GCal action was attempted (hook returned None / no-op), "
        "the tool_result MUST NOT carry gcal_sync — the LLM should not "
        "volunteer calendar status in that case."
    )
