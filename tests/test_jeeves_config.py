"""The ``jeeves:`` config block (task #81, stage 1).

Three properties carry the weight: every fail-closed default actually fails
closed, every path derives from the instance's own data dir (never a shared
``./data`` literal), and no instance name appears anywhere.
"""

from __future__ import annotations

import pytest
import structlog

from alfred.jeeves import config as cfg

KALLE_LIKE = {"logging": {"dir": "/home/andrew/.alfred/other/data"}}
SALEM_LIKE = {"logging": {"dir": "./data"}}


# ---------------------------------------------------------------------------
# Absent block ⇒ fully inert
# ---------------------------------------------------------------------------


def test_an_absent_block_yields_a_completely_inert_jeeves():
    """The correct state for every instance that has no microphone: nothing
    listens, nothing routes, nothing is processed."""
    loaded = cfg.load_from_unified({})
    assert loaded.mode == cfg.JEEVES_MODE_SYNTHETIC
    assert loaded.is_live is False
    assert loaded.wake.provider == cfg.WAKE_PROVIDER_OFF
    assert loaded.cues.route_target == ""
    assert loaded.miss_audio_dir == ""


def test_a_non_dict_block_is_tolerated():
    for junk in (None, "jeeves", 42, []):
        loaded = cfg.load_from_unified({"jeeves": junk})
        assert loaded.mode == cfg.JEEVES_MODE_SYNTHETIC


def test_unknown_keys_are_dropped_not_fatal():
    """The load-time schema-tolerance contract: a config written by a newer
    build must not crash an older one."""
    loaded = cfg.load_from_unified({"jeeves": {
        "mode": "live",
        "future_knob": True,
        "ring": {"seconds": 60, "future_sub_knob": "x"},
    }})
    assert loaded.is_live
    assert loaded.ring.seconds == 60


# ---------------------------------------------------------------------------
# The mode line — fail-closed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw", [
    "live", "LIVE", " live ", "Live",
])
def test_only_the_exact_word_live_opens_the_live_path(raw: str):
    assert cfg.load_from_unified({"jeeves": {"mode": raw}}).is_live


@pytest.mark.parametrize("raw", [
    None, "", "synthetic", "livee", "l1ve", "true", 1, {}, "clinical",
])
def test_everything_else_resolves_to_synthetic(raw):
    """A typo, a truncated value or an omitted key can never arm a
    microphone. ``clinical`` is in the list deliberately: Jeeves is never a
    clinical instrument, and the scribe's word must not mean anything here."""
    loaded = cfg.load_from_unified({"jeeves": {"mode": raw}})
    assert loaded.mode == cfg.JEEVES_MODE_SYNTHETIC
    assert loaded.is_live is False


# ---------------------------------------------------------------------------
# Per-instance paths — the #53/#74 shape
# ---------------------------------------------------------------------------


def test_paths_derive_from_the_instances_own_data_dir():
    loaded = cfg.load_from_unified(KALLE_LIKE)
    assert loaded.telemetry_path == \
        "/home/andrew/.alfred/other/data/jeeves/telemetry.jsonl"
    assert loaded.mark_log_path == \
        "/home/andrew/.alfred/other/data/jeeves/marks.jsonl"


def test_the_suspend_store_is_ALWAYS_derived_never_left_empty():
    """#98, ruling 3. Every other path knob here means "off" when unset; this
    one cannot, because a suspension with nowhere to live would not survive
    the restart it exists to survive. ``suspend.read_status("")`` resolves to
    NOT suspended (a config state, not an unknown one), so a loaded config
    reaching that branch would be a toggle silently enforcing nothing."""
    for raw in ({}, KALLE_LIKE, SALEM_LIKE, {"jeeves": {}}):
        loaded = cfg.load_from_unified(raw)
        assert loaded.suspend_state_path, (
            f"a loaded config left the company toggle storeless: {raw}"
        )
        assert loaded.suspend_state_path.endswith("jeeves/suspended.json")


def test_the_suspend_store_is_per_instance_too():
    a = cfg.load_from_unified(KALLE_LIKE)
    b = cfg.load_from_unified(SALEM_LIKE)
    assert a.suspend_state_path != b.suspend_state_path
    # ...and an explicit value still wins over the derivation.
    explicit = cfg.load_from_unified({
        "logging": {"dir": "/data/x"},
        "jeeves": {"suspend_state_path": "/var/jeeves/s.json"},
    })
    assert explicit.suspend_state_path == "/var/jeeves/s.json"


def test_the_retention_window_falls_back_on_nonsense():
    """A zero or negative retention would delete every retained window on the
    next sweep, including the one reported thirty seconds ago."""
    for bad in (0, -3, "seven", None, [], 0.0):
        loaded = cfg.load_from_unified({"jeeves": {"miss_audio_retention_days": bad}})
        assert loaded.miss_audio_retention_days == \
            cfg.DEFAULT_MISS_AUDIO_RETENTION_DAYS
    # ...and a real value is honoured (the control).
    assert cfg.load_from_unified(
        {"jeeves": {"miss_audio_retention_days": 3}}
    ).miss_audio_retention_days == 3


def test_two_instances_never_share_a_jeeves_file():
    """The failure this prevents is real and has happened for other stores:
    co-located instances share one WorkingDirectory and differ only by
    --config, so a cwd-relative default is ONE file for all of them."""
    a = cfg.load_from_unified(KALLE_LIKE)
    b = cfg.load_from_unified(SALEM_LIKE)
    assert a.telemetry_path != b.telemetry_path
    assert a.mark_log_path != b.mark_log_path


def test_an_explicit_path_always_wins():
    loaded = cfg.load_from_unified({
        "logging": {"dir": "/data/x"},
        "jeeves": {"telemetry_path": "/var/jeeves/t.jsonl"},
    })
    assert loaded.telemetry_path == "/var/jeeves/t.jsonl"
    # ...and the un-overridden one still derives.
    assert loaded.mark_log_path == "/data/x/jeeves/marks.jsonl"


def test_a_blank_logging_dir_does_not_root_anchor_the_paths():
    """``.get('dir', './data')`` hands back "" for an explicitly blank value,
    which would turn ``<dir>/jeeves/…`` into ``/jeeves/…`` at the filesystem
    root. The shared resolver closes that hole; this pins it stays closed."""
    loaded = cfg.load_from_unified({"logging": {"dir": "   "}})
    assert loaded.telemetry_path.startswith("./data/")


def test_no_shipped_default_is_a_cwd_relative_literal_in_the_module():
    """#74 pattern. No STRING LITERAL in the module may be a cwd-relative
    data path — the value comes from the shared instance-path resolver, so a
    future edit cannot reintroduce the shared-file bug by copying a literal.

    Docstrings are excluded because prose is allowed (and required) to
    EXPLAIN the trap; a scan that fires on its own explanation gets deleted.
    """
    import ast
    from pathlib import Path

    tree = ast.parse(Path(cfg.__file__).read_text(encoding="utf-8"))
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef))
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    offenders = [
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
        and "./data" in node.value
    ]
    assert offenders == [], (
        f"cwd-relative data-path literals in jeeves/config.py: {offenders}"
    )


# ---------------------------------------------------------------------------
# The YAML-null trap
# ---------------------------------------------------------------------------


def test_a_yaml_null_route_target_leaves_route_inert():
    """``str(None)`` is ``"None"`` — a truthy string that would arm the ROUTE
    verb pointed at a peer called "None"."""
    loaded = cfg.load_from_unified({"jeeves": {"cues": {"route_target": None}}})
    assert loaded.cues.route_target == ""


def test_a_yaml_null_miss_audio_dir_stays_empty():
    loaded = cfg.load_from_unified({"jeeves": {"miss_audio_dir": None}})
    assert loaded.miss_audio_dir == ""


def test_a_yaml_null_path_still_derives_the_instance_default():
    loaded = cfg.load_from_unified({
        "logging": {"dir": "/d"}, "jeeves": {"telemetry_path": None},
    })
    assert loaded.telemetry_path == "/d/jeeves/telemetry.jsonl"


# ---------------------------------------------------------------------------
# The STT key
# ---------------------------------------------------------------------------


def test_an_unresolved_env_placeholder_blanks_the_api_key(monkeypatch):
    """An unset env var survives substitution as the LITERAL placeholder —
    truthy, publicly known from the example config, and it would send every
    cued window at a credential that cannot work."""
    monkeypatch.delenv("JEEVES_TEST_KEY_UNSET", raising=False)
    with structlog.testing.capture_logs() as captured:
        loaded = cfg.load_from_unified({"jeeves": {
            "stt": {"api_key": "${JEEVES_TEST_KEY_UNSET}"},
        }})
    assert loaded.stt.api_key == ""
    events = [c for c in captured
              if c.get("event") == "jeeves.config.unresolved_api_key_placeholder"]
    assert len(events) == 1


def test_a_resolved_env_key_is_carried(monkeypatch):
    monkeypatch.setenv("JEEVES_TEST_KEY_SET", "DUMMY_GROQ_TEST_KEY")
    loaded = cfg.load_from_unified({"jeeves": {
        "stt": {"api_key": "${JEEVES_TEST_KEY_SET}"},
    }})
    assert loaded.stt.api_key == "DUMMY_GROQ_TEST_KEY"


def test_the_stt_block_satisfies_the_vocab_seams_shape():
    """The seam reads exactly two attributes off whatever it is handed; if
    the block stops carrying them, Jeeves silently loses every
    operator-approved learned term."""
    loaded = cfg.load_from_unified({"jeeves": {"stt": {
        "vocab_terms": ["RRTS", "6203 bearing"],
        "vocab_decided_path": "/d/decided.jsonl",
    }}})
    assert hasattr(loaded.stt, "vocab_terms")
    assert hasattr(loaded.stt, "vocab_decided_path")
    assert loaded.stt.vocab_terms == ["RRTS", "6203 bearing"]


# ---------------------------------------------------------------------------
# Numeric coercion — nonsense keeps the default, never crashes the load
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["x", None, -5, 0, {}, []])
def test_a_nonsense_ring_size_keeps_the_ratified_default(bad):
    loaded = cfg.load_from_unified({"jeeves": {"ring": {"seconds": bad}}})
    assert loaded.ring.seconds == cfg.DEFAULT_RING_SECONDS


def test_a_yaml_string_number_is_coerced():
    loaded = cfg.load_from_unified({"jeeves": {
        "ring": {"seconds": "900"},
        "window": {"lookback_seconds": "30.5"},
    }})
    assert loaded.ring.seconds == 900
    assert loaded.window.lookback_seconds == pytest.approx(30.5)


def test_the_ratified_defaults_are_the_ratified_numbers():
    """CONTRACT PIN. 30-minute ring (ruling 5), 45-second lookback and the
    3s/60s lookahead (design §3). Changing any of these is a design decision
    — update the pin in the same commit."""
    loaded = cfg.load_from_unified({})
    assert loaded.ring.seconds == 1800
    assert loaded.window.lookback_seconds == 45.0
    assert loaded.window.silence_seconds == 3.0
    assert loaded.window.max_lookahead_seconds == 60.0


# ---------------------------------------------------------------------------
# The wake provider
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("provider", ["off", "fake", "openwakeword"])
def test_known_providers_are_carried(provider: str):
    loaded = cfg.load_from_unified({"jeeves": {"wake": {"provider": provider}}})
    assert loaded.wake.provider == provider


def test_an_unknown_provider_falls_back_to_off_LOUDLY():
    """FAIL-CLOSED, and loud because the failure is otherwise invisible: an
    operator who typed 'openwakework' has a device that will never hear
    anything and no other symptom that says so."""
    with structlog.testing.capture_logs() as captured:
        loaded = cfg.load_from_unified({"jeeves": {
            "wake": {"provider": "openwakework"},
        }})
    assert loaded.wake.provider == cfg.WAKE_PROVIDER_OFF
    events = [c for c in captured
              if c.get("event") == "jeeves.config.unknown_wake_provider"]
    assert len(events) == 1
    assert events[0]["configured"] == "openwakework"
    assert events[0]["applied"] == cfg.WAKE_PROVIDER_OFF


def test_the_trials_confusion_set_is_the_shipped_default():
    """Q6 TRIAL. The operator's in-recording probe measured exactly this set
    over a bass line; it is not a guess."""
    loaded = cfg.load_from_unified({})
    assert set(loaded.wake.confusable_terms) == {
        "cheese", "leaves", "jesus", "jeeps", "eaves",
    }


def test_a_configured_confusion_set_replaces_the_default():
    loaded = cfg.load_from_unified({"jeeves": {
        "wake": {"confusable_terms": ["Cheese", "Geez"]},
    }})
    assert loaded.wake.confusable_terms == ["cheese", "geez"]


def test_an_empty_confusion_list_keeps_the_measured_default():
    """An empty list is far more likely to be a config accident than a
    deliberate decision to stop tagging variants."""
    loaded = cfg.load_from_unified({"jeeves": {"wake": {"confusable_terms": []}}})
    assert loaded.wake.confusable_terms
