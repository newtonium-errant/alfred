"""Unit tests for :func:`alfred.audit.agent_slug_for`.

Promoted from ``alfred.telegram.conversation._agent_slug`` so non-talker
attribution writers (audit sweep, capture_batch, calibration, Daily
Sync proposal-confirm) share one slug derivation. Same shape: lowercased
``config.instance.name`` with ``"talker"`` fallback for ``None`` configs
or empty names.

The hardcoding sweep follow-up (``project_hardcoding_followups.md``)
treats this helper as the canonical resolver — items 1-3 all derive
their attribution slug through it.
"""

from __future__ import annotations

from dataclasses import dataclass

from alfred.audit import agent_slug_for


@dataclass
class _FakeInstance:
    name: str | None


@dataclass
class _FakeConfig:
    instance: _FakeInstance | None


def test_agent_slug_for_lowercases_salem():
    cfg = _FakeConfig(instance=_FakeInstance(name="Salem"))
    assert agent_slug_for(cfg) == "salem"


def test_agent_slug_for_lowercases_hypatia():
    cfg = _FakeConfig(instance=_FakeInstance(name="Hypatia"))
    assert agent_slug_for(cfg) == "hypatia"


def test_agent_slug_for_preserves_kalle_hyphen():
    """``KAL-LE`` is a hyphenated instance name — the slug keeps the hyphen
    (the marker_id contract is ``[\\w-]+`` so hyphens are valid; lowercase
    only)."""
    cfg = _FakeConfig(instance=_FakeInstance(name="KAL-LE"))
    assert agent_slug_for(cfg) == "kal-le"


def test_agent_slug_for_strips_whitespace():
    cfg = _FakeConfig(instance=_FakeInstance(name="  Salem  "))
    assert agent_slug_for(cfg) == "salem"


def test_agent_slug_for_none_config_returns_talker():
    """``None`` config falls back to ``"talker"`` — the historical
    default for legacy callers / tests that skip the plumb."""
    assert agent_slug_for(None) == "talker"


def test_agent_slug_for_empty_name_returns_talker():
    cfg = _FakeConfig(instance=_FakeInstance(name=""))
    assert agent_slug_for(cfg) == "talker"


def test_agent_slug_for_missing_instance_returns_talker():
    cfg = _FakeConfig(instance=None)
    assert agent_slug_for(cfg) == "talker"


def test_agent_slug_for_object_without_instance_attr():
    """Duck-typing: any object lacking ``.instance`` falls back to
    ``"talker"``. Lets the helper accept arbitrary configs (raw dicts
    wrapped in dataclasses, mocks) without a TypeError."""
    class _Empty:
        pass

    assert agent_slug_for(_Empty()) == "talker"
