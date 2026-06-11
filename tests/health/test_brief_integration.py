"""Tests for the brief's Health section integration (BIT c6).

Covers ``alfred.brief.health_section.render_health_section`` reading
a BIT record out of the vault and the fallback path that reads the
BIT state file when no vault record is available.
"""

from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent

from alfred.brief.health_section import (
    _find_latest_bit_record,
    _parse_frontmatter,
    _per_tool_lines,
    render_health_section,
)


def _write_bit_record(
    vault: Path,
    date_str: str,
    overall: str = "ok",
    bit_dir: str = "run",
) -> Path:
    """Write a BIT record file in the vault, returning its path.

    ``bit_dir`` defaults to ``"run"`` (the canonical home since
    2026-06-12); pass ``"process"`` to model a pre-migration legacy
    record.
    """
    record_dir = vault / bit_dir
    record_dir.mkdir(parents=True, exist_ok=True)
    path = record_dir / f"Alfred BIT {date_str}.md"
    content = dedent(f"""\
        ---
        type: run
        name: Alfred BIT {date_str}
        overall_status: {overall}
        mode: quick
        created: '{date_str}'
        started: '{date_str}T05:55:00-03:00'
        tool_counts:
          ok: 5
          warn: 1
          fail: 0
          skip: 1
        tools_checked:
        - curator
        - janitor
        - distiller
        - surveyor
        - brief
        - mail
        - talker
        tags:
        - bit
        - health
        ---

        # Alfred BIT {date_str}

        ## Summary

        Alfred BIT (quick) — [ OK ]

        [ OK ] curator  (100 ms)
            [ OK ] vault-path — /vault
        [ OK ] janitor  (100 ms)
        [WARN] surveyor  (92 ms) — ollama 404
        [ OK ] distiller
        [ OK ] brief
        [ OK ] mail
        [SKIP] talker — no telegram section in config

        Totals: ok=5 warn=1 fail=0 skip=1

        ## Raw report (JSON)
        """)
    path.write_text(content, encoding="utf-8")
    return path


class TestFindLatestBitRecord:
    def test_no_records_returns_none(self, tmp_path: Path) -> None:
        assert _find_latest_bit_record(tmp_path) is None

    def test_picks_lexicographic_last(self, tmp_path: Path) -> None:
        _write_bit_record(tmp_path, "2026-04-17")
        _write_bit_record(tmp_path, "2026-04-19")
        _write_bit_record(tmp_path, "2026-04-18")
        latest = _find_latest_bit_record(tmp_path)
        assert latest is not None
        assert latest.name == "Alfred BIT 2026-04-19.md"

    def test_missing_bit_dirs(self, tmp_path: Path) -> None:
        # Neither run/ nor process/ exists yet — no record, no crash.
        assert _find_latest_bit_record(tmp_path) is None

    def test_legacy_process_record_still_found(self, tmp_path: Path) -> None:
        """Pre-2026-06-12 records living in process/ are still found."""
        _write_bit_record(tmp_path, "2026-04-19", bit_dir="process")
        latest = _find_latest_bit_record(tmp_path)
        assert latest is not None
        assert latest.name == "Alfred BIT 2026-04-19.md"
        assert latest.parent.name == "process"

    def test_newest_filename_wins_across_dirs(self, tmp_path: Path) -> None:
        """Newest-dated filename wins regardless of which dir holds it."""
        # Older legacy record in process/, newer in run/ → run record.
        _write_bit_record(tmp_path, "2026-04-18", bit_dir="process")
        _write_bit_record(tmp_path, "2026-04-19", bit_dir="run")
        latest = _find_latest_bit_record(tmp_path)
        assert latest is not None
        assert latest.name == "Alfred BIT 2026-04-19.md"
        assert latest.parent.name == "run"

    def test_newest_filename_wins_when_legacy_is_newer(
        self, tmp_path: Path
    ) -> None:
        """Not-yet-migrated case: newer record in process/ still wins."""
        _write_bit_record(tmp_path, "2026-04-19", bit_dir="run")
        _write_bit_record(tmp_path, "2026-04-20", bit_dir="process")
        latest = _find_latest_bit_record(tmp_path)
        assert latest is not None
        assert latest.name == "Alfred BIT 2026-04-20.md"
        assert latest.parent.name == "process"

    def test_filename_tie_prefers_run_dir(self, tmp_path: Path) -> None:
        """Migration-overlap: same filename in both dirs → run/ wins."""
        _write_bit_record(tmp_path, "2026-04-19", bit_dir="process")
        _write_bit_record(tmp_path, "2026-04-19", bit_dir="run")
        latest = _find_latest_bit_record(tmp_path)
        assert latest is not None
        assert latest.name == "Alfred BIT 2026-04-19.md"
        assert latest.parent.name == "run"


class TestParseFrontmatter:
    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        assert _parse_frontmatter(tmp_path / "nope.md") is None

    def test_no_frontmatter_returns_none(self, tmp_path: Path) -> None:
        p = tmp_path / "a.md"
        p.write_text("# Heading\nno frontmatter\n", encoding="utf-8")
        assert _parse_frontmatter(p) is None

    def test_parses_yaml(self, tmp_path: Path) -> None:
        p = _write_bit_record(tmp_path, "2026-04-19")
        fm = _parse_frontmatter(p)
        assert fm is not None
        assert fm["overall_status"] == "ok"
        assert fm["mode"] == "quick"


class TestPerToolLines:
    def test_extracts_tool_lines(self) -> None:
        body = dedent("""\
            ## Summary

            Alfred BIT (quick) — [ OK ]

            [ OK ] curator  (100 ms)
                [ OK ] vault-path — /vault
            [WARN] janitor — state corrupt
            [FAIL] surveyor  (50 ms) — ollama unreachable
            [SKIP] mail — no config

            ## Raw report
        """)
        lines = _per_tool_lines(body)
        assert len(lines) == 4
        # (tool, status, detail)
        assert lines[0] == ("curator", "ok", "")
        assert lines[1] == ("janitor", "warn", "state corrupt")
        assert lines[2] == ("surveyor", "fail", "ollama unreachable")
        assert lines[3] == ("mail", "skip", "no config")

    def test_returns_empty_on_missing_summary(self) -> None:
        assert _per_tool_lines("# nothing here\n") == []


class TestRenderHealthSection:
    def test_renders_fresh_record(self, tmp_path: Path) -> None:
        _write_bit_record(tmp_path, "2026-04-19", overall="ok")
        md = render_health_section(tmp_path, today="2026-04-19")
        assert "**Overall:** ok" in md
        assert "curator" in md
        assert "surveyor" in md
        assert "ollama 404" in md

    def test_renders_stale_record(self, tmp_path: Path) -> None:
        _write_bit_record(tmp_path, "2026-04-18")
        md = render_health_section(tmp_path, today="2026-04-19")
        assert "stale" in md
        assert "2026-04-18" in md

    def test_no_record_no_state_returns_placeholder(self, tmp_path: Path) -> None:
        md = render_health_section(tmp_path, today="2026-04-19")
        assert "No BIT run recorded yet" in md

    def test_no_record_falls_back_to_state(self, tmp_path: Path) -> None:
        # Create a state file but no vault record
        state_path = tmp_path / "bit_state.json"
        state_path.write_text(json.dumps({
            "version": 1,
            "runs": [{
                "date": "2026-04-19",
                "generated_at": "2026-04-19T05:55:00+00:00",
                "vault_path": "run/Alfred BIT 2026-04-19.md",
                "overall_status": "warn",
                "mode": "quick",
                "tool_counts": {"ok": 5, "warn": 1, "fail": 0, "skip": 1},
            }],
        }), encoding="utf-8")
        md = render_health_section(
            tmp_path,
            state_path=state_path,
            today="2026-04-19",
        )
        assert "warn" in md
        assert "Full report unavailable" in md

    def test_corrupt_state_file_returns_placeholder(self, tmp_path: Path) -> None:
        state_path = tmp_path / "bit_state.json"
        state_path.write_text("not json", encoding="utf-8")
        md = render_health_section(
            tmp_path,
            state_path=state_path,
            today="2026-04-19",
        )
        assert "No BIT run recorded" in md

    def test_empty_state_runs_returns_placeholder(self, tmp_path: Path) -> None:
        state_path = tmp_path / "bit_state.json"
        state_path.write_text(
            json.dumps({"version": 1, "runs": []}),
            encoding="utf-8",
        )
        md = render_health_section(
            tmp_path,
            state_path=state_path,
            today="2026-04-19",
        )
        assert "No BIT run recorded" in md

    def test_includes_record_link(self, tmp_path: Path) -> None:
        _write_bit_record(tmp_path, "2026-04-19")
        md = render_health_section(tmp_path, today="2026-04-19")
        assert "[[run/Alfred BIT 2026-04-19]]" in md

    def test_legacy_record_link_points_at_process(self, tmp_path: Path) -> None:
        """A pre-migration record renders its link at its real location."""
        _write_bit_record(tmp_path, "2026-04-19", bit_dir="process")
        md = render_health_section(tmp_path, today="2026-04-19")
        assert "[[process/Alfred BIT 2026-04-19]]" in md
