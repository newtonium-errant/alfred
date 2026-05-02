"""Tests for c8: per-instance config-path threading on queue-path helpers.

The canonical-proposals queue-path helpers in
``daily_sync.canonical_proposals_section`` and ``daily_sync.reply_dispatch``
both used to call ``alfred.transport.config.load_config()`` with no path
argument, defaulting to ``"config.yaml"``. On a per-instance daily_sync
daemon (Hypatia, KAL-LE) this silently re-read Salem's config and looked
up the wrong proposals JSONL.

c8 threads ``DailySyncConfig.config_path`` (populated by
``_load_unified_config``'s synthetic ``_config_path`` key) through to
``load_config(path)``. Both helpers fall back to ``"config.yaml"`` when
``config_path`` is unset (backward compat for test fixtures that don't
go through the CLI).

Mirrors the talker-side regression coverage shipped in commit 420364b.
"""

from __future__ import annotations

from typing import Any

import pytest

from alfred.daily_sync.config import DailySyncConfig


# --- canonical_proposals_section._proposals_queue_path ----------------


def test_proposals_queue_path_uses_config_path_from_daily_sync_config(
    monkeypatch: pytest.MonkeyPatch,
):
    """``_proposals_queue_path`` must load transport config from
    ``DailySyncConfig.config_path``, NOT default ``"config.yaml"``.
    Fails closed if the helper reverts to defaulting."""
    from alfred.daily_sync import canonical_proposals_section as cps

    captured_paths: list[Any] = []

    class _Canonical:
        proposals_path = "/tmp/hypatia/proposals.jsonl"

    class _StubTransportConfig:
        canonical = _Canonical()

    def _stub_load(path: Any = "config.yaml") -> _StubTransportConfig:
        captured_paths.append(path)
        return _StubTransportConfig()

    monkeypatch.setattr("alfred.transport.config.load_config", _stub_load)

    fake_config_path = "/etc/alfred/config.hypatia.yaml"
    config = DailySyncConfig(enabled=True)
    config.config_path = fake_config_path

    result = cps._proposals_queue_path(config)
    assert result == "/tmp/hypatia/proposals.jsonl"
    assert captured_paths == [fake_config_path], (
        f"queue-path helper must thread DailySyncConfig.config_path "
        f"{fake_config_path!r} through to transport load_config; "
        f"the c8 fix is broken if it reverts to defaulting. "
        f"Captured: {captured_paths!r}"
    )


def test_proposals_queue_path_falls_back_when_config_path_unset(
    monkeypatch: pytest.MonkeyPatch,
):
    """If ``DailySyncConfig.config_path`` is None (e.g. test fixtures
    that build a config directly), the helper falls back to
    ``"config.yaml"`` for backward compat."""
    from alfred.daily_sync import canonical_proposals_section as cps

    captured_paths: list[Any] = []

    class _Canonical:
        proposals_path = "/tmp/salem/proposals.jsonl"

    class _StubTransportConfig:
        canonical = _Canonical()

    def _stub_load(path: Any = "config.yaml") -> _StubTransportConfig:
        captured_paths.append(path)
        return _StubTransportConfig()

    monkeypatch.setattr("alfred.transport.config.load_config", _stub_load)

    config = DailySyncConfig(enabled=True)
    assert config.config_path is None

    result = cps._proposals_queue_path(config)
    assert result == "/tmp/salem/proposals.jsonl"
    assert captured_paths == ["config.yaml"]


# --- reply_dispatch._canonical_proposals_queue_path -------------------


def test_reply_dispatch_queue_path_uses_config_path_from_daily_sync_config(
    monkeypatch: pytest.MonkeyPatch,
):
    """The reply_dispatch helper mirrors the canonical_proposals_section
    one — same threading contract, same regression risk."""
    from alfred.daily_sync import reply_dispatch as rd

    captured_paths: list[Any] = []

    class _Canonical:
        proposals_path = "/tmp/kalle/proposals.jsonl"

    class _StubTransportConfig:
        canonical = _Canonical()

    def _stub_load(path: Any = "config.yaml") -> _StubTransportConfig:
        captured_paths.append(path)
        return _StubTransportConfig()

    monkeypatch.setattr("alfred.transport.config.load_config", _stub_load)

    fake_config_path = "/etc/alfred/config.kalle.yaml"
    config = DailySyncConfig(enabled=True)
    config.config_path = fake_config_path

    result = rd._canonical_proposals_queue_path(config)
    assert result == "/tmp/kalle/proposals.jsonl"
    assert captured_paths == [fake_config_path], (
        f"reply_dispatch queue-path helper must thread "
        f"DailySyncConfig.config_path {fake_config_path!r} through to "
        f"transport load_config; the c8 fix is broken if it reverts to "
        f"defaulting. Captured: {captured_paths!r}"
    )


def test_reply_dispatch_queue_path_falls_back_when_config_path_unset(
    monkeypatch: pytest.MonkeyPatch,
):
    """``config.config_path is None`` → falls back to ``"config.yaml"``."""
    from alfred.daily_sync import reply_dispatch as rd

    captured_paths: list[Any] = []

    class _Canonical:
        proposals_path = "/tmp/salem/proposals.jsonl"

    class _StubTransportConfig:
        canonical = _Canonical()

    def _stub_load(path: Any = "config.yaml") -> _StubTransportConfig:
        captured_paths.append(path)
        return _StubTransportConfig()

    monkeypatch.setattr("alfred.transport.config.load_config", _stub_load)

    config = DailySyncConfig(enabled=True)
    assert config.config_path is None

    result = rd._canonical_proposals_queue_path(config)
    assert result == "/tmp/salem/proposals.jsonl"
    assert captured_paths == ["config.yaml"]


def test_reply_dispatch_queue_path_falls_back_when_config_arg_omitted(
    monkeypatch: pytest.MonkeyPatch,
):
    """Pre-c8 callers that omit the config arg entirely (or pass None)
    keep working — backward compat for the call-site signature change."""
    from alfred.daily_sync import reply_dispatch as rd

    captured_paths: list[Any] = []

    class _Canonical:
        proposals_path = "/tmp/salem/proposals.jsonl"

    class _StubTransportConfig:
        canonical = _Canonical()

    def _stub_load(path: Any = "config.yaml") -> _StubTransportConfig:
        captured_paths.append(path)
        return _StubTransportConfig()

    monkeypatch.setattr("alfred.transport.config.load_config", _stub_load)

    # Zero-arg call
    result = rd._canonical_proposals_queue_path()
    assert result == "/tmp/salem/proposals.jsonl"

    # Explicit None
    result = rd._canonical_proposals_queue_path(None)
    assert result == "/tmp/salem/proposals.jsonl"

    assert captured_paths == ["config.yaml", "config.yaml"]
