"""Round-trip test per tool: populate → save → reload → assert equality.

Each tool that persists JSON state has the same atomic-write contract
(``path.with_suffix('.tmp')`` → ``os.replace``). These tests are the
cross-cutting safety net: if any tool's ``to_dict`` / ``from_dict``
drifts out of sync, the round-trip assertion surfaces it immediately.

Scope discipline (per step-c task brief): ONE round-trip per tool.
Not a deep read of every state code path — just proof that the JSON
schema holds for a representative mutation.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Curator
# ---------------------------------------------------------------------------

def test_curator_state_roundtrip(state_path: Path) -> None:
    """Curator state tracks processed inbox files and their outputs."""
    from alfred.curator.state import StateManager

    mgr = StateManager(state_path)
    mgr.state.mark_processed(
        filename="inbox_item_42.md",
        inbox_path="inbox/inbox_item_42.md",
        files_created=["person/Alice.md", "note/Hello.md"],
        files_modified=["project/Alfred.md"],
        backend_used="claude",
    )
    mgr.save()

    assert state_path.exists()
    # .tmp file should NOT linger — atomic-rename contract
    assert not state_path.with_suffix(".tmp").exists()

    reloaded = StateManager(state_path)
    state = reloaded.load()

    assert state.version == 2
    assert state.is_processed("inbox_item_42.md")
    entry = state.processed["inbox_item_42.md"]
    assert entry.inbox_path == "inbox/inbox_item_42.md"
    assert entry.files_created == ["person/Alice.md", "note/Hello.md"]
    assert entry.files_modified == ["project/Alfred.md"]
    assert entry.backend_used == "claude"
    # last_run gets stamped on mark_processed; just confirm it's an ISO-ish string
    assert state.last_run and "T" in state.last_run


# ---------------------------------------------------------------------------
# Janitor
# ---------------------------------------------------------------------------

def test_janitor_state_roundtrip(state_path: Path) -> None:
    """Janitor state tracks file hashes, open issues, fix log, triage IDs."""
    from alfred.janitor.issues import FixLogEntry, SweepResult
    from alfred.janitor.state import JanitorState

    state = JanitorState(state_path, max_sweep_history=5)
    state.update_file("person/Alice.md", md5="abc123", issue_codes=["LINK001"])
    state.update_file("task/Ship it.md", md5="def456", issue_codes=[])

    sweep = SweepResult(
        sweep_id="sweep-01",
        timestamp=datetime.now(timezone.utc).isoformat(),
        files_scanned=2,
        issues_found=1,
        files_fixed=0,
    )
    state.add_sweep(sweep)

    state.add_fix_log(
        FixLogEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            sweep_id="sweep-01",
            file="person/Alice.md",
            issue_code="LINK001",
            action="fixed",
            detail="Resolved [[person/alice]] to [[person/Alice]]",
        )
    )
    state.ignore_file("inbox/processed/old.md", reason="processed")
    # JanitorState exposes pending_writes as a plain dict — callers mutate directly.
    state.pending_writes["person/Bob.md"] = "pending123"
    state.last_deep_sweep = "2026-04-19T10:00:00+00:00"
    state.mark_triage_seen("triage-deadbeef")
    state.save_sweep_issues({"person/Alice.md": ["LINK001"]})
    state.record_enrichment_attempt("person/Alice.md", max_attempts=10)

    state.save()
    assert state_path.exists()
    assert not state_path.with_suffix(".tmp").exists()

    reloaded = JanitorState(state_path, max_sweep_history=5)
    reloaded.load()

    assert reloaded.version == 1
    assert "person/Alice.md" in reloaded.files
    assert reloaded.files["person/Alice.md"].md5 == "abc123"
    assert reloaded.files["person/Alice.md"].open_issues == ["LINK001"]
    assert reloaded.files["person/Alice.md"].enrichment_attempts == 1
    assert "sweep-01" in reloaded.sweeps
    assert reloaded.sweeps["sweep-01"].files_scanned == 2
    assert len(reloaded.fix_log) == 1
    assert reloaded.fix_log[0].issue_code == "LINK001"
    assert reloaded.ignored == {"inbox/processed/old.md": "processed"}
    assert reloaded.pending_writes == {"person/Bob.md": "pending123"}
    assert reloaded.last_deep_sweep == "2026-04-19T10:00:00+00:00"
    assert reloaded.has_seen_triage("triage-deadbeef")
    assert reloaded.previous_sweep_issues == {"person/Alice.md": ["LINK001"]}


# ---------------------------------------------------------------------------
# Distiller
# ---------------------------------------------------------------------------

def test_distiller_state_roundtrip(state_path: Path) -> None:
    """Distiller state tracks file MD5s, extraction runs, and audit log."""
    from alfred.distiller.state import (
        DistillerState,
        ExtractionLogEntry,
        RunResult,
    )

    state = DistillerState(state_path, max_run_history=5)
    state.update_file(
        "session/Foo.md",
        md5="aaa",
        learn_records=["decision/Use Python.md"],
    )

    run = RunResult(
        run_id="run-01",
        timestamp=datetime.now(timezone.utc).isoformat(),
        candidates_found=3,
        candidates_processed=2,
        records_created={"decision": 1, "assumption": 1},
        batches=1,
    )
    state.add_run(run)

    state.add_log_entry(
        ExtractionLogEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            run_id="run-01",
            action="created",
            learn_type="decision",
            learn_file="decision/Use Python.md",
            source_files=["session/Foo.md"],
            detail="Decided on Python after discussion",
        )
    )
    state.pending_writes["decision/Use Python.md"] = "pending456"
    state.last_deep_extraction = "2026-04-19T12:00:00+00:00"

    state.save()
    assert state_path.exists()
    assert not state_path.with_suffix(".tmp").exists()

    reloaded = DistillerState(state_path, max_run_history=5)
    reloaded.load()

    assert reloaded.version == 1
    assert "session/Foo.md" in reloaded.files
    assert reloaded.files["session/Foo.md"].md5 == "aaa"
    assert reloaded.files["session/Foo.md"].learn_records_created == [
        "decision/Use Python.md"
    ]
    assert "run-01" in reloaded.runs
    assert reloaded.runs["run-01"].records_created == {
        "decision": 1,
        "assumption": 1,
    }
    assert len(reloaded.extraction_log) == 1
    assert reloaded.extraction_log[0].learn_type == "decision"
    assert reloaded.pending_writes == {"decision/Use Python.md": "pending456"}
    assert reloaded.last_deep_extraction == "2026-04-19T12:00:00+00:00"


# ---------------------------------------------------------------------------
# Surveyor
# ---------------------------------------------------------------------------

def test_surveyor_state_roundtrip(state_path: Path) -> None:
    """Surveyor state tracks per-file embedding metadata + clusters."""
    from alfred.surveyor.state import ClusterState, PipelineState

    state = PipelineState(state_path)
    state.update_file("note/One.md", md5="h1")
    state.update_file("note/Two.md", md5="h2")
    state.mark_embedded("note/One.md")
    state.update_clusters(
        semantic_assignments={"note/One.md": 3, "note/Two.md": 3},
        structural_assignments={"note/One.md": 1, "note/Two.md": 2},
    )
    # update_clusters only updates per-file assignments. Cluster metadata
    # (labels, members) is written by the label-writer stage — populate
    # directly to exercise the ClusterState round-trip.
    state.clusters["3"] = ClusterState(
        label=["testing", "notes"],
        member_files=["note/One.md", "note/Two.md"],
        last_labeled="2026-04-19T00:00:00+00:00",
    )
    state.mark_pending_write("note/Three.md", expected_md5="hpending")

    state.save()
    assert state_path.exists()
    assert not state_path.with_suffix(".tmp").exists()

    reloaded = PipelineState(state_path)
    reloaded.load()

    assert reloaded.version == 1
    assert reloaded.last_run and "T" in reloaded.last_run
    assert "note/One.md" in reloaded.files
    assert reloaded.files["note/One.md"].md5 == "h1"
    assert reloaded.files["note/One.md"].semantic_cluster_id == 3
    assert reloaded.files["note/One.md"].structural_community_id == 1
    assert reloaded.files["note/One.md"].last_embedded
    assert "3" in reloaded.clusters
    assert reloaded.clusters["3"].label == ["testing", "notes"]
    assert reloaded.pending_writes == {"note/Three.md": "hpending"}


def test_surveyor_state_load_tolerates_unknown_keys(state_path: Path) -> None:
    """``PipelineState.load()`` must ignore unknown keys in ``files`` /
    ``clusters`` records.

    Reproduces the 2026-04-28 KAL-LE/Hypatia status crash: those instance
    configs omit both ``surveyor:`` and ``janitor:``, so both tools'
    state-path defaults collide on ``./data/state.json``. Surveyor's
    status-load path was hitting janitor's saved file and crashing on
    ``FileState.__init__() got an unexpected keyword argument 'last_scanned'``.

    The fix mirrors ``distiller/state.py``'s forward-compat filter — load
    must skip unknown keys, not crash.
    """
    import json

    janitor_shaped_state = {
        "version": 1,
        "last_run": "2026-04-26T02:27:58.901423+00:00",
        "files": {
            "note/One.md": {
                "md5": "abc123",
                # janitor-only fields below — surveyor must ignore them
                "last_scanned": "2026-04-26T02:27:58.901423+00:00",
                "open_issues": ["FM001"],
                "enrichment_attempts": 0,
                "last_enrichment_attempt": "",
                "enrichment_stale": False,
            },
        },
        "clusters": {
            "1": {
                "label": ["unit-tests"],
                "member_files": ["note/One.md"],
                "last_labeled": "2026-04-26T02:27:58.901423+00:00",
                # synthetic unknown key — must not crash
                "spurious_field": "should be ignored",
            },
        },
        "pending_writes": {},
    }
    state_path.write_text(json.dumps(janitor_shaped_state), encoding="utf-8")

    from alfred.surveyor.state import PipelineState

    st = PipelineState(state_path)
    st.load()  # MUST NOT raise

    # Known fields survived the filter
    assert "note/One.md" in st.files
    assert st.files["note/One.md"].md5 == "abc123"
    # Cluster known fields survived
    assert "1" in st.clusters
    assert st.clusters["1"].label == ["unit-tests"]
    # Unknown janitor fields silently dropped — confirms filter shape
    assert not hasattr(st.files["note/One.md"], "last_scanned")
    assert not hasattr(st.files["note/One.md"], "open_issues")


def test_janitor_state_load_tolerates_unknown_keys(state_path: Path) -> None:
    """Symmetric forward-compat tolerance for janitor.

    Mirrors the surveyor test — protects against the inverse default-path
    collision and against future schema field renames. Filtering on
    ``__dataclass_fields__`` is the cross-tool contract for state load.
    """
    import json

    surveyor_shaped_state = {
        "version": 1,
        "files": {
            "note/One.md": {
                "md5": "abc123",
                # surveyor-only fields — janitor must ignore them
                "last_embedded": "2026-04-26T02:27:58.901423+00:00",
                "semantic_cluster_id": 5,
                "structural_community_id": 2,
            },
        },
        "sweeps": {},
        "fix_log": [],
        "ignored": {},
        "pending_writes": {},
    }
    state_path.write_text(json.dumps(surveyor_shaped_state), encoding="utf-8")

    from alfred.janitor.state import JanitorState

    st = JanitorState(state_path, max_sweep_history=5)
    st.load()  # MUST NOT raise

    assert "note/One.md" in st.files
    assert st.files["note/One.md"].md5 == "abc123"
    # Surveyor fields silently dropped
    assert not hasattr(st.files["note/One.md"], "last_embedded")
    assert not hasattr(st.files["note/One.md"], "semantic_cluster_id")


# ---------------------------------------------------------------------------
# Talker (Telegram)
# ---------------------------------------------------------------------------

def test_talker_state_roundtrip(state_path: Path) -> None:
    """Telegram state tracks active/closed session transcripts per chat."""
    from alfred.telegram.state import StateManager

    mgr = StateManager(state_path)
    mgr.load()
    mgr.set_active(
        chat_id=1234,
        session={
            "session_id": "abc-123",
            "started_at": "2026-04-19T10:00:00+00:00",
            "last_message_at": "2026-04-19T10:05:00+00:00",
            "model": "claude-sonnet-4-6",
            "transcript": [
                {"role": "user", "content": "Hi"},
                {"role": "assistant", "content": "Hello"},
            ],
            "vault_ops": [],
        },
    )
    mgr.append_closed(
        {
            "session_id": "old-001",
            "chat_id": "5678",
            "closed_at": "2026-04-18T22:00:00+00:00",
            "turn_count": 4,
        }
    )
    mgr.save()

    assert state_path.exists()
    assert not state_path.with_suffix(".tmp").exists()

    reloaded = StateManager(state_path)
    data = reloaded.load()

    assert data["version"] == 1
    active = reloaded.get_active(1234)
    assert active is not None
    assert active["session_id"] == "abc-123"
    assert active["model"] == "claude-sonnet-4-6"
    assert len(active["transcript"]) == 2
    assert active["transcript"][0] == {"role": "user", "content": "Hi"}
    assert len(data["closed_sessions"]) == 1
    assert data["closed_sessions"][0]["session_id"] == "old-001"


# ---------------------------------------------------------------------------
# Brief
# ---------------------------------------------------------------------------

def test_brief_state_roundtrip(state_path: Path) -> None:
    """Brief state tracks per-date run history."""
    from alfred.brief.state import BriefRun, StateManager

    mgr = StateManager(state_path)
    mgr.state.add_run(
        BriefRun(
            date="2026-04-19",
            generated_at="2026-04-19T06:00:00+00:00",
            vault_path="/vault",
            sections=["weather", "tasks", "projects"],
            success=True,
        )
    )
    mgr.state.add_run(
        BriefRun(
            date="2026-04-18",
            generated_at="2026-04-18T06:00:00+00:00",
            vault_path="/vault",
            sections=["tasks"],
            success=False,
        )
    )
    mgr.save()

    assert state_path.exists()
    assert not state_path.with_suffix(".tmp").exists()

    reloaded = StateManager(state_path)
    state = reloaded.load()

    assert state.version == 1
    assert len(state.runs) == 2
    assert state.has_brief_for_date("2026-04-19")
    # 2026-04-18 run had success=False, so has_brief_for_date returns False
    assert not state.has_brief_for_date("2026-04-18")
    # last_run is set to the latest .add_run's generated_at
    assert state.last_run == "2026-04-18T06:00:00+00:00"


# ---------------------------------------------------------------------------
# Instructor
# ---------------------------------------------------------------------------

def test_instructor_state_roundtrip(state_path: Path) -> None:
    """Instructor state tracks file hashes, retry counts, last run ts."""
    from alfred.instructor.state import InstructorState

    state = InstructorState(state_path)
    state.record_hash("note/Some Note.md", "hash-aaa")
    state.record_hash("task/Thing.md", "hash-bbb")
    state.bump_retry("task/Thing.md")
    state.bump_retry("task/Thing.md")
    state.stamp_run()

    state.save()
    assert state_path.exists()
    # .tmp file should NOT linger — atomic-rename contract
    assert not state_path.with_suffix(".tmp").exists()

    reloaded = InstructorState(state_path)
    reloaded.load()

    assert reloaded.version == 1
    assert reloaded.file_hashes == {
        "note/Some Note.md": "hash-aaa",
        "task/Thing.md": "hash-bbb",
    }
    assert reloaded.get_retry_count("task/Thing.md") == 2
    assert reloaded.get_retry_count("unknown") == 0
    assert reloaded.last_run_ts is not None
    assert "T" in reloaded.last_run_ts


def test_instructor_state_clear_retry_on_load(state_path: Path) -> None:
    """clear_retry drops the per-path entry; hash_unchanged gate works."""
    from alfred.instructor.state import InstructorState

    state = InstructorState(state_path)
    state.record_hash("note/X.md", "hash-x")
    state.bump_retry("note/X.md")
    state.clear_retry("note/X.md")
    state.save()

    reloaded = InstructorState(state_path)
    reloaded.load()
    assert reloaded.get_retry_count("note/X.md") == 0
    assert reloaded.hash_unchanged("note/X.md", "hash-x")
    assert not reloaded.hash_unchanged("note/X.md", "something-else")


def test_instructor_state_load_tolerates_corrupt_file(state_path: Path) -> None:
    """Corrupt JSON state file must not crash the daemon on startup.

    Same contract as every other tool: fall back to empty state so the
    next save heals the file.
    """
    state_path.write_text("not valid json at all", encoding="utf-8")

    from alfred.instructor.state import InstructorState

    state = InstructorState(state_path)
    state.load()
    # Should NOT have raised — empty state substituted.
    assert state.file_hashes == {}
    assert state.retry_counts == {}
    assert state.last_run_ts is None


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------

def test_transport_state_roundtrip(state_path: Path) -> None:
    """Transport state tracks pending scheduled sends + send log + dead letter."""
    from alfred.transport.state import TransportState

    state = TransportState.create(state_path)
    state.enqueue({
        "id": "evt-001",
        "user_id": 123,
        "text": "Reminder: call Dr Bailey",
        "scheduled_at": "2026-04-21T12:00:00+00:00",
        "dedupe_key": "reminder-task/call.md-20260421T1200",
    })
    state.record_send({
        "id": "snd-001",
        "user_id": 123,
        "dedupe_key": "brief-2026-04-19",
        "sent_at": "2026-04-19T10:00:00+00:00",
        "telegram_message_ids": [42, 43],
    })
    state.append_dead_letter(
        {
            "id": "evt-stale",
            "user_id": 123,
            "text": "Stale reminder",
            "scheduled_at": "2026-04-01T00:00:00+00:00",
        },
        reason="stale_reminder_window_exceeded",
    )

    state.save()
    assert state_path.exists()
    # .tmp file should NOT linger — atomic-rename contract
    assert not state_path.with_suffix(".tmp").exists()

    reloaded = TransportState.create(state_path)
    reloaded.load()

    assert reloaded.version == 1
    assert len(reloaded.pending_queue) == 1
    assert reloaded.pending_queue[0]["id"] == "evt-001"
    assert len(reloaded.send_log) == 1
    assert reloaded.send_log[0]["dedupe_key"] == "brief-2026-04-19"
    assert len(reloaded.dead_letter) == 1
    dl = reloaded.dead_letter[0]
    assert dl["dead_letter_reason"] == "stale_reminder_window_exceeded"
    assert "dead_lettered_at" in dl


def test_transport_state_pop_due_splits_by_schedule(state_path: Path) -> None:
    """``pop_due`` returns entries whose scheduled_at <= now; leaves others."""
    from datetime import datetime, timezone

    from alfred.transport.state import TransportState

    state = TransportState.create(state_path)
    state.enqueue({"id": "past", "scheduled_at": "2026-04-01T00:00:00+00:00"})
    state.enqueue({"id": "future", "scheduled_at": "2099-01-01T00:00:00+00:00"})
    state.enqueue({"id": "now-no-schedule"})  # no scheduled_at → send now

    now = datetime(2026, 4, 20, 12, 0, tzinfo=timezone.utc)
    due = state.pop_due(now)
    due_ids = sorted(e["id"] for e in due)
    assert due_ids == ["now-no-schedule", "past"]
    # The future entry survives.
    assert len(state.pending_queue) == 1
    assert state.pending_queue[0]["id"] == "future"


def test_transport_state_dedupe_window(state_path: Path) -> None:
    """``find_recent_send`` honours the 24h window + empty-key short-circuit."""
    from datetime import datetime, timedelta, timezone

    from alfred.transport.state import TransportState

    state = TransportState.create(state_path)
    recent = datetime(2026, 4, 20, 10, 0, tzinfo=timezone.utc)
    state.record_send({
        "dedupe_key": "brief-2026-04-20",
        "sent_at": recent.isoformat(),
    })

    # Within window
    match = state.find_recent_send("brief-2026-04-20", now=recent + timedelta(hours=1))
    assert match is not None
    # Outside window
    miss = state.find_recent_send("brief-2026-04-20", now=recent + timedelta(hours=25))
    assert miss is None
    # Empty key
    assert state.find_recent_send("", now=recent) is None
    # Unknown key
    assert state.find_recent_send("never-seen", now=recent) is None


def test_transport_state_tolerates_corrupt_file(state_path: Path) -> None:
    """A malformed state file must not crash on load — empty state substitutes."""
    state_path.write_text("{ this is not json", encoding="utf-8")

    from alfred.transport.state import TransportState

    state = TransportState.create(state_path)
    state.load()
    assert state.pending_queue == []
    assert state.send_log == []
    assert state.dead_letter == []


# ---------------------------------------------------------------------------
# Mail
# ---------------------------------------------------------------------------

def test_mail_state_roundtrip(state_path: Path) -> None:
    """Mail state tracks seen message IDs per account."""
    from alfred.mail.state import StateManager

    mgr = StateManager(state_path)
    mgr.state.mark_seen("personal", "msg-001")
    mgr.state.mark_seen("personal", "msg-002")
    mgr.state.mark_seen("work", "msg-003")
    mgr.save()

    assert state_path.exists()
    assert not state_path.with_suffix(".tmp").exists()

    reloaded = StateManager(state_path)
    state = reloaded.load()

    assert state.version == 1
    assert state.is_seen("personal", "msg-001")
    assert state.is_seen("personal", "msg-002")
    assert state.is_seen("work", "msg-003")
    assert not state.is_seen("personal", "msg-999")
    assert not state.is_seen("other_account", "msg-001")


# ---------------------------------------------------------------------------
# Cross-cutting: malformed JSON tolerance
# ---------------------------------------------------------------------------

def test_mail_state_load_tolerates_corrupt_file(state_path: Path) -> None:
    """A malformed JSON state file must not crash the daemon on startup.

    All of Alfred's *Manager.load() methods catch JSONDecodeError and fall
    back to an empty state — this is the contract that lets a corrupted
    state file self-heal on next save instead of wedging the daemon.
    """
    state_path.write_text("{ this is not valid json", encoding="utf-8")

    from alfred.mail.state import StateManager

    mgr = StateManager(state_path)
    state = mgr.load()
    # Should NOT have raised — empty state substituted.
    assert state.seen_ids == {}


def test_curator_state_load_tolerates_corrupt_file(state_path: Path) -> None:
    """Mirror of the mail tolerance check — curator has its own path."""
    state_path.write_text("not json at all", encoding="utf-8")

    from alfred.curator.state import StateManager

    mgr = StateManager(state_path)
    state = mgr.load()
    assert state.processed == {}


# ---------------------------------------------------------------------------
# State-path collision regression (Item 4 / KAL-LE P0 review)
# ---------------------------------------------------------------------------


def test_default_state_paths_are_tool_unique() -> None:
    """Each tool's default ``state.path`` MUST be unique across tools.

    Pre-fix all four tools defaulted to ``./data/state.json`` — a per-
    instance config that omitted some tools' blocks would let one
    tool's state file get loaded by another tool's load() path,
    crashing or silently mis-reporting in ``alfred status``.
    """
    from alfred.curator.config import StateConfig as CuratorState
    from alfred.distiller.config import StateConfig as DistillerState
    from alfred.janitor.config import StateConfig as JanitorState
    from alfred.surveyor.config import StateConfig as SurveyorState

    paths = {
        "curator": CuratorState().path,
        "janitor": JanitorState().path,
        "distiller": DistillerState().path,
        "surveyor": SurveyorState().path,
    }
    # All values must be distinct.
    assert len(set(paths.values())) == len(paths), (
        f"State path defaults collide: {paths}"
    )
    # And none should be the legacy shared default.
    for tool, p in paths.items():
        assert p != "./data/state.json", (
            f"{tool} still defaults to the legacy shared path"
        )
