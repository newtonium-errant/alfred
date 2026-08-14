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
    RETURN_KIND_PLAIN,
    RETURN_KIND_SNOOZE,
    RETURN_KIND_WAITING,
    DueReminder,
    classify_return,
    clear_remind_at_and_stamp,
    find_due_reminders,
    format_reminder,
    render_return_line,
    resolve_return_slot,
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

    due, stale, refused, _skipped = find_due_reminders(
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

    due, stale, refused, _skipped = find_due_reminders(
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

    due, _stale, _refused, _skipped = find_due_reminders(
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

    due, _stale, _refused, _skipped = find_due_reminders(
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

    due, stale, refused, _skipped = find_due_reminders(
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

    due, stale, refused, _skipped = find_due_reminders(
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
    due, stale, refused, _skipped = find_due_reminders(
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
    due, _stale, refused, _skipped = find_due_reminders(
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
    due, _stale, refused, _skipped = find_due_reminders(
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
    due, stale, refused, _skipped = find_due_reminders(
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
    due, _stale, _refused, _skipped = find_due_reminders(
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
    due, _stale, _refused, _skipped = find_due_reminders(
        tmp_task_vault, NOW, stale_max_minutes=180,
    )
    assert format_reminder(due[0]) == "Reminder: Call Dr Bailey (due 2026-04-24)"


def test_format_reminder_title_only_when_no_due(tmp_task_vault: Path) -> None:
    task_dir = tmp_task_vault / "task"
    _write_task(
        task_dir, "Plain reminder",
        remind_at=WITHIN_GRACE_REMIND_AT,
    )
    due, _stale, _refused, _skipped = find_due_reminders(
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

    due, _stale, _refused, _skipped = find_due_reminders(
        tmp_task_vault, NOW, stale_max_minutes=180,
    )
    assert len(due) == 1
    clear_remind_at_and_stamp(due[0], NOW)

    # Re-scan — the stamped task should no longer be due.
    due_after, _stale, _refused, _skipped = find_due_reminders(
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
    due, _stale, _refused, _skipped = find_due_reminders(
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


# ---------------------------------------------------------------------------
# Phase 2c+h — return kinds, chase framing, closed-skip signal, re-snooze
# ---------------------------------------------------------------------------


def _entry(
    *,
    waiting_on: str | None = None,
    return_slot: str | None = None,
    due: str | None = None,
    reminder_text: str | None = None,
    title: str = "Some task",
    reminded_at: datetime | None = None,
) -> DueReminder:
    """Build a DueReminder directly.

    Every entry gets a ``remind_at`` — that is the point. A due reminder
    always has one, so any classifier that keys off it would put all of
    these in the same bucket.
    """
    return DueReminder(
        abs_path=Path("/nonexistent/task/x.md"),
        rel_path="task/x.md",
        title=title,
        remind_at=NOW,
        due=due,
        reminder_text=reminder_text,
        status="todo",
        waiting_on=waiting_on,
        return_slot=return_slot,
        reminded_at=reminded_at,
    )


def test_classify_return_snooze_when_return_slot_present() -> None:
    assert classify_return(_entry(return_slot="duty")) == RETURN_KIND_SNOOZE


def test_classify_return_waiting_when_waiting_on_present() -> None:
    assert classify_return(_entry(waiting_on="Carfax")) == RETURN_KIND_WAITING


def test_classify_return_plain_when_neither_field_present() -> None:
    """The third class is a real answer, not a fallback.

    ``task/Order Fergus Tick Meds from Vet.md`` in the live vault is
    exactly this shape: a residual ``remind_at`` and neither field.
    """
    assert classify_return(_entry()) == RETURN_KIND_PLAIN


def test_classify_return_return_slot_outranks_waiting_on() -> None:
    """Both fields set — the explicitly-chosen slot wins."""
    entry = _entry(return_slot="duty", waiting_on="Carfax")
    assert classify_return(entry) == RETURN_KIND_SNOOZE


def test_classify_return_treats_blank_strings_as_absent() -> None:
    """Frontmatter round-trips a blanked field as ``""``.

    Without this, ``waiting_on: ""`` would frame a message as a chase
    against nobody.
    """
    assert classify_return(_entry(return_slot="", waiting_on="")) == (
        RETURN_KIND_PLAIN
    )
    assert classify_return(_entry(waiting_on="   ")) == RETURN_KIND_PLAIN


def test_classify_return_is_not_driven_by_remind_at_presence() -> None:
    """Anti-proxy pin: the discriminator is the FIELDS, never remind_at.

    All three entries carry an identical ``remind_at`` and differ only
    in ``return_slot`` / ``waiting_on``. Any implementation that keys
    off ``remind_at`` — the mismeasurement this rule exists to prevent —
    collapses these into one kind and fails here.

    The assertion is on the whole set rather than one arm, so the pin
    cannot pass while a single class is broken.
    """
    entries = {
        "snooze": _entry(return_slot="duty"),
        "waiting": _entry(waiting_on="Carfax"),
        "plain": _entry(),
    }
    assert all(e.remind_at == NOW for e in entries.values())

    kinds = {name: classify_return(e) for name, e in entries.items()}
    assert kinds == {
        "snooze": RETURN_KIND_SNOOZE,
        "waiting": RETURN_KIND_WAITING,
        "plain": RETURN_KIND_PLAIN,
    }
    assert len(set(kinds.values())) == 3


def test_format_reminder_frames_waiting_item_as_chase() -> None:
    text = format_reminder(_entry(waiting_on="Carfax", title="Fix mileage"))
    assert text == "Chase Carfax: Fix mileage"
    assert "Reminder:" not in text


def test_format_reminder_chase_keeps_due_date() -> None:
    text = format_reminder(
        _entry(waiting_on="Duncan (Cleveland Insurance)",
               title="Confirm cancellation", due="2026-08-21")
    )
    assert text == (
        "Chase Duncan (Cleveland Insurance): Confirm cancellation "
        "(due 2026-08-21)"
    )


def test_format_reminder_operator_text_outranks_chase_framing() -> None:
    """Rule 1 still wins — a generated frame must not overwrite the
    operator's own words."""
    text = format_reminder(
        _entry(waiting_on="Carfax", reminder_text="Ring them, ask for Dave")
    )
    assert text == "Ring them, ask for Dave"


def test_format_reminder_snooze_and_plain_are_not_framed_as_chase() -> None:
    """Positive control for the chase pin: the OTHER kinds must keep the
    plain wording, so the chase arm is proven to be selective rather
    than always-on."""
    assert format_reminder(_entry(return_slot="duty", title="Pay MBF")) == (
        "Reminder: Pay MBF"
    )
    assert format_reminder(_entry(title="Pay MBF")) == "Reminder: Pay MBF"


def test_find_due_reminders_populates_return_kind_fields(
    tmp_task_vault: Path,
) -> None:
    """The fields the routing pass wrote must actually reach the entry."""
    task_dir = tmp_task_vault / "task"
    _write_task(
        task_dir, "Septic", remind_at=WITHIN_GRACE_REMIND_AT,
        extra='return_slot: "routine"\nescalate_on: "2029-09-01"\n'
              'escalate_to: "duty"',
    )
    due, _stale, _refused, _skipped = find_due_reminders(
        tmp_task_vault, NOW, 180,
    )
    assert len(due) == 1
    assert due[0].return_slot == "routine"
    assert due[0].escalate_on == "2029-09-01"
    assert due[0].escalate_to == "duty"
    assert classify_return(due[0]) == RETURN_KIND_SNOOZE


def test_find_due_reminders_closed_task_lands_in_skipped_closed(
    tmp_task_vault: Path,
) -> None:
    """A due reminder on an already-done task is reported, not silent.

    Carries its own positive control: an otherwise-identical OPEN task
    in the same vault must still land in ``due``. Without that, this
    pin would pass just as happily against a scanner that returned
    nothing at all.
    """
    task_dir = tmp_task_vault / "task"
    _write_task(
        task_dir, "Already done", status="done",
        remind_at=WITHIN_GRACE_REMIND_AT,
    )
    _write_task(
        task_dir, "Still open", status="todo",
        remind_at=WITHIN_GRACE_REMIND_AT,
    )

    due, _stale, _refused, skipped = find_due_reminders(
        tmp_task_vault, NOW, 180,
    )

    assert [e.title for e in skipped] == ["Already done"]
    assert skipped[0].status == "done"
    # Positive control — the scan CAN still return a live reminder.
    assert [e.title for e in due] == ["Still open"]


def test_find_due_reminders_future_reminder_on_closed_task_is_not_skipped(
    tmp_task_vault: Path,
) -> None:
    """Not-yet-due is not a skip. A closed task holding a future
    ``remind_at`` is just history."""
    task_dir = tmp_task_vault / "task"
    _write_task(
        task_dir, "Closed future", status="done",
        remind_at=(NOW + timedelta(days=30)).isoformat(),
    )
    due, _stale, _refused, skipped = find_due_reminders(
        tmp_task_vault, NOW, 180,
    )
    assert due == []
    assert skipped == []


def test_find_due_reminders_already_fired_closed_task_is_not_skipped(
    tmp_task_vault: Path,
) -> None:
    """A closed task whose reminder already fired is finished business —
    reporting it every tick would be the noise this signal avoids."""
    task_dir = tmp_task_vault / "task"
    _write_task(
        task_dir, "Closed fired", status="cancelled",
        remind_at=WITHIN_GRACE_REMIND_AT,
        reminded_at=(NOW + timedelta(seconds=1)).isoformat(),
    )
    _due, _stale, _refused, skipped = find_due_reminders(
        tmp_task_vault, NOW, 180,
    )
    assert skipped == []


# ---------------------------------------------------------------------------
# Phase 2c+h — log emission, driven through _tick (the production path)
# ---------------------------------------------------------------------------


def _tick_env(vault: Path) -> tuple[TransportConfig, TransportState, list, object]:
    """Config/state/send-collector for a _tick run."""
    sent: list[dict] = []

    async def _send(
        user_id: int, text: str, dedupe_key: str | None = None,
    ) -> list[int]:
        sent.append({"user_id": user_id, "text": text, "dedupe_key": dedupe_key})
        return [100 + len(sent)]

    config = TransportConfig(
        server=ServerConfig(),
        scheduler=SchedulerConfig(
            poll_interval_seconds=30, stale_reminder_max_minutes=180,
        ),
        auth=AuthConfig(),
        state=StateConfig(),
    )
    state = TransportState.create(vault / "state.json")
    return config, state, sent, _send


def _events(captured: list[dict], name: str) -> list[dict]:
    return [c for c in captured if c.get("event") == name]


async def test_tick_logs_skipped_task_closed(tmp_task_vault: Path) -> None:
    """The closed-task skip must be visible at production log level.

    Drives ``_tick``, not ``find_due_reminders`` — a bucket nobody logs
    is still silence. Positive control in the same test: an open task
    fires, proving the tick ran rather than erroring out early.
    """
    import structlog

    task_dir = tmp_task_vault / "task"
    real_now = datetime.now(timezone.utc)
    fresh = (real_now - timedelta(seconds=30)).isoformat()
    _write_task(task_dir, "Done already", status="done", remind_at=fresh)
    _write_task(task_dir, "Open one", status="todo", remind_at=fresh)

    config, state, sent, send = _tick_env(tmp_task_vault)
    with structlog.testing.capture_logs() as captured:
        await _tick(config, state, send, tmp_task_vault, user_id=42)

    matches = _events(captured, "transport.scheduler.reminders_skipped_task_closed")
    assert len(matches) == 1
    event = matches[0]
    assert event["count"] == 1
    assert event["paths"] == ["task/Done already.md"]
    assert event["statuses"] == ["done"]
    assert event["kinds"] == [RETURN_KIND_PLAIN]

    # Positive control: the tick really ran and the open task fired.
    assert len(sent) == 1
    assert "Open one" in sent[0]["text"]


async def test_tick_emits_no_skip_log_when_nothing_closed(
    tmp_task_vault: Path,
) -> None:
    """Negative control — the signal must not fire spuriously, or it
    stops meaning anything."""
    import structlog

    task_dir = tmp_task_vault / "task"
    real_now = datetime.now(timezone.utc)
    _write_task(
        task_dir, "Open one", status="todo",
        remind_at=(real_now - timedelta(seconds=30)).isoformat(),
    )

    config, state, sent, send = _tick_env(tmp_task_vault)
    with structlog.testing.capture_logs() as captured:
        await _tick(config, state, send, tmp_task_vault, user_id=42)

    assert _events(captured, "transport.scheduler.reminders_skipped_task_closed") == []
    assert len(sent) == 1


async def test_tick_emits_resnooze_signal_on_rearmed_reminder(
    tmp_task_vault: Path,
) -> None:
    """A reminder that fired before and was re-armed is a correction
    signal: the operator pushed it rather than acting on it."""
    import structlog

    task_dir = tmp_task_vault / "task"
    real_now = datetime.now(timezone.utc)
    remind_at = real_now - timedelta(seconds=30)
    previously_reminded = remind_at - timedelta(hours=1)
    _write_task(
        task_dir, "Pay MBF", status="todo",
        remind_at=remind_at.isoformat(),
        reminded_at=previously_reminded.isoformat(),
        extra='return_slot: "duty"',
    )

    config, state, sent, send = _tick_env(tmp_task_vault)
    with structlog.testing.capture_logs() as captured:
        await _tick(config, state, send, tmp_task_vault, user_id=42)

    matches = _events(captured, "transport.scheduler.reminder_resnoozed")
    assert len(matches) == 1
    event = matches[0]
    assert event["kind"] == RETURN_KIND_SNOOZE
    assert event["return_slot"] == "duty"
    assert event["pushed_by_seconds"] == 3600.0
    # It still fires — the signal observes, it does not suppress.
    assert len(sent) == 1


async def test_tick_emits_no_resnooze_on_first_fire(
    tmp_task_vault: Path,
) -> None:
    """Negative control: a first-time fire is not a re-snooze."""
    import structlog

    task_dir = tmp_task_vault / "task"
    real_now = datetime.now(timezone.utc)
    _write_task(
        task_dir, "First fire", status="todo",
        remind_at=(real_now - timedelta(seconds=30)).isoformat(),
    )

    config, state, sent, send = _tick_env(tmp_task_vault)
    with structlog.testing.capture_logs() as captured:
        await _tick(config, state, send, tmp_task_vault, user_id=42)

    assert _events(captured, "transport.scheduler.reminder_resnoozed") == []
    assert len(sent) == 1


async def test_tick_fired_log_carries_return_kind_and_chase_text(
    tmp_task_vault: Path,
) -> None:
    """End-to-end: a waiting item reaches the operator as a chase, and
    the fire log records which kind it was."""
    import structlog

    task_dir = tmp_task_vault / "task"
    real_now = datetime.now(timezone.utc)
    _write_task(
        task_dir, "Fix Carfax mileage", status="todo",
        remind_at=(real_now - timedelta(seconds=30)).isoformat(),
        extra='waiting_on: "Carfax"',
    )

    config, state, sent, send = _tick_env(tmp_task_vault)
    with structlog.testing.capture_logs() as captured:
        await _tick(config, state, send, tmp_task_vault, user_id=42)

    matches = _events(captured, "transport.scheduler.reminder_fired")
    assert len(matches) == 1
    assert matches[0]["kind"] == RETURN_KIND_WAITING
    assert matches[0]["waiting_on"] == "Carfax"

    assert len(sent) == 1
    assert sent[0]["text"] == "Chase Carfax: Fix Carfax mileage"


# ---------------------------------------------------------------------------
# Phase 2c+h — slot resolution + slot-write at fire time
# ---------------------------------------------------------------------------


def test_resolve_return_slot_uses_return_slot() -> None:
    slot, rule = resolve_return_slot(_entry(return_slot="duty"), NOW)
    assert (slot, rule) == ("duty", "return_slot")


def test_resolve_return_slot_applies_operator_routine_alias() -> None:
    """The two live records say ``routine``; the deck speaks ``rhythm``."""
    slot, rule = resolve_return_slot(_entry(return_slot="routine"), NOW)
    assert (slot, rule) == ("rhythm", "return_slot")


def test_resolve_return_slot_escalates_once_escalate_on_has_passed() -> None:
    """Escalation is generic — any record with the two fields, not
    septic-special."""
    entry = _entry(return_slot="routine")
    entry.escalate_on = "2020-01-01"
    entry.escalate_to = "duty"
    slot, rule = resolve_return_slot(entry, NOW)
    assert (slot, rule) == ("duty", "escalated")


def test_resolve_return_slot_does_not_escalate_before_the_date() -> None:
    """Positive control for the escalation pin: the same record before
    its date keeps its ordinary slot, so the pin proves the DATE is what
    fires rather than the mere presence of the fields."""
    entry = _entry(return_slot="routine")
    entry.escalate_on = "2099-01-01"
    entry.escalate_to = "duty"
    slot, rule = resolve_return_slot(entry, NOW)
    assert (slot, rule) == ("rhythm", "return_slot")


def test_resolve_return_slot_malformed_escalate_on_does_not_escalate() -> None:
    """Escalation moves work INTO Duty, so an unparseable date must not
    be able to trigger it. Fail toward the calmer answer."""
    entry = _entry(return_slot="routine")
    entry.escalate_on = "not-a-date"
    entry.escalate_to = "duty"
    slot, rule = resolve_return_slot(entry, NOW)
    assert (slot, rule) == ("rhythm", "return_slot")


def test_resolve_return_slot_none_for_waiting_item() -> None:
    """A chase has no ruled slot — and must not borrow one."""
    slot, rule = resolve_return_slot(_entry(waiting_on="Carfax"), NOW)
    assert (slot, rule) == (None, "none")


def test_resolve_return_slot_reports_unrecognized_separately_from_absent() -> None:
    """``none`` and ``unrecognized`` are different failures: one is a
    record that never asked for a slot, the other is a ruling that did
    not land."""
    slot, rule = resolve_return_slot(_entry(return_slot="wobble"), NOW)
    assert (slot, rule) == (None, "unrecognized")


async def test_tick_writes_ruled_slot_onto_returning_snooze(
    tmp_task_vault: Path,
) -> None:
    """End-to-end delivery: the record comes back carrying its slot, so
    the deck's rule-1 classifier deals it where the operator ruled."""
    import frontmatter

    task_dir = tmp_task_vault / "task"
    real_now = datetime.now(timezone.utc)
    path = _write_task(
        task_dir, "Pay MBF", status="todo",
        remind_at=(real_now - timedelta(seconds=30)).isoformat(),
        extra='return_slot: "duty"',
    )

    config, state, sent, send = _tick_env(tmp_task_vault)
    await _tick(config, state, send, tmp_task_vault, user_id=42)

    fm = frontmatter.load(str(path)).metadata
    assert fm["slot"] == "duty"
    # Written on the SAME round-trip that disarms the reminder.
    assert "remind_at" not in fm
    assert fm.get("reminded_at")
    assert fm["return_slot"] == "duty"  # the ruling itself is preserved


async def test_tick_writes_rhythm_for_operator_routine_wording(
    tmp_task_vault: Path,
) -> None:
    """The real TheJamieClinic/Septic shape, end to end."""
    import frontmatter

    task_dir = tmp_task_vault / "task"
    real_now = datetime.now(timezone.utc)
    path = _write_task(
        task_dir, "Setup TheJamieClinic Email", status="todo",
        remind_at=(real_now - timedelta(seconds=30)).isoformat(),
        extra='return_slot: "routine"',
    )

    config, state, sent, send = _tick_env(tmp_task_vault)
    await _tick(config, state, send, tmp_task_vault, user_id=42)

    assert frontmatter.load(str(path)).metadata["slot"] == "rhythm"


async def test_tick_does_not_write_slot_for_waiting_item(
    tmp_task_vault: Path,
) -> None:
    """Negative control — a chase gets no slot invented for it."""
    import frontmatter

    task_dir = tmp_task_vault / "task"
    real_now = datetime.now(timezone.utc)
    path = _write_task(
        task_dir, "Fix Carfax mileage", status="todo",
        remind_at=(real_now - timedelta(seconds=30)).isoformat(),
        extra='waiting_on: "Carfax"',
    )

    config, state, sent, send = _tick_env(tmp_task_vault)
    await _tick(config, state, send, tmp_task_vault, user_id=42)

    fm = frontmatter.load(str(path)).metadata
    assert "slot" not in fm
    assert "remind_at" not in fm  # still disarmed


async def test_tick_logs_slot_write_and_non_application(
    tmp_task_vault: Path,
) -> None:
    """Both outcomes are observable: a slot that landed, and a ruling
    that did not."""
    import structlog

    task_dir = tmp_task_vault / "task"
    real_now = datetime.now(timezone.utc)
    fresh = (real_now - timedelta(seconds=30)).isoformat()
    _write_task(task_dir, "Good slot", remind_at=fresh,
                extra='return_slot: "duty"')
    _write_task(task_dir, "Typo slot", remind_at=fresh,
                extra='return_slot: "duti"')

    config, state, sent, send = _tick_env(tmp_task_vault)
    with structlog.testing.capture_logs() as captured:
        await _tick(config, state, send, tmp_task_vault, user_id=42)

    written = _events(captured, "transport.scheduler.return_slot_written")
    assert len(written) == 1
    assert written[0]["slot"] == "duty"
    assert written[0]["rule"] == "return_slot"

    lost = _events(captured, "transport.scheduler.return_slot_not_applied")
    assert len(lost) == 1
    assert lost[0]["return_slot"] == "duti"

    assert len(sent) == 2  # both still fired


def test_format_reminder_delegates_to_the_shared_renderer() -> None:
    """One wording, every surface.

    The Telegram wrapper must not drift from the renderer the deck
    reader will call — two surfaces wording the same return differently
    is a bug that only appears in front of the operator.
    """
    for entry in (
        _entry(waiting_on="Carfax", title="Fix mileage"),
        _entry(return_slot="duty", title="Pay MBF", due="2026-08-21"),
        _entry(title="Plain one"),
        _entry(reminder_text="Verbatim", title="Ignored"),
    ):
        assert format_reminder(entry) == render_return_line(entry)
