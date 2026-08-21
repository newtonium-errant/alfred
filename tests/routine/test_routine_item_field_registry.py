"""``routine_item`` accepted-field registry — writability + loud refusal.

THE INCIDENT (2026-08-20, measured on the live box). The operator asked
Salem to set Pool Chemistry on a soft 3-day cadence **with a 7-day
escalation**. Salem dispatched ``routine_item``. The tool answered::

    talker.routine_item.result action=edit kind=edited ok=True

and the stored item was::

    {'priority': 'tracked', 'text': 'Pool Chemistry',
     'warn_after_gap_days': 3, 'target_cadence_days': 3}

``escalate_after_gap_days`` is absent. The field is real — declared on
``alfred.routine.config.Item`` and consumed by
``alfred.tier.compute.classify_routine_item`` — but the talker's dict→argv
hop read a HAND-WRITTEN subset of ``fields`` and everything else fell on the
floor. The operator's 7-day escalation was discarded behind ``ok=True``.

Two halves are pinned here, and the second is the general one:

  PART 1 — ``escalate_after_gap_days`` (and its annotation sibling
  ``warn_after_gap_days``) are writable end to end, including the
  conflict semantics with ``due_pattern``.

  PART 2 — a field the tool CANNOT write is refused BY NAME, loudly,
  with nothing written. This is the defect class: silent absence that
  reports success is the exact inverse of the intentionally-left-blank
  rule.

## The trap these tests exist to keep shut

``escalate_at_days`` and ``escalate_after_gap_days`` are one word apart and
mean different things:

  * ``escalate_at_days``        — days **BEFORE DUE** (deadline axis;
                                  requires ``due_pattern``)
  * ``escalate_after_gap_days`` — days **SINCE LAST COMPLETION**
                                  (neglect-gap axis; requires NO
                                  ``due_pattern``)

Supporting one and not the other, with no refusal on the missing one, is
what made a whole day of "it reported success" plausible.

## What "end to end" means here

``test_acceptance_pool_chemistry_*`` runs the operator's real request
through EVERY production layer: the talker dispatcher builds the argv, the
REAL ``build_parser()`` parses that argv, the REAL ``cmd_routine`` handler
executes it, and the assertion reads the record off disk. The single
substitution is ``--config <tmp>``, which the real subprocess would pick up
from its working directory — no layer of the chain is stubbed or
reimplemented.
"""

from __future__ import annotations

import io
import json
import subprocess
from contextlib import redirect_stdout
from datetime import date, datetime, timezone
from pathlib import Path

import frontmatter  # type: ignore[import-untyped]
import pytest
import structlog
import yaml

import alfred.routine.cli  # noqa: F401  (breaks the cli/cli_items import cycle)
from alfred.cli import build_parser, cmd_routine
from alfred.routine import cli_items
from alfred.routine.cli import (
    ITEM_KIND_CADENCE_CONFLICT,
    ITEM_KIND_EDITED,
    ITEM_KIND_INVALID_FIELD,
    cmd_item_add,
    cmd_item_edit,
)
from alfred.routine.config import Item, RoutineConfig
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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _config(vault_path: Path, tmp_path: Path) -> RoutineConfig:
    config = RoutineConfig(vault_path=str(vault_path), instance_name="salem")
    config.state.path = str(tmp_path / "routine_state.json")
    return config


def _write_routine(vault_path: Path, name: str, payload: dict) -> Path:
    routine_dir = vault_path / "routine"
    routine_dir.mkdir(parents=True, exist_ok=True)
    fm_str = yaml.dump(payload, default_flow_style=False, sort_keys=False)
    path = routine_dir / f"{name}.md"
    path.write_text(f"---\n{fm_str}---\n\n# {name}\n", encoding="utf-8")
    return path


def _items(vault_path: Path, name: str) -> list[dict]:
    post = frontmatter.load(str(vault_path / "routine" / f"{name}.md"))
    return list(post.metadata.get("items") or [])


def _item(vault_path: Path, record: str, text: str) -> dict:
    for it in _items(vault_path, record):
        if str(it.get("text", "")).strip() == text:
            return it
    raise AssertionError(f"item {text!r} not found on {record!r}")


def _salem_talker_config(tmp_path: Path) -> TalkerConfig:
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir(parents=True, exist_ok=True)
    return TalkerConfig(
        bot_token="x",
        allowed_users=[1],
        primary_users=["person/Test"],
        anthropic=AnthropicConfig(api_key="x", model="claude-opus-4-7"),
        stt=STTConfig(api_key="x", model="whisper-large-v3"),
        session=SessionConfig(state_path=str(tmp_path / "state.json")),
        vault=VaultConfig(path=str(vault_dir)),
        logging=LoggingConfig(file=str(tmp_path / "talker.log")),
        instance=InstanceConfig(name="Salem", tool_set="talker"),
    )


def _session() -> Session:
    now = datetime(2026, 8, 21, tzinfo=timezone.utc)
    return Session(
        session_id="sess-1", chat_id=1, started_at=now,
        last_message_at=now, model="claude-opus-4-7",
    )


def _cfg_file(tmp_path: Path) -> Path:
    """Minimal unified config the REAL ``cmd_routine`` can load.

    ``telegram.instance.name`` is required — ``routine.load_from_unified``
    resolves the instance through it and the Salem-only gate reads the
    result. Per the CLAUDE.md ``_build`` empty-dict trap, populate it
    rather than letting the block default empty.
    """
    cfg = {
        "vault": {"path": str(tmp_path / "vault")},
        "logging": {"dir": str(tmp_path / "data")},
        "telegram": {"instance": {"name": "Salem"}},
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# The real-CLI executor — used as the subprocess.run stand-in
# ---------------------------------------------------------------------------


def _real_cli_runner(cfg_path: Path, spawned: list[list[str]]):
    """Return a ``subprocess.run`` replacement that runs the REAL CLI.

    The dispatcher builds ``[sys.executable, '-m', 'alfred', 'routine',
    'item', ...]``. Rather than spawn an interpreter (which would read the
    developer's own ``config.yaml``), we strip the interpreter prefix and
    feed the REMAINING ARGV — the dispatcher's own, unmodified — to the real
    ``build_parser()`` and the real ``cmd_routine``. ``--config`` is the only
    addition, standing in for the working directory a real subprocess would
    have had.

    This keeps the chain honest: argv is produced by production code and
    consumed by production code. A field that the dispatcher fails to thread
    cannot be rescued here, which is the entire point.
    """

    def _run(argv, *args, **kwargs):
        spawned.append(list(argv))
        assert argv[1:3] == ["-m", "alfred"], argv
        cli_argv = ["--config", str(cfg_path), *argv[3:]]
        buf = io.StringIO()
        code = 0
        with redirect_stdout(buf):
            try:
                ns = build_parser().parse_args(cli_argv)
                cmd_routine(ns)
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
        proc = subprocess.CompletedProcess(
            args=argv, returncode=code, stdout=buf.getvalue(), stderr="",
        )
        return proc

    return _run


# ===========================================================================
# PART 1 — the field is writable, end to end through the real CLI path
# ===========================================================================


@pytest.mark.asyncio
async def test_acceptance_pool_chemistry_escalate_after_gap_days_end_to_end(
    tmp_path, monkeypatch,
) -> None:
    """THE ACCEPTANCE TEST — the operator's actual 2026-08-20 request.

    Pool Chemistry, soft 3-day cadence, 7-day escalation, driven through
    the tool path Salem uses. Pre-fix this stored the item WITHOUT
    ``escalate_after_gap_days`` and answered ``ok=True``.
    """
    vault = tmp_path / "vault"
    _write_routine(vault, "Core Daily", {
        "type": "routine", "name": "Core Daily",
        "cadence": {"type": "daily"},
        "items": [
            {"text": "Brush Teeth", "priority": "tracked"},
            {
                "text": "Pool Chemistry", "priority": "tracked",
                "warn_after_gap_days": 3, "target_cadence_days": 3,
            },
        ],
    })
    cfg = _cfg_file(tmp_path)
    spawned: list[list[str]] = []
    monkeypatch.setattr(subprocess, "run", _real_cli_runner(cfg, spawned))

    result = await conversation._dispatch_routine_item(
        tool_input={
            "action": "edit",
            "record": "Core Daily",
            "item": "Pool Chemistry",
            "fields": {"escalate_after_gap_days": 7},
        },
        session=_session(),
        config=_salem_talker_config(tmp_path),
    )
    parsed = json.loads(result)

    # The canary the model sees.
    assert parsed["ok"] is True, parsed
    assert parsed["kind"] == ITEM_KIND_EDITED, parsed
    assert "escalate_after_gap_days" in parsed.get("fields_changed", []), parsed

    # The flag genuinely crossed the dispatcher→CLI boundary.
    argv = spawned[0]
    assert "--escalate-after-gap-days" in argv, argv
    assert argv[argv.index("--escalate-after-gap-days") + 1] == "7", argv

    # THE OPERATOR'S ASK, on disk.
    item = _item(vault, "Core Daily", "Pool Chemistry")
    assert item["escalate_after_gap_days"] == 7, item
    # Composes with the soft cadence rather than replacing it.
    assert item["target_cadence_days"] == 3, item
    assert item["warn_after_gap_days"] == 3, item
    # Untouched neighbour — the atomic-mutation contract.
    assert _item(vault, "Core Daily", "Brush Teeth") == {
        "text": "Brush Teeth", "priority": "tracked",
    }

    # And the value the tier engine will actually read back.
    reparsed = Item.from_dict(item)
    assert reparsed is not None and reparsed.escalate_after_gap_days == 7


@pytest.mark.asyncio
async def test_acceptance_value_survives_the_round_trip_to_the_tier_engine(
    tmp_path, monkeypatch,
) -> None:
    """The stored 7 actually drives a Duty visit at a 7-day gap.

    Writing the field is only half the operator's ask; the point of the
    field is the escalation. Drives the REAL
    ``tier.compute.classify_routine_item`` off the REAL stored record, so
    a value that is written but inert would fail here.
    """
    from alfred.tier.compute import classify_routine_item

    vault = tmp_path / "vault"
    _write_routine(vault, "Core Daily", {
        "type": "routine", "name": "Core Daily",
        "cadence": {"type": "daily"},
        "items": [{
            "text": "Pool Chemistry", "priority": "tracked",
            "target_cadence_days": 3,
        }],
    })
    cfg = _cfg_file(tmp_path)
    monkeypatch.setattr(subprocess, "run", _real_cli_runner(cfg, []))

    await conversation._dispatch_routine_item(
        tool_input={
            "action": "edit", "record": "Core Daily",
            "item": "Pool Chemistry",
            "fields": {"escalate_after_gap_days": 7},
        },
        session=_session(), config=_salem_talker_config(tmp_path),
    )
    stored = Item.from_dict(_item(vault, "Core Daily", "Pool Chemistry"))
    assert stored is not None

    today = date(2026, 8, 21)

    def _classify(last_completed: str):
        # Every field sourced from the STORED item — this is a round-trip
        # assertion, so re-deriving any of them here would hollow it out.
        return classify_routine_item(
            priority=stored.priority,
            due_pattern=stored.due_pattern,
            surface_at_days=stored.surface_at_days,
            escalate_at_days=stored.escalate_at_days,
            target_cadence_days=stored.target_cadence_days,
            escalate_after_gap_days=stored.escalate_after_gap_days,
            completion_log={"Pool Chemistry": [last_completed]},
            item_text="Pool Chemistry",
            today=today,
        )

    # 8 days since completion → past the 7-day threshold → T1 + Duty visit.
    escalated = _classify("2026-08-13")
    assert escalated.tier == 1, escalated
    assert escalated.gap_escalated is True, escalated

    # POSITIVE CONTROL for the threshold: 5 days is INSIDE the 7-day
    # window, so the item must stay quiet. Without this, the assertion
    # above would pass just as well against a build that escalates
    # unconditionally.
    quiet = _classify("2026-08-16")
    assert quiet.gap_escalated is False, quiet
    assert quiet.tier != 1, quiet


def test_escalate_after_gap_days_writable_on_add(tmp_path) -> None:
    vault = tmp_path / "vault"
    _write_routine(vault, "Daily", {
        "type": "routine", "name": "Daily",
        "cadence": {"type": "daily"}, "items": [],
    })
    code = cmd_item_add(
        _config(vault, tmp_path), record_name="Daily",
        item_text="Add Fuel", escalate_after_gap_days=3,
    )
    assert code == 0
    assert _item(vault, "Daily", "Add Fuel")["escalate_after_gap_days"] == 3


def test_warn_after_gap_days_writable_on_add(tmp_path) -> None:
    """The annotation sibling — same one-line shape, also previously
    unwritable. Independent of every escalation axis (per the field table
    on ``tier.compute.classify_routine_item``), so no conflict handling."""
    vault = tmp_path / "vault"
    _write_routine(vault, "Daily", {
        "type": "routine", "name": "Daily",
        "cadence": {"type": "daily"}, "items": [],
    })
    code = cmd_item_add(
        _config(vault, tmp_path), record_name="Daily",
        item_text="Water Plants", warn_after_gap_days=4,
        due_pattern=json.dumps({"type": "weekly", "day": "thu"}),
    )
    # Coexists with a due_pattern — deliberately NOT a conflict.
    assert code == 0
    stored = _item(vault, "Daily", "Water Plants")
    assert stored["warn_after_gap_days"] == 4
    assert stored["due_pattern"]["type"] == "weekly"


def test_escalate_after_gap_days_rejects_zero_because_engine_gates_gt_zero(
    tmp_path,
) -> None:
    """0 must be REFUSED, not stored.

    ``classify_routine_item`` gates the neglect-gap branch on
    ``gap_threshold > 0``, so a stored 0 would be accepted, written, and
    then never fire — the same silent-drop shape one layer down. The
    zero-allowance is deliberately scoped to ``escalate_at_days`` alone
    (``_ZERO_VALID_FIELDS``), whose 0 means "T1 on the due date itself".
    """
    vault = tmp_path / "vault"
    _write_routine(vault, "Daily", {
        "type": "routine", "name": "Daily",
        "cadence": {"type": "daily"}, "items": [],
    })
    code = cmd_item_add(
        _config(vault, tmp_path), record_name="Daily",
        item_text="Inert", escalate_after_gap_days=0,
    )
    assert code == 1
    assert _items(vault, "Daily") == []

    # POSITIVE CONTROL — the SAME zero on the due-axis field IS valid,
    # proving the rejection above is about this field, not about zeros.
    code_ok = cmd_item_add(
        _config(vault, tmp_path), record_name="Daily",
        item_text="On Due Date", escalate_at_days=0,
        due_pattern=json.dumps({"type": "weekly", "day": "thu"}),
    )
    assert code_ok == 0
    assert _item(vault, "Daily", "On Due Date")["escalate_at_days"] == 0


def test_gap_escalation_composes_with_target_cadence_on_add(
    tmp_path,
) -> None:
    """Gap escalation + soft cadence is the COMMON shape, not a conflict.

    This is the operator's Pool Chemistry encoding — a quiet daily
    presence that becomes a Duty visit once neglected. A conflict check
    that over-fired here would break the feature on its main target.
    """
    vault = tmp_path / "vault"
    _write_routine(vault, "Daily", {
        "type": "routine", "name": "Daily",
        "cadence": {"type": "daily"}, "items": [],
    })
    code = cmd_item_add(
        _config(vault, tmp_path), record_name="Daily", item_text="Fuel",
        target_cadence_days=3, escalate_after_gap_days=7,
    )
    assert code == 0
    stored = _item(vault, "Daily", "Fuel")
    assert stored["target_cadence_days"] == 3
    assert stored["escalate_after_gap_days"] == 7


def test_gap_escalation_conflicts_with_due_pattern_on_add(tmp_path) -> None:
    """The gap axis and the deadline axis are mutually exclusive."""
    vault = tmp_path / "vault"
    _write_routine(vault, "Daily", {
        "type": "routine", "name": "Daily",
        "cadence": {"type": "daily"}, "items": [],
    })
    code = cmd_item_add(
        _config(vault, tmp_path), record_name="Daily", item_text="Confused",
        escalate_after_gap_days=7,
        due_pattern=json.dumps({"type": "weekly", "day": "thu"}),
    )
    assert code == 1
    assert _items(vault, "Daily") == []


def test_setting_gap_on_a_deadline_item_refuses_then_clear_flag_allows(
    tmp_path, capsys,
) -> None:
    """Edit-side conflict + its explicit opt-in, and the WHY of the refusal.

    Asserts the refusal is the CADENCE_CONFLICT kind, not merely a
    non-zero exit — an unrelated denial (unknown item, bad value) would
    look identical at the exit-code level.
    """
    vault = tmp_path / "vault"
    _write_routine(vault, "Daily", {
        "type": "routine", "name": "Daily",
        "cadence": {"type": "daily"},
        "items": [{
            "text": "Pay Rent", "priority": "tracked",
            "due_pattern": {"type": "monthly", "day": 1},
        }],
    })
    config = _config(vault, tmp_path)

    code = cmd_item_edit(
        config, record_name="Daily", item_text="Pay Rent",
        escalate_after_gap_days=5, wants_json=True,
    )
    assert code == 1
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["kind"] == ITEM_KIND_CADENCE_CONFLICT, payload
    assert "escalate_at_days" in payload["error"], payload["error"]
    # File untouched by the refusal.
    assert "escalate_after_gap_days" not in _item(vault, "Daily", "Pay Rent")

    # The opt-in performs the switch: deadline dropped, gap threshold set.
    code_ok = cmd_item_edit(
        config, record_name="Daily", item_text="Pay Rent",
        escalate_after_gap_days=5, clear_due_pattern=True,
    )
    assert code_ok == 0
    stored = _item(vault, "Daily", "Pay Rent")
    assert stored["escalate_after_gap_days"] == 5
    assert "due_pattern" not in stored


def test_setting_due_pattern_on_a_gap_item_refuses_then_clear_flag_allows(
    tmp_path,
) -> None:
    """The mirror direction — gap axis → deadline axis needs its own opt-in."""
    vault = tmp_path / "vault"
    _write_routine(vault, "Daily", {
        "type": "routine", "name": "Daily",
        "cadence": {"type": "daily"},
        "items": [{
            "text": "Fuel", "priority": "tracked",
            "escalate_after_gap_days": 3,
        }],
    })
    config = _config(vault, tmp_path)
    weekly = json.dumps({"type": "weekly", "day": "thu"})

    assert cmd_item_edit(
        config, record_name="Daily", item_text="Fuel", due_pattern=weekly,
    ) == 1
    assert _item(vault, "Daily", "Fuel")["escalate_after_gap_days"] == 3

    assert cmd_item_edit(
        config, record_name="Daily", item_text="Fuel", due_pattern=weekly,
        clear_escalate_after_gap_days=True,
    ) == 0
    stored = _item(vault, "Daily", "Fuel")
    assert "escalate_after_gap_days" not in stored
    assert stored["due_pattern"]["type"] == "weekly"


def test_clear_due_pattern_preserves_escalate_after_gap_days(
    tmp_path,
) -> None:
    """``--clear-due-pattern`` strips the DUE-axis knobs only.

    ``escalate_at_days`` / ``surface_at_days`` are meaningless without a
    deadline; the gap threshold is the opposite — it only starts working
    once the deadline is gone. Stripping it would break the hard → gap
    switch, which is a supported path.
    """
    vault = tmp_path / "vault"
    _write_routine(vault, "Daily", {
        "type": "routine", "name": "Daily",
        "cadence": {"type": "daily"},
        "items": [{
            "text": "Thing", "priority": "tracked",
            "due_pattern": {"type": "weekly", "day": "thu"},
            "escalate_at_days": 1, "surface_at_days": 3,
            "escalate_after_gap_days": 9,
        }],
    })
    assert cmd_item_edit(
        _config(vault, tmp_path), record_name="Daily", item_text="Thing",
        clear_due_pattern=True,
    ) == 0
    stored = _item(vault, "Daily", "Thing")
    assert "due_pattern" not in stored
    assert "escalate_at_days" not in stored
    assert "surface_at_days" not in stored
    # The gap threshold SURVIVES.
    assert stored["escalate_after_gap_days"] == 9


def test_setting_gap_and_clearing_it_in_one_call_is_refused(
    tmp_path,
) -> None:
    """Contradictory intent must not resolve into a silent discard.

    The clear runs after the set, so proceeding would store nothing and
    still answer success — the shape this whole commit exists to close.
    """
    vault = tmp_path / "vault"
    _write_routine(vault, "Daily", {
        "type": "routine", "name": "Daily",
        "cadence": {"type": "daily"},
        "items": [{"text": "Fuel", "priority": "tracked"}],
    })
    assert cmd_item_edit(
        _config(vault, tmp_path), record_name="Daily", item_text="Fuel",
        escalate_after_gap_days=4, clear_escalate_after_gap_days=True,
    ) == 1
    assert "escalate_after_gap_days" not in _item(vault, "Daily", "Fuel")


# ===========================================================================
# PART 2 — an unwritable field is REFUSED BY NAME, never dropped
# ===========================================================================


@pytest.mark.asyncio
async def test_unsupported_field_refused_by_name_with_nothing_written(
    tmp_path, monkeypatch,
) -> None:
    """The general fix. ``slot`` is a real Item field this tool can't write.

    Asserts WHY it refused (the named field in a structured list), not
    merely THAT it refused — a refusal for an unrelated cause would carry
    the same ``ok=False``.
    """
    vault = tmp_path / "vault"
    _write_routine(vault, "Core Daily", {
        "type": "routine", "name": "Core Daily",
        "cadence": {"type": "daily"},
        "items": [{"text": "Pool Chemistry", "priority": "tracked"}],
    })
    cfg = _cfg_file(tmp_path)
    spawned: list[list[str]] = []
    monkeypatch.setattr(subprocess, "run", _real_cli_runner(cfg, spawned))

    result = await conversation._dispatch_routine_item(
        tool_input={
            "action": "edit", "record": "Core Daily",
            "item": "Pool Chemistry",
            "fields": {"slot": "fuel"},
        },
        session=_session(), config=_salem_talker_config(tmp_path),
    )
    parsed = json.loads(result)

    assert parsed["ok"] is False, parsed
    assert parsed["kind"] == ITEM_KIND_INVALID_FIELD, parsed
    # Named, structurally — not just mentioned in prose.
    assert parsed["unsupported_fields"] == ["slot"], parsed
    assert "slot" in parsed["error"]
    # Tells the caller what it CAN write, so a retry is possible.
    assert "escalate_after_gap_days" in parsed["accepted_fields"], parsed
    # Nothing was touched.
    assert _item(vault, "Core Daily", "Pool Chemistry") == {
        "text": "Pool Chemistry", "priority": "tracked",
    }


@pytest.mark.asyncio
async def test_unsupported_field_refusal_never_spawns_subprocess(
    tmp_path, monkeypatch,
) -> None:
    """Debris pin + its POSITIVE CONTROL.

    A refusal must not run the CLI at all: a partial write followed by an
    error is worse than no write, because the operator then has to work
    out which half landed. The control proves the counter can register —
    without it this assertion passes identically against a dispatcher
    that never spawns anything.
    """
    vault = tmp_path / "vault"
    _write_routine(vault, "Core Daily", {
        "type": "routine", "name": "Core Daily",
        "cadence": {"type": "daily"},
        "items": [{"text": "Pool Chemistry", "priority": "tracked"}],
    })
    cfg = _cfg_file(tmp_path)
    spawned: list[list[str]] = []
    monkeypatch.setattr(subprocess, "run", _real_cli_runner(cfg, spawned))
    config = _salem_talker_config(tmp_path)

    await conversation._dispatch_routine_item(
        tool_input={
            "action": "edit", "record": "Core Daily",
            "item": "Pool Chemistry", "fields": {"not_a_field": 1},
        },
        session=_session(), config=config,
    )
    assert spawned == [], spawned

    # POSITIVE CONTROL — the nearest ADMISSIBLE neighbour does spawn.
    await conversation._dispatch_routine_item(
        tool_input={
            "action": "edit", "record": "Core Daily",
            "item": "Pool Chemistry",
            "fields": {"escalate_after_gap_days": 7},
        },
        session=_session(), config=config,
    )
    assert len(spawned) == 1, spawned


@pytest.mark.asyncio
async def test_refusal_names_the_confusable_sibling_with_both_meanings(
    tmp_path, monkeypatch,
) -> None:
    """The trap that made the original bug plausible.

    A caller reaching for the gap axis with a near-name must be told
    which field is which — not merely handed the closest string match,
    which for ``escalate_gap_days`` is the DEADLINE-axis field.
    """
    vault = tmp_path / "vault"
    _write_routine(vault, "Core Daily", {
        "type": "routine", "name": "Core Daily",
        "cadence": {"type": "daily"},
        "items": [{"text": "Pool Chemistry", "priority": "tracked"}],
    })
    monkeypatch.setattr(
        subprocess, "run", _real_cli_runner(_cfg_file(tmp_path), []),
    )
    result = await conversation._dispatch_routine_item(
        tool_input={
            "action": "edit", "record": "Core Daily",
            "item": "Pool Chemistry", "fields": {"escalate_gap_days": 7},
        },
        session=_session(), config=_salem_talker_config(tmp_path),
    )
    error = json.loads(result)["error"]
    # BOTH axes named, with the discriminating meanings.
    assert "escalate_at_days" in error
    assert "escalate_after_gap_days" in error
    assert "BEFORE DUE" in error
    assert "SINCE LAST COMPLETION" in error


@pytest.mark.asyncio
async def test_remove_accepts_no_fields_at_all(
    tmp_path, monkeypatch,
) -> None:
    """``remove`` takes no fields, so any field on it is confusion."""
    vault = tmp_path / "vault"
    _write_routine(vault, "Core Daily", {
        "type": "routine", "name": "Core Daily",
        "cadence": {"type": "daily"},
        "items": [{"text": "Pool Chemistry", "priority": "tracked"}],
    })
    monkeypatch.setattr(
        subprocess, "run", _real_cli_runner(_cfg_file(tmp_path), []),
    )
    result = await conversation._dispatch_routine_item(
        tool_input={
            "action": "remove", "record": "Core Daily",
            "item": "Pool Chemistry", "fields": {"priority": "tracked"},
        },
        session=_session(), config=_salem_talker_config(tmp_path),
    )
    parsed = json.loads(result)
    assert parsed["kind"] == ITEM_KIND_INVALID_FIELD, parsed
    assert parsed["unsupported_fields"] == ["priority"], parsed
    # The item survives the refusal.
    assert len(_items(vault, "Core Daily")) == 1


@pytest.mark.asyncio
async def test_refusal_emits_the_named_observability_event(
    tmp_path, monkeypatch,
) -> None:
    """Log-emission pin — the operator's grep surface for this refusal.

    Per the standing log-emission discipline: the production log line is
    pinned with its key fields, so a refactor that drops it fails here
    rather than silently degrading observability.
    """
    vault = tmp_path / "vault"
    _write_routine(vault, "Core Daily", {
        "type": "routine", "name": "Core Daily",
        "cadence": {"type": "daily"},
        "items": [{"text": "Pool Chemistry", "priority": "tracked"}],
    })
    monkeypatch.setattr(
        subprocess, "run", _real_cli_runner(_cfg_file(tmp_path), []),
    )
    with structlog.testing.capture_logs() as captured:
        await conversation._dispatch_routine_item(
            tool_input={
                "action": "edit", "record": "Core Daily",
                "item": "Pool Chemistry", "fields": {"slot": "fuel"},
            },
            session=_session(), config=_salem_talker_config(tmp_path),
        )
    matches = [
        c for c in captured
        if c.get("event") == "talker.routine_item.unsupported_field"
    ]
    assert len(matches) == 1, captured
    assert matches[0]["unsupported_fields"] == ["slot"]
    assert "escalate_after_gap_days" in matches[0]["accepted_fields"]
    assert matches[0]["action"] == "edit"


# ===========================================================================
# Registry integrity — the guards that stop this recurring
# ===========================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "spec",
    cli_items.ITEM_FIELD_SPECS,
    ids=lambda s: f"{s.name}",
)
async def test_every_registered_field_reaches_argv(
    spec, tmp_path, monkeypatch,
) -> None:
    """Parametrized over the WHOLE family — the anti-drift guard.

    A field can be accepted by the refusal gate and still not be threaded
    into argv; that combination is exactly the original defect wearing a
    validation layer. Drives EVERY registered spec through the real
    dispatcher and asserts its flag lands, including the incumbents the
    table inherited rather than only the fields this commit added.
    """
    sample: dict[str, object] = {
        "value": 5,
        "json": {"type": "weekly", "day": "thu"},
        "bool_pair": True,
        "switch": True,
        "text": "Renamed Thing",
    }[spec.encoding]
    action = "edit" if "edit" in spec.actions else "add"
    if spec.name == "priority":
        sample = "tracked"

    vault = tmp_path / "vault"
    _write_routine(vault, "Core Daily", {
        "type": "routine", "name": "Core Daily",
        "cadence": {"type": "daily"},
        "items": [{"text": "Pool Chemistry", "priority": "tracked"}],
    })
    spawned: list[list[str]] = []
    monkeypatch.setattr(
        subprocess, "run", _real_cli_runner(_cfg_file(tmp_path), spawned),
    )
    await conversation._dispatch_routine_item(
        tool_input={
            "action": action, "record": "Core Daily",
            "item": "Pool Chemistry", "fields": {spec.name: sample},
        },
        session=_session(), config=_salem_talker_config(tmp_path),
    )
    assert spawned, f"{spec.name}: dispatcher refused a REGISTERED field"
    assert spec.flag in spawned[0], (
        f"{spec.name}: registered in ITEM_FIELD_SPECS but its flag "
        f"{spec.flag} never reached argv — {spawned[0]}"
    )


# --- The record-landing guard ------------------------------------------
#
# WHY THIS EXISTS, measured rather than assumed. The argv guard above
# proves the dispatcher emits the flag; it says NOTHING about whether
# ``cli.py`` threads that parsed flag into the handler. Those are different
# hops and the second one is where the threading trap lives.
#
# Measured on this branch before this test was written: deleting any of the
# four ``getattr(args, ...)`` threadings this lane added — warn_after_gap_days
# on add, warn_after_gap_days on edit, clear_escalate_after_gap_days on edit,
# and escalate_after_gap_days ON THE ADD PATH — scored RED 0 against the
# then-43-test file. The last of those is this lane's own field: the exact
# 2026-08-20 defect, on the other verb, fully invisible. The end-to-end
# acceptance test covers only the EDIT path, so it could not see it.
#
# This guard closes the whole family at the altitude that can see it: drive
# each field through dispatcher → real argparse → real handler and assert
# the effect on the RECORD.

_UNSET = object()


def _landing_case(spec, action: str):
    """``(seed_item, fields, check)`` for one (spec, action) pair.

    KeyError on an unmapped spec is deliberate: a field added to the
    registry with no landing case must FAIL here rather than be silently
    skipped, which is how a guard quietly stops covering its family.
    """
    base = {"text": "Pool Chemistry", "priority": "tracked"}
    cases = {
        "priority": (
            base, {"priority": "aspirational"},
            lambda it: it["priority"] == "aspirational",
        ),
        "target_cadence_days": (
            base, {"target_cadence_days": 5},
            lambda it: it["target_cadence_days"] == 5,
        ),
        "due_pattern": (
            base, {"due_pattern": {"type": "weekly", "day": "thu"}},
            lambda it: it["due_pattern"]["type"] == "weekly",
        ),
        "surface_at_days": (
            base, {"surface_at_days": 3},
            lambda it: it["surface_at_days"] == 3,
        ),
        "escalate_at_days": (
            base, {"escalate_at_days": 2},
            lambda it: it["escalate_at_days"] == 2,
        ),
        "escalate_after_gap_days": (
            base, {"escalate_after_gap_days": 7},
            lambda it: it["escalate_after_gap_days"] == 7,
        ),
        "warn_after_gap_days": (
            base, {"warn_after_gap_days": 4},
            lambda it: it["warn_after_gap_days"] == 4,
        ),
        "self_care": (
            base, {"self_care": True},
            lambda it: it["self_care"] is True,
        ),
        "text": (
            base, {"text": "Renamed Thing"},
            lambda it: it is _UNSET,  # asserted via rename below
        ),
        "clear_due_pattern": (
            {**base, "due_pattern": {"type": "weekly", "day": "thu"}},
            {"clear_due_pattern": True},
            lambda it: "due_pattern" not in it,
        ),
        "clear_target_cadence_days": (
            {**base, "target_cadence_days": 3},
            {"clear_target_cadence_days": True},
            lambda it: "target_cadence_days" not in it,
        ),
        "clear_escalate_after_gap_days": (
            {**base, "escalate_after_gap_days": 3},
            {"clear_escalate_after_gap_days": True},
            lambda it: "escalate_after_gap_days" not in it,
        ),
    }
    return cases[spec.name]


_LANDING_PARAMS = [
    (spec, action)
    for spec in cli_items.ITEM_FIELD_SPECS
    for action in ("add", "edit")
    if action in spec.actions
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("spec", "action"),
    _LANDING_PARAMS,
    ids=lambda v: v if isinstance(v, str) else v.name,
)
async def test_every_registered_field_lands_on_the_record(
    spec, action, tmp_path, monkeypatch,
) -> None:
    """Dispatcher → real argparse → real handler → RECORD, per field.

    The guard that catches a threading break. Covers BOTH verbs, so the
    add path is no longer a blind spot.
    """
    seed, fields, check = _landing_case(spec, action)
    vault = tmp_path / "vault"
    # For add, the item must NOT already exist (duplicate_item refusal);
    # for edit it must. Seeded accordingly, with the seed's extra fields
    # carried onto the add call so clear_* has something to clear.
    if action == "add":
        extra = {k: v for k, v in seed.items() if k not in ("text", "priority")}
        _write_routine(vault, "Core Daily", {
            "type": "routine", "name": "Core Daily",
            "cadence": {"type": "daily"}, "items": [],
        })
        fields = {**extra, **fields}
    else:
        _write_routine(vault, "Core Daily", {
            "type": "routine", "name": "Core Daily",
            "cadence": {"type": "daily"}, "items": [dict(seed)],
        })

    spawned: list[list[str]] = []
    monkeypatch.setattr(
        subprocess, "run", _real_cli_runner(_cfg_file(tmp_path), spawned),
    )
    result = await conversation._dispatch_routine_item(
        tool_input={
            "action": action, "record": "Core Daily",
            "item": "Pool Chemistry", "fields": fields,
        },
        session=_session(), config=_salem_talker_config(tmp_path),
    )
    parsed = json.loads(result)
    assert parsed.get("ok") is True, (spec.name, action, parsed)

    if spec.name == "text":
        # Rename: the OLD text is gone and the NEW one is present.
        texts = [str(i.get("text")) for i in _items(vault, "Core Daily")]
        assert "Renamed Thing" in texts, texts
        assert "Pool Chemistry" not in texts, texts
        return

    stored = _item(vault, "Core Daily", "Pool Chemistry")
    assert check(stored), (
        f"{spec.name} on {action}: accepted and reported ok=True but the "
        f"effect never reached the record — {stored}"
    )


@pytest.mark.parametrize(
    "spec", cli_items.ITEM_FIELD_SPECS, ids=lambda s: f"{s.name}",
)
def test_every_registered_flag_exists_on_the_real_parser(spec) -> None:
    """The other end of the same thread: a flag in the table must be a
    flag argparse actually defines. A table row pointing at a
    non-existent flag would make the dispatcher build argv that the CLI
    rejects — loud, but only at runtime, and only for that one field."""
    parser = build_parser()
    action = "edit" if "edit" in spec.actions else "add"
    help_text = _subparser_help(parser, action)
    assert spec.flag in help_text, (
        f"{spec.flag} is registered in ITEM_FIELD_SPECS but "
        f"`alfred routine item {action} --help` does not define it"
    )


def _subparser_help(parser, action: str) -> str:
    """Format the help for ``routine item <action>`` off the REAL parser."""
    routine = _find_subparser(parser, "routine")
    item = _find_subparser(routine, "item")
    return _find_subparser(item, action).format_help()


def _find_subparser(parser, name: str):
    import argparse as _ap
    for act in parser._actions:
        if isinstance(act, _ap._SubParsersAction) and name in act.choices:
            return act.choices[name]
    raise AssertionError(f"subparser {name!r} not found")


def test_known_unwritable_covers_every_boarded_item_field() -> None:
    """PREMISE PIN — the boarded set is exactly Item minus the accepted set.

    The claim this pins is the one the sibling sweep rests on: the bound
    of "which routine-item fields can the tool not write" is
    ``Item.__dataclass_fields__``, and every member of the difference is
    BOARDED WITH A REASON rather than forgotten. A field added to ``Item``
    and to neither list fails here instead of falling into the generic
    "unknown field" branch, where it would read to a caller as a typo.
    """
    accepted = (
        set(cli_items.accepted_item_fields("add"))
        | set(cli_items.accepted_item_fields("edit"))
    )
    boarded = {
        f for f in Item.__dataclass_fields__
        if f not in accepted and f != "text"
    }
    assert boarded == set(cli_items._KNOWN_UNWRITABLE_FIELDS), (
        f"Item fields with no home: "
        f"{boarded ^ set(cli_items._KNOWN_UNWRITABLE_FIELDS)}"
    )


def test_tool_schema_enumerates_every_registered_field() -> None:
    """DRIFT PIN — the model-facing schema is a second spelling of the table.

    A field the model is never told about will never be sent, which is
    the same capability gap as a dropped field by a quieter route. The
    schema description is hand-written (the routine package is
    lazy-imported in ``conversation``), so the equality needs enforcing
    rather than assuming.
    """
    description = (
        conversation._ROUTINE_ITEM_TOOL_SCHEMA["input_schema"]
        ["properties"]["fields"]["description"]
    )
    missing = [
        spec.name for spec in cli_items.ITEM_FIELD_SPECS
        if spec.name not in description
    ]
    assert not missing, (
        f"registered in ITEM_FIELD_SPECS but absent from the "
        f"routine_item tool schema the model reads: {missing}"
    )


def test_accepted_sets_are_derived_not_hand_maintained() -> None:
    """The accepted sets must come FROM the table.

    Guards the single-source-of-truth claim itself: if these were
    hand-maintained alongside the table they could disagree, which is the
    original defect with extra steps.
    """
    for action in ("add", "edit"):
        expected = {
            s.name for s in cli_items.ITEM_FIELD_SPECS if action in s.actions
        }
        assert cli_items.accepted_item_fields(action) == expected
    assert cli_items.accepted_item_fields("remove") == frozenset()
    # An unknown action accepts nothing (fail-closed).
    assert cli_items.accepted_item_fields("bogus") == frozenset()
