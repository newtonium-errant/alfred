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

# A remind_at value that's within the past-grace window of NOW —
# scheduler treats this as "fires normally" rather than "refused as
# past-time writer error". 30s past < 60s grace.
#
# Pre-guardrail tests used ``NOW - 1h`` for "fires normally"; that's
# now well outside the grace and lands in ``refused_past_time``.
# Tests that want the legitimate-fire path use this constant instead.
WITHIN_GRACE_REMIND_AT = (NOW - timedelta(seconds=30)).isoformat()
WITHIN_GRACE_REMINDED_AT_OLDER = (NOW - timedelta(days=1)).isoformat()


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
    """Reminder within the past-grace window fires normally."""
    task_dir = tmp_task_vault / "task"
    _write_task(
        task_dir, "Call Dr Bailey",
        # Within 60s grace — legitimate "remind me right now" case.
        remind_at=WITHIN_GRACE_REMIND_AT,
    )

    due, stale, refused = find_due_reminders(
        tmp_task_vault, NOW, stale_max_minutes=180,
    )
    assert len(due) == 1
    assert len(stale) == 0
    assert len(refused) == 0
    assert due[0].title == "Call Dr Bailey"
    assert due[0].status == "todo"


def test_find_due_reminders_skips_future(tmp_task_vault: Path) -> None:
    task_dir = tmp_task_vault / "task"
    _write_task(
        task_dir, "Tomorrow task",
        remind_at="2099-04-20T18:00:00+00:00",
    )

    due, stale, refused = find_due_reminders(
        tmp_task_vault, NOW, stale_max_minutes=180,
    )
    assert due == []
    assert stale == []
    assert refused == []


def test_find_due_reminders_skips_already_reminded(tmp_task_vault: Path) -> None:
    """When reminded_at >= remind_at, we don't fire again."""
    task_dir = tmp_task_vault / "task"
    _write_task(
        task_dir, "Already sent",
        remind_at=WITHIN_GRACE_REMIND_AT,
        # reminded_at >= remind_at — already-fired guard short-circuits.
        reminded_at=NOW.isoformat(),
    )

    due, _stale, _refused = find_due_reminders(
        tmp_task_vault, NOW, stale_max_minutes=180,
    )
    assert due == []


def test_find_due_reminders_re_arms_when_remind_at_moves_forward(
    tmp_task_vault: Path,
) -> None:
    """Updating remind_at to a new later value after it was last fired re-arms."""
    task_dir = tmp_task_vault / "task"
    _write_task(
        task_dir, "Follow-up",
        remind_at=WITHIN_GRACE_REMIND_AT,  # new value, within grace
        reminded_at=WITHIN_GRACE_REMINDED_AT_OLDER,  # older — re-arm fires
    )

    due, _stale, _refused = find_due_reminders(
        tmp_task_vault, NOW, stale_max_minutes=180,
    )
    assert len(due) == 1
    assert due[0].title == "Follow-up"


def test_find_due_reminders_skips_wrong_status(tmp_task_vault: Path) -> None:
    task_dir = tmp_task_vault / "task"
    _write_task(
        task_dir, "Done task", status="done",
        remind_at=WITHIN_GRACE_REMIND_AT,
    )
    _write_task(
        task_dir, "Cancelled task", status="cancelled",
        remind_at=WITHIN_GRACE_REMIND_AT,
    )

    due, stale, refused = find_due_reminders(
        tmp_task_vault, NOW, stale_max_minutes=180,
    )
    assert due == []
    assert stale == []
    assert refused == []


def test_find_due_reminders_splits_stale_from_live(tmp_task_vault: Path) -> None:
    """Reminders within grace fire; older ones land in ``refused_past_time``.

    Pre-guardrail this test asserted "30m past = fresh, days past =
    stale". The new past-grace cutoff (60s) supersedes the stale
    cutoff (3h) — anything more than 60s past is now refused, not
    bucketed as fresh-but-stale. The test reflects the new contract.
    """
    task_dir = tmp_task_vault / "task"
    _write_task(
        task_dir, "Within grace",
        remind_at=WITHIN_GRACE_REMIND_AT,
    )
    _write_task(
        task_dir, "Past grace (30m)",
        remind_at="2026-04-20T17:30:00+00:00",  # 30m past — outside grace
    )
    _write_task(
        task_dir, "Days past",
        remind_at="2026-04-17T00:00:00+00:00",  # days past — outside grace
    )

    due, stale, refused = find_due_reminders(
        tmp_task_vault, NOW, stale_max_minutes=180,
    )
    assert [e.title for e in due] == ["Within grace"]
    # Both past-grace entries refused (the 60s past-grace is narrower
    # than the 3h stale window, so stale is now unreachable in
    # practice — see the dead-code rationale in scheduler.py).
    assert sorted(e.title for e in refused) == sorted([
        "Past grace (30m)", "Days past",
    ])
    assert stale == []


def test_find_due_reminders_no_task_dir(tmp_path: Path) -> None:
    """Missing task/ directory returns empty — don't raise."""
    due, stale, refused = find_due_reminders(
        tmp_path, NOW, stale_max_minutes=180,
    )
    assert due == []
    assert stale == []
    assert refused == []


# ---------------------------------------------------------------------------
# Past-time refusal guardrail (P1 from QA finding)
# ---------------------------------------------------------------------------


def test_refused_past_time_at_grace_boundary_just_past(
    tmp_task_vault: Path,
) -> None:
    """Reminder 90s in the past lands in ``refused_past_time``."""
    task_dir = tmp_task_vault / "task"
    _write_task(
        task_dir, "Slightly past grace",
        remind_at=(NOW - timedelta(seconds=90)).isoformat(),
    )
    due, _stale, refused = find_due_reminders(
        tmp_task_vault, NOW, stale_max_minutes=180,
    )
    assert due == []
    assert len(refused) == 1
    assert refused[0].title == "Slightly past grace"


def test_refused_past_time_at_grace_boundary_just_within(
    tmp_task_vault: Path,
) -> None:
    """Reminder 30s in the past fires normally (within 60s grace)."""
    task_dir = tmp_task_vault / "task"
    _write_task(
        task_dir, "Within grace",
        remind_at=(NOW - timedelta(seconds=30)).isoformat(),
    )
    due, _stale, refused = find_due_reminders(
        tmp_task_vault, NOW, stale_max_minutes=180,
    )
    assert len(due) == 1
    assert refused == []


def test_refused_past_time_six_days_past_qa_repro(
    tmp_task_vault: Path,
) -> None:
    """Direct repro of the QA-finding bug: ``remind_at`` 6 days past.

    Pre-guardrail the scheduler bucketed this as ``stale`` and
    ``clear_remind_at_and_stamp`` consumed it without notifying the
    user (Andrew lost the LASIK reminder). Post-guardrail the same
    input lands in ``refused_past_time`` and the task is left
    intact for the operator to repair.
    """
    task_dir = tmp_task_vault / "task"
    _write_task(
        task_dir, "QA repro",
        remind_at=(NOW - timedelta(days=6)).isoformat(),
    )
    due, stale, refused = find_due_reminders(
        tmp_task_vault, NOW, stale_max_minutes=180,
    )
    assert due == []
    assert stale == []  # narrower past-grace supersedes stale bucketing
    assert len(refused) == 1
    assert refused[0].title == "QA repro"


# ---------------------------------------------------------------------------
# format_reminder
# ---------------------------------------------------------------------------


def test_format_reminder_uses_reminder_text_when_set(tmp_task_vault: Path) -> None:
    task_dir = tmp_task_vault / "task"
    _write_task(
        task_dir, "Fuel check",
        remind_at=WITHIN_GRACE_REMIND_AT,
        reminder_text="Get gas before the route",
    )
    due, _stale, _refused = find_due_reminders(
        tmp_task_vault, NOW, stale_max_minutes=180,
    )
    assert format_reminder(due[0]) == "Get gas before the route"


def test_format_reminder_includes_due_when_present(tmp_task_vault: Path) -> None:
    task_dir = tmp_task_vault / "task"
    _write_task(
        task_dir, "Call Dr Bailey",
        remind_at=WITHIN_GRACE_REMIND_AT,
        due="2026-04-24",
    )
    due, _stale, _refused = find_due_reminders(
        tmp_task_vault, NOW, stale_max_minutes=180,
    )
    assert format_reminder(due[0]) == "Reminder: Call Dr Bailey (due 2026-04-24)"


def test_format_reminder_title_only_when_no_due(tmp_task_vault: Path) -> None:
    task_dir = tmp_task_vault / "task"
    _write_task(
        task_dir, "Plain reminder",
        remind_at=WITHIN_GRACE_REMIND_AT,
    )
    due, _stale, _refused = find_due_reminders(
        tmp_task_vault, NOW, stale_max_minutes=180,
    )
    assert format_reminder(due[0]) == "Reminder: Plain reminder"


# ---------------------------------------------------------------------------
# clear_remind_at_and_stamp
# ---------------------------------------------------------------------------


def test_clear_remind_at_and_stamp_mutates_frontmatter(tmp_task_vault: Path) -> None:
    task_dir = tmp_task_vault / "task"
    _write_task(
        task_dir, "Call Dr Bailey",
        remind_at=WITHIN_GRACE_REMIND_AT,
    )

    due, _stale, _refused = find_due_reminders(
        tmp_task_vault, NOW, stale_max_minutes=180,
    )
    assert len(due) == 1
    clear_remind_at_and_stamp(due[0], NOW)

    # Re-scan — the stamped task should no longer be due.
    due_after, _stale, _refused = find_due_reminders(
        tmp_task_vault, NOW, stale_max_minutes=180,
    )
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
        remind_at=WITHIN_GRACE_REMIND_AT,
    )
    due, _stale, _refused = find_due_reminders(
        tmp_task_vault, NOW, stale_max_minutes=180,
    )
    clear_remind_at_and_stamp(due[0], NOW)
    clear_remind_at_and_stamp(due[0], NOW)  # exact same timestamp
    content = due[0].abs_path.read_text(encoding="utf-8")
    assert content.count("<!-- ALFRED:REMINDER") == 1


# ---------------------------------------------------------------------------
# _tick — end-to-end
# ---------------------------------------------------------------------------


async def test_tick_fires_within_grace_and_refuses_past_grace(
    tmp_task_vault: Path,
) -> None:
    """End-to-end: a within-grace reminder fires; a past-grace one refuses.

    Renamed from ``test_tick_fires_due_and_dead_letters_stale`` —
    pre-guardrail this asserted the stale path dead-letters. The
    new past-grace cutoff (60s) supersedes the stale cutoff (3h),
    so any past-by-more-than-60s reminder refuses instead. The
    refused entry is NOT dispatched, NOT dead-lettered, and NOT
    stamped — the task stays in "todo, no reminder fired" state
    for the operator to repair.
    """
    task_dir = tmp_task_vault / "task"
    _write_task(
        task_dir, "Within grace",
        remind_at="2026-04-20T17:30:00+00:00",  # placeholder — rewritten below
    )
    _write_task(
        task_dir, "Past grace",
        remind_at="2026-04-17T00:00:00+00:00",  # placeholder — rewritten below
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

    # The scheduler calls ``datetime.now(UTC)`` directly so we can't
    # freeze its clock — instead, shift the fixture timestamps so
    # "now" being the real clock still gives the intended split.
    from datetime import datetime as _dt, timezone as _tz
    real_now = _dt.now(_tz.utc)

    task_dir_files = sorted(task_dir.glob("*.md"))
    fresh_path = next(p for p in task_dir_files if p.stem == "Within grace")
    past_path = next(p for p in task_dir_files if p.stem == "Past grace")

    # Within-grace: 30s past — fires normally.
    fresh_remind = (real_now - timedelta(seconds=30)).isoformat()
    # Past-grace: 48h past — refuses (was stale + dead-letter pre-fix).
    past_remind = (real_now - timedelta(hours=48)).isoformat()
    fresh_path.write_text(
        fresh_path.read_text().replace(
            "2026-04-20T17:30:00+00:00", fresh_remind,
        ),
    )
    past_path.write_text(
        past_path.read_text().replace(
            "2026-04-17T00:00:00+00:00", past_remind,
        ),
    )

    await _tick(config, state, _send, tmp_task_vault, user_id=42)

    # Within-grace was dispatched.
    assert len(sent) == 1
    assert sent[0]["user_id"] == 42
    assert sent[0]["text"].startswith("Reminder: Within grace")
    assert "reminder-task/Within grace.md" in sent[0]["dedupe_key"]

    # Past-grace was REFUSED — NOT dead-lettered, NOT consumed.
    assert state.dead_letter == [], (
        f"refused-past-time entries must NOT be dead-lettered. "
        f"Got: {state.dead_letter}"
    )

    # Past-grace task's frontmatter is intact: ``remind_at`` still
    # set, ``reminded_at`` not stamped. Operator can repair the date
    # and the next tick will pick it up.
    import frontmatter
    past_post = frontmatter.load(str(past_path))
    assert past_post.metadata.get("remind_at") == past_remind, (
        "refused task's remind_at must NOT be cleared"
    )
    assert "reminded_at" not in past_post.metadata, (
        "refused task must NOT have reminded_at stamped"
    )
    assert "<!-- ALFRED:REMINDER" not in (past_post.content or ""), (
        "refused task must NOT have a fired audit comment appended"
    )

    # Send log captured ONLY the within-grace dispatch.
    assert len(state.send_log) == 1
    assert "Within grace" in state.send_log[0]["text"]


async def test_tick_logs_warning_for_refused_past_time(
    tmp_task_vault: Path,
) -> None:
    """Past-grace refusal emits ``scheduler.reminder_refused_past_time``.

    Operator greps the warning to spot tasks Salem (or another
    writer) miscalculated. Per ``feedback_intentionally_left_blank.md``:
    silent suppression is the bug — the warning must be loud and
    re-emitted on every tick the task is seen until the operator
    repairs the date.

    Uses ``structlog.testing.capture_logs`` per
    ``feedback_structlog_assertion_patterns.md`` — pytest's caplog
    doesn't reliably capture from async code paths in this codebase.
    """
    from structlog.testing import capture_logs

    task_dir = tmp_task_vault / "task"
    _write_task(
        task_dir, "Past grace task",
        remind_at="2026-04-17T00:00:00+00:00",  # placeholder
    )

    async def _send(user_id: int, text: str, dedupe_key: str | None = None) -> list[int]:
        return [42]

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

    from datetime import datetime as _dt, timezone as _tz
    real_now = _dt.now(_tz.utc)
    past_path = next((task_dir).glob("*.md"))
    past_remind = (real_now - timedelta(hours=48)).isoformat()
    past_path.write_text(
        past_path.read_text().replace(
            "2026-04-17T00:00:00+00:00", past_remind,
        ),
    )

    with capture_logs() as captured:
        await _tick(config, state, _send, tmp_task_vault, user_id=42)

    refusal_logs = [
        c for c in captured
        if c.get("event") == "transport.scheduler.reminder_refused_past_time"
    ]
    assert len(refusal_logs) == 1, (
        f"expected exactly one refusal log, got {len(refusal_logs)}. "
        f"All captured: {[c.get('event') for c in captured]}"
    )
    log_entry = refusal_logs[0]
    assert log_entry["log_level"] == "warning"
    assert log_entry["title"] == "Past grace task"
    assert "task/Past grace task.md" in log_entry["path"]
    assert log_entry["delta_seconds"] < -3600  # ~48h past
    assert log_entry["grace_seconds"] == 60
    assert "hint" in log_entry  # operator pointer present


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
