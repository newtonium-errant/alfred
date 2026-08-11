"""Production wiring for the Jeeves capture route (task #81, stage 2).

THE TRAP THIS FILE EXISTS FOR. A default-``None`` gate parameter tested only
by direct invocation is a standing hazard: the tests thread it, production
never does, every pin stays green, and the feature is accepted-then-ignored
in the field. The route tests above call ``register_jeeves_routes``
themselves — which proves the registrar works and proves nothing about
whether anything CALLS it.

So these pins walk the real call chain: the talker daemon's
``wire_transport_app`` call, ``wire_transport_app``'s registrar call, and the
transport config that supplies the flag. All three must agree or the route
never mounts on a real instance.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from alfred.telegram import daemon as telegram_daemon
from alfred.transport import server as transport_server
from alfred.transport.config import (
    DEFAULT_JEEVES_MAX_TRANSCRIPT_CHARS,
    JeevesRouteConfig,
    TransportConfig,
    load_from_unified,
)


def _wire_call_kwargs() -> set[str]:
    """Keyword names the daemon passes to ``wire_transport_app``.

    An AST walk of the daemon source rather than a grep: the call spans
    forty-odd lines and a substring search would match a comment as happily
    as an argument.
    """
    tree = ast.parse(Path(telegram_daemon.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "wire_transport_app"
        ):
            return {kw.arg for kw in node.keywords if kw.arg}
    raise AssertionError("the daemon no longer calls wire_transport_app")


# ---------------------------------------------------------------------------
# The production call site
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kwarg", [
    "jeeves_enabled", "jeeves_config", "jeeves_raw_config",
])
def test_the_daemon_threads_every_jeeves_parameter(kwarg: str):
    """Without this, ``transport.jeeves.enabled: true`` in an operator's
    config would be read, typed, and then never reach the registrar — the
    route silently absent on a correctly-configured instance."""
    assert kwarg in _wire_call_kwargs()


def test_wire_transport_app_accepts_those_exact_names():
    """The other half of the same contract: a rename on either side breaks
    the call, and a keyword typo would be a TypeError only at daemon
    startup — on the box, at 3am."""
    params = set(inspect.signature(
        transport_server.wire_transport_app).parameters)
    assert {"jeeves_enabled", "jeeves_config", "jeeves_raw_config"} <= params


def test_the_daemon_passes_the_UNIFIED_config_not_the_transport_block():
    """LOAD-BEARING. The route's mode gate reads the TOP-LEVEL ``jeeves:``
    block. Handing it ``transport.jeeves`` would leave the gate reading an
    empty config — which fails CLOSED, so a correctly-configured live
    instance would refuse every real capture with no obvious cause.

    ``raw`` is the same variable the feed wiring already passes, which is
    what makes it checkable here rather than only in production.
    """
    tree = ast.parse(Path(telegram_daemon.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "wire_transport_app"
        ):
            by_name = {kw.arg: kw.value for kw in node.keywords if kw.arg}
            raw_arg = by_name["jeeves_raw_config"]
            assert isinstance(raw_arg, ast.Name), (
                "jeeves_raw_config must be the unified config dict"
            )
            assert raw_arg.id == "raw"
            # ...and it is NOT the transport sub-block, which is what an
            # obvious-looking edit would reach for.
            assert not isinstance(by_name["jeeves_raw_config"], ast.Attribute)
            return
    raise AssertionError("the daemon no longer calls wire_transport_app")


def test_wire_transport_app_really_calls_the_registrar():
    """A parameter accepted and dropped is the same as a parameter never
    threaded."""
    source = inspect.getsource(transport_server.wire_transport_app)
    assert "register_jeeves_routes(" in source
    assert "jeeves_raw_config=jeeves_raw_config" in source


def test_both_branches_call_the_registrar():
    """Disabled still calls it, so the "ran, did not mount" signal fires and
    an operator audit can tell "off" from "wiring silently skipped"."""
    source = inspect.getsource(transport_server.wire_transport_app)
    assert source.count("register_jeeves_routes(") == 2


# ---------------------------------------------------------------------------
# The config that feeds it
# ---------------------------------------------------------------------------


def test_the_transport_config_carries_a_jeeves_block():
    assert isinstance(TransportConfig().jeeves, JeevesRouteConfig)


def test_the_route_is_OFF_by_default():
    """Opt-in inertness: an instance with no garage microphone mounts
    nothing and its transport server is byte-unchanged."""
    assert TransportConfig().jeeves.enabled is False
    assert load_from_unified({"transport": {}}).jeeves.enabled is False


def test_an_enabled_block_is_loaded_from_yaml():
    loaded = load_from_unified({"transport": {"jeeves": {
        "enabled": True,
        "max_transcript_chars": 4096,
        "types": ["note"],
    }}})
    assert loaded.jeeves.enabled is True
    assert loaded.jeeves.max_transcript_chars == 4096
    assert loaded.jeeves.types == ["note"]


@pytest.mark.parametrize("bad", ["x", None, 0, -5, {}])
def test_a_nonsense_cap_keeps_the_default(bad):
    """A zero or negative cap would refuse every capture, and a cap of 1 is
    not meaningfully less broken — the symptom in the garage is "Jeeves
    stopped working" either way. Falling back to a WORKING default is the
    fail-safe direction for a device the operator cannot see."""
    loaded = load_from_unified({"transport": {"jeeves": {
        "enabled": True, "max_transcript_chars": bad,
    }}})
    assert loaded.jeeves.max_transcript_chars == DEFAULT_JEEVES_MAX_TRANSCRIPT_CHARS


def test_a_non_positive_cap_is_reported_not_silently_replaced():
    """An operator whose typo was overridden should be able to find out."""
    import structlog

    with structlog.testing.capture_logs() as captured:
        load_from_unified({"transport": {"jeeves": {
            "enabled": True, "max_transcript_chars": 0,
        }}})
    events = [c for c in captured
              if c.get("event") == "transport.jeeves.max_transcript_chars_invalid"]
    assert len(events) == 1
    assert events[0]["applied"] == DEFAULT_JEEVES_MAX_TRANSCRIPT_CHARS


def test_a_non_dict_block_is_tolerated():
    loaded = load_from_unified({"transport": {"jeeves": "yes please"}})
    assert loaded.jeeves.enabled is False


def test_junk_type_entries_are_dropped():
    loaded = load_from_unified({"transport": {"jeeves": {
        "enabled": True, "types": ["note", "", None, 42, "  "],
    }}})
    assert loaded.jeeves.types == ["note"]


# ---------------------------------------------------------------------------
# The example config documents both halves
# ---------------------------------------------------------------------------


def test_the_example_config_documents_the_receiving_side():
    """An operator enabling this needs to know a DEDICATED peer token is
    required — the route pins the peer NAME, so reusing another token fails
    with a 401 that says nothing about tokens."""
    example = (
        Path(__file__).resolve().parents[1] / "config.yaml.example"
    ).read_text(encoding="utf-8")
    assert "/vault/jeeves/capture" in example
    assert "ALFRED_JEEVES_TOKEN" in example
    assert "max_transcript_chars" in example


def test_the_example_config_documents_the_device_side():
    example = (
        Path(__file__).resolve().parents[1] / "config.yaml.example"
    ).read_text(encoding="utf-8")
    assert "jeeves:" in example
    assert "openwakeword" in example
    assert "route_target" in example
    assert "miss_audio_dir" in example
