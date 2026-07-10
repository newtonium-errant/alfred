"""Tests for the STAY-C sovereign scribe slot standup (scribe P1-d).

The "STAY-C exists as a running sovereign instance" milestone: the slot comes
UP boundary-enforced + guard-armed + idle-ready, no audio pipeline (P2). Pins
the boot sequence (boundary re-validate + http-guard self-install + ILB
signal), the cloud-key-refuses-boot → exit-79 path, the config template load,
and the barrier-d allowlist over the real template.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import structlog
import yaml

from alfred.orchestrator import _SOVEREIGN_BREACH_EXIT, _run_scribe
from alfred.scribe import SCRIBE_MODE_SYNTHETIC, load_from_unified
from alfred.scribe.daemon import startup
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
    raw = {
        "instance": {"name": "STAY-C"},
        "sovereign": {"enabled": True},
        "scribe": {
            "mode": "synthetic",
            "stt": {"provider": "faster-whisper"},
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
