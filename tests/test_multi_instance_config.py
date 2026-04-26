"""Tests for the Stage 3.5 multi-instance config plumbing (c1).

Covers the three user-visible contracts added in commit 1:
  - ``daemon.pid_path`` overrides the legacy ``logging.dir`` / ``alfred.pid``
    path (so KAL-LE doesn't collide with Salem's pid file)
  - ``telegram.instance.skill_bundle`` selects which SKILL bundle the
    talker loads
  - ``telegram.instance.tool_set`` is round-tripped into config (actual
    tool-set wiring lands in c5/c6; here we just prove config carries
    the field)
"""

from __future__ import annotations

from pathlib import Path

from alfred.cli import _resolve_pid_path
from alfred.telegram.config import load_from_unified
from alfred.telegram.daemon import _load_system_prompt


def test_pid_path_defaults_to_logging_dir():
    """Absent ``daemon.pid_path`` → ``<logging.dir>/alfred.pid`` (legacy)."""
    raw = {"logging": {"dir": "./data"}}
    assert _resolve_pid_path(raw) == Path("./data/alfred.pid")


def test_pid_path_respects_daemon_override():
    """``daemon.pid_path`` wins when set — this is how KAL-LE avoids collision."""
    raw = {
        "daemon": {"pid_path": "/home/andrew/.alfred/kalle/data/alfred.pid"},
        "logging": {"dir": "./data"},  # Should be ignored.
    }
    assert _resolve_pid_path(raw) == Path(
        "/home/andrew/.alfred/kalle/data/alfred.pid"
    )


def test_pid_path_default_when_no_logging_section():
    """Totally empty config falls back to ``./data/alfred.pid``."""
    assert _resolve_pid_path({}) == Path("./data/alfred.pid")


def test_instance_skill_bundle_default_is_vault_talker():
    """A minimal but valid instance block falls back to ``vault-talker``.

    ``InstanceConfig.name`` is required as of 2026-04-26 (no "Alfred"
    default — see ``feedback_hardcoding_and_alfred_naming.md``), so a
    bare ``{"telegram": {}}`` raises at load time. The skill_bundle
    default check therefore needs an explicit minimum-valid instance
    block.
    """
    raw = {"telegram": {"instance": {"name": "Salem"}}}
    config = load_from_unified(raw)
    assert config.instance.skill_bundle == "vault-talker"


def test_instance_skill_bundle_override_is_respected():
    """KAL-LE overrides to ``vault-kalle``."""
    raw = {
        "telegram": {
            "instance": {
                "name": "KAL-LE",
                "canonical": "K.A.L.L.E.",
                "skill_bundle": "vault-kalle",
                "tool_set": "kalle",
            },
        },
    }
    config = load_from_unified(raw)
    assert config.instance.skill_bundle == "vault-kalle"
    assert config.instance.tool_set == "kalle"
    assert config.instance.name == "KAL-LE"
    assert config.instance.canonical == "K.A.L.L.E."


def test_instance_tool_set_default_is_talker():
    raw = {"telegram": {"instance": {"name": "Salem"}}}
    config = load_from_unified(raw)
    assert config.instance.tool_set == "talker"


def test_load_from_unified_requires_instance_name() -> None:
    """A config without ``instance.name`` raises ``TypeError`` at load time.

    Pinned 2026-04-26 by removing the previous ``"Alfred"`` default —
    "Alfred" is the project name, never an instance name (see
    ``feedback_hardcoding_and_alfred_naming.md``). A silent default
    produced wrong-attribution prose ("Alfred's earlier message" on a
    Salem-installed bot) and silent peer-protocol misconfiguration.
    Required-field semantics surface the misconfiguration immediately.
    """
    import pytest

    # Empty telegram block → no instance dict → InstanceConfig() called
    # with no kwargs → required ``name`` field raises TypeError.
    with pytest.raises(TypeError):
        load_from_unified({"telegram": {}})

    # Empty instance block → same path.
    with pytest.raises(TypeError):
        load_from_unified({"telegram": {"instance": {}}})

    # Instance with canonical but no name → still missing the required
    # field.
    with pytest.raises(TypeError):
        load_from_unified({"telegram": {"instance": {"canonical": "X"}}})

    # Instance with name → loads cleanly.
    cfg = load_from_unified({"telegram": {"instance": {"name": "Salem"}}})
    assert cfg.instance.name == "Salem"


def test_load_system_prompt_reads_from_skill_bundle(tmp_path: Path):
    """_load_system_prompt resolves ``<skills_dir>/<skill_bundle>/SKILL.md``."""
    # Build two bundles; the second one is what KAL-LE would use.
    talker_dir = tmp_path / "vault-talker"
    talker_dir.mkdir()
    (talker_dir / "SKILL.md").write_text("TALKER PROMPT", encoding="utf-8")

    kalle_dir = tmp_path / "vault-kalle"
    kalle_dir.mkdir()
    (kalle_dir / "SKILL.md").write_text("KALLE PROMPT", encoding="utf-8")

    # Default bundle (Salem).
    default_prompt = _load_system_prompt(tmp_path)
    assert default_prompt == "TALKER PROMPT"

    # Explicit bundle (KAL-LE).
    kalle_prompt = _load_system_prompt(tmp_path, skill_bundle="vault-kalle")
    assert kalle_prompt == "KALLE PROMPT"


def test_load_system_prompt_missing_bundle_returns_empty(tmp_path: Path):
    """Nonexistent bundle → empty string + warning; daemon still boots."""
    prompt = _load_system_prompt(tmp_path, skill_bundle="vault-nonexistent")
    assert prompt == ""
