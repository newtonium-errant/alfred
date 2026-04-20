"""Tests for the bundled vault-instructor SKILL.md file.

Commit 5 ships the real SKILL. These tests verify:
1. The file is at the expected bundled path and loadable.
2. Instance templating substitutes both placeholders.
3. The executor's ``_load_skill`` returns the templated content.
4. The SKILL contains the JSON-summary contract the executor parses.
"""

from __future__ import annotations

from pathlib import Path

from alfred._data import get_skills_dir
from alfred.instructor.config import (
    AnthropicConfig,
    InstanceConfig,
    InstructorConfig,
)
from alfred.instructor.executor import _load_skill


def test_skill_file_exists_in_bundled_path() -> None:
    skills = get_skills_dir()
    skill_path = skills / "vault-instructor" / "SKILL.md"
    assert skill_path.is_file(), f"Missing skill file at {skill_path}"


def test_skill_has_templating_placeholders() -> None:
    """Confirms the SKILL carries both placeholder tokens.

    If a future edit accidentally removes the templating hooks, the
    instance-override feature silently breaks. This catches that.
    """
    skills = get_skills_dir()
    skill_path = skills / "vault-instructor" / "SKILL.md"
    content = skill_path.read_text(encoding="utf-8")
    assert "{{instance_name}}" in content
    assert "{{instance_canonical}}" in content


def test_skill_has_json_summary_contract() -> None:
    """The SKILL must explicitly mention the JSON summary shape the
    executor's ``_parse_agent_summary`` expects.

    If the SKILL stops mentioning the contract, we drift into
    fallback-summary territory and the ``alfred_instructions_last[].result``
    field stops carrying the status classification.
    """
    skills = get_skills_dir()
    skill_path = skills / "vault-instructor" / "SKILL.md"
    content = skill_path.read_text(encoding="utf-8")
    assert '"status":' in content
    assert '"summary":' in content
    # Status values the executor validates against.
    for status in ("done", "ambiguous", "refused"):
        assert status in content


def test_load_skill_applies_instance_templating(tmp_path: Path) -> None:
    """_load_skill replaces both placeholder tokens when config is passed."""
    # Build a throwaway skills_dir with just the placeholder substrings.
    skills = tmp_path / "skills"
    (skills / "vault-instructor").mkdir(parents=True)
    (skills / "vault-instructor" / "SKILL.md").write_text(
        "I am {{instance_name}} / formally {{instance_canonical}}.\n",
        encoding="utf-8",
    )

    config = InstructorConfig(
        anthropic=AnthropicConfig(api_key="DUMMY_ANTHROPIC_TEST_KEY"),
        instance=InstanceConfig(name="Salem", canonical="S.A.L.E.M."),
    )
    prompt = _load_skill(skills, config)
    assert "{{instance_name}}" not in prompt
    assert "{{instance_canonical}}" not in prompt
    assert "Salem" in prompt
    assert "S.A.L.E.M." in prompt


def test_load_skill_without_config_leaves_templates(tmp_path: Path) -> None:
    """Backwards-compat: calling _load_skill without a config keeps the
    tokens literal. Matches the c4 test-fixture behaviour."""
    skills = tmp_path / "skills"
    (skills / "vault-instructor").mkdir(parents=True)
    (skills / "vault-instructor" / "SKILL.md").write_text(
        "{{instance_name}}\n",
        encoding="utf-8",
    )
    assert _load_skill(skills) == "{{instance_name}}\n"


def test_load_skill_uses_default_alfred_identity() -> None:
    """When ``instance`` is the default (``Alfred`` / ``Alfred``), the
    templated output carries ``Alfred`` in both slots."""
    skills = get_skills_dir()
    config = InstructorConfig(
        anthropic=AnthropicConfig(api_key="DUMMY_ANTHROPIC_TEST_KEY"),
    )
    prompt = _load_skill(skills, config)
    # Placeholders gone, "Alfred" appears in the header line.
    assert "{{instance_name}}" not in prompt
    assert "{{instance_canonical}}" not in prompt
    assert "Alfred" in prompt
