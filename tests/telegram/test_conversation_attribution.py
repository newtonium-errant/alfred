"""Tests for c2 of the calibration audit gap arc — talker dispatcher
wires ``vault_create`` and ``vault_edit`` through the attribution-marker
helpers in ``alfred.vault.attribution``.

Covers:
    * ``vault_create`` with body produces wrapped markers + audit entry.
    * ``vault_create`` without body (template default) does NOT add markers.
    * ``vault_edit body_append`` wraps only the appended fragment.
    * Existing ``attribution_audit`` entries on a record are preserved.
    * Section title falls back through name → first heading → placeholder.
    * Reason carries the session id for trace.
    * Smoke: simulated turn lands a note with both BEGIN_INFERRED in body
      and the audit list in frontmatter.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import frontmatter
import pytest

from alfred.telegram import conversation
from alfred.telegram.session import Session


def _make_session(session_id: str = "abc12345-0000-0000-0000-000000000000") -> Session:
    now = datetime.now(timezone.utc)
    return Session(
        session_id=session_id,
        chat_id=1,
        started_at=now,
        last_message_at=now,
        model="claude-sonnet-4-6",
    )


def _read_record(vault_path: str, rel_path: str) -> tuple[dict, str]:
    full = Path(vault_path) / rel_path
    post = frontmatter.load(str(full))
    return dict(post.metadata), post.content


# --- vault_create ---------------------------------------------------------


@pytest.mark.asyncio
async def test_vault_create_with_body_wraps_in_inferred_markers(
    state_mgr, talker_config,
) -> None:
    sess = _make_session()
    state_mgr.set_active(1, sess.to_dict())

    body = "## Sender-Specific Overrides\n\nSubstack → ignore by default.\n"
    result_json = await conversation._execute_tool(
        "vault_create",
        {
            "type": "note",
            "name": "Email Triage Override",
            "set_fields": {},
            "body": body,
        },
        vault_path=talker_config.vault.path,
        state=state_mgr,
        session=sess,
        config=talker_config,
    )
    result = json.loads(result_json)
    assert "path" in result, result

    fm, content = _read_record(talker_config.vault.path, result["path"])

    # Body carries the BEGIN/END marker pair.
    assert "<!-- BEGIN_INFERRED" in content
    assert "<!-- END_INFERRED" in content
    # Original body content survived inside the wrapping.
    assert "Substack → ignore" in content

    # Frontmatter audit list has exactly one entry, well-formed.
    audit = fm.get("attribution_audit")
    assert isinstance(audit, list)
    assert len(audit) == 1
    entry = audit[0]
    assert entry["agent"] == talker_config.instance.name.lower()
    assert entry["section_title"] == "Email Triage Override"  # uses ``name``
    assert entry["confirmed_by_andrew"] is False
    assert entry["confirmed_at"] is None
    # Reason carries the session_id for trace.
    assert sess.session_id in entry["reason"]
    # marker_id matches both BEGIN/END lines.
    marker_id = entry["marker_id"]
    assert content.count(marker_id) == 2
    assert marker_id.startswith("inf-")


@pytest.mark.asyncio
async def test_vault_create_without_body_skips_marker_wrapping(
    state_mgr, talker_config,
) -> None:
    """Template-default body path should NOT add markers — there's no
    inferred prose, just template scaffold."""
    sess = _make_session()
    state_mgr.set_active(1, sess.to_dict())

    result_json = await conversation._execute_tool(
        "vault_create",
        {
            "type": "note",
            "name": "Template Default Note",
            "set_fields": {},
            # No ``body`` — vault_create falls through to template default.
        },
        vault_path=talker_config.vault.path,
        state=state_mgr,
        session=sess,
        config=talker_config,
    )
    result = json.loads(result_json)
    fm, content = _read_record(talker_config.vault.path, result["path"])
    assert "BEGIN_INFERRED" not in content
    assert "attribution_audit" not in fm


# --- vault_edit body_append ----------------------------------------------


@pytest.mark.asyncio
async def test_vault_edit_body_append_wraps_only_appended_fragment(
    state_mgr, talker_config,
) -> None:
    """The pre-existing record body must NOT get wrapped — only the
    appended fragment carries the inferred marker."""
    sess = _make_session()
    state_mgr.set_active(1, sess.to_dict())

    # Seed an existing note with Andrew-typed body content.
    seed_path = Path(talker_config.vault.path) / "note" / "Existing Note.md"
    seed_path.write_text(
        "---\ntype: note\nname: Existing Note\ncreated: '2026-04-23'\n---\n\n"
        "# Existing Note\n\nAndrew typed this.\n",
        encoding="utf-8",
    )

    appended = "## New Section\n\nSalem inferred this from chat.\n"
    result_json = await conversation._execute_tool(
        "vault_edit",
        {
            "path": "note/Existing Note.md",
            "body_append": appended,
        },
        vault_path=talker_config.vault.path,
        state=state_mgr,
        session=sess,
        config=talker_config,
    )
    result = json.loads(result_json)
    assert "path" in result, result

    fm, content = _read_record(talker_config.vault.path, "note/Existing Note.md")

    # Pre-existing body content survives unwrapped.
    assert "Andrew typed this." in content
    # Marker lines exist exactly once each.
    assert content.count("BEGIN_INFERRED") == 1
    assert content.count("END_INFERRED") == 1
    # The Andrew-typed paragraph sits BEFORE the BEGIN marker.
    begin_idx = content.find("BEGIN_INFERRED")
    andrew_idx = content.find("Andrew typed this.")
    assert andrew_idx < begin_idx
    # Salem's appended content sits inside the marker pair.
    end_idx = content.find("END_INFERRED")
    assert content.find("Salem inferred this") > begin_idx
    assert content.find("Salem inferred this") < end_idx

    audit = fm.get("attribution_audit")
    assert isinstance(audit, list) and len(audit) == 1
    # Section title comes from the heading in the fragment.
    assert audit[0]["section_title"] == "New Section"


@pytest.mark.asyncio
async def test_vault_edit_preserves_existing_attribution_audit(
    state_mgr, talker_config,
) -> None:
    """A second body_append edit must not clobber the prior audit entry."""
    sess = _make_session()
    state_mgr.set_active(1, sess.to_dict())

    # Seed a note that already carries one attribution_audit entry.
    seed_path = Path(talker_config.vault.path) / "note" / "Seeded.md"
    seed_path.write_text(
        "---\n"
        "type: note\n"
        "name: Seeded\n"
        "created: '2026-04-23'\n"
        "attribution_audit:\n"
        "  - marker_id: inf-20260420-salem-aaaaaa\n"
        "    agent: salem\n"
        "    date: '2026-04-20T00:00:00+00:00'\n"
        "    section_title: Old Section\n"
        "    reason: prior write\n"
        "    confirmed_by_andrew: false\n"
        "    confirmed_at: null\n"
        "---\n\n"
        "Body.\n",
        encoding="utf-8",
    )

    appended = "## Latest Section\n\nNew inferred bullet.\n"
    await conversation._execute_tool(
        "vault_edit",
        {
            "path": "note/Seeded.md",
            "body_append": appended,
        },
        vault_path=talker_config.vault.path,
        state=state_mgr,
        session=sess,
        config=talker_config,
    )

    fm, _ = _read_record(talker_config.vault.path, "note/Seeded.md")
    audit = fm.get("attribution_audit")
    assert isinstance(audit, list)
    # Original entry survives + new entry appended.
    assert len(audit) == 2
    ids = {e["marker_id"] for e in audit}
    assert "inf-20260420-salem-aaaaaa" in ids


@pytest.mark.asyncio
async def test_vault_edit_without_body_append_does_not_touch_audit(
    state_mgr, talker_config,
) -> None:
    """A frontmatter-only edit (set_fields, no body_append) shouldn't
    write attribution_audit — there's no body content to attribute."""
    sess = _make_session()
    state_mgr.set_active(1, sess.to_dict())

    seed_path = Path(talker_config.vault.path) / "note" / "PlainEdit.md"
    seed_path.write_text(
        "---\ntype: note\nname: PlainEdit\ncreated: '2026-04-23'\n"
        "tags: []\n---\n\nBody.\n",
        encoding="utf-8",
    )

    await conversation._execute_tool(
        "vault_edit",
        {
            "path": "note/PlainEdit.md",
            "set_fields": {"status": "active"},
        },
        vault_path=talker_config.vault.path,
        state=state_mgr,
        session=sess,
        config=talker_config,
    )

    fm, content = _read_record(talker_config.vault.path, "note/PlainEdit.md")
    assert fm.get("status") == "active"
    assert "attribution_audit" not in fm
    assert "BEGIN_INFERRED" not in content


# --- Helper coverage ------------------------------------------------------


def test_section_title_for_create_prefers_name() -> None:
    title = conversation._section_title_for_create(
        "My Note", "## Heading\nbody",
    )
    assert title == "My Note"


def test_section_title_for_create_falls_back_to_first_heading() -> None:
    title = conversation._section_title_for_create("", "## My Heading\nbody")
    assert title == "My Heading"


def test_section_title_for_create_falls_back_to_placeholder() -> None:
    title = conversation._section_title_for_create("", "no headings here")
    assert title == "talker-write"


def test_section_title_for_edit_append_uses_fragment_heading() -> None:
    title = conversation._section_title_for_edit_append(
        "## Override\nbody", "process/Email Triage Rules.md",
    )
    assert title == "Override"


def test_section_title_for_edit_append_falls_back_to_file_stem() -> None:
    title = conversation._section_title_for_edit_append(
        "no heading just body", "process/Email Triage Rules.md",
    )
    assert title == "Email Triage Rules"


def test_agent_slug_lowercases_instance_name() -> None:
    from alfred.telegram.config import InstanceConfig, TalkerConfig
    cfg = TalkerConfig(instance=InstanceConfig(name="Salem"))
    assert conversation._agent_slug(cfg) == "salem"
    cfg_kalle = TalkerConfig(instance=InstanceConfig(name="KAL-LE"))
    assert conversation._agent_slug(cfg_kalle) == "kal-le"


def test_agent_slug_default_when_config_none() -> None:
    assert conversation._agent_slug(None) == "talker"


# --- Smoke: end-to-end shape on a fresh note ------------------------------


@pytest.mark.asyncio
async def test_smoke_conversation_turn_creates_attributed_note(
    state_mgr, talker_config,
) -> None:
    """A representative talker vault_create call lands a note that carries
    BOTH the BEGIN_INFERRED marker in body AND the audit list in
    frontmatter — the two-layer marker contract."""
    sess = _make_session()
    state_mgr.set_active(1, sess.to_dict())

    result_json = await conversation._execute_tool(
        "vault_create",
        {
            "type": "note",
            "name": "Smoke Inferred Note",
            "set_fields": {},
            "body": "Synthesised summary of what Andrew said.\n",
        },
        vault_path=talker_config.vault.path,
        state=state_mgr,
        session=sess,
        config=talker_config,
    )
    result = json.loads(result_json)
    note_path = Path(talker_config.vault.path) / result["path"]
    raw = note_path.read_text(encoding="utf-8")

    # Both layers present.
    assert "BEGIN_INFERRED" in raw
    assert "attribution_audit:" in raw
    # Reply to Andrew (the talker's own message text) is OUT of scope —
    # this test only asserts that the marker is body-content only. We
    # check the negative by asserting the marker appears only inside the
    # body region of the markdown, never in the YAML frontmatter section.
    yaml_end = raw.index("---\n", 4)  # second '---' closes frontmatter
    yaml_block = raw[: yaml_end + 4]
    body_block = raw[yaml_end + 4:]
    assert "BEGIN_INFERRED" not in yaml_block
    assert "BEGIN_INFERRED" in body_block
