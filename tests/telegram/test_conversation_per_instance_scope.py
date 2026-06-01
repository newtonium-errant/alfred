"""Per-instance scope routing in the talker dispatcher.

Code-reviewer P0: prior to this fix the ``_execute_tool`` dispatcher in
``conversation.py`` hardcoded ``scope.check_scope("talker", ...)``
regardless of which instance was running. Hypatia ``document`` and
KAL-LE ``pattern`` creates were rejected at ``talker_types_only``
*before* the scope-aware ``_validate_type`` gate ever engaged. The bot
path was a release-blocker for Phase 1 Hypatia even though the CLI
agent path was already unblocked by commit b0217c2.

This file pins the new contract: the dispatcher reads
``config.instance.tool_set`` and routes ``check_scope`` + the
``vault_create`` / ``vault_edit`` ``scope=`` kwarg to the matching
scope key. The two-gate contract (``_validate_type`` + ``check_scope``
allowlist) propagates correctly on the bot path.

Coverage:
    * Salem (``tool_set="talker"``) → ``check_scope("talker", ...)``
      and ``ops.vault_create(scope="talker")`` — note creates work,
      pattern creates rejected at the talker allowlist.
    * KAL-LE (``tool_set="kalle"``) → ``check_scope("kalle", ...)`` —
      pattern creates work; task creates rejected (operational types
      are Salem's territory).
    * Hypatia (``tool_set="hypatia"``) → ``check_scope("hypatia", ...)``
      — document creates work; pattern creates rejected (kalle-only).
    * No config (``config=None``) → defaults to ``"talker"`` for
      backwards compatibility with legacy callers + tests.
    * Every ``InstanceConfig.tool_set`` value used in shipped configs
      is a valid scope key — guards against future config typos
      silently falling through to talker.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from alfred.telegram import conversation
from alfred.telegram.config import (
    AnthropicConfig,
    InstanceConfig,
    LoggingConfig,
    SessionConfig,
    STTConfig,
    TalkerConfig,
    VaultConfig,
)
from alfred.telegram.session import Session
from alfred.telegram.state import StateManager
from alfred.vault.scope import SCOPE_RULES


# --- Fixtures --------------------------------------------------------------


def _make_vault(tmp_path: Path) -> Path:
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir(exist_ok=True)
    # Mirror enough of the scaffold tree that vault_create can land
    # records of every type we exercise. ``daily`` added 2026-05-31
    # (c6) for the talker tier_curation pre-set tests below.
    for sub in (
        "session", "task", "note", "project",
        "pattern", "principle",
        "document", "concept", "source",
        "daily",
    ):
        (vault_dir / sub).mkdir(exist_ok=True)
    return vault_dir


def _make_config(
    tmp_path: Path,
    *,
    instance_name: str,
    tool_set: str,
) -> TalkerConfig:
    vault_dir = _make_vault(tmp_path)
    return TalkerConfig(
        bot_token="test-token",
        allowed_users=[1],
        anthropic=AnthropicConfig(api_key="test-key"),
        stt=STTConfig(api_key="test-stt"),
        session=SessionConfig(state_path=str(tmp_path / "talker_state.json")),
        vault=VaultConfig(path=str(vault_dir)),
        logging=LoggingConfig(file=str(tmp_path / "talker.log")),
        instance=InstanceConfig(name=instance_name, tool_set=tool_set),
    )


def _make_session(session_id: str = "scope-test-session") -> Session:
    now = datetime.now(timezone.utc)
    return Session(
        session_id=session_id,
        chat_id=1,
        started_at=now,
        last_message_at=now,
        model="claude-sonnet-4-6",
    )


# --- Salem (talker) --------------------------------------------------------


@pytest.mark.asyncio
async def test_salem_dispatcher_routes_to_talker_scope_for_note(tmp_path):
    """Salem (tool_set=talker) → note create succeeds via talker_types_only."""
    config = _make_config(tmp_path, instance_name="Salem", tool_set="talker")
    sess = _make_session()
    state = StateManager(config.session.state_path)

    result = await conversation._execute_tool(
        tool_name="vault_create",
        tool_input={
            "type": "note",
            "name": "Salem Test Note",
            "set_fields": {},
        },
        vault_path=config.vault.path,
        state=state,
        session=sess,
        config=config,
    )
    parsed = json.loads(result)
    assert "path" in parsed, parsed
    assert parsed["path"].startswith("note/")


@pytest.mark.asyncio
async def test_salem_dispatcher_rejects_pattern_create(tmp_path):
    """Salem must NOT be able to create kalle-only types."""
    config = _make_config(tmp_path, instance_name="Salem", tool_set="talker")
    sess = _make_session()
    state = StateManager(config.session.state_path)

    result = await conversation._execute_tool(
        tool_name="vault_create",
        tool_input={
            "type": "pattern",
            "name": "Bad Pattern",
            "set_fields": {},
        },
        vault_path=config.vault.path,
        state=state,
        session=sess,
        config=config,
    )
    parsed = json.loads(result)
    assert "error" in parsed
    # Talker scope's allowlist message — proves we routed to "talker".
    assert "scope denied" in parsed["error"].lower()
    assert "talker" in parsed["error"].lower()


# --- KAL-LE (kalle) --------------------------------------------------------


@pytest.mark.asyncio
async def test_kalle_dispatcher_routes_to_kalle_scope_for_pattern(tmp_path):
    """KAL-LE (tool_set=kalle) → pattern create succeeds via kalle_types_only."""
    config = _make_config(tmp_path, instance_name="KAL-LE", tool_set="kalle")
    sess = _make_session()
    state = StateManager(config.session.state_path)

    result = await conversation._execute_tool(
        tool_name="vault_create",
        tool_input={
            "type": "pattern",
            "name": "Test Pattern",
            "set_fields": {},
        },
        vault_path=config.vault.path,
        state=state,
        session=sess,
        config=config,
    )
    parsed = json.loads(result)
    assert "path" in parsed, parsed
    assert parsed["path"].startswith("pattern/")


@pytest.mark.asyncio
async def test_kalle_dispatcher_rejects_task_create(tmp_path):
    """KAL-LE has no operational types — task creates must be denied."""
    config = _make_config(tmp_path, instance_name="KAL-LE", tool_set="kalle")
    sess = _make_session()
    state = StateManager(config.session.state_path)

    result = await conversation._execute_tool(
        tool_name="vault_create",
        tool_input={
            "type": "task",
            "name": "Bad Task",
            "set_fields": {},
        },
        vault_path=config.vault.path,
        state=state,
        session=sess,
        config=config,
    )
    parsed = json.loads(result)
    assert "error" in parsed
    # The kalle allowlist message — proves we routed to "kalle", NOT
    # talker (which would have allowed task).
    assert "scope denied" in parsed["error"].lower()
    assert "kalle" in parsed["error"].lower()


# --- Hypatia (hypatia) -----------------------------------------------------


@pytest.mark.asyncio
async def test_hypatia_dispatcher_routes_to_hypatia_scope_for_document(tmp_path):
    """Hypatia (tool_set=hypatia) → document create succeeds via the
    hypatia scope's create allowlist + the scope-aware _validate_type."""
    config = _make_config(tmp_path, instance_name="Hypatia", tool_set="hypatia")
    sess = _make_session()
    state = StateManager(config.session.state_path)

    result = await conversation._execute_tool(
        tool_name="vault_create",
        tool_input={
            "type": "document",
            "name": "Test Document",
            "set_fields": {},
        },
        vault_path=config.vault.path,
        state=state,
        session=sess,
        config=config,
    )
    parsed = json.loads(result)
    assert "path" in parsed, parsed
    assert parsed["path"].startswith("document/")


@pytest.mark.asyncio
async def test_hypatia_dispatcher_rejects_pattern_create(tmp_path):
    """Hypatia must NOT be able to create kalle-only types."""
    config = _make_config(tmp_path, instance_name="Hypatia", tool_set="hypatia")
    sess = _make_session()
    state = StateManager(config.session.state_path)

    result = await conversation._execute_tool(
        tool_name="vault_create",
        tool_input={
            "type": "pattern",
            "name": "Bad Pattern",
            "set_fields": {},
        },
        vault_path=config.vault.path,
        state=state,
        session=sess,
        config=config,
    )
    parsed = json.loads(result)
    assert "error" in parsed
    assert "scope denied" in parsed["error"].lower()
    assert "hypatia" in parsed["error"].lower()


# --- Default / fallback ----------------------------------------------------


@pytest.mark.asyncio
async def test_dispatcher_defaults_to_talker_when_config_is_none(tmp_path):
    """Legacy callers that pass ``config=None`` must keep getting talker scope.

    A handful of older test paths and the early ``_execute_tool`` callers
    didn't plumb ``config`` through. They must continue to work as
    Salem-shaped — the fallback string ``"talker"`` is what makes that
    contract explicit.
    """
    vault_dir = _make_vault(tmp_path)
    sess = _make_session()
    state = StateManager(str(tmp_path / "state.json"))

    result = await conversation._execute_tool(
        tool_name="vault_create",
        tool_input={
            "type": "note",
            "name": "No Config Note",
            "set_fields": {},
        },
        vault_path=str(vault_dir),
        state=state,
        session=sess,
        config=None,
    )
    parsed = json.loads(result)
    assert "path" in parsed, parsed


# --- Config typo guard (code-reviewer P2) ---------------------------------


def test_every_shipped_tool_set_is_a_valid_scope_key():
    """Every ``tool_set`` value used in a shipped config maps to a real
    ``SCOPE_RULES`` entry.

    Without this assertion a typo in ``config.<instance>.yaml`` (e.g.
    ``tool_set: hypatya``) would silently fall through to the
    dispatcher's ``"talker"`` fallback — exactly the silent-misroute
    failure mode that motivated this fix in the first place.

    The list below mirrors the live shipped configs and the
    ``tools_for_set`` / ``VAULT_TOOLS_BY_SET`` registry. Adding a new
    instance? Add its tool_set string here AND add it to
    ``SCOPE_RULES`` — bouncing this test fails loud at CI time.
    """
    shipped_tool_sets = {"talker", "kalle", "hypatia"}
    for tool_set in shipped_tool_sets:
        assert tool_set in SCOPE_RULES, (
            f"tool_set {tool_set!r} has no SCOPE_RULES entry — the "
            f"dispatcher's check_scope({tool_set!r}, ...) call would "
            f"raise ScopeError on every tool invocation."
        )


# --- Phase 2B B2 (2026-05-30): Conversational routine creation -----------


@pytest.mark.asyncio
async def test_salem_dispatcher_creates_routine_via_vault_create(tmp_path):
    """End-to-end happy path: Salem creates a ``routine`` record via the
    ``vault_create`` dispatcher.

    The route is identical to ``test_salem_dispatcher_routes_to_talker_
    scope_for_note`` (same tool, same path), but exercises the new B2
    scope widening — ``routine`` was added to ``TALKER_CREATE_TYPES``.

    Required frontmatter for routine records: ``name`` (set
    automatically by ops.vault_create from the ``name`` arg),
    ``cadence`` (dict — top-level "is the routine firing today"
    rhythm), ``items`` (list of dicts — the operational unit). The
    ``set_fields`` payload supplies the latter two.

    Asserted:
      * Result has ``path`` field starting with ``routine/`` (Salem's
        canonical routine directory per schema.py TYPE_DIRECTORY).
      * File actually landed on disk at the expected path.
    """
    config = _make_config(
        tmp_path, instance_name="Salem", tool_set="talker",
    )
    sess = _make_session()
    state = StateManager(config.session.state_path)

    result = await conversation._execute_tool(
        tool_name="vault_create",
        tool_input={
            "type": "routine",
            "name": "B2 Test Routine",
            "set_fields": {
                # Top-level cadence — fires daily so the aggregator
                # always evaluates per-item rules. Mirrors the
                # production Core Daily routine's cadence shape.
                "cadence": {"type": "daily"},
                # Single item with a soft-cadence target — the
                # canonical "walk the dog every 3 days" shape that
                # the SKILL's worked example A documents.
                "items": [
                    {
                        "text": "Test item",
                        "priority": "aspirational",
                        "target_cadence_days": 3,
                    },
                ],
            },
        },
        vault_path=config.vault.path,
        state=state,
        session=sess,
        config=config,
    )
    parsed = json.loads(result)
    # Success path — no scope/validator error in the response.
    assert "error" not in parsed, parsed
    assert "path" in parsed, parsed
    assert parsed["path"].startswith("routine/"), parsed
    # File materialized on disk.
    created = Path(config.vault.path) / parsed["path"]
    assert created.is_file(), f"routine record not on disk at {created}"


@pytest.mark.asyncio
async def test_salem_dispatcher_creates_routine_with_due_pattern_item(
    tmp_path,
):
    """Hard-cadence path: routine item carries a ``due_pattern`` dict
    instead of ``target_cadence_days``. Mirrors the SKILL's worked
    example C shape (biweekly with anchor + escalate_at_days).

    Pins that the dispatcher passes nested dicts through without
    flattening — the routine aggregator's ``Item.from_dict`` parses
    the per-item ``due_pattern`` sub-dict directly.
    """
    config = _make_config(
        tmp_path, instance_name="Salem", tool_set="talker",
    )
    sess = _make_session()
    state = StateManager(config.session.state_path)

    result = await conversation._execute_tool(
        tool_name="vault_create",
        tool_input={
            "type": "routine",
            "name": "Hard Cadence Test",
            "set_fields": {
                "cadence": {"type": "daily"},
                "items": [
                    {
                        "text": "Garbage out",
                        "priority": "critical",
                        # Source-of-truth verified against
                        # alfred.routine.config.DuePattern.from_dict:
                        # singular ``day`` (NOT plural ``days``);
                        # value is a 3-letter weekday name.
                        "due_pattern": {
                            "type": "biweekly",
                            "day": "thu",
                            "anchor": "2026-05-28",
                        },
                        "escalate_at_days": 1,
                    },
                ],
            },
        },
        vault_path=config.vault.path,
        state=state,
        session=sess,
        config=config,
    )
    parsed = json.loads(result)
    assert "error" not in parsed, parsed
    assert parsed["path"].startswith("routine/"), parsed


@pytest.mark.asyncio
async def test_salem_dispatcher_rejects_routine_without_required_fields(
    tmp_path,
):
    """Routine record created WITHOUT the required ``cadence`` /
    ``items`` frontmatter is rejected at the schema-validator gate.

    Pins that the validator's required-field check applies on the
    talker create path — the scope widening doesn't bypass schema
    validation. Even though the routine schema requires ``name`` +
    ``cadence`` + ``items``, this test omits both ``cadence`` and
    ``items`` and asserts the error mentions the missing fields."""
    config = _make_config(
        tmp_path, instance_name="Salem", tool_set="talker",
    )
    sess = _make_session()
    state = StateManager(config.session.state_path)

    result = await conversation._execute_tool(
        tool_name="vault_create",
        tool_input={
            "type": "routine",
            "name": "Missing Required Fields",
            "set_fields": {},  # No cadence, no items.
        },
        vault_path=config.vault.path,
        state=state,
        session=sess,
        config=config,
    )
    parsed = json.loads(result)
    # Validator-layer error returned to the model — the scope-layer
    # didn't suppress it. Error message names the missing required
    # field(s) so Salem can ask the operator for the missing info.
    assert "error" in parsed, parsed
    # ``cadence`` is among the required fields per schema.py:807;
    # the validator's message names it explicitly. (The exact phrasing
    # is "missing required field" + the field name.)
    err = parsed["error"].lower()
    assert "cadence" in err or "items" in err or "required" in err, (
        f"Expected validator error mentioning cadence/items/required; got: {parsed!r}"
    )


@pytest.mark.asyncio
async def test_routine_in_vault_create_tool_schema_enum(tmp_path):
    """The ``vault_create`` tool schema enum must include ``routine``
    so the Anthropic SDK doesn't validation-reject the model's
    ``type: routine`` payload BEFORE the dispatcher ever sees it.

    Pre-B2 the enum lagged ``TALKER_CREATE_TYPES`` (the enum listed
    14 types; the constant had 15 incl. preference; B2 adds the 16th:
    routine). The enum is a soft rail in the schema layer — the
    scope check is the load-bearing gate — but a missing enum entry
    drops the payload at the SDK validator level with no model-
    visible explanation. Pin both the constant and the enum stay in
    lockstep.

    **Drift-proof shape (code-reviewer NOTE 2026-05-30):** the
    assertion checks the full set-difference between
    ``TALKER_CREATE_TYPES`` and the enum, NOT just a literal
    ``"routine" in enum_values``. The literal-membership shape pinned
    THIS addition but would have let the next constant addition
    silently lag the enum and produce the same silent-SDK-reject
    failure pattern the pre-2026-05-30 enum lag produced for
    ``org`` / ``location`` adds. The set-difference shape surfaces
    ANY missing type, including future additions, with a message
    naming the gap.
    """
    from alfred.vault.scope import TALKER_CREATE_TYPES

    # The schema lives at module-import time, no fixture needed.
    schema = next(
        t for t in conversation.TALKER_VAULT_TOOLS
        if t["name"] == "vault_create"
    )
    enum_values = schema["input_schema"]["properties"]["type"]["enum"]
    missing = TALKER_CREATE_TYPES - set(enum_values)
    assert not missing, (
        f"vault_create tool schema enum lags TALKER_CREATE_TYPES "
        f"(missing: {sorted(missing)!r}). Every type in "
        f"TALKER_CREATE_TYPES MUST also appear in the enum, or the "
        f"Anthropic SDK validator drops the model's payload before "
        f"the dispatcher ever sees it — silent failure with no "
        f"model-visible explanation. Update the enum in "
        f"src/alfred/telegram/conversation.py to match."
    )


# --- c6 (2026-05-31): talker tier_curation pre-set on daily/ records -----


@pytest.mark.asyncio
async def test_talker_can_create_future_daily_with_just_tier_curation(
    tmp_path,
):
    """End-to-end happy path: vault_create on ``daily/<future-date>.md``
    with ONLY ``tier_curation`` set_fields lands successfully.

    Per c6 spec: operator says "set tomorrow's tier list: T1 = X" →
    talker dispatches vault_create with type=daily, name=<tomorrow-iso>,
    set_fields={tier_curation: {...}}, body=None. The conversation.py
    per-type gate admits the write; the file lands at
    ``daily/<future-date>.md`` with the curation block in frontmatter.
    The aggregator's next fire on that date will preserve the block."""
    from datetime import date, timedelta
    config = _make_config(
        tmp_path, instance_name="Salem", tool_set="talker",
    )
    sess = _make_session()
    state = StateManager(config.session.state_path)

    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    tier_block = {
        "t1": ["Drive Pierre to soccer", "Submit RRTS invoice"],
        "t2": ["Call mom"],
        "t3": [],
    }
    result = await conversation._execute_tool(
        tool_name="vault_create",
        tool_input={
            "type": "daily",
            "name": tomorrow,
            "set_fields": {"tier_curation": tier_block},
        },
        vault_path=config.vault.path,
        state=state,
        session=sess,
        config=config,
    )
    parsed = json.loads(result)
    assert "path" in parsed, parsed
    assert parsed["path"] == f"daily/{tomorrow}.md"

    # On-disk verification: the file exists with the curation block.
    import frontmatter
    post = frontmatter.load(
        str(Path(config.vault.path) / f"daily/{tomorrow}.md"),
    )
    assert post.metadata.get("tier_curation") == tier_block
    assert post.metadata.get("type") == "daily"


@pytest.mark.asyncio
async def test_talker_dispatcher_rejects_daily_with_aggregator_owned_field(
    tmp_path,
):
    """Field-allowlist enforcement at the dispatch layer: an LLM
    attempting to pre-set ``routines_contributing`` (aggregator-owned)
    alongside ``tier_curation`` is rejected scope_denied.

    Pins the per-type field gate. Without it, the talker could pre-
    write any frontmatter field on a daily record and the aggregator
    would either preserve garbage or stomp it inconsistently."""
    from datetime import date, timedelta
    config = _make_config(
        tmp_path, instance_name="Salem", tool_set="talker",
    )
    sess = _make_session()
    state = StateManager(config.session.state_path)
    tomorrow = (date.today() + timedelta(days=1)).isoformat()

    result = await conversation._execute_tool(
        tool_name="vault_create",
        tool_input={
            "type": "daily",
            "name": tomorrow,
            "set_fields": {
                "tier_curation": {"t1": ["X"]},
                "routines_contributing": ["faked"],  # aggregator-owned
            },
        },
        vault_path=config.vault.path,
        state=state,
        session=sess,
        config=config,
    )
    parsed = json.loads(result)
    assert "error" in parsed, parsed
    assert "scope denied" in parsed["error"].lower()
    # The error names the rejected field so the operator (or model
    # reading the error response) can correct the next attempt.
    assert "routines_contributing" in parsed["error"]


@pytest.mark.asyncio
async def test_talker_dispatcher_rejects_daily_with_past_date(tmp_path):
    """Date-future gate at the dispatch layer: rejecting past dates
    prevents the talker from stomping a historical daily/ record the
    aggregator already wrote. Today is allowed (the aggregator may
    not have fired yet pre-05:59 ADT); only strictly-past dates fail."""
    from datetime import date, timedelta
    config = _make_config(
        tmp_path, instance_name="Salem", tool_set="talker",
    )
    sess = _make_session()
    state = StateManager(config.session.state_path)
    yesterday = (date.today() - timedelta(days=1)).isoformat()

    result = await conversation._execute_tool(
        tool_name="vault_create",
        tool_input={
            "type": "daily",
            "name": yesterday,
            "set_fields": {"tier_curation": {"t1": ["X"]}},
        },
        vault_path=config.vault.path,
        state=state,
        session=sess,
        config=config,
    )
    parsed = json.loads(result)
    assert "error" in parsed, parsed
    assert "scope denied" in parsed["error"].lower()
    # Error message names the date so the model can fix the input.
    assert yesterday in parsed["error"]


@pytest.mark.asyncio
async def test_talker_dispatcher_rejects_daily_with_body_content(tmp_path):
    """Defense-in-depth: body content rejected on daily/ creates.
    The aggregator owns the body (``render_daily_body``); pre-writing
    body would either be stomped by the next aggregator fire or
    survive only until the next stomping write. Either way the
    operator-facing semantics are confused; reject fail-loud."""
    from datetime import date, timedelta
    config = _make_config(
        tmp_path, instance_name="Salem", tool_set="talker",
    )
    sess = _make_session()
    state = StateManager(config.session.state_path)
    tomorrow = (date.today() + timedelta(days=1)).isoformat()

    result = await conversation._execute_tool(
        tool_name="vault_create",
        tool_input={
            "type": "daily",
            "name": tomorrow,
            "set_fields": {"tier_curation": {"t1": ["X"]}},
            "body": "# This body will be stomped\n\nDon't write here.\n",
        },
        vault_path=config.vault.path,
        state=state,
        session=sess,
        config=config,
    )
    parsed = json.loads(result)
    assert "error" in parsed
    assert "aggregator-owned" in parsed["error"].lower()


@pytest.mark.asyncio
async def test_talker_can_edit_existing_daily_tier_curation(tmp_path):
    """End-to-end: vault_edit on an existing daily/<date>.md record
    succeeds for the tier_curation field. Mirrors the create test but
    exercises the edit branch."""
    from datetime import date, timedelta
    import frontmatter
    config = _make_config(
        tmp_path, instance_name="Salem", tool_set="talker",
    )
    sess = _make_session()
    state = StateManager(config.session.state_path)
    tomorrow = (date.today() + timedelta(days=1)).isoformat()

    # Pre-seed the daily record (simulate a prior talker pre-write
    # OR an aggregator-written file).
    daily_path = Path(config.vault.path) / f"daily/{tomorrow}.md"
    post = frontmatter.Post(
        "",
        type="daily",
        date=tomorrow,
        tier_curation={"t1": ["Initial"], "t2": [], "t3": []},
    )
    daily_path.write_text(
        frontmatter.dumps(post) + "\n", encoding="utf-8",
    )

    # Edit only the tier_curation field — should succeed.
    new_block = {"t1": ["Updated"], "t2": ["Added"], "t3": []}
    result = await conversation._execute_tool(
        tool_name="vault_edit",
        tool_input={
            "path": f"daily/{tomorrow}.md",
            "set_fields": {"tier_curation": new_block},
        },
        vault_path=config.vault.path,
        state=state,
        session=sess,
        config=config,
    )
    parsed = json.loads(result)
    assert "path" in parsed, parsed
    # Verify on-disk frontmatter overwritten.
    post_after = frontmatter.load(str(daily_path))
    assert post_after.metadata["tier_curation"] == new_block


@pytest.mark.asyncio
async def test_talker_dispatcher_rejects_edit_of_non_tier_curation_on_daily(
    tmp_path,
):
    """vault_edit on a daily/ record can ONLY touch tier_curation;
    attempting to set ``routines_contributing`` via edit is rejected
    at the dispatch layer."""
    from datetime import date, timedelta
    import frontmatter
    config = _make_config(
        tmp_path, instance_name="Salem", tool_set="talker",
    )
    sess = _make_session()
    state = StateManager(config.session.state_path)
    tomorrow = (date.today() + timedelta(days=1)).isoformat()

    daily_path = Path(config.vault.path) / f"daily/{tomorrow}.md"
    post = frontmatter.Post(
        "",
        type="daily",
        date=tomorrow,
        tier_curation={"t1": [], "t2": [], "t3": []},
    )
    daily_path.write_text(
        frontmatter.dumps(post) + "\n", encoding="utf-8",
    )

    result = await conversation._execute_tool(
        tool_name="vault_edit",
        tool_input={
            "path": f"daily/{tomorrow}.md",
            "set_fields": {
                "tier_curation": {"t1": ["X"]},
                "routines_contributing": ["faked"],
            },
        },
        vault_path=config.vault.path,
        state=state,
        session=sess,
        config=config,
    )
    parsed = json.loads(result)
    assert "error" in parsed
    assert "scope denied" in parsed["error"].lower()
    assert "routines_contributing" in parsed["error"]


def test_aggregator_preserves_talker_pre_set_tier_curation(tmp_path):
    """Integration: pre-write a daily/<date>.md with a tier_curation
    block (simulating talker pre-set), then run the aggregator on the
    same date; assert the curation block survives the aggregator's
    overwrite of the file.

    This is the LOAD-BEARING contract for c6: the whole point of
    the talker pre-set is that the aggregator's 05:59 ADT fire
    preserves it. ``_load_existing_tier_curation`` (aggregator.py:828)
    is the read-side; this test pins it round-trips through the full
    aggregator fire."""
    import frontmatter
    from alfred.routine import aggregator as agg_mod
    from alfred.routine.config import OutputConfig, RoutineConfig

    vault = _make_vault(tmp_path)

    # Pre-write a tier_curation block as the talker would have.
    target_date = "2026-06-15"
    daily_path = vault / f"daily/{target_date}.md"
    pre_set_curation = {
        "t1": ["Drive Pierre to soccer", "Submit RRTS invoice"],
        "t2": ["Call mom"],
        "t3": [],
    }
    post = frontmatter.Post(
        "",
        type="daily",
        date=target_date,
        tier_curation=pre_set_curation,
    )
    daily_path.write_text(
        frontmatter.dumps(post) + "\n", encoding="utf-8",
    )

    # Run the aggregator on the same date. Minimal config — empty
    # routine directory means no items, but the aggregator still
    # writes the daily file. The key contract: tier_curation MUST
    # appear on the post-aggregator frontmatter.
    routine_dir = vault / "routine"
    routine_dir.mkdir(exist_ok=True)  # empty — no routine records
    config = RoutineConfig(
        vault_path=str(vault),
        output=OutputConfig(directory="daily", name_template="{date}"),
    )

    from datetime import date as date_cls
    agg_mod.run_aggregator_once(
        config=config,
        today=date_cls.fromisoformat(target_date),
    )

    # Re-load the file — tier_curation must survive.
    post_after = frontmatter.load(str(daily_path))
    assert post_after.metadata.get("tier_curation") == pre_set_curation, (
        f"aggregator stomped pre-set tier_curation; got: "
        f"{post_after.metadata.get('tier_curation')!r}"
    )
