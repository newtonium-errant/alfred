"""Tests for the ``gcal_list_events`` talker tool — Phase A capability-audit
close 2026-05-06 (closes the "Salem said 'I have no calendar read access'"
honesty gap from conversation ``0e52c745``).

Coverage:

* Tool surface gating (``tools_for_set``):
    - ``gcal_enabled=False`` (default) → tool absent from every set
    - ``gcal_enabled=True`` → tool present in talker / kalle / hypatia
      sets (per-instance opt-in via the operator's gcal block)

* Schema shape:
    - ``calendar`` enum strictly admits ``alfred`` / ``primary``
    - top-level schema carries no oneOf / allOf / anyOf (the
      2026-05-06 P0 regression pin extends to the new tool too)

* Dispatch:
    - calendar="alfred" → ``alfred_calendar_id`` resolved + passed
    - calendar="primary" → ``primary_calendar_id`` resolved + passed
    - chat-shape pruning: title / start / end / location /
      description-truncated; ``raw`` blob NOT echoed back
    - empty result → ``{"events": []}`` + structured "ran, nothing
      to do" log per ``feedback_intentionally_left_blank.md``
    - GCal API errors caught + returned as ``{"error": ...}``
      (does NOT propagate)

* Validation rejection (returns to model as ``{"error": ...}``):
    - missing / invalid calendar alias
    - missing / non-ISO start / end
    - tz-naive start / end (GCal requires tz-aware)
    - GCal disabled in config
    - calendar ID empty in config

Tests do not exercise the real GCal API. They stub the
:class:`GCalClient` constructor + ``list_events`` to verify the
dispatcher's translation layer.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import yaml

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


# --- Fixtures --------------------------------------------------------------


def _config_with_path(
    tmp_path: Path,
    *,
    config_path: str,
    tool_set: str = "talker",
) -> TalkerConfig:
    """TalkerConfig pointing at a real config.yaml file (so the lazy
    ``config.config_path`` resolver inside the dispatch helper finds
    something to load)."""
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir(exist_ok=True)
    return TalkerConfig(
        bot_token="x",
        allowed_users=[1],
        primary_users=["person/Andrew Newton"],
        anthropic=AnthropicConfig(
            api_key="DUMMY_ANTHROPIC_TEST_KEY", model="claude-opus-4-7",
        ),
        stt=STTConfig(
            api_key="DUMMY_STT_TEST_KEY", model="whisper-large-v3",
        ),
        session=SessionConfig(state_path=str(tmp_path / "state.json")),
        vault=VaultConfig(path=str(vault_dir)),
        logging=LoggingConfig(file=str(tmp_path / "talker.log")),
        instance=InstanceConfig(
            name="Salem", canonical="salem", tool_set=tool_set,
        ),
        config_path=config_path,
    )


def _write_gcal_config(
    tmp_path: Path,
    *,
    enabled: bool = True,
    alfred_calendar_id: str = "alfred-calendar-id-FAKE",
    primary_calendar_id: str = "primary-calendar-id-FAKE",
) -> Path:
    """Write a minimal config.yaml with a gcal block. Returns the path."""
    config_path = tmp_path / "config.yaml"
    payload = {
        "gcal": {
            "enabled": enabled,
            "credentials_path": str(tmp_path / "creds.json"),
            "token_path": str(tmp_path / "token.json"),
            "alfred_calendar_id": alfred_calendar_id,
            "primary_calendar_id": primary_calendar_id,
        },
    }
    config_path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return config_path


def _session() -> Session:
    now = datetime(2026, 5, 6, tzinfo=timezone.utc)
    return Session(
        session_id="sess-gcal-1",
        chat_id=1,
        started_at=now,
        last_message_at=now,
        model="claude-opus-4-7",
    )


def _make_event(
    *,
    title: str = "Chiropractor",
    start: datetime | None = None,
    end: datetime | None = None,
    description: str = "",
    location: str = "",
):
    """Build a stub GCalEvent-shaped object (the real dataclass is
    declared in ``alfred.integrations.gcal`` but importing it would
    add a setup dep on Google's libraries)."""
    if start is None:
        start = datetime(2026, 5, 7, 14, 0, tzinfo=timezone.utc)
    if end is None:
        end = datetime(2026, 5, 7, 15, 0, tzinfo=timezone.utc)
    ev = MagicMock()
    ev.title = title
    ev.start = start
    ev.end = end
    ev.description = description
    ev.raw = {"location": location} if location else {}
    return ev


# --- Tool surface registration --------------------------------------------


def test_gcal_tool_absent_when_gcal_disabled():
    """Default behaviour: ``gcal_enabled=False`` → tool not in any set.
    Mirrors the honesty contract — model can't see a tool that isn't
    wired."""
    for set_name in ("talker", "kalle", "hypatia"):
        names = {t["name"] for t in conversation.tools_for_set(set_name)}
        assert "gcal_list_events" not in names, (
            f"gcal_list_events leaked into {set_name!r} when "
            f"gcal_enabled=False (default)"
        )


def test_gcal_tool_present_when_gcal_enabled_talker():
    """Salem (talker) opted in via ``gcal.enabled: true`` → tool surfaced."""
    names = {
        t["name"]
        for t in conversation.tools_for_set("talker", gcal_enabled=True)
    }
    assert "gcal_list_events" in names


def test_gcal_tool_present_when_gcal_enabled_kalle():
    """Per-instance opt-in: KAL-LE could enable GCal in future config
    (Phase B+); the tool surface registry must support it."""
    names = {
        t["name"]
        for t in conversation.tools_for_set("kalle", gcal_enabled=True)
    }
    assert "gcal_list_events" in names


def test_gcal_tool_present_when_gcal_enabled_hypatia():
    """Same opt-in path for Hypatia."""
    names = {
        t["name"]
        for t in conversation.tools_for_set("hypatia", gcal_enabled=True)
    }
    assert "gcal_list_events" in names


def test_gcal_tool_does_not_mutate_base_registry():
    """``tools_for_set(..., gcal_enabled=True)`` must NOT append to the
    underlying registry. A subsequent call without the flag must see
    the unaugmented list."""
    with_flag = conversation.tools_for_set("talker", gcal_enabled=True)
    without_flag = conversation.tools_for_set("talker", gcal_enabled=False)
    assert "gcal_list_events" in {t["name"] for t in with_flag}
    assert "gcal_list_events" not in {t["name"] for t in without_flag}


# --- Schema shape ---------------------------------------------------------


def test_gcal_tool_schema_required_fields():
    """Schema requires ``calendar`` / ``start`` / ``end``; calendar is
    a strict ``alfred``/``primary`` enum."""
    tools = conversation.tools_for_set("talker", gcal_enabled=True)
    gcal = next(t for t in tools if t["name"] == "gcal_list_events")
    schema = gcal["input_schema"]
    assert set(schema["required"]) == {"calendar", "start", "end"}
    cal_prop = schema["properties"]["calendar"]
    assert cal_prop["enum"] == ["alfred", "primary"]


def test_gcal_tool_schema_has_no_top_level_combinator():
    """Anthropic-API regression pin (companion to the 2026-05-06 P0
    fix). No oneOf / allOf / anyOf at the top level of the new tool's
    input_schema. The top-level-combinator regression killed Salem's
    talker for hours; the new tool must not reintroduce the same
    bug class."""
    tools = conversation.tools_for_set("talker", gcal_enabled=True)
    gcal = next(t for t in tools if t["name"] == "gcal_list_events")
    schema = gcal["input_schema"]
    for forbidden_key in ("oneOf", "allOf", "anyOf"):
        assert forbidden_key not in schema, (
            f"top-level {forbidden_key!r} in gcal_list_events "
            f"input_schema would trigger Anthropic HTTP 400. See "
            f"tests/test_vault_edit_tool_schema.py for the parent "
            f"regression pin."
        )


# --- Dispatch — happy path -------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_alfred_calendar_resolves_and_returns_events(
    tmp_path, monkeypatch,
):
    """calendar='alfred' → resolves alfred_calendar_id → returns
    chat-shape dicts."""
    config_path = _write_gcal_config(tmp_path, enabled=True)
    config = _config_with_path(tmp_path, config_path=str(config_path))

    captured: dict = {}

    class FakeClient:
        def __init__(self, **kwargs):
            captured["init_kwargs"] = kwargs
        def list_events(self, calendar_id, time_min, time_max, **kwargs):
            captured["calendar_id"] = calendar_id
            captured["time_min"] = time_min
            captured["time_max"] = time_max
            return [
                _make_event(
                    title="Chiropractor",
                    description="Adjustment + cupping. Bring intake form.",
                    location="123 Main St, Halifax",
                ),
            ]

    monkeypatch.setattr(
        "alfred.integrations.gcal.GCalClient", FakeClient,
    )

    result_str = await conversation._execute_tool(
        tool_name="gcal_list_events",
        tool_input={
            "calendar": "alfred",
            "start": "2026-05-07T00:00:00-03:00",
            "end": "2026-05-08T00:00:00-03:00",
        },
        vault_path=str(tmp_path / "vault"),
        state=None,
        session=_session(),
        config=config,
    )
    parsed = json.loads(result_str)
    assert parsed["calendar"] == "alfred"
    assert len(parsed["events"]) == 1
    ev = parsed["events"][0]
    # Chat-shape: title / start / end / location / description.
    assert ev["title"] == "Chiropractor"
    assert ev["location"] == "123 Main St, Halifax"
    assert "Adjustment" in ev["description"]
    # ISO 8601 strings, not raw datetime objects.
    assert isinstance(ev["start"], str)
    assert "T" in ev["start"]
    # No raw blob in the chat shape.
    assert "raw" not in ev
    assert "id" not in ev
    # Calendar ID resolution worked.
    assert captured["calendar_id"] == "alfred-calendar-id-FAKE"


@pytest.mark.asyncio
async def test_dispatch_primary_calendar_resolves_id(tmp_path, monkeypatch):
    """calendar='primary' → resolves primary_calendar_id."""
    config_path = _write_gcal_config(tmp_path, enabled=True)
    config = _config_with_path(tmp_path, config_path=str(config_path))

    captured: dict = {}

    class FakeClient:
        def __init__(self, **kwargs): pass
        def list_events(self, calendar_id, time_min, time_max, **kwargs):
            captured["calendar_id"] = calendar_id
            return []

    monkeypatch.setattr(
        "alfred.integrations.gcal.GCalClient", FakeClient,
    )

    await conversation._execute_tool(
        tool_name="gcal_list_events",
        tool_input={
            "calendar": "primary",
            "start": "2026-05-07T00:00:00-03:00",
            "end": "2026-05-08T00:00:00-03:00",
        },
        vault_path=str(tmp_path / "vault"),
        state=None,
        session=_session(),
        config=config,
    )
    assert captured["calendar_id"] == "primary-calendar-id-FAKE"


@pytest.mark.asyncio
async def test_dispatch_empty_result_returns_empty_events_list(
    tmp_path, monkeypatch,
):
    """Empty calendar window → ``{"events": []}`` not an error.
    Per ``feedback_intentionally_left_blank.md``, the empty case is
    a legitimate answer ("you have nothing on Tuesday"), not a
    failure mode."""
    config_path = _write_gcal_config(tmp_path, enabled=True)
    config = _config_with_path(tmp_path, config_path=str(config_path))

    class FakeClient:
        def __init__(self, **kwargs): pass
        def list_events(self, *args, **kwargs):
            return []

    monkeypatch.setattr(
        "alfred.integrations.gcal.GCalClient", FakeClient,
    )

    result_str = await conversation._execute_tool(
        tool_name="gcal_list_events",
        tool_input={
            "calendar": "alfred",
            "start": "2026-05-07T00:00:00-03:00",
            "end": "2026-05-08T00:00:00-03:00",
        },
        vault_path=str(tmp_path / "vault"),
        state=None,
        session=_session(),
        config=config,
    )
    parsed = json.loads(result_str)
    assert parsed == {"calendar": "alfred", "events": []}


@pytest.mark.asyncio
async def test_dispatch_truncates_long_descriptions(tmp_path, monkeypatch):
    """Description > 500 chars → truncated with horizontal ellipsis.
    Keeps token cost reasonable + transcript review readable."""
    config_path = _write_gcal_config(tmp_path, enabled=True)
    config = _config_with_path(tmp_path, config_path=str(config_path))

    long_desc = "A" * 1000

    class FakeClient:
        def __init__(self, **kwargs): pass
        def list_events(self, *args, **kwargs):
            return [_make_event(description=long_desc)]

    monkeypatch.setattr(
        "alfred.integrations.gcal.GCalClient", FakeClient,
    )

    result_str = await conversation._execute_tool(
        tool_name="gcal_list_events",
        tool_input={
            "calendar": "alfred",
            "start": "2026-05-07T00:00:00-03:00",
            "end": "2026-05-08T00:00:00-03:00",
        },
        vault_path=str(tmp_path / "vault"),
        state=None,
        session=_session(),
        config=config,
    )
    parsed = json.loads(result_str)
    desc = parsed["events"][0]["description"]
    assert desc.endswith("…")
    # 500 chars + ellipsis = 501 chars total (the ellipsis is one char).
    assert len(desc) == 501


# --- Dispatch — error paths ------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_rejects_invalid_calendar_alias(tmp_path):
    """Calendar enum strict — anything other than alfred/primary
    rejected (validation runs before any GCal client construction,
    so no monkeypatch needed)."""
    config_path = _write_gcal_config(tmp_path, enabled=True)
    config = _config_with_path(tmp_path, config_path=str(config_path))

    result_str = await conversation._execute_tool(
        tool_name="gcal_list_events",
        tool_input={
            "calendar": "rogue",
            "start": "2026-05-07T00:00:00-03:00",
            "end": "2026-05-08T00:00:00-03:00",
        },
        vault_path=str(tmp_path / "vault"),
        state=None,
        session=_session(),
        config=config,
    )
    parsed = json.loads(result_str)
    assert "error" in parsed
    assert "alfred" in parsed["error"]
    assert "primary" in parsed["error"]


@pytest.mark.asyncio
async def test_dispatch_rejects_naive_datetime(tmp_path):
    """tz-naive ISO strings rejected — GCal requires tz-aware input."""
    config_path = _write_gcal_config(tmp_path, enabled=True)
    config = _config_with_path(tmp_path, config_path=str(config_path))

    result_str = await conversation._execute_tool(
        tool_name="gcal_list_events",
        tool_input={
            "calendar": "alfred",
            "start": "2026-05-07T00:00:00",  # no offset
            "end": "2026-05-08T00:00:00",
        },
        vault_path=str(tmp_path / "vault"),
        state=None,
        session=_session(),
        config=config,
    )
    parsed = json.loads(result_str)
    assert "error" in parsed
    assert "timezone-aware" in parsed["error"]


@pytest.mark.asyncio
async def test_dispatch_rejects_invalid_iso_string(tmp_path):
    """Malformed ISO string rejected."""
    config_path = _write_gcal_config(tmp_path, enabled=True)
    config = _config_with_path(tmp_path, config_path=str(config_path))

    result_str = await conversation._execute_tool(
        tool_name="gcal_list_events",
        tool_input={
            "calendar": "alfred",
            "start": "not-a-date",
            "end": "2026-05-08T00:00:00-03:00",
        },
        vault_path=str(tmp_path / "vault"),
        state=None,
        session=_session(),
        config=config,
    )
    parsed = json.loads(result_str)
    assert "error" in parsed
    assert "invalid 'start'" in parsed["error"]


@pytest.mark.asyncio
async def test_dispatch_returns_error_when_gcal_disabled(tmp_path):
    """Defensive: tools_for_set should not expose the tool when GCal
    is disabled, but if a misconfiguration races mid-restart the
    dispatcher itself fails honestly rather than silently no-op."""
    config_path = _write_gcal_config(tmp_path, enabled=False)
    config = _config_with_path(tmp_path, config_path=str(config_path))

    result_str = await conversation._execute_tool(
        tool_name="gcal_list_events",
        tool_input={
            "calendar": "alfred",
            "start": "2026-05-07T00:00:00-03:00",
            "end": "2026-05-08T00:00:00-03:00",
        },
        vault_path=str(tmp_path / "vault"),
        state=None,
        session=_session(),
        config=config,
    )
    parsed = json.loads(result_str)
    assert "error" in parsed
    assert "not enabled" in parsed["error"]


@pytest.mark.asyncio
async def test_dispatch_returns_error_when_calendar_id_unset(tmp_path):
    """alfred_calendar_id empty → fail honestly with operator-actionable
    message instead of calling GCal with an empty calendar ID."""
    config_path = _write_gcal_config(
        tmp_path, enabled=True, alfred_calendar_id="",
    )
    config = _config_with_path(tmp_path, config_path=str(config_path))

    result_str = await conversation._execute_tool(
        tool_name="gcal_list_events",
        tool_input={
            "calendar": "alfred",
            "start": "2026-05-07T00:00:00-03:00",
            "end": "2026-05-08T00:00:00-03:00",
        },
        vault_path=str(tmp_path / "vault"),
        state=None,
        session=_session(),
        config=config,
    )
    parsed = json.loads(result_str)
    assert "error" in parsed
    assert "alfred_calendar_id" in parsed["error"]


@pytest.mark.asyncio
async def test_dispatch_catches_api_error_returns_to_model(
    tmp_path, monkeypatch,
):
    """GCal API errors must surface as ``{"error": ...}`` so the model
    can recover (apologise / retry / pick another tool), NOT propagate
    out of the tool dispatcher."""
    config_path = _write_gcal_config(tmp_path, enabled=True)
    config = _config_with_path(tmp_path, config_path=str(config_path))

    from alfred.integrations.gcal import GCalAPIError

    class FakeClient:
        def __init__(self, **kwargs): pass
        def list_events(self, *args, **kwargs):
            raise GCalAPIError("simulated API failure")

    monkeypatch.setattr(
        "alfred.integrations.gcal.GCalClient", FakeClient,
    )

    result_str = await conversation._execute_tool(
        tool_name="gcal_list_events",
        tool_input={
            "calendar": "alfred",
            "start": "2026-05-07T00:00:00-03:00",
            "end": "2026-05-08T00:00:00-03:00",
        },
        vault_path=str(tmp_path / "vault"),
        state=None,
        session=_session(),
        config=config,
    )
    # The dispatcher caught the exception — we got back a dict, not
    # a propagated exception.
    parsed = json.loads(result_str)
    assert "error" in parsed
    assert "simulated API failure" in parsed["error"]


@pytest.mark.asyncio
async def test_dispatch_catches_not_authorized_returns_to_model(
    tmp_path, monkeypatch,
):
    """Specific exception class for OAuth / not-authorized → operator-
    actionable error message naming the CLI command."""
    config_path = _write_gcal_config(tmp_path, enabled=True)
    config = _config_with_path(tmp_path, config_path=str(config_path))

    from alfred.integrations.gcal import GCalNotAuthorized

    class FakeClient:
        def __init__(self, **kwargs):
            raise GCalNotAuthorized("token missing")

    monkeypatch.setattr(
        "alfred.integrations.gcal.GCalClient", FakeClient,
    )

    result_str = await conversation._execute_tool(
        tool_name="gcal_list_events",
        tool_input={
            "calendar": "alfred",
            "start": "2026-05-07T00:00:00-03:00",
            "end": "2026-05-08T00:00:00-03:00",
        },
        vault_path=str(tmp_path / "vault"),
        state=None,
        session=_session(),
        config=config,
    )
    parsed = json.loads(result_str)
    assert "error" in parsed
    assert "alfred gcal authorize" in parsed["error"]


# --- _resolve_gcal_enabled_for_run_turn helper -----------------------------
#
# The lazy resolver inside run_turn — verifies the per-turn gating
# decision is correctly fed to ``tools_for_set``.


def test_resolve_gcal_enabled_returns_true_when_enabled(tmp_path):
    config_path = _write_gcal_config(tmp_path, enabled=True)
    config = _config_with_path(tmp_path, config_path=str(config_path))
    assert conversation._resolve_gcal_enabled_for_run_turn(config) is True


def test_resolve_gcal_enabled_returns_false_when_disabled(tmp_path):
    config_path = _write_gcal_config(tmp_path, enabled=False)
    config = _config_with_path(tmp_path, config_path=str(config_path))
    assert conversation._resolve_gcal_enabled_for_run_turn(config) is False


def test_resolve_gcal_enabled_returns_false_on_missing_config_path(
    tmp_path,
):
    """Defensive: missing config.yaml → False (matches pre-feature
    behaviour: no GCal tool surfaced, model can't see capability that
    isn't wired)."""
    nonexistent = tmp_path / "does_not_exist.yaml"
    config = _config_with_path(tmp_path, config_path=str(nonexistent))
    assert conversation._resolve_gcal_enabled_for_run_turn(config) is False


def test_resolve_gcal_enabled_returns_false_when_no_gcal_block(tmp_path):
    """Config without a ``gcal:`` section → False (loader returns
    default-disabled GCalConfig)."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("vault:\n  path: /tmp/vault\n", encoding="utf-8")
    config = _config_with_path(tmp_path, config_path=str(config_path))
    assert conversation._resolve_gcal_enabled_for_run_turn(config) is False


# --- _gcal_event_to_chat_dict (pure) ---------------------------------------


def test_event_to_chat_dict_strips_raw_blob_and_id():
    """Pruned shape: only the chat-relevant fields. Smoke test of the
    pure helper."""
    ev = _make_event(
        title="Test",
        description="short",
        location="HQ",
    )
    out = conversation._gcal_event_to_chat_dict(ev)
    assert set(out.keys()) == {"title", "start", "end", "location", "description"}
    # Sanity: ISO 8601 string format.
    assert "T" in out["start"]
    assert "T" in out["end"]


def test_event_to_chat_dict_handles_missing_location():
    """No location in raw → empty string (chat-friendly default)."""
    ev = _make_event()
    ev.raw = {}
    out = conversation._gcal_event_to_chat_dict(ev)
    assert out["location"] == ""


def test_event_to_chat_dict_truncates_at_500():
    """Description capped at the ``_GCAL_DESCRIPTION_TRUNC_CHARS``
    constant + horizontal ellipsis suffix."""
    long_desc = "x" * (conversation._GCAL_DESCRIPTION_TRUNC_CHARS + 100)
    ev = _make_event(description=long_desc)
    out = conversation._gcal_event_to_chat_dict(ev)
    assert out["description"].endswith("…")
    assert len(out["description"]) == conversation._GCAL_DESCRIPTION_TRUNC_CHARS + 1


def test_event_to_chat_dict_preserves_short_descriptions():
    """Short descriptions kept verbatim — no spurious ellipsis."""
    ev = _make_event(description="Bring the intake form.")
    out = conversation._gcal_event_to_chat_dict(ev)
    assert out["description"] == "Bring the intake form."
    assert "…" not in out["description"]
