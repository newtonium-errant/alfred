"""GROUND #7 sovereign-gated exit-79 propagation (#42).

The orchestrator's four-edit change (E1-E4 in ``run_all``): a SOVEREIGN
instance whose child breaches the no-egress boundary at RUNTIME (exit 79)
tears every sibling down and re-raises 79 to the supervisor, so systemd's
``RestartPreventExitStatus=79`` keeps the instance DOWN. A NON-sovereign
instance keeps today's drop-slot-and-continue behavior byte-for-byte.

These pins isolate the RUNTIME path by monkeypatching
``orchestrator._enforce_sovereign_boundary_or_exit`` to a no-op — the
pre-spawn LOAD gate is validated elsewhere (tests/sovereign/...); here we
prove the monitor-loop propagation, not the load gate.

Four behavioral pins + one source-level KNOWN-GAP pin:
  (i)   sovereign + child-exits-79           → SystemExit(79), no restart
  (ii)  non-sovereign + one-79 + one-healthy → returns None, 79 dropped, healthy untouched
  (iii) sovereign + graceful shutdown        → returns None, NO false 79 (trickiest edge)
  (iv)  sovereign + 2 slots, one 79          → SystemExit(79) AND sibling torn down
  (2b)  dashboard/TUI supervision paths still restart 79 today → PINNED (task #59)

The fake runners use the same counter-file harness as ``test_exit_codes.py``:
a per-tool file with one line per start, so "how many times did the
orchestrator (re)start this tool" is observable from the parent.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

import pytest

import alfred.orchestrator as orchestrator


# ---------------------------------------------------------------------------
# Counter-file harness (mirrors test_exit_codes.py; top-level fns so the
# multiprocessing children can pickle them by qualified name)
# ---------------------------------------------------------------------------


def _read_start_count(path: Path) -> int:
    """Number of lines in the per-tool start-counter file (one line per start)."""
    try:
        return sum(1 for _ in path.open("r", encoding="utf-8"))
    except (FileNotFoundError, OSError):
        return 0


def _record_start(raw: dict[str, Any], tool: str) -> None:
    counter = raw.get("_fake_runner", {}).get("counter_files", {}).get(tool)
    if counter:
        try:
            with open(counter, "a", encoding="utf-8") as f:
                f.write("start\n")
        except OSError:
            pass


def _wire_counter(raw: dict, orch_dirs: dict, tool: str) -> Path:
    path = orch_dirs["data"] / f"count_{tool}"
    raw.setdefault("_fake_runner", {}).setdefault("counter_files", {})[tool] = str(path)
    return path


# --- Fakes (top-level for pickling) ----------------------------------------


def _fake_exit_79_curator_3arg(
    raw: dict[str, Any], skills_dir: str, suppress_stdout: bool = False,
) -> None:
    """Curator-shape (3-arg) fake: short delay so the parent observes it alive,
    then exit 79 (sovereign boundary breach)."""
    _record_start(raw, "curator")
    time.sleep(0.1)
    sys.exit(79)


def _fake_healthy_longlived_curator_3arg(
    raw: dict[str, Any], skills_dir: str, suppress_stdout: bool = False,
) -> None:
    """Curator-shape (3-arg) healthy fake: records a start then blocks until the
    orchestrator terminates it (used for the graceful-shutdown pin)."""
    _record_start(raw, "curator")
    time.sleep(60)


def _fake_healthy_longlived_surveyor_2arg(
    raw: dict[str, Any], suppress_stdout: bool = False,
) -> None:
    """Surveyor-shape (2-arg) healthy fake: records a start then blocks. Stays
    alive so the parent can prove it was left running (ii) / torn down (iv)."""
    _record_start(raw, "surveyor")
    time.sleep(60)


@pytest.fixture
def isolate_runtime_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """No-op the PRE-SPAWN load gate so these tests isolate the RUNTIME path.

    ``run_all`` reads ``raw['sovereign']['enabled']`` directly for the
    propagation flag (E1), so a no-op'd load gate does NOT disable the
    propagation under test — it only skips the fork-time boundary refusal.
    """
    monkeypatch.setattr(
        orchestrator, "_enforce_sovereign_boundary_or_exit", lambda raw: None
    )
    return None


# ---------------------------------------------------------------------------
# (i) PIN 1 — sovereign breach propagates exit 79
# ---------------------------------------------------------------------------


def test_sovereign_breach_propagates_exit_79(
    orchestrator_raw_config, orch_dirs, fast_sleep, isolate_runtime_path,
    install_fake_runners, fire_sentinel_after, capsys,
) -> None:
    """Sovereign instance + child exits 79 → run_all raises SystemExit(79),
    the tool is NOT restarted, and the teardown line is printed."""
    raw = orchestrator_raw_config
    raw["sovereign"] = {"enabled": True}
    counter = _wire_counter(raw, orch_dirs, "curator")
    install_fake_runners({"curator": _fake_exit_79_curator_3arg})

    # Safety sentinel in case the propagation regressed to drop-and-continue
    # (would otherwise hang until "All daemons failed"); the 79 path should
    # sys.exit BEFORE this fires.
    fire_sentinel_after(5.0)

    with pytest.raises(SystemExit) as exc:
        orchestrator.run_all(
            raw, only="curator",
            skills_dir=orch_dirs["skills"],
            pid_path=orch_dirs["pid_path"],
            live_mode=False,
        )

    assert exc.value.code == 79, f"expected propagated exit 79, got {exc.value.code}"

    start_count = _read_start_count(counter)
    assert start_count == 1, (
        f"79-breach must NOT restart the tool; got {start_count} starts"
    )

    out = capsys.readouterr().out
    assert "propagating exit 79" in out, (
        "teardown line 'propagating exit 79' must be printed on the sovereign path"
    )


# ---------------------------------------------------------------------------
# (i-b) PIN 1b (CF1) — truthy-non-True enabled ("true"/1/"yes") still propagates
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("enabled_value", ["true", 1, "yes"], ids=["str_true", "int_1", "str_yes"])
def test_truthy_sovereign_enabled_still_propagates_exit_79(
    orchestrator_raw_config, orch_dirs, fast_sleep, isolate_runtime_path,
    install_fake_runners, fire_sentinel_after, capsys, enabled_value,
) -> None:
    """CF1 regression guard: the runtime propagation gate reads ``enabled``
    TRUTHILY (mirroring the load gate + boundary + scribe startup), NOT strict
    ``is True``. A config with ``enabled: "true"`` (or 1 / "yes") boots
    sovereign at the load gate, so the runtime gate MUST agree — otherwise a
    breach silently reverts to drop-and-continue → exit 0 (the "79 theater"
    bug). Same fake-79 setup as PIN 1, only the enabled VALUE differs."""
    raw = orchestrator_raw_config
    raw["sovereign"] = {"enabled": enabled_value}
    _wire_counter(raw, orch_dirs, "curator")
    install_fake_runners({"curator": _fake_exit_79_curator_3arg})

    fire_sentinel_after(5.0)

    with pytest.raises(SystemExit) as exc:
        orchestrator.run_all(
            raw, only="curator",
            skills_dir=orch_dirs["skills"],
            pid_path=orch_dirs["pid_path"],
            live_mode=False,
        )

    assert exc.value.code == 79, (
        f"truthy enabled={enabled_value!r} must still propagate 79, "
        f"got {exc.value.code}"
    )
    assert "propagating exit 79" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# (ii) PIN 2 — non-sovereign 79 drops and continues (zero-regression pin)
# ---------------------------------------------------------------------------


def test_nonsovereign_79_drops_and_continues(
    orchestrator_raw_config, orch_dirs, fast_sleep, isolate_runtime_path,
    install_fake_runners, fire_sentinel_after, capsys,
) -> None:
    """NON-sovereign instance: one fake exits 79, one is healthy. run_all
    returns None (NO SystemExit), the 79 tool is dropped (no restart), and the
    healthy tool runs untouched. This pins the byte-for-byte-unchanged path."""
    raw = orchestrator_raw_config
    # No sovereign block → sovereign_enabled is False.
    curator_counter = _wire_counter(raw, orch_dirs, "curator")
    surveyor_counter = _wire_counter(raw, orch_dirs, "surveyor")
    install_fake_runners({
        "curator": _fake_exit_79_curator_3arg,
        "surveyor": _fake_healthy_longlived_surveyor_2arg,
    })

    # Healthy tool never exits → stop the loop with the sentinel.
    fire_sentinel_after(1.0)

    result = orchestrator.run_all(
        raw, only="curator,surveyor",
        skills_dir=orch_dirs["skills"],
        pid_path=orch_dirs["pid_path"],
        live_mode=False,
    )

    assert result is None, "non-sovereign 79 must NOT propagate (returns None)"

    assert _read_start_count(curator_counter) == 1, "79 tool must be dropped, not restarted"
    assert _read_start_count(surveyor_counter) == 1, "healthy tool must run untouched"

    out = capsys.readouterr().out
    # The breach line still prints (it always did), but the SOVEREIGN teardown
    # line must NOT — that is the zero-regression contract.
    assert "sovereign boundary breach" in out
    assert "propagating exit 79" not in out, (
        "non-sovereign path must never propagate 79"
    )


# ---------------------------------------------------------------------------
# (iii) PIN 3 — graceful shutdown on a sovereign instance does NOT false-79
# ---------------------------------------------------------------------------


def test_graceful_down_on_sovereign_does_not_propagate_79(
    orchestrator_raw_config, orch_dirs, fast_sleep, isolate_runtime_path,
    install_fake_runners, fire_sentinel_after,
) -> None:
    """THE TRICKIEST EDGE. A sovereign instance running normally, then a
    graceful shutdown (sentinel fires BEFORE any child-exit) → run_all returns
    None, NO SystemExit(79). Proves the propagation is keyed ONLY on the
    79-detection site, never on "the loop broke"."""
    raw = orchestrator_raw_config
    raw["sovereign"] = {"enabled": True}
    _wire_counter(raw, orch_dirs, "curator")
    # Long-lived healthy child → the ONLY way the loop exits is the sentinel.
    install_fake_runners({"curator": _fake_healthy_longlived_curator_3arg})

    fire_sentinel_after(0.5)

    try:
        result = orchestrator.run_all(
            raw, only="curator",
            skills_dir=orch_dirs["skills"],
            pid_path=orch_dirs["pid_path"],
            live_mode=False,
        )
    except SystemExit as exc:  # pragma: no cover - only hit on regression
        pytest.fail(
            f"graceful shutdown wrongly propagated SystemExit({exc.code}) — "
            f"the sovereign flag latched on 'loop broke' instead of the "
            f"79-detection site"
        )

    assert result is None, "graceful shutdown must return None, not propagate"


# ---------------------------------------------------------------------------
# (iv) PIN 4 — multi-slot sovereign: one 79 tears down ALL and exits 79
# ---------------------------------------------------------------------------


def test_multislot_sovereign_one_79_tears_down_all_and_exits_79(
    orchestrator_raw_config, orch_dirs, fast_sleep, isolate_runtime_path,
    install_fake_runners, fire_sentinel_after, capsys,
) -> None:
    """Sovereign instance with TWO slots — one exits 79 at runtime, the other
    is healthy and long-lived. Proves abort-ALL-siblings (R9) + propagation:
    run_all raises SystemExit(79) AND the healthy sibling was torn down in the
    finally (its '[surveyor] stopped' line proves it was still alive when the
    breach fired)."""
    raw = orchestrator_raw_config
    raw["sovereign"] = {"enabled": True}
    curator_counter = _wire_counter(raw, orch_dirs, "curator")
    surveyor_counter = _wire_counter(raw, orch_dirs, "surveyor")
    install_fake_runners({
        "curator": _fake_exit_79_curator_3arg,
        "surveyor": _fake_healthy_longlived_surveyor_2arg,
    })

    fire_sentinel_after(5.0)  # safety only; the 79 path should exit first

    with pytest.raises(SystemExit) as exc:
        orchestrator.run_all(
            raw, only="curator,surveyor",
            skills_dir=orch_dirs["skills"],
            pid_path=orch_dirs["pid_path"],
            live_mode=False,
        )

    assert exc.value.code == 79

    out = capsys.readouterr().out
    # The healthy sibling was STILL ALIVE when the breach fired → the finally
    # terminated it → "  [surveyor] stopped". That is the abort-all-siblings pin.
    assert "[surveyor] stopped" in out, (
        "healthy sibling must be torn down when a sovereign sibling breaches"
    )
    assert "propagating exit 79" in out

    # The breaching curator started exactly once (no restart); the healthy
    # surveyor started exactly once (torn down, never restarted).
    assert _read_start_count(curator_counter) == 1
    assert _read_start_count(surveyor_counter) == 1


# ---------------------------------------------------------------------------
# (2b) KNOWN-GAP pin — dashboard/TUI supervision paths still restart 79 (#59)
# ---------------------------------------------------------------------------


def test_KNOWN_GAP_dashboard_paths_do_not_yet_propagate_79() -> None:
    """PIN the CURRENT (unfixed) behavior of the live dashboard + Textual TUI
    supervision loops: they special-case ONLY exit 78 (``missing_deps_exit``)
    for no-restart, so a 79 (sovereign breach) FALLS THROUGH to the restart
    path. #42 deliberately scoped its fix to the plain foreground monitor loop
    (STAY-C runs ``--_internal-foreground`` non-live, so it is unaffected). This
    is a documented-not-silent gap tracked by task #59; when #59 lands and adds
    sovereign/79 handling to these paths, the ``not in`` asserts flip and force
    this pin to be updated in lockstep.

    Source-level (not behavioral) so it needs neither rich nor textual
    installed — reads the module files directly.
    """
    import alfred
    pkg_dir = Path(alfred.__file__).resolve().parent
    dash_src = (pkg_dir / "dashboard.py").read_text(encoding="utf-8")
    tui_src = (pkg_dir / "tui" / "app.py").read_text(encoding="utf-8")

    # Both handle 78 (missing deps) for no-restart today...
    assert "missing_deps_exit" in dash_src, "dashboard should special-case 78"
    assert "missing_deps_exit" in tui_src, "TUI should special-case 78"

    # ...and NEITHER yet special-cases the sovereign 79 breach → 79 is treated
    # as restartable (the gap #59 tracks). If either grows sovereign handling,
    # update this pin + close #59.
    for name, src in (("dashboard.py", dash_src), ("tui/app.py", tui_src)):
        assert "_SOVEREIGN_BREACH_EXIT" not in src, (
            f"{name} now references the sovereign breach exit — #59 may be "
            f"fixed; update this KNOWN-GAP pin."
        )
        assert "sovereign" not in src.lower(), (
            f"{name} now mentions 'sovereign' — #59 may be fixed; update this pin."
        )
