"""Pins for the promoted root Anthropic-SDK helper (``alfred._anthropic_compat``).

These are the FIRST tests of the real in-process SDK call path: the email callers'
``_default_llm_caller`` was previously exercised only via an injected fake ``llm_caller``, so the
construct-client-and-call block itself was untested before the centralization. Covers: the three
fail-silent modes + their per-prefix events, the concatenated-text success, the LOAD-BEARING
Opus-``temperature`` quirk drop, and — per the wrapper-delegation gate condition — that the REAL
``email_classifier`` / ``email_filing`` wrappers thread their OWN prefix + config through the shared
helper. Regression pins run UNCONDITIONALLY.
"""
from __future__ import annotations

import importlib
import sys
import types

import pytest
import structlog

from alfred._anthropic_compat import call_anthropic_text, messages_create_kwargs


# --- a fake ``anthropic`` module injected via sys.modules -------------------

def _make_fake_anthropic(recorder, *, blocks=None, raises=None):
    """A stand-in ``anthropic`` module whose ``Anthropic(api_key=...).messages.create(**kwargs)``
    records the kwargs into ``recorder``, then raises ``raises`` or returns a response with ``blocks``."""
    default_blocks = blocks if blocks is not None else [types.SimpleNamespace(text="ok")]

    class _Messages:
        def create(self, **kwargs):
            recorder["kwargs"] = kwargs
            if raises is not None:
                raise raises
            return types.SimpleNamespace(content=default_blocks)

    class Anthropic:  # noqa: N801 — mirrors the SDK's real class name
        def __init__(self, *, api_key):
            recorder["api_key"] = api_key
            self.messages = _Messages()

    mod = types.ModuleType("anthropic")
    mod.Anthropic = Anthropic
    return mod


def _events(cap, name):
    return [e for e in cap if e.get("event") == name]


# --- call_anthropic_text: the shared helper directly -----------------------

def test_missing_sdk_returns_empty_and_logs(monkeypatch):
    monkeypatch.setitem(sys.modules, "anthropic", None)  # `import anthropic` → ImportError
    log = structlog.get_logger("test.compat")
    with structlog.testing.capture_logs() as cap:
        out = call_anthropic_text(api_key="k", model="claude-sonnet-4-6", max_tokens=8,
                                  system="s", user="u", log=log, log_prefix="email_classifier")
    assert out == ""
    assert len(_events(cap, "email_classifier.anthropic_not_installed")) == 1


@pytest.mark.parametrize("api_key", ["", "${ANTHROPIC_API_KEY}"])
def test_missing_or_placeholder_key_returns_empty_and_logs(monkeypatch, api_key):
    rec: dict = {}
    monkeypatch.setitem(sys.modules, "anthropic", _make_fake_anthropic(rec))
    log = structlog.get_logger("test.compat")
    with structlog.testing.capture_logs() as cap:
        out = call_anthropic_text(api_key=api_key, model="claude-sonnet-4-6", max_tokens=8,
                                  system="s", user="u", log=log, log_prefix="email_filing")
    assert out == ""
    assert len(_events(cap, "email_filing.no_api_key")) == 1
    assert "kwargs" not in rec  # never called create() — gated before the SDK


def test_success_concatenates_text_blocks_ignoring_non_text(monkeypatch):
    rec: dict = {}
    blocks = [types.SimpleNamespace(text="Hello "),
              types.SimpleNamespace(),                  # a non-text block (no .text) — ignored
              types.SimpleNamespace(text="world")]
    monkeypatch.setitem(sys.modules, "anthropic", _make_fake_anthropic(rec, blocks=blocks))
    out = call_anthropic_text(api_key="sk-real", model="claude-sonnet-4-6", max_tokens=8,
                              system="sys", user="usr", log=structlog.get_logger("t"),
                              log_prefix="email_classifier")
    assert out == "Hello world"
    assert rec["api_key"] == "sk-real"
    # the create() call carried exactly the built kwargs (no temperature by default)
    assert rec["kwargs"]["model"] == "claude-sonnet-4-6"
    assert rec["kwargs"]["max_tokens"] == 8
    assert rec["kwargs"]["system"] == "sys"
    assert rec["kwargs"]["messages"] == [{"role": "user", "content": "usr"}]
    assert "temperature" not in rec["kwargs"]


def test_sdk_error_returns_empty_and_logs(monkeypatch):
    rec: dict = {}
    monkeypatch.setitem(sys.modules, "anthropic",
                        _make_fake_anthropic(rec, raises=RuntimeError("boom")))
    log = structlog.get_logger("test.compat")
    with structlog.testing.capture_logs() as cap:
        out = call_anthropic_text(api_key="sk-real", model="claude-sonnet-4-6", max_tokens=8,
                                  system="s", user="u", log=log, log_prefix="email_filing")
    assert out == ""
    ev = _events(cap, "email_filing.llm_call_failed")
    assert len(ev) == 1
    assert ev[0]["error"] == "boom" and ev[0]["model"] == "claude-sonnet-4-6"


# --- the LOAD-BEARING quirk pin: Opus drops temperature, others keep it -----

def test_opus_drops_temperature(monkeypatch):
    rec: dict = {}
    monkeypatch.setitem(sys.modules, "anthropic", _make_fake_anthropic(rec))
    call_anthropic_text(api_key="sk-real", model="claude-opus-4-8", max_tokens=8,
                        system="s", user="u", log=structlog.get_logger("t"),
                        log_prefix="x", temperature=0.3)
    assert "temperature" not in rec["kwargs"]  # 400-quirk: dropped for the opus family


def test_non_opus_keeps_temperature(monkeypatch):
    rec: dict = {}
    monkeypatch.setitem(sys.modules, "anthropic", _make_fake_anthropic(rec))
    call_anthropic_text(api_key="sk-real", model="claude-sonnet-4-6", max_tokens=8,
                        system="s", user="u", log=structlog.get_logger("t"),
                        log_prefix="x", temperature=0.3)
    assert rec["kwargs"]["temperature"] == 0.3


def test_messages_create_kwargs_root_is_the_quirk_gate():
    # the root module is the canonical home for the quirk gate (telegram re-exports it).
    assert messages_create_kwargs(model="claude-opus-4-8", temperature=0.5) == {"model": "claude-opus-4-8"}
    assert messages_create_kwargs(model="claude-sonnet-4-6", temperature=0.5) == {
        "model": "claude-sonnet-4-6", "temperature": 0.5}


# --- wrapper delegation: the REAL email callers thread prefix + config ------

@pytest.mark.parametrize(
    "module_path, prefix",
    [("alfred.email_classifier.classifier", "email_classifier"),
     ("alfred.email_filing.classifier", "email_filing")],
)
def test_wrapper_threads_own_prefix_and_config(monkeypatch, module_path, prefix):
    # Drives the REAL wrapper (not call_anthropic_text directly) — confirms it unpacks
    # config.anthropic.{api_key,model,max_tokens} and threads its OWN prefix.
    mod = importlib.import_module(module_path)
    cfg = types.SimpleNamespace(anthropic=types.SimpleNamespace(
        api_key="sk-real", model="claude-sonnet-4-6", max_tokens=42))

    # success — returns concatenated text; create() gets the config's model/max_tokens
    rec: dict = {}
    monkeypatch.setitem(sys.modules, "anthropic",
                        _make_fake_anthropic(rec, blocks=[types.SimpleNamespace(text="cat")]))
    assert mod._default_llm_caller("sys", "usr", cfg) == "cat"
    assert rec["kwargs"]["model"] == "claude-sonnet-4-6"
    assert rec["kwargs"]["max_tokens"] == 42

    # failure — the wrapper emits ITS OWN prefix, never the sibling's
    bad_cfg = types.SimpleNamespace(anthropic=types.SimpleNamespace(
        api_key="", model="claude-sonnet-4-6", max_tokens=42))
    with structlog.testing.capture_logs() as cap:
        assert mod._default_llm_caller("sys", "usr", bad_cfg) == ""
    assert len(_events(cap, f"{prefix}.no_api_key")) == 1
    sibling = "email_filing" if prefix == "email_classifier" else "email_classifier"
    assert _events(cap, f"{sibling}.no_api_key") == []
