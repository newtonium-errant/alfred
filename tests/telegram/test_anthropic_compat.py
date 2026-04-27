"""Tests for ``_anthropic_compat.messages_create_kwargs``.

The helper centralizes the Opus-4.x ``temperature`` quirk so every site
that builds ``client.messages.create`` kwargs gets the same behaviour.
These unit tests pin the rule; the per-call-site tests in
``test_temperature_strip_call_sites.py`` confirm callers route through here.
"""
from __future__ import annotations

from alfred.telegram._anthropic_compat import messages_create_kwargs


def test_opus_4_7_drops_temperature() -> None:
    out = messages_create_kwargs(
        model="claude-opus-4-7", max_tokens=10, temperature=0.7
    )
    assert "temperature" not in out
    assert out == {"model": "claude-opus-4-7", "max_tokens": 10}


def test_opus_4_5_drops_temperature() -> None:
    out = messages_create_kwargs(
        model="claude-opus-4-5", max_tokens=10, temperature=0.7
    )
    assert "temperature" not in out


def test_sonnet_keeps_temperature() -> None:
    out = messages_create_kwargs(
        model="claude-sonnet-4-6", max_tokens=10, temperature=0.7
    )
    assert out["temperature"] == 0.7


def test_haiku_keeps_temperature() -> None:
    out = messages_create_kwargs(
        model="claude-haiku-4-5-20251001", max_tokens=10, temperature=0.7
    )
    assert out["temperature"] == 0.7


def test_temperature_none_is_omitted_for_every_family() -> None:
    for model in (
        "claude-opus-4-7",
        "claude-sonnet-4-6",
        "claude-haiku-4-5",
    ):
        out = messages_create_kwargs(model=model, max_tokens=10, temperature=None)
        assert "temperature" not in out, model


def test_omitted_temperature_is_omitted_for_every_family() -> None:
    for model in (
        "claude-opus-4-7",
        "claude-sonnet-4-6",
        "claude-haiku-4-5",
    ):
        out = messages_create_kwargs(model=model, max_tokens=10)
        assert "temperature" not in out, model


def test_passes_through_arbitrary_kwargs() -> None:
    out = messages_create_kwargs(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        temperature=0.2,
        system=[{"type": "text", "text": "sys"}],
        messages=[{"role": "user", "content": "hi"}],
        tools=[{"name": "t"}],
        tool_choice={"type": "auto"},
    )
    assert out["system"] == [{"type": "text", "text": "sys"}]
    assert out["messages"] == [{"role": "user", "content": "hi"}]
    assert out["tools"] == [{"name": "t"}]
    assert out["tool_choice"] == {"type": "auto"}
    assert out["temperature"] == 0.2


def test_passes_through_arbitrary_kwargs_on_opus() -> None:
    # Same payload, opus model — only temperature should be dropped.
    out = messages_create_kwargs(
        model="claude-opus-4-7",
        max_tokens=2048,
        temperature=0.2,
        system=[{"type": "text", "text": "sys"}],
        messages=[{"role": "user", "content": "hi"}],
        tools=[{"name": "t"}],
    )
    assert "temperature" not in out
    assert out["system"] == [{"type": "text", "text": "sys"}]
    assert out["tools"] == [{"name": "t"}]
