"""Tests for ``alfred.transport.scheduler``.

Covers:

- ``find_due_reminders`` classification: past/future/stale/already-reminded/
  wrong-status/wrong-type.
- ``format_reminder`` template + reminder_text override.
- ``clear_remind_at_and_stamp`` record-rewrite: drops ``remind_at``,
  sets ``reminded_at``, appends the ``ALFRED:REMINDER`` body audit.
- ``_tick`` end-to-end: due reminders fire via send_fn; stale
  reminders dead-letter instead.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from textwrap import dedent

import pytest

from alfred.transport.config import (
    AuthConfig,
    SchedulerConfig,
    ServerConfig,
    StateConfig,
    TransportConfig,
)
from alfred.transport.scheduler import (
    clear_remind_at_and_stamp,
    find_due_reminders,
    format_reminder,
    _tick,
)
from alfred.transport.state import TransportState


NOW = datetime(2026, 4, 20, 18, 0, tzinfo=timezone.utc)


def _write_task(
    task_dir: Path,
    name: str,
    *,
    status: str = "todo",
    remind_at: str | None = None,
    reminded_at: str | None = None,
    due: str | None = None,
    reminder_text: str | None = None,
    type_: str = "task",
    extra: str = "",
) -> Path:
    """Helper to write a task record with controlled frontmatter.

    Returns the absolute path.
    """
    fm_lines = [
        f"type: {type_}",
        f"name: {name}",
        f"status: {status}",
        "created: 2026-04-20",
    ]
    if remind_at is not None:
        fm_lines.append(f'remind_at: "{remind_at}"')
    if reminded_at is not None:
        fm_lines.append(f'reminded_at: "{reminded_at}"')
    if due is not None:
        fm_lines.append(f'due: "{due}"')
    if reminder_text is not None:
        fm_lines.append(f'reminder_text: "{reminder_text}"')
    if extra:
        fm_lines.append(extra)

    body = f"---\n{chr(10).join(fm_lines)}\n---\n\n# {name}\n\nBody text."
    path = task_dir / f"{name}.md"
    path.write_text(body, encoding="utf-8")
    return path


@pytest.fixture
def tmp_task_vault(tmp_path: Path) -> Path:
    """Vault with an empty ``task/`` subdir, matching the scheduler's
    real walk path.
    """
    (tmp_path / "task").mkdir()
    return tmp_path


# ---------------------------------------------------------------------------
# find_due_reminders
# ---------------------------------------------------------------------------


def test_find_due_reminders_returns_past_due(tmp_task_vault: Path) -> None:
    task_dir = tmp_task_vault / "task"
    _write_task(
        task_dir, "Call Dr Bailey",
        remind_at="2026-04-20T17:00:00+00:00",  # 1h ago from NOW
    )

    due, stale = find_due_reminders(tmp_task_vault, NOW, stale_max_minutes=180)
    assert len(due) == 1
    assert len(stale) == 0
    assert due[0].title == "Call Dr Bailey"
    assert due[0].status == "todo"


def test_find_due_reminders_skips_future(tmp_task_vault: Path) -> None:
    task_dir = tmp_task_vault / "task"
    _write_task(
        task_dir, "Tomorrow task",
        remind_at="2099-04-20T18:00:00+00:00",
    )

    due, stale = find_due_reminders(tmp_task_vault, NOW, stale_max_minutes=180)
    assert due == []
    assert stale == []


def test_find_due_reminders_skips_already_reminded(tmp_task_vault: Path) -> None:
    """When reminded_at >= remind_at, we don't fire again."""
    task_dir = tmp_task_vault / "task"
    _write_task(
        task_dir, "Already sent",
        remind_at="2026-04-20T17:00:00+00:00",
        reminded_at="2026-04-20T17:00:01+00:00",
    )

    due, _stale = find_due_reminders(tmp_task_vault, NOW, stale_max_minutes=180)
    assert due == []


def test_find_due_reminders_re_arms_when_remind_at_moves_forward(
    tmp_task_vault: Path,
) -> None:
    """Updating remind_at to a new later value after it was last fired re-arms."""
    task_dir = tmp_task_vault / "task"
    _write_task(
        task_dir, "Follow-up",
        remind_at="2026-04-20T17:30:00+00:00",  # new time, in past
        reminded_at="2026-04-19T00:00:00+00:00",  # older than remind_at
    )

    due, _ = find_due_reminders(tmp_task_vault, NOW, stale_max_minutes=180)
    assert len(due) == 1
    assert due[0].title == "Follow-up"


def test_find_due_reminders_skips_wrong_status(tmp_task_vault: Path) -> None:
    task_dir = tmp_task_vault / "task"
    _write_task(
        task_dir, "Done task", status="done",
        remind_at="2026-04-20T17:00:00+00:00",
    )
    _write_task(
        task_dir, "Cancelled task", status="cancelled",
        remind_at="2026-04-20T17:00:00+00:00",
    )

    due, stale = find_due_reminders(tmp_task_vault, NOW, stale_max_minutes=180)
    assert due == []
    assert stale == []


def test_find_due_reminders_splits_stale_from_live(tmp_task_vault: Path) -> None:
    """Reminders older than stale_max_minutes go into the stale list."""
    task_dir = tmp_task_vault / "task"
    _write_task(
        task_dir, "Fresh reminder",
        remind_at="2026-04-20T17:30:00+00:00",  # 30m stale
    )
    _write_task(
        task_dir, "Stale reminder",
        remind_at="2026-04-17T00:00:00+00:00",  # days stale
    )

    due, stale = find_due_reminders(tmp_task_vault, NOW, stale_max_minutes=180)
    assert [e.title for e in due] == ["Fresh reminder"]
    assert [e.title for e in stale] == ["Stale reminder"]


def test_find_due_reminders_no_task_dir(tmp_path: Path) -> None:
    """Missing task/ directory returns empty — don't raise."""
    due, stale = find_due_reminders(tmp_path, NOW, stale_max_minutes=180)
    assert due == []
    assert stale == []


# ---------------------------------------------------------------------------
# format_reminder
# ---------------------------------------------------------------------------


def test_format_reminder_uses_reminder_text_when_set(tmp_task_vault: Path) -> None:
    task_dir = tmp_task_vault / "task"
    _write_task(
        task_dir, "Fuel check",
        remind_at="2026-04-20T17:00:00+00:00",
        reminder_text="Get gas before the route",
    )
    due, _ = find_due_reminders(tmp_task_vault, NOW, stale_max_minutes=180)
    assert format_reminder(due[0]) == "Get gas before the route"


def test_format_reminder_includes_due_when_present(tmp_task_vault: Path) -> None:
    task_dir = tmp_task_vault / "task"
    _write_task(
        task_dir, "Call Dr Bailey",
        remind_at="2026-04-20T17:00:00+00:00",
        due="2026-04-24",
    )
    due, _ = find_due_reminders(tmp_task_vault, NOW, stale_max_minutes=180)
    assert format_reminder(due[0]) == "Reminder: Call Dr Bailey (due 2026-04-24)"


def test_format_reminder_title_only_when_no_due(tmp_task_vault: Path) -> None:
    task_dir = tmp_task_vault / "task"
    _write_task(
        task_dir, "Plain reminder",
        remind_at="2026-04-20T17:00:00+00:00",
    )
    due, _ = find_due_reminders(tmp_task_vault, NOW, stale_max_minutes=180)
    assert format_reminder(due[0]) == "Reminder: Plain reminder"


# ---------------------------------------------------------------------------
# clear_remind_at_and_stamp
# ---------------------------------------------------------------------------


def test_clear_remind_at_and_stamp_mutates_frontmatter(tmp_task_vault: Path) -> None:
    task_dir = tmp_task_vault / "task"
    _write_task(
        task_dir, "Call Dr Bailey",
        remind_at="2026-04-20T17:00:00+00:00",
    )

    due, _ = find_due_reminders(tmp_task_vault, NOW, stale_max_minutes=180)
    assert len(due) == 1
    clear_remind_at_and_stamp(due[0], NOW)

    # Re-scan — the stamped task should no longer be due.
    due_after, _ = find_due_reminders(tmp_task_vault, NOW, stale_max_minutes=180)
    assert due_after == []

    # Inspect the file directly.
    import frontmatter
    post = frontmatter.load(str(due[0].abs_path))
    assert "remind_at" not in post.metadata
    assert post.metadata["reminded_at"] == NOW.isoformat()
    assert "<!-- ALFRED:REMINDER" in post.content
    assert NOW.isoformat() in post.content


def test_clear_remind_at_and_stamp_idempotent_same_timestamp(
    tmp_task_vault: Path,
) -> None:
    """Re-stamping with the same ``now`` doesn't duplicate the audit line.

    A second ``_tick`` that races to the same record (unlikely but
    possible) would otherwise stack identical audit lines.
    """
    task_dir = tmp_task_vault / "task"
    _write_task(
        task_dir, "Idempotent",
        remind_at="2026-04-20T17:00:00+00:00",
    )
    due, _ = find_due_reminders(tmp_task_vault, NOW, stale_max_minutes=180)
    clear_remind_at_and_stamp(due[0], NOW)
    clear_remind_at_and_stamp(due[0], NOW)  # exact same timestamp
    content = due[0].abs_path.read_text(encoding="utf-8")
    assert content.count("<!-- ALFRED:REMINDER") == 1


# ---------------------------------------------------------------------------
# _tick — end-to-end
# ---------------------------------------------------------------------------


async def test_tick_fires_due_and_dead_letters_stale(tmp_task_vault: Path) -> None:
    task_dir = tmp_task_vault / "task"
    _write_task(
        task_dir, "Fresh",
        remind_at="2026-04-20T17:30:00+00:00",  # 30m stale — within window
    )
    _write_task(
        task_dir, "Stale",
        remind_at="2026-04-17T00:00:00+00:00",  # > 180m stale
    )

    sent: list[dict] = []

    async def _send(user_id: int, text: str, dedupe_key: str | None = None) -> list[int]:
        sent.append({"user_id": user_id, "text": text, "dedupe_key": dedupe_key})
        return [100 + len(sent)]

    config = TransportConfig(
        server=ServerConfig(),
        scheduler=SchedulerConfig(
            poll_interval_seconds=30,
            stale_reminder_max_minutes=180,
        ),
        auth=AuthConfig(),
        state=StateConfig(),
    )
    state = TransportState.create(tmp_task_vault / "state.json")

    # Freeze "now" by monkey-patching the module's datetime import.
    # Simpler: pick a stale cutoff and a time window that makes the
    # stale/fresh split deterministic without mocking clocks. The
    # scheduler calls ``datetime.now(UTC)`` — so we can't trivially
    # freeze it inside _tick. Instead we shift the test fixture
    # timestamps so "now" being the real clock still gives us the
    # intended stale/fresh split.
    from datetime import datetime as _dt, timezone as _tz
    real_now = _dt.now(_tz.utc)

    # Rewrite the task records to use times relative to real_now.
    task_dir_files = sorted(task_dir.glob("*.md"))
    fresh_path = next(p for p in task_dir_files if p.stem == "Fresh")
    stale_path = next(p for p in task_dir_files if p.stem == "Stale")

    fresh_remind = (real_now - timedelta(minutes=30)).isoformat()
    stale_remind = (real_now - timedelta(hours=48)).isoformat()
    fresh_path.write_text(
        fresh_path.read_text().replace(
            "2026-04-20T17:30:00+00:00", fresh_remind,
        ),
    )
    stale_path.write_text(
        stale_path.read_text().replace(
            "2026-04-17T00:00:00+00:00", stale_remind,
        ),
    )

    await _tick(config, state, _send, tmp_task_vault, user_id=42)

    # Fresh was dispatched.
    assert len(sent) == 1
    assert sent[0]["user_id"] == 42
    assert sent[0]["text"].startswith("Reminder: Fresh")
    assert "reminder-task/Fresh.md" in sent[0]["dedupe_key"]

    # Stale was dead-lettered.
    assert len(state.dead_letter) == 1
    assert state.dead_letter[0]["dead_letter_reason"] == (
        "stale_reminder_window_exceeded"
    )

    # Send log captured the fresh dispatch.
    assert len(state.send_log) == 1


async def test_tick_drains_scheduled_pending_queue(tmp_task_vault: Path) -> None:
    """pending_queue entries whose scheduled_at has passed get sent."""
    from datetime import datetime as _dt, timezone as _tz
    real_now = _dt.now(_tz.utc)

    sent: list[dict] = []

    async def _send(user_id: int, text: str, dedupe_key: str | None = None) -> list[int]:
        sent.append({"user_id": user_id, "text": text})
        return [42]

    config = TransportConfig(
        server=ServerConfig(),
        scheduler=SchedulerConfig(),
        auth=AuthConfig(),
        state=StateConfig(),
    )
    state = TransportState.create(tmp_task_vault / "state.json")
    state.enqueue({
        "id": "past-scheduled",
        "user_id": 42,
        "text": "Scheduled send due",
        "scheduled_at": (real_now - timedelta(minutes=5)).isoformat(),
    })
    state.enqueue({
        "id": "future-scheduled",
        "user_id": 42,
        "text": "Not yet",
        "scheduled_at": (real_now + timedelta(hours=1)).isoformat(),
    })

    await _tick(config, state, _send, tmp_task_vault, user_id=42)

    assert [s["text"] for s in sent] == ["Scheduled send due"]
    assert len(state.pending_queue) == 1  # future entry survives
    assert state.pending_queue[0]["id"] == "future-scheduled"


async def test_tick_retains_pending_on_send_failure(tmp_task_vault: Path) -> None:
    """If send_fn raises for a pending-queue entry, the entry is re-parked."""
    from datetime import datetime as _dt, timezone as _tz
    real_now = _dt.now(_tz.utc)

    async def _send(user_id: int, text: str, dedupe_key: str | None = None) -> list[int]:
        raise RuntimeError("telegram temporarily down")

    config = TransportConfig(
        server=ServerConfig(),
        scheduler=SchedulerConfig(),
        auth=AuthConfig(),
        state=StateConfig(),
    )
    state = TransportState.create(tmp_task_vault / "state.json")
    state.enqueue({
        "id": "will-retry",
        "user_id": 42,
        "text": "Eventually",
        "scheduled_at": (real_now - timedelta(minutes=1)).isoformat(),
    })

    await _tick(config, state, _send, tmp_task_vault, user_id=42)
    # Entry is back in the queue for next tick.
    assert any(e.get("id") == "will-retry" for e in state.pending_queue)


# ---------------------------------------------------------------------------
# Schema + scope + SKILL contract (cross-agent c4 safety net)
# ---------------------------------------------------------------------------


def test_schema_exposes_reminder_fields() -> None:
    """The schema module must document the reminder fields tuple.

    Bundled with the scheduler contract — if this tuple disappears
    the scheduler documentation above the dataclass drifts silently.
    """
    from alfred.vault import schema

    assert hasattr(schema, "REMINDER_FIELDS")
    assert set(schema.REMINDER_FIELDS) == {
        "remind_at", "reminded_at", "reminder_text",
    }


def test_talker_scope_permits_task_edits() -> None:
    """Talker scope allows edits to task records (any field).

    The SKILL's Setting-Reminders section assumes the talker can
    ``set_fields`` ``remind_at`` / ``reminder_text`` on task records.
    If the scope narrows to an allowlist later, this test surfaces
    the drift immediately.
    """
    from alfred.vault.scope import check_scope

    # Unconstrained edit — no ScopeError.
    check_scope(
        scope="talker",
        operation="edit",
        rel_path="task/Call Dr Bailey.md",
        record_type="task",
        fields=["remind_at", "reminder_text"],
    )


def test_talker_skill_has_setting_reminders_section() -> None:
    """The SKILL must document the remind_at contract.

    Cross-agent c4 contract: schema + scope + SKILL update ship
    together. This is the belt-and-braces — if a future edit drops
    the section, the test surfaces it in CI before operators hit
    the gap.
    """
    from alfred._data import get_skills_dir

    skill_path = get_skills_dir() / "vault-talker" / "SKILL.md"
    content = skill_path.read_text(encoding="utf-8")
    # Section heading + both contract keywords.
    assert "## Setting reminders" in content
    assert "remind_at" in content
    assert "reminder_text" in content
