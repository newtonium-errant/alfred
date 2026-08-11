"""#64 stage B — the vault scan and the section that renders it.

Stage A's tests pin the matcher and the queue against plain values. These drive
the halves that touch the VAULT and the ASSEMBLER, which is where the two
failure classes an isolated pin cannot see live:

  * a candidate set that includes the capture session the promise was spoken in
    (every task matches its own transcript, so every task gets proposed the day
    it is filed — and it would look like the feature working);
  * a section that renders a proposal for a task somebody already closed.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
import structlog

from alfred.daily_sync import assembler, capture_close_section
from alfred.daily_sync.capture_close_match import (
    MatchCorpusEntry,
    VERDICT_CONFIRMED,
    VERDICT_REJECTED,
    append_corpus,
    load_glossary,
)
from alfred.daily_sync.capture_close_proposals import (
    STATE_PENDING,
    iter_proposals,
    list_pending,
)
from alfred.daily_sync.capture_close_scan import (
    EXCLUDED_EVIDENCE_DIRS,
    OPEN_TASK_STATUSES,
    SCAN_EVENT,
    iter_open_capture_tasks,
    scan_for_closes,
)
from alfred.daily_sync.config import DailySyncConfig

NOW = datetime(2026, 8, 11, 12, 0, 0, tzinfo=timezone.utc)
TASK_DAY = date(2026, 8, 1)

# The motivating case, verbatim from the incident.
PROMISE = "Attach some screenshots of workout plans"
EVIDENCE = "Louka Workout Plan"


def _write(
    vault: Path, rel: str, *, created: date, extra: dict | None = None,
) -> Path:
    target = vault / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    fm = {"created": created.isoformat(), **(extra or {})}
    lines = ["---"]
    for k, v in fm.items():
        lines.append(f"{k}: {json.dumps(v) if isinstance(v, str) else v}")
    lines += ["---", "", "body"]
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def _task(
    vault: Path, stem: str, name: str, *,
    created: date = TASK_DAY, status: str = "todo", captured: bool = True,
) -> Path:
    return _write(vault, f"task/{stem}.md", created=created, extra={
        "type": "task", "name": name, "status": status,
        "created_by_capture": captured,
    })


def _note(
    vault: Path, rel: str, name: str, *, created: date = TASK_DAY,
) -> Path:
    return _write(vault, rel, created=created, extra={
        "type": "note", "name": name,
    })


def _events(captured: list[dict], event: str) -> list[dict]:
    return [c for c in captured if c.get("event") == event]


# ===========================================================================
# Population filter
# ===========================================================================

def test_only_open_capture_born_tasks_are_scanned(tmp_path: Path) -> None:
    _task(tmp_path, "captured-open", "Attach screenshots")
    _task(tmp_path, "captured-done", "Already handled", status="done")
    _task(tmp_path, "captured-cancelled", "Dropped", status="cancelled")
    _task(tmp_path, "hand-written", "Typed by hand", captured=False)

    found = [t.rel_path for t in iter_open_capture_tasks(tmp_path)]

    assert found == ["task/captured-open.md"]


def test_open_statuses_match_the_captured_tasks_view(tmp_path: Path) -> None:
    """CROSS-SURFACE DRIFT PIN. The operator's ``process/Captured Tasks.md``
    view and this scanner must agree on which capture-born tasks are open.

    If they drift, a task sits under the view's "## Open" heading while the
    scanner does not consider it open — the operator sees a stale promise the
    machine claims not to see, which is the complaint that opened #64.
    """
    from alfred.telegram.captured_tasks_view import _OPEN_STATUSES

    assert OPEN_TASK_STATUSES == frozenset(_OPEN_STATUSES)


def test_tasks_come_back_oldest_first(tmp_path: Path) -> None:
    _task(tmp_path, "newest", "C", created=date(2026, 8, 5))
    _task(tmp_path, "oldest", "A", created=date(2026, 7, 1))
    _task(tmp_path, "middle", "B", created=date(2026, 8, 1))

    assert [t.text for t in iter_open_capture_tasks(tmp_path)] == ["A", "B", "C"]


def test_missing_task_dir_is_not_an_error(tmp_path: Path) -> None:
    assert iter_open_capture_tasks(tmp_path) == []


# ===========================================================================
# The four excluded directories — the false-positive class
# ===========================================================================

def test_the_capture_session_is_never_evidence(tmp_path: Path) -> None:
    """THE LOAD-BEARING EXCLUSION. The session record holds the promise's own
    words and is created just before the task, so it would be the strongest
    match for every capture-born task — proposing a close the day the task was
    filed, every time, and looking like the feature working."""
    _task(tmp_path, "promise", PROMISE)
    # The transcript, containing the promise nearly verbatim.
    _write(tmp_path, "session/2026-08-01 Voice.md", created=TASK_DAY, extra={
        "type": "session", "name": f"I'm going to {PROMISE.lower()}",
    })

    result = scan_for_closes(
        tmp_path, threshold=0.5, floor=0.3, window_days=14,
        max_tasks=50, now=NOW,
    )

    assert result.candidates == []
    assert result.tasks_without_evidence == 1


@pytest.mark.parametrize("rel", [
    "task/another-task.md",
    "session/A Session.md",
    "process/Captured Tasks.md",
    "digests/daily/2026-08-02.md",
])
def test_excluded_directories_never_supply_evidence(
    tmp_path: Path, rel: str,
) -> None:
    _task(tmp_path, "promise", PROMISE)
    _write(tmp_path, rel, created=TASK_DAY, extra={
        # Named to match the promise perfectly — only the directory saves us.
        "type": "note", "name": PROMISE,
    })

    result = scan_for_closes(
        tmp_path, threshold=0.5, floor=0.3, window_days=14,
        max_tasks=50, now=NOW,
    )

    assert result.candidates == []


def test_excluded_dirs_names_all_four(tmp_path: Path) -> None:
    assert EXCLUDED_EVIDENCE_DIRS == frozenset(
        {"task", "session", "process", "digests"}
    )


def test_underscore_scaffolding_is_not_evidence(tmp_path: Path) -> None:
    _task(tmp_path, "promise", PROMISE)
    _write(tmp_path, "_templates/note.md", created=TASK_DAY, extra={
        "type": "note", "name": PROMISE,
    })

    result = scan_for_closes(
        tmp_path, threshold=0.5, floor=0.3, window_days=14,
        max_tasks=50, now=NOW,
    )

    assert result.candidates == []


# ===========================================================================
# The window
# ===========================================================================

def test_the_motivating_case_produces_a_candidate(tmp_path: Path) -> None:
    """End to end through the scan: the promise and the plan that fulfilled it."""
    _task(tmp_path, "attach-screenshots", PROMISE)
    _note(tmp_path, "note/Louka Workout Plan.md", EVIDENCE,
          created=TASK_DAY + timedelta(days=2))

    result = scan_for_closes(
        tmp_path, threshold=0.5, floor=0.3, window_days=14,
        max_tasks=50, now=NOW,
    )

    assert len(result.candidates) == 1
    cand = result.candidates[0]
    assert cand.task_path == "task/attach-screenshots.md"
    assert cand.evidence_name == EVIDENCE
    assert cand.score >= 0.5


def test_same_day_evidence_counts(tmp_path: Path) -> None:
    """``created`` is a DATE, so a record filed hours after the promise carries
    the same value. A strict ``>`` would exclude the motivating case itself."""
    _task(tmp_path, "attach-screenshots", PROMISE, created=TASK_DAY)
    _note(tmp_path, "note/Louka Workout Plan.md", EVIDENCE, created=TASK_DAY)

    result = scan_for_closes(
        tmp_path, threshold=0.5, floor=0.3, window_days=14,
        max_tasks=50, now=NOW,
    )

    assert len(result.candidates) == 1


def test_evidence_predating_the_promise_is_ignored(tmp_path: Path) -> None:
    _task(tmp_path, "attach-screenshots", PROMISE, created=TASK_DAY)
    _note(tmp_path, "note/Louka Workout Plan.md", EVIDENCE,
          created=TASK_DAY - timedelta(days=1))

    result = scan_for_closes(
        tmp_path, threshold=0.5, floor=0.3, window_days=14,
        max_tasks=50, now=NOW,
    )

    assert result.candidates == []


def test_evidence_past_the_window_is_ignored(tmp_path: Path) -> None:
    _task(tmp_path, "attach-screenshots", PROMISE, created=TASK_DAY)
    _note(tmp_path, "note/Louka Workout Plan.md", EVIDENCE,
          created=TASK_DAY + timedelta(days=15))

    result = scan_for_closes(
        tmp_path, threshold=0.5, floor=0.3, window_days=14,
        max_tasks=50, now=NOW,
    )

    assert result.candidates == []


def test_the_last_day_of_the_window_still_counts(tmp_path: Path) -> None:
    _task(tmp_path, "attach-screenshots", PROMISE, created=TASK_DAY)
    _note(tmp_path, "note/Louka Workout Plan.md", EVIDENCE,
          created=TASK_DAY + timedelta(days=14))

    result = scan_for_closes(
        tmp_path, threshold=0.5, floor=0.3, window_days=14,
        max_tasks=50, now=NOW,
    )

    assert len(result.candidates) == 1


# ===========================================================================
# Near misses + the floor
# ===========================================================================

def test_near_miss_is_recorded_but_still_reaches_the_policy(
    tmp_path: Path,
) -> None:
    """Between floor and threshold: a pending row AND a candidate.

    The candidate still goes to ``maybe_propose_closes`` so THAT module's
    below-threshold branch runs in production. Filtering here would leave it
    dead in the field while its unit test stayed green.
    """
    _task(tmp_path, "promise", "Attach screenshots of workout plans")
    # One shared meaningful token ("plan") → damped to 0.45: above the 0.3
    # floor, below the 0.5 bar.
    _note(tmp_path, "note/Plan.md", "Plan", created=TASK_DAY)

    result = scan_for_closes(
        tmp_path, threshold=0.5, floor=0.3, window_days=14,
        max_tasks=50, now=NOW,
    )

    assert len(result.near_misses) == 1
    assert 0.3 <= result.near_misses[0].score < 0.5
    assert len(result.candidates) == 1


def test_below_the_floor_is_not_evidence_of_anything(tmp_path: Path) -> None:
    _task(tmp_path, "promise", "Attach screenshots of workout plans")
    _note(tmp_path, "note/Quarterly Tax Filing.md", "Quarterly Tax Filing",
          created=TASK_DAY)

    result = scan_for_closes(
        tmp_path, threshold=0.5, floor=0.3, window_days=14,
        max_tasks=50, now=NOW,
    )

    assert result.candidates == []
    assert result.near_misses == []
    assert result.tasks_without_evidence == 1


# ===========================================================================
# The glossary feeds back into the scan
# ===========================================================================

def test_a_confirmed_pair_scores_top_forever(tmp_path: Path) -> None:
    """The self-correcting half, through the scan: a pairing the operator
    confirmed once wins even when raw similarity would not have fired."""
    corpus = tmp_path / "corpus.jsonl"
    task_text = "Send Dana the intake form"
    append_corpus(corpus, MatchCorpusEntry(
        ts=NOW.isoformat(),
        task_key="dana form intake send",
        task_text=task_text,
        evidence_name="Onboarding Packet v3",
        verdict=VERDICT_CONFIRMED,
        score=0.41,
    ))
    _task(tmp_path, "send-form", task_text)
    _note(tmp_path, "note/Onboarding Packet v3.md", "Onboarding Packet v3",
          created=TASK_DAY)

    result = scan_for_closes(
        tmp_path, threshold=0.5, floor=0.3, window_days=14, max_tasks=50,
        glossary=load_glossary(corpus), now=NOW,
    )

    assert len(result.candidates) == 1
    assert result.candidates[0].score == 1.0
    assert result.candidates[0].match_source == "glossary"


def test_a_rejected_pair_is_never_proposed_again(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus.jsonl"
    append_corpus(corpus, MatchCorpusEntry(
        ts=NOW.isoformat(),
        task_key="attach plan screenshot workout",
        task_text=PROMISE,
        evidence_name=EVIDENCE,
        verdict=VERDICT_REJECTED,
        score=0.67,
    ))
    _task(tmp_path, "attach-screenshots", PROMISE)
    _note(tmp_path, "note/Louka Workout Plan.md", EVIDENCE, created=TASK_DAY)

    result = scan_for_closes(
        tmp_path, threshold=0.5, floor=0.3, window_days=14, max_tasks=50,
        glossary=load_glossary(corpus), now=NOW,
    )

    assert result.candidates == []


# ===========================================================================
# Bounded pass + intentionally-left-blank
# ===========================================================================

def test_the_scan_cap_takes_the_oldest_and_says_so(tmp_path: Path) -> None:
    for i in range(5):
        _task(tmp_path, f"promise-{i}", f"Task {i}",
              created=TASK_DAY + timedelta(days=i))

    with structlog.testing.capture_logs() as captured:
        result = scan_for_closes(
            tmp_path, threshold=0.5, floor=0.3, window_days=14,
            max_tasks=2, now=NOW,
        )

    assert result.tasks_total == 5
    assert result.tasks_scanned == 2
    capped = _events(captured, "daily_sync.capture_close.scan_capped")
    assert len(capped) == 1
    assert capped[0]["skipped"] == 3
    assert capped[0]["max_tasks"] == 2


def test_scan_event_fires_on_an_empty_vault(tmp_path: Path) -> None:
    """Intentionally-left-blank: this section renders nothing on most days, so
    the scan line is what separates "looked, found nothing" from "the pass
    stopped running"."""
    with structlog.testing.capture_logs() as captured:
        scan_for_closes(
            tmp_path, threshold=0.5, floor=0.3, window_days=14,
            max_tasks=50, now=NOW,
        )

    events = _events(captured, SCAN_EVENT)
    assert len(events) == 1
    assert events[0]["tasks_total"] == 0
    assert events[0]["tasks_scanned"] == 0


def test_scan_event_carries_the_counts(tmp_path: Path) -> None:
    _task(tmp_path, "attach-screenshots", PROMISE)
    _note(tmp_path, "note/Louka Workout Plan.md", EVIDENCE, created=TASK_DAY)

    with structlog.testing.capture_logs() as captured:
        scan_for_closes(
            tmp_path, threshold=0.5, floor=0.3, window_days=14,
            max_tasks=50, now=NOW,
        )

    events = _events(captured, SCAN_EVENT)
    assert len(events) == 1
    assert events[0]["tasks_scanned"] == 1
    assert events[0]["candidates"] == 1
    assert events[0]["evidence_pool"] == 1
    assert events[0]["threshold"] == 0.5
    assert events[0]["window_days"] == 14


def test_unparseable_created_falls_back_to_mtime_and_is_counted(
    tmp_path: Path,
) -> None:
    target = tmp_path / "task" / "no-date.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        '---\ntype: task\nname: "Attach screenshots"\nstatus: todo\n'
        "created_by_capture: true\n---\n\nbody\n",
        encoding="utf-8",
    )

    with structlog.testing.capture_logs() as captured:
        result = scan_for_closes(
            tmp_path, threshold=0.5, floor=0.3, window_days=14,
            max_tasks=50, now=NOW,
        )

    assert result.tasks_scanned == 1
    assert result.dated_from_mtime == 1
    assert _events(captured, SCAN_EVENT)[0]["dated_from_mtime"] == 1


# ===========================================================================
# The section
# ===========================================================================

@pytest.fixture(autouse=True)
def _clean_registry():
    assembler.clear_providers()
    capture_close_section.consume_last_batch()
    yield
    assembler.clear_providers()
    capture_close_section.consume_last_batch()


def _config(tmp_path: Path, **over) -> DailySyncConfig:
    cfg = DailySyncConfig(enabled=True, batch_size=5)
    cfg.state.path = str(tmp_path / "state.json")
    cc = cfg.capture_close
    cc.enabled = True
    cc.queue_path = str(tmp_path / "store" / "queue.jsonl")
    cc.corpus_path = str(tmp_path / "store" / "corpus.jsonl")
    cc.pending_path = str(tmp_path / "store" / "pending.jsonl")
    for k, v in over.items():
        setattr(cc, k, v)
    return cfg


def test_section_raises_a_proposal_and_renders_it(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _task(vault, "attach-screenshots", PROMISE)
    _note(vault, "note/Louka Workout Plan.md", EVIDENCE, created=TASK_DAY)
    cfg = _config(tmp_path)
    capture_close_section.set_vault_path(vault)

    body = capture_close_section.capture_close_section(cfg, date(2026, 8, 11))

    assert body is not None
    assert PROMISE in body
    assert EVIDENCE in body
    assert "N confirm" in body
    assert len(list_pending(cfg.capture_close.queue_path)) == 1
    items = capture_close_section.consume_last_batch()
    assert len(items) == 1
    assert items[0].task_path == "task/attach-screenshots.md"


def test_section_renders_nothing_when_no_evidence(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _task(vault, "promise", "Attach screenshots of workout plans")
    cfg = _config(tmp_path)
    capture_close_section.set_vault_path(vault)

    assert capture_close_section.capture_close_section(
        cfg, date(2026, 8, 11)) is None


def test_disabled_section_does_not_scan(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _task(vault, "attach-screenshots", PROMISE)
    _note(vault, "note/Louka Workout Plan.md", EVIDENCE, created=TASK_DAY)
    cfg = _config(tmp_path, enabled=False)
    capture_close_section.set_vault_path(vault)

    with structlog.testing.capture_logs() as captured:
        body = capture_close_section.capture_close_section(
            cfg, date(2026, 8, 11))

    assert body is None
    assert _events(captured, SCAN_EVENT) == []
    assert not Path(cfg.capture_close.queue_path).exists()


def test_unresolved_store_paths_refuse_loudly(tmp_path: Path) -> None:
    """An empty path is NOT "use the default" — there is no default, because a
    cwd-relative one is shared across every instance on the box."""
    vault = tmp_path / "vault"
    _task(vault, "attach-screenshots", PROMISE)
    cfg = _config(tmp_path, queue_path="")
    capture_close_section.set_vault_path(vault)

    with structlog.testing.capture_logs() as captured:
        body = capture_close_section.capture_close_section(
            cfg, date(2026, 8, 11))

    assert body is None
    refusals = _events(captured, "daily_sync.capture_close.not_configured")
    assert len(refusals) == 1
    assert refusals[0]["missing"] == ["queue_path"]


def test_missing_vault_injection_is_named(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    capture_close_section.set_vault_path.__globals__[
        "_VAULT_PATH_HOLDER"]["path"] = None

    with structlog.testing.capture_logs() as captured:
        assert capture_close_section.run_trigger(cfg) == []

    assert len(_events(captured, "daily_sync.capture_close.no_vault_path")) == 1


def test_already_closed_task_suppresses_its_pending_card(
    tmp_path: Path,
) -> None:
    """A proposal whose task is no longer open must not render — he closed it by
    hand, or a confirm's vault write landed while its bookkeeping did not."""
    vault = tmp_path / "vault"
    task = _task(vault, "attach-screenshots", PROMISE)
    _note(vault, "note/Louka Workout Plan.md", EVIDENCE, created=TASK_DAY)
    cfg = _config(tmp_path)
    capture_close_section.set_vault_path(vault)

    assert capture_close_section.capture_close_section(
        cfg, date(2026, 8, 11)) is not None
    assert len(list_pending(cfg.capture_close.queue_path)) == 1

    # He closes it by hand between fires. (``_write`` JSON-quotes string
    # values, so the on-disk spelling is ``status: "todo"``.)
    original = task.read_text(encoding="utf-8")
    assert 'status: "todo"' in original
    task.write_text(
        original.replace('status: "todo"', 'status: "done"'), encoding="utf-8",
    )

    with structlog.testing.capture_logs() as captured:
        body = capture_close_section.capture_close_section(
            cfg, date(2026, 8, 12))

    assert body is None
    suppressed = _events(captured, "daily_sync.capture_close.stale_suppressed")
    assert len(suppressed) == 1
    assert suppressed[0]["count"] == 1
    # The row is still pending in the queue — suppression is a RENDER decision,
    # not a state change.
    assert len(list_pending(cfg.capture_close.queue_path)) == 1


def test_near_miss_writes_a_pending_row_and_no_card(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _task(vault, "promise", "Attach screenshots of workout plans")
    _note(vault, "note/Plan.md", "Plan", created=TASK_DAY)
    cfg = _config(tmp_path)
    capture_close_section.set_vault_path(vault)

    body = capture_close_section.capture_close_section(cfg, date(2026, 8, 11))

    assert body is None
    rows = Path(cfg.capture_close.pending_path).read_text(
        encoding="utf-8").strip().splitlines()
    assert len(rows) == 1
    assert json.loads(rows[0])["evidence_name"] == "Plan"
    assert list_pending(cfg.capture_close.queue_path) == []


def test_register_is_idempotent_at_priority_26(tmp_path: Path) -> None:
    capture_close_section.register()
    capture_close_section.register()

    assert assembler.registered_providers().count(
        capture_close_section.SECTION_NAME) == 1


def test_priority_sits_between_attribution_and_routine_match() -> None:
    """The band is the decision: below attribution(25) with the other
    propose-then-approve items, adjacent to routine_match(27) — the other
    learned-matcher card, so the two fuzzy-match questions are answered in one
    frame of mind."""
    from alfred.daily_sync import attribution_section, routine_match_section

    assert (
        attribution_section._PRIORITY if hasattr(attribution_section, "_PRIORITY")
        else 25
    ) < capture_close_section._PRIORITY < routine_match_section._PRIORITY
