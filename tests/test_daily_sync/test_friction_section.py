"""Tests for the K3 c2 friction queue section provider.

Covers:
- All 3 categories rendered with correct headers + bullet shapes.
- Empty-state explicit "No friction items today" line per
  ``feedback_intentionally_left_blank.md``.
- Section provider returns None when ``set_friction_log_path`` was
  never called (defensive guard for tests that exercise the provider
  without daemon setup).
- After-render: events get marked surfaced via the side index.
- Re-fire on same data: surfaced events are SKIPPED.
- start_index renumbering — global numbering across sections.
- Priority pinned at 23 (between radar at 22 and attribution at 25).
- Idempotent register.
- _surfaced_index_path produces the expected sibling path.
- end-to-end via the assembler.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from alfred.daily_sync import assembler, friction_section
from alfred.daily_sync.config import DailySyncConfig


@pytest.fixture(autouse=True)
def _clean_registry():
    assembler.clear_providers()
    friction_section._LAST_BATCH_HOLDER["items"] = []
    friction_section._LOG_PATH_HOLDER.clear()
    yield
    assembler.clear_providers()
    friction_section._LAST_BATCH_HOLDER["items"] = []
    friction_section._LOG_PATH_HOLDER.clear()


@pytest.fixture
def config() -> DailySyncConfig:
    return DailySyncConfig(enabled=True)


def _seed_friction_log(path: Path, events: list[dict]) -> None:
    """Write a friction-event JSONL log with the given events."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for ev in events:
            fh.write(json.dumps(ev) + "\n")


def _ev_failed(event_id: str, prefix: str, count: int = 4) -> dict:
    return {
        "event_id": event_id,
        "kind": "failed_pattern",
        "detected_at": "2026-05-05T07:30:00+00:00",
        "day_bucket": "2026-05-05",
        "prefix": prefix,
        "count": count,
        "last_failure": "2026-05-05T06:55:00+00:00",
        "sample_command": f"{prefix} --verbose",
        "suggestion": (
            f"`{prefix}` failed {count} times in the last window — "
            "consider adding to bash_exec allowlist, checking the "
            "binary install, or fixing the command shape"
        ),
        "surfaced_at": None,
    }


def _ev_repeated(event_id: str, command: str, count: int = 7) -> dict:
    return {
        "event_id": event_id,
        "kind": "repeated_pattern",
        "detected_at": "2026-05-05T07:30:00+00:00",
        "day_bucket": "2026-05-05",
        "command": command,
        "count": count,
        "suggestion": (
            f"You ran `{command}` {count} times in the last window "
            "— consider scripting, aliasing, or a Make target"
        ),
        "surfaced_at": None,
    }


def _ev_missing(event_id: str, tool: str) -> dict:
    return {
        "event_id": event_id,
        "kind": "missing_tool",
        "detected_at": "2026-05-05T07:30:00+00:00",
        "day_bucket": "2026-05-05",
        "tool": tool,
        "failed_command": f"{tool} foo bar",
        "suggestion": (
            f"`{tool}` is not installed (or not on KAL-LE's PATH) — "
            "install in KAL-LE's venv or system, or replace with a "
            "present alternative"
        ),
        "surfaced_at": None,
    }


# ---------------------------------------------------------------------------
# _surfaced_index_path
# ---------------------------------------------------------------------------


class TestSurfacedIndexPath:
    def test_jsonl_log_produces_sibling_jsonl(self):
        log = Path("/tmp/x/kalle_friction_log.jsonl")
        side = friction_section._surfaced_index_path(log)
        assert side == Path("/tmp/x/kalle_friction_log.surfaced.jsonl")

    def test_log_without_jsonl_suffix_appends_suffix(self):
        log = Path("/tmp/x/raw_log")
        side = friction_section._surfaced_index_path(log)
        assert side == Path("/tmp/x/raw_log.surfaced.jsonl")


# ---------------------------------------------------------------------------
# load_friction_events / load_surfaced_event_ids
# ---------------------------------------------------------------------------


class TestLoadFrictionEvents:
    def test_missing_log_returns_empty_list(self, tmp_path: Path):
        assert friction_section.load_friction_events(
            tmp_path / "nonexistent.jsonl",
        ) == []

    def test_skips_malformed_rows(self, tmp_path: Path):
        path = tmp_path / "friction.jsonl"
        path.write_text(
            json.dumps(_ev_failed("e1", "uv sync")) + "\n"
            "NOT JSON\n"
            "\n"
            + json.dumps(_ev_failed("e2", "pip install")) + "\n",
            encoding="utf-8",
        )
        events = friction_section.load_friction_events(path)
        assert [e["event_id"] for e in events] == ["e1", "e2"]


class TestSurfacedIdsLoadAppend:
    def test_load_returns_empty_when_no_side_index(self, tmp_path: Path):
        log = tmp_path / "friction.jsonl"
        log.touch()
        assert friction_section.load_surfaced_event_ids(log) == set()

    def test_append_then_load_round_trip(self, tmp_path: Path):
        log = tmp_path / "friction.jsonl"
        friction_section.append_surfaced(log, ["e1", "e2", "e3"])
        ids = friction_section.load_surfaced_event_ids(log)
        assert ids == {"e1", "e2", "e3"}
        # Side index has the surfaced_at timestamps.
        side = friction_section._surfaced_index_path(log)
        loaded = [
            json.loads(line) for line in side.read_text().splitlines()
        ]
        assert all("surfaced_at" in row for row in loaded)

    def test_append_empty_is_noop(self, tmp_path: Path):
        log = tmp_path / "friction.jsonl"
        friction_section.append_surfaced(log, [])
        side = friction_section._surfaced_index_path(log)
        assert not side.exists()


# ---------------------------------------------------------------------------
# render_batch — empty + populated paths
# ---------------------------------------------------------------------------


class TestRenderBatch:
    def test_empty_renders_explicit_no_items_line(self):
        rendered, summaries = friction_section.render_batch(
            [], date(2026, 5, 5),
        )
        assert "## Friction queue" in rendered
        assert "No friction items today" in rendered
        assert summaries == []

    def test_three_categories_render_with_headers(self):
        events = [
            _ev_failed("e1", "uv sync", count=4),
            _ev_repeated("e2", "pytest tests/test_foo.py -v", count=7),
            _ev_missing("e3", "ripgrep"),
        ]
        rendered, summaries = friction_section.render_batch(
            events, date(2026, 5, 5),
        )
        assert "## Friction queue" in rendered
        assert "### Failed-pattern signals" in rendered
        assert "### Repeated-pattern signals" in rendered
        assert "### Missing tooling" in rendered
        # Each category surfaces its primary identifier.
        assert "uv sync" in rendered
        assert "pytest tests/test_foo.py -v" in rendered
        assert "ripgrep" in rendered
        # Summaries mirror render order.
        assert [s.kind for s in summaries] == [
            "failed_pattern", "repeated_pattern", "missing_tool",
        ]
        assert [s.event_id for s in summaries] == ["e1", "e2", "e3"]

    def test_category_headers_omitted_when_zero_events_in_category(self):
        """Only failed_pattern present → no headers for the other two."""
        events = [_ev_failed("e1", "uv sync")]
        rendered, _ = friction_section.render_batch(events, date(2026, 5, 5))
        assert "### Failed-pattern signals" in rendered
        assert "### Repeated-pattern signals" not in rendered
        assert "### Missing tooling" not in rendered

    def test_start_index_renumbering(self):
        """Items get GLOBAL item_numbers — when 5 items already
        rendered above (start_index=6), friction items become #6, #7."""
        events = [
            _ev_failed("e1", "uv sync"),
            _ev_repeated("e2", "pytest -v"),
        ]
        rendered, summaries = friction_section.render_batch(
            events, date(2026, 5, 5), start_index=6,
        )
        assert summaries[0].item_number == 6
        assert summaries[1].item_number == 7


# ---------------------------------------------------------------------------
# friction_section provider — log-path-unset + render-and-mark-surfaced
# ---------------------------------------------------------------------------


class TestFrictionSectionProvider:
    def test_returns_none_when_log_path_unset(self, config):
        out = friction_section.friction_section(config, date(2026, 5, 5))
        assert out is None

    def test_renders_empty_state_when_log_missing(
        self, tmp_path: Path, config,
    ):
        """Per intentionally-left-blank: log file missing → still render
        the empty-state line, NOT None. Distinguishes 'analyzer hasn't
        run yet' from 'section provider didn't fire'."""
        friction_section.set_friction_log_path(
            tmp_path / "absent.jsonl",
        )
        out = friction_section.friction_section(config, date(2026, 5, 5))
        assert out is not None
        assert "No friction items today" in out

    def test_renders_events_and_marks_surfaced(
        self, tmp_path: Path, config,
    ):
        log = tmp_path / "friction.jsonl"
        _seed_friction_log(log, [
            _ev_failed("e1", "uv sync", count=4),
            _ev_missing("e2", "ripgrep"),
        ])
        friction_section.set_friction_log_path(log)
        out = friction_section.friction_section(config, date(2026, 5, 5))
        assert "uv sync" in out
        assert "ripgrep" in out

        # Side index now records both as surfaced.
        surfaced = friction_section.load_surfaced_event_ids(log)
        assert surfaced == {"e1", "e2"}

        # Items captured for state persistence.
        items = friction_section.consume_last_batch()
        assert {i.event_id for i in items} == {"e1", "e2"}

    def test_skips_already_surfaced_events_on_re_fire(
        self, tmp_path: Path, config,
    ):
        log = tmp_path / "friction.jsonl"
        _seed_friction_log(log, [
            _ev_failed("e1", "uv sync"),
            _ev_repeated("e2", "pytest"),
        ])
        friction_section.set_friction_log_path(log)

        # First fire surfaces both.
        first = friction_section.friction_section(config, date(2026, 5, 5))
        assert "uv sync" in first
        first_items = friction_section.consume_last_batch()
        assert len(first_items) == 2

        # Second fire on same data → empty-state because both are
        # already surfaced.
        second = friction_section.friction_section(config, date(2026, 5, 5))
        assert "No friction items today" in second
        second_items = friction_section.consume_last_batch()
        assert second_items == []

    def test_partial_surfacing_only_renders_fresh(
        self, tmp_path: Path, config,
    ):
        """Side index has e1; log has e1+e2. Provider renders only e2."""
        log = tmp_path / "friction.jsonl"
        _seed_friction_log(log, [
            _ev_failed("e1", "uv sync"),
            _ev_repeated("e2", "pytest"),
        ])
        # Pre-stage e1 as already surfaced.
        friction_section.append_surfaced(log, ["e1"])
        friction_section.set_friction_log_path(log)

        out = friction_section.friction_section(config, date(2026, 5, 5))
        assert "pytest" in out
        # uv sync should NOT be in the rendered text — it was surfaced
        # in a prior fire.
        assert "uv sync" not in out


# ---------------------------------------------------------------------------
# Registration + assembler integration
# ---------------------------------------------------------------------------


class TestFrictionSectionRegistration:
    def test_register_idempotent(self):
        friction_section.register()
        friction_section.register()
        names = assembler.registered_providers()
        assert names.count("friction") == 1

    def test_friction_priority_pinned_at_23(self):
        """Priority 23 — between radar at 22 and attribution at 25.
        Pinning the priority via this test keeps the contract
        grep-able."""
        friction_section.register()
        entry = next(
            (e for e in assembler._REGISTRY if e.name == "friction"),
            None,
        )
        assert entry is not None
        assert entry.priority == 23

    def test_assembler_includes_friction_section(
        self, tmp_path: Path, config,
    ):
        log = tmp_path / "friction.jsonl"
        _seed_friction_log(log, [
            _ev_failed("e1", "uv sync", count=4),
        ])
        friction_section.set_friction_log_path(log)
        friction_section.register()
        body = assembler.assemble_message(config, date(2026, 5, 5))
        assert "Daily Sync — 2026-05-05" in body
        assert "## Friction queue" in body
        assert "uv sync" in body

    def test_assembler_includes_empty_state_when_log_missing(
        self, tmp_path: Path, config,
    ):
        """Even when no friction events exist, the section still
        renders (via the empty-state line) — doesn't fall through to
        the empty-Daily-Sync header."""
        friction_section.set_friction_log_path(tmp_path / "missing.jsonl")
        friction_section.register()
        body = assembler.assemble_message(config, date(2026, 5, 5))
        assert "## Friction queue" in body
        assert "No friction items today" in body
        # Empty-Daily-Sync header should NOT fire because friction
        # contributed a non-None section.
        assert "No items today" not in body
