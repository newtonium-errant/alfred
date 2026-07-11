"""Tests for the STAY-C sovereign scribe slot standup (scribe P1-d).

The "STAY-C exists as a running sovereign instance" milestone: the slot comes
UP boundary-enforced + guard-armed + idle-ready, no audio pipeline (P2). Pins
the boot sequence (boundary re-validate + http-guard self-install + ILB
signal), the cloud-key-refuses-boot → exit-79 path, the config template load,
and the barrier-d allowlist over the real template.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
import structlog
import yaml

from alfred.orchestrator import _MISSING_DEPS_EXIT, _SOVEREIGN_BREACH_EXIT, _run_scribe
from alfred.scribe import SCRIBE_MODE_SYNTHETIC, load_from_unified
from alfred.scribe.daemon import _state_path, run as scribe_run, startup
from alfred.sovereign import (
    CLOUD_KEY_ENV_VARS,
    SOVEREIGN_ALLOWED_SECTIONS,
    SovereignBoundaryError,
    is_sovereign_http_guard_installed,
    uninstall_sovereign_http_guard,
    validate_sovereign_boundary,
)

# The repo ships the ``.example`` template (per-instance ``config.*.yaml`` are
# gitignored; deploy copies this to /data/algernon/stayc-clinical/
# config.stayc-clinical.yaml).
_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.stayc-clinical.yaml.example"


def _sovereign_raw(**overrides):
    # provider=fake so the boot tests need no [scribe] extra — startup() now
    # runs ensure_backend_available (P2-b), and the fake backend needs no dep.
    # (The barrier-a allowlist still admits fake; faster-whisper is exercised
    # separately in test_scribe_stt.py.)
    raw = {
        "instance": {"name": "STAY-C"},
        "sovereign": {"enabled": True},
        "scribe": {
            "mode": "synthetic",
            "stt": {"provider": "fake"},
            "llm": {"base_url": "http://127.0.0.1:11434"},
        },
        "logging": {"dir": "./data"},
    }
    raw.update(overrides)
    return raw


@pytest.fixture(autouse=True)
def _guard_cleanup():
    # startup() installs the process-global http guard; uninstall after each
    # test so it never leaks into another test's httpx calls.
    yield
    uninstall_sovereign_http_guard()


@pytest.fixture(autouse=True)
def _scrub_cloud_env(monkeypatch):
    # Simulate the env -u scrubbed launch so os.environ-reading paths (the
    # runner) see no cloud key unless a test sets one.
    for key in CLOUD_KEY_ENV_VARS:
        monkeypatch.delenv(key, raising=False)


# ---------------------------------------------------------------------------
# Boot under sovereign + synthetic — the standup happy path
# ---------------------------------------------------------------------------

def test_startup_boots_boundary_validated_guard_armed():
    with structlog.testing.capture_logs() as caps:
        config = startup(_sovereign_raw(), env={})
    assert config.mode == SCRIBE_MODE_SYNTHETIC
    assert is_sovereign_http_guard_installed() is True
    up = [c for c in caps if c.get("event") == "scribe.daemon.up"]
    assert len(up) == 1
    assert up[0]["sovereign_ok"] is True
    assert up[0]["http_guard_installed"] is True
    # #40: the sovereign attestation reflects aiohttp web-transport coverage
    # (True in this venv where aiohttp is installed).
    assert up[0]["aiohttp_guard_installed"] is True
    assert up[0]["mode"] == SCRIBE_MODE_SYNTHETIC
    assert up[0]["has_input"] is False


def test_guard_self_install_blocks_non_loopback_from_scribe_process():
    startup(_sovereign_raw(), env={})
    # A non-loopback httpx call from within the scribe process is blocked by
    # the guard the daemon self-installed (the real per-process coverage —
    # spawn children don't inherit the parent's monkeypatch).
    with pytest.raises(SovereignBoundaryError) as exc:
        httpx.Client(timeout=1.0).get("http://8.8.8.8/")
    assert exc.value.reason == "http_guard"


def test_startup_requires_sovereign_block():
    raw = _sovereign_raw()
    raw.pop("sovereign")
    with pytest.raises(SovereignBoundaryError) as exc:
        startup(raw, env={})
    assert exc.value.reason == "scribe_requires_sovereign"


# ---------------------------------------------------------------------------
# Cloud-key present → boundary refuses → exit 79 (no-restart)
# ---------------------------------------------------------------------------

def test_startup_refuses_with_cloud_key_in_env():
    with pytest.raises(SovereignBoundaryError) as exc:
        startup(_sovereign_raw(), env={"ANTHROPIC_API_KEY": "DUMMY_ANTHROPIC_TEST_KEY"})
    assert exc.value.reason == "barrier_c"


def test_runner_exits_79_on_cloud_key_breach(tmp_path, monkeypatch):
    # The daemon runner maps a boundary breach in its OWN process to exit 79
    # (non-restartable). A cloud key in the process env => barrier c => 79.
    monkeypatch.setenv("GROQ_API_KEY", "DUMMY_GROQ_TEST_KEY")
    raw = _sovereign_raw(logging={"dir": str(tmp_path)})
    with pytest.raises(SystemExit) as exc:
        _run_scribe(raw, suppress_stdout=False)
    assert exc.value.code == _SOVEREIGN_BREACH_EXIT == 79


def test_runner_exits_78_when_stt_extra_missing(tmp_path, monkeypatch):
    # scribe P2-b: a real-model STT provider (faster-whisper) configured while
    # the [scribe] extra is missing => the daemon exits 78 (missing deps,
    # no-restart) rather than boot a scribe that cannot transcribe. Deterministic
    # regardless of install state: force faster-whisper "unavailable".
    import alfred.scribe.stt as stt_mod
    monkeypatch.setattr(stt_mod, "_faster_whisper_available", lambda: False)
    raw = _sovereign_raw(
        logging={"dir": str(tmp_path)},
        scribe={
            "mode": "synthetic",
            "stt": {"provider": "faster-whisper"},
            "llm": {"base_url": "http://127.0.0.1:11434"},
        },
    )
    with pytest.raises(SystemExit) as exc:
        _run_scribe(raw, suppress_stdout=False)
    assert exc.value.code == _MISSING_DEPS_EXIT == 78


# ---------------------------------------------------------------------------
# The config.stayc-clinical.yaml template
# ---------------------------------------------------------------------------

def _load_template() -> dict:
    with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def test_config_template_exists():
    assert _CONFIG_PATH.is_file()


def test_config_template_loads_scribe_and_sovereign():
    raw = _load_template()
    assert raw.get("sovereign", {}).get("enabled") is True
    cfg = load_from_unified(raw)
    assert cfg.mode == SCRIBE_MODE_SYNTHETIC
    assert cfg.stt.provider == "faster-whisper"
    assert cfg.llm.base_url == "http://127.0.0.1:11434"


def test_config_template_only_allowlisted_sections():
    raw = _load_template()
    # Every top-level section must be barrier-d allowlisted (minus the
    # synthetic _config_path stamped at load, which the template file omits).
    for key in raw:
        assert key in SOVEREIGN_ALLOWED_SECTIONS, f"non-allowlisted section: {key}"


def test_config_template_passes_the_boundary():
    raw = _load_template()
    validate_sovereign_boundary(raw, env={})  # no raise — sovereign_ok


def test_config_template_has_no_cloud_key_placeholders():
    # No ${...CLOUD_KEY} anywhere in the template.
    text = _CONFIG_PATH.read_text(encoding="utf-8")
    for key in CLOUD_KEY_ENV_VARS:
        assert "${" + key + "}" not in text


def test_config_template_mutation_non_allowlisted_section_refused():
    # Mutation-verify: adding a non-allowlisted section (agent) to the real
    # template makes the boundary refuse at barrier d.
    raw = _load_template()
    raw["agent"] = {"backend": "claude"}
    with pytest.raises(SovereignBoundaryError) as exc:
        validate_sovereign_boundary(raw, env={})
    assert exc.value.reason == "barrier_d"


# ---------------------------------------------------------------------------
# run() WIRING — the assembly seam the P2-d suite missed (production BLOCK:
# a missing `from pathlib import Path` made daemon.run() DOA at NameError).
# ---------------------------------------------------------------------------

def test_state_path_builds_under_logging_dir():
    # Direct, cheap pin — _state_path uses Path (the missing import).
    assert _state_path({"logging": {"dir": "/data/x"}}, None) == "/data/x/scribe_state.json"
    # default when logging.dir absent
    assert _state_path({}, None).endswith("scribe_state.json")


class _StopLoop(BaseException):
    """A one-shot sentinel to break run()'s infinite loop. BaseException so the
    loop's ``except Exception`` does not swallow it."""


def test_run_wires_state_path_and_enters_loop_without_nameerror(tmp_path, monkeypatch):
    # Exercises the run() ASSEMBLY SEAM: startup() → _state_path() →
    # ScribeState() → run_sweep(). Before the fix, _state_path()'s bare Path
    # raised NameError here (the daemon was DOA). This reaches run_sweep, proving
    # the wiring holds.
    #
    # MUTATION-BIND: remove `from pathlib import Path` from daemon.py and this
    # test goes RED — run() raises NameError from _state_path() before
    # run_sweep is ever reached, so pytest.raises(_StopLoop) fails.
    seen = {}

    async def _one_shot(config, state, vault_path):
        seen["state_type"] = type(state).__name__
        seen["vault_path_type"] = type(vault_path).__name__
        raise _StopLoop

    import alfred.scribe.pipeline as pl
    monkeypatch.setattr(pl, "run_sweep", _one_shot)

    raw = _sovereign_raw(
        logging={"dir": str(tmp_path)},
        vault={"path": str(tmp_path / "vault")},
    )
    with pytest.raises(_StopLoop):
        asyncio.run(scribe_run(raw, env={}))

    # run() built ScribeState + a Path vault_path and entered the sweep loop.
    assert seen["state_type"] == "ScribeState"
    assert seen["vault_path_type"] == "PosixPath"
    # the state file path was under logging.dir (no NameError building it)
    assert (tmp_path / "scribe_state.json") == Path(_state_path(raw, None))
