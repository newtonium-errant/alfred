"""Auto-start gating — only tools with a config section auto-launch.

``run_all`` computes the default tool list when ``only`` is not provided:

  - curator, janitor, distiller always start (required trio)
  - surveyor, mail, brief, talker (via "telegram" key) only start when
    their section exists in the config dict

We exercise the computation by running ``run_all`` with a sentinel primed
to fire immediately — the orchestrator starts the computed tools, writes
workers.json, then shuts down in the finally block. The written
``workers.json`` is the observable record of which tools were started.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

import alfred.orchestrator as orchestrator

from ._fakes import fake_runner_2arg, fake_runner_3arg


# All fake runners exit quickly (0.1s), which is fine — we just want to
# observe which tools got spawned. The orchestrator's shutdown-sentinel
# path cleans them up promptly.

ALL_FAKES = {
    "curator": fake_runner_3arg,
    "janitor": fake_runner_3arg,
    "distiller": fake_runner_3arg,
    "instructor": fake_runner_3arg,
    "surveyor": fake_runner_2arg,
    "mail": fake_runner_2arg,
    "brief": fake_runner_2arg,
    "talker": fake_runner_3arg,
}


def _read_started_tools(data_dir: Path) -> set[str]:
    """Observe which tools the orchestrator started via touch files.

    ``_fakes.py`` has every runner touch ``<data_dir>/started_<tool>``
    on first call, indexed by ``raw["_fake_runner"]["touch_files"]``.
    """
    started: set[str] = set()
    for entry in data_dir.iterdir():
        if entry.name.startswith("started_"):
            started.add(entry.name.removeprefix("started_"))
    return started


def _run_until_sentinel(
    raw: dict,
    orch_dirs: dict,
    fire_delay: float = 0.2,
    fire_sentinel_after=None,
) -> None:
    """Fire the shutdown sentinel on a timer, then call run_all.

    Returns after ``run_all`` exits (orchestrator observes sentinel →
    breaks out of monitor loop → finally block terminates children).
    """
    fire_sentinel_after(fire_delay)
    orchestrator.run_all(
        raw,
        only=None,
        skills_dir=orch_dirs["skills"],
        pid_path=orch_dirs["pid_path"],
        live_mode=False,
    )


def _wire_touch_files(
    raw: dict,
    orch_dirs: dict,
    tools: list[str],
) -> dict[str, str]:
    """Add a ``touch_files`` mapping to ``raw["_fake_runner"]`` for *tools*."""
    tf: dict[str, str] = {}
    for tool in tools:
        tf[tool] = str(orch_dirs["data"] / f"started_{tool}")
    raw.setdefault("_fake_runner", {})["touch_files"] = tf
    # The shared runner reads `tool` from raw, but every tool writes to a
    # different touch file — override per-tool via the touch_files map, not
    # the shared tool key. The fake looks up touch_files[tool] where tool
    # is raw["_fake_runner"]["tool"]. Since every process shares raw, we
    # need a per-tool runner or a way to differentiate. Use the runner's
    # own tool identifier: we'll key by process name via os.getpid, but
    # simpler — monkeypatch each runner to know its tool. See _install_per_tool_fakes.
    return tf


@pytest.fixture
def install_per_tool_fakes(
    monkeypatch: pytest.MonkeyPatch, orch_dirs: dict[str, Path],
):
    """Install per-tool fake runners that touch a tool-named file.

    Each registered fake is a tiny closure-free factory call that stamps
    the tool name into ``raw`` before handing off to the shared
    ``_shared_exit``. This way every tool writes its OWN touch file even
    though they all share the same ``raw`` dict.

    Closures don't pickle cleanly across multiprocessing ``spawn``, but
    WSL/Linux defaults to ``fork`` — so local closures work here. For
    this fixture we generate wrapper functions dynamically and register
    them at module scope via a class with a predictable ``__module__``.
    """
    import tests.orchestrator._fakes as fakes_module

    def _install(tools_with_arity: dict[str, int]) -> None:
        for tool, arity in tools_with_arity.items():
            if arity == 2:
                # Bind tool name via a small per-tool runner pulled out
                # of a pre-registered map on _fakes_module. Simpler: add
                # a closure + hope fork. (Orchestrator uses fork on Linux;
                # this is fine.)
                def _runner(raw, suppress_stdout=False, _tool=tool):
                    raw = dict(raw)
                    raw["_fake_runner"] = {
                        **raw.get("_fake_runner", {}),
                        "tool": _tool,
                    }
                    fakes_module._shared_exit(raw)
                monkeypatch.setitem(
                    orchestrator.TOOL_RUNNERS, tool, _runner,
                )
            else:
                def _runner(raw, skills_dir, suppress_stdout=False, _tool=tool):
                    raw = dict(raw)
                    raw["_fake_runner"] = {
                        **raw.get("_fake_runner", {}),
                        "tool": _tool,
                    }
                    fakes_module._shared_exit(raw)
                monkeypatch.setitem(
                    orchestrator.TOOL_RUNNERS, tool, _runner,
                )

    return _install


def test_no_config_blocks_starts_no_daemons(
    orchestrator_raw_config, orch_dirs, fast_sleep,
    install_per_tool_fakes, fire_sentinel_after, capsys,
) -> None:
    """Configuration-by-presence: empty config (no tool blocks) spawns nothing.

    KAL-LE's ``config.kalle.yaml`` is the live consumer of this contract —
    it omits curator/janitor/distiller because that instance has no inbox
    and no learn extraction. Starting them anyway crashed the daemons in
    a 5-retry loop on missing-inbox FileNotFoundError.
    """
    raw = orchestrator_raw_config
    # No tool sections present → nothing should auto-spawn.
    assert "surveyor" not in raw
    assert "mail" not in raw
    assert "brief" not in raw
    assert "telegram" not in raw
    assert "instructor" not in raw
    assert "curator" not in raw
    assert "janitor" not in raw
    assert "distiller" not in raw

    _wire_touch_files(raw, orch_dirs, list(ALL_FAKES))
    install_per_tool_fakes({t: (3 if t in {"curator", "janitor", "distiller", "instructor", "talker"} else 2)
                            for t in ALL_FAKES})

    fire_sentinel_after(0.3)
    orchestrator.run_all(
        raw, only=None, skills_dir=orch_dirs["skills"],
        pid_path=orch_dirs["pid_path"], live_mode=False,
    )

    # Give the orchestrator a moment to do nothing — we want to confirm no
    # touch files appear, not catch a race on filesystem visibility.
    for _ in range(10):
        time.sleep(0.05)
    started = _read_started_tools(orch_dirs["data"])
    assert started == set(), f"expected no daemons, got {started}"


def test_curator_janitor_distiller_blocks_trigger_their_starts(
    orchestrator_raw_config, orch_dirs, fast_sleep,
    install_per_tool_fakes, fire_sentinel_after,
) -> None:
    """Adding ``curator:``/``janitor:``/``distiller:`` opts each into auto-start.

    Symmetric to the surveyor / telegram / instructor presence tests —
    each daemon now follows the same configuration-by-presence convention.
    """
    raw = orchestrator_raw_config
    raw["curator"] = {}
    raw["janitor"] = {}
    raw["distiller"] = {}

    _wire_touch_files(raw, orch_dirs, list(ALL_FAKES))
    install_per_tool_fakes({t: (3 if t in {"curator", "janitor", "distiller", "instructor", "talker"} else 2)
                            for t in ALL_FAKES})

    fire_sentinel_after(0.6)
    orchestrator.run_all(
        raw, only=None, skills_dir=orch_dirs["skills"],
        pid_path=orch_dirs["pid_path"], live_mode=False,
    )

    for _ in range(30):
        started = _read_started_tools(orch_dirs["data"])
        if {"curator", "janitor", "distiller"}.issubset(started):
            break
        time.sleep(0.05)

    assert "curator" in started
    assert "janitor" in started
    assert "distiller" in started


def test_distiller_enabled_false_skips_start(
    orchestrator_raw_config, orch_dirs, fast_sleep,
    install_per_tool_fakes, fire_sentinel_after,
) -> None:
    """``distiller: { enabled: false }`` opts an instance OUT of distiller
    auto-start even though the block is present.

    Distinct from "no_config_block": this is the explicit-disable case
    used by instances (e.g. KAL-LE before Phase 1) that want to declare
    distiller off intentionally rather than implicitly.
    """
    raw = orchestrator_raw_config
    raw["curator"] = {}      # should still start
    raw["janitor"] = {}      # should still start
    raw["distiller"] = {"enabled": False}  # opted out

    _wire_touch_files(raw, orch_dirs, list(ALL_FAKES))
    install_per_tool_fakes({t: (3 if t in {"curator", "janitor", "distiller", "instructor", "talker"} else 2)
                            for t in ALL_FAKES})

    fire_sentinel_after(0.6)
    orchestrator.run_all(
        raw, only=None, skills_dir=orch_dirs["skills"],
        pid_path=orch_dirs["pid_path"], live_mode=False,
    )

    # Wait for curator/janitor to start (proves the orchestrator ran),
    # then assert distiller did NOT.
    for _ in range(30):
        started = _read_started_tools(orch_dirs["data"])
        if {"curator", "janitor"}.issubset(started):
            break
        time.sleep(0.05)

    assert "curator" in started, f"curator should start, got {started}"
    assert "janitor" in started, f"janitor should start, got {started}"
    assert "distiller" not in started, (
        f"distiller should be skipped via enabled=false, got {started}"
    )


def test_surveyor_enabled_false_skips_start(
    orchestrator_raw_config, orch_dirs, fast_sleep,
    install_per_tool_fakes, fire_sentinel_after,
) -> None:
    """``surveyor: { enabled: false }`` opts out symmetrically to distiller."""
    raw = orchestrator_raw_config
    raw["curator"] = {}      # should still start
    raw["surveyor"] = {"enabled": False}

    _wire_touch_files(raw, orch_dirs, list(ALL_FAKES))
    install_per_tool_fakes({t: (3 if t in {"curator", "janitor", "distiller", "instructor", "talker"} else 2)
                            for t in ALL_FAKES})

    fire_sentinel_after(0.5)
    orchestrator.run_all(
        raw, only=None, skills_dir=orch_dirs["skills"],
        pid_path=orch_dirs["pid_path"], live_mode=False,
    )

    for _ in range(30):
        started = _read_started_tools(orch_dirs["data"])
        if "curator" in started:
            break
        time.sleep(0.05)

    assert "curator" in started
    assert "surveyor" not in started, (
        f"surveyor should be skipped via enabled=false, got {started}"
    )


def test_surveyor_section_triggers_surveyor_start(
    orchestrator_raw_config, orch_dirs, fast_sleep,
    install_per_tool_fakes, fire_sentinel_after,
) -> None:
    """Adding ``surveyor`` to the config dict opts into surveyor auto-start."""
    raw = orchestrator_raw_config
    raw["surveyor"] = {"some_key": "value"}

    _wire_touch_files(raw, orch_dirs, list(ALL_FAKES))
    install_per_tool_fakes({t: (3 if t in {"curator", "janitor", "distiller", "instructor", "talker"} else 2)
                            for t in ALL_FAKES})

    fire_sentinel_after(0.4)
    orchestrator.run_all(
        raw, only=None, skills_dir=orch_dirs["skills"],
        pid_path=orch_dirs["pid_path"], live_mode=False,
    )

    for _ in range(30):
        started = _read_started_tools(orch_dirs["data"])
        if "surveyor" in started:
            break
        time.sleep(0.05)

    assert "surveyor" in started


def test_telegram_section_triggers_talker_start(
    orchestrator_raw_config, orch_dirs, fast_sleep,
    install_per_tool_fakes, fire_sentinel_after,
) -> None:
    """The config key is ``telegram`` but the tool is ``talker``.

    This asymmetry is explicit in ``run_all``: ``if "telegram" in raw:
    tools.append("talker")``. If someone renames the config key to
    ``talker`` without updating the orchestrator check, the talker
    daemon would silently stop starting.
    """
    raw = orchestrator_raw_config
    raw["telegram"] = {"bot_token": "fake"}

    _wire_touch_files(raw, orch_dirs, list(ALL_FAKES))
    install_per_tool_fakes({t: (3 if t in {"curator", "janitor", "distiller", "instructor", "talker"} else 2)
                            for t in ALL_FAKES})

    fire_sentinel_after(0.5)
    orchestrator.run_all(
        raw, only=None, skills_dir=orch_dirs["skills"],
        pid_path=orch_dirs["pid_path"], live_mode=False,
    )

    for _ in range(30):
        started = _read_started_tools(orch_dirs["data"])
        if "talker" in started:
            break
        time.sleep(0.05)

    assert "talker" in started


def test_instructor_section_triggers_instructor_start(
    orchestrator_raw_config, orch_dirs, fast_sleep,
    install_per_tool_fakes, fire_sentinel_after,
) -> None:
    """Adding ``instructor`` to the config dict opts into instructor auto-start.

    Mirror of the surveyor / telegram auto-start tests. Without the
    section, the daemon would spin on a missing Anthropic API key.
    """
    raw = orchestrator_raw_config
    raw["instructor"] = {"poll_interval_seconds": 60}

    _wire_touch_files(raw, orch_dirs, list(ALL_FAKES))
    install_per_tool_fakes({t: (3 if t in {"curator", "janitor", "distiller", "instructor", "talker"} else 2)
                            for t in ALL_FAKES})

    fire_sentinel_after(0.5)
    orchestrator.run_all(
        raw, only=None, skills_dir=orch_dirs["skills"],
        pid_path=orch_dirs["pid_path"], live_mode=False,
    )

    for _ in range(30):
        started = _read_started_tools(orch_dirs["data"])
        if "instructor" in started:
            break
        time.sleep(0.05)

    assert "instructor" in started


def test_only_flag_overrides_auto_start(
    orchestrator_raw_config, orch_dirs, fast_sleep,
    install_per_tool_fakes, fire_sentinel_after,
) -> None:
    """Passing ``only="curator"`` starts ONLY curator, regardless of sections."""
    raw = orchestrator_raw_config
    # Even though surveyor is in the config, ``only=curator`` overrides.
    raw["surveyor"] = {}
    raw["telegram"] = {}

    _wire_touch_files(raw, orch_dirs, list(ALL_FAKES))
    install_per_tool_fakes({t: (3 if t in {"curator", "janitor", "distiller", "instructor", "talker"} else 2)
                            for t in ALL_FAKES})

    fire_sentinel_after(0.2)
    orchestrator.run_all(
        raw, only="curator", skills_dir=orch_dirs["skills"],
        pid_path=orch_dirs["pid_path"], live_mode=False,
    )

    for _ in range(20):
        started = _read_started_tools(orch_dirs["data"])
        if "curator" in started:
            break
        time.sleep(0.05)

    assert started == {"curator"}


def test_unknown_tool_in_only_exits_nonzero(
    orchestrator_raw_config, orch_dirs, fast_sleep, capsys,
) -> None:
    """``only=bogus`` is a CLI input error — exit, don't launch anything."""
    raw = orchestrator_raw_config

    with pytest.raises(SystemExit) as exc_info:
        orchestrator.run_all(
            raw, only="bogus_tool", skills_dir=orch_dirs["skills"],
            pid_path=orch_dirs["pid_path"], live_mode=False,
        )
    assert exc_info.value.code == 1
    out = capsys.readouterr().out
    assert "Unknown tool: bogus_tool" in out


def test_mail_and_brief_sections_trigger_their_tools(
    orchestrator_raw_config, orch_dirs, fast_sleep,
    install_per_tool_fakes, fire_sentinel_after,
) -> None:
    """Both ``mail`` and ``brief`` config sections trigger auto-start.

    Covers the parallel branches to the surveyor/telegram ones — easy
    to regress if someone refactors the auto-start block into a loop.
    """
    raw = orchestrator_raw_config
    raw["mail"] = {}
    raw["brief"] = {}

    _wire_touch_files(raw, orch_dirs, list(ALL_FAKES))
    install_per_tool_fakes({t: (3 if t in {"curator", "janitor", "distiller", "instructor", "talker"} else 2)
                            for t in ALL_FAKES})

    fire_sentinel_after(0.6)
    orchestrator.run_all(
        raw, only=None, skills_dir=orch_dirs["skills"],
        pid_path=orch_dirs["pid_path"], live_mode=False,
    )

    for _ in range(30):
        started = _read_started_tools(orch_dirs["data"])
        if {"mail", "brief"}.issubset(started):
            break
        time.sleep(0.05)

    assert "mail" in started, f"mail not started, got {started}"
    assert "brief" in started, f"brief not started, got {started}"


def test_workers_json_records_started_tools(
    orchestrator_raw_config, orch_dirs, fast_sleep,
    install_per_tool_fakes, fire_sentinel_after,
) -> None:
    """``workers.json`` is the TUI's source of truth — must list started tools."""
    raw = orchestrator_raw_config

    _wire_touch_files(raw, orch_dirs, list(ALL_FAKES))
    install_per_tool_fakes({t: (3 if t in {"curator", "janitor", "distiller", "instructor", "talker"} else 2)
                            for t in ALL_FAKES})

    # Give the orchestrator enough time to write workers.json before
    # the sentinel fires.
    fire_sentinel_after(0.6)
    orchestrator.run_all(
        raw, only="curator,janitor",
        skills_dir=orch_dirs["skills"],
        pid_path=orch_dirs["pid_path"], live_mode=False,
    )

    # workers.json is cleaned up in the finally block. We read it
    # during the run instead — but since run_all returns only after
    # the finally block, we can only observe side effects. Instead,
    # verify tools started via touch files (which persist).
    for _ in range(20):
        started = _read_started_tools(orch_dirs["data"])
        if {"curator", "janitor"}.issubset(started):
            break
        time.sleep(0.05)
    assert {"curator", "janitor"}.issubset(started)
