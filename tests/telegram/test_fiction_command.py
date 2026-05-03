"""Tests for Hypatia's ``/fiction <title>`` slash command (Phase 2.5).

Pairs with the SKILL revision in ``src/alfred/_bundled/skills/
vault-talker/SKILL.md`` (prompt-tuner's lane). Both paths produce
the same on-disk shape — these tests pin the shape so the SKILL's
natural-language scaffolding doesn't drift away from the slash
command's deterministic scaffolding.

Coverage:
  * Slug derivation (basic + edge cases: punctuation / unicode /
    all-caps / leading numbers / whitespace / empty / overlong)
  * Directory + all 5 files + characters/.gitkeep created with
    correct frontmatter
  * continuity.md contains wikilinks pointing into the right
    siblings
  * Idempotency: second invocation with same title doesn't
    overwrite, returns informative result
  * Config gate: ``/fiction`` not registered as a CommandHandler
    when knob is False (or block absent)
  * Config gate: ``/fiction`` IS registered when knob is True
  * .gitkeep file exists and is empty (signals "intentional empty
    dir" to git)
"""

from __future__ import annotations

import tempfile
from datetime import date
from pathlib import Path

import frontmatter
import pytest

from alfred.telegram import bot, fiction
from alfred.telegram.config import (
    AnthropicConfig,
    FictionConfig,
    InstanceConfig,
    LoggingConfig,
    SessionConfig,
    STTConfig,
    TalkerConfig,
    VaultConfig,
)


# ---------------------------------------------------------------------------
# Slug derivation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title, expected",
    [
        # Basic
        ("The Glass Forest", "the-glass-forest"),
        ("Storm's End", "storms-end"),  # apostrophe dropped
        ("simple", "simple"),
        # Whitespace
        ("  multiple   spaces  ", "multiple-spaces"),
        ("\t\nleading-trailing\t\n", "leading-trailing"),
        # Punctuation
        ("Hello, World!", "hello-world"),
        ("50/50", "5050"),  # slash dropped (not whitespace, not in keep-set)
        ("Title: Subtitle", "title-subtitle"),
        # Numbers + leading numbers
        ("1984", "1984"),
        ("3 Body Problem", "3-body-problem"),
        # Case
        ("ALL CAPS TITLE", "all-caps-title"),
        ("MixedCase", "mixedcase"),
        # Unicode — NFKD-normalized + combining marks stripped, base
        # ASCII letters survive. The original ship lost the leading
        # letter of "über" / "café" wholesale; fixed in Phase 2.5
        # follow-up after the SKILL revision surfaced this as a real
        # parity gap (Hypatia might propose a fiction project titled
        # "Café Society" — slug should be "cafe-society", not
        # "caf-society").
        ("über", "uber"),
        ("café", "cafe"),
        ("Naïve résumé", "naive-resume"),
        ("São Paulo", "sao-paulo"),
        # Edge: empty / all-punctuation
        ("", "untitled-fiction"),
        ("   ", "untitled-fiction"),
        ("!!!", "untitled-fiction"),
        ("---", "untitled-fiction"),
    ],
)
def test_slug_from_title_cases(title, expected):
    assert fiction.slug_from_title(title) == expected


def test_slug_from_title_handles_overlong_input():
    """80-char cap with last-hyphen-snap to avoid mid-word truncation."""
    title = "a-very-long-title-that-keeps-going-and-going-with-many-many-words-for-testing-truncation"
    slug = fiction.slug_from_title(title)
    assert len(slug) <= 80
    # Should land on a word boundary, not mid-word.
    assert not slug.endswith("-")
    # Should preserve at least the first half.
    assert slug.startswith("a-very-long-title-that-keeps-going")


def test_slug_from_title_non_string_returns_default():
    """Defensive: non-string input falls through to default rather than
    crashing on attribute access."""
    assert fiction.slug_from_title(None) == "untitled-fiction"  # type: ignore[arg-type]
    assert fiction.slug_from_title(42) == "untitled-fiction"  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Scaffolding — created path
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_today() -> str:
    return "2026-05-03"


@pytest.fixture
def vault_root(tmp_path: Path) -> Path:
    """Hypatia-shaped vault root: just the directory (the scaffolder
    creates draft/fiction/<slug>/ on demand)."""
    return tmp_path


def test_scaffold_creates_directory_and_all_files(vault_root, fake_today):
    result = fiction.scaffold_fiction_project(
        vault_root, "The Glass Forest", today=fake_today,
    )
    assert result.status == "created"
    assert result.slug == "the-glass-forest"
    assert result.rel_dir == "draft/fiction/the-glass-forest"

    project_dir = vault_root / "draft" / "fiction" / "the-glass-forest"
    assert project_dir.is_dir()

    # Every element file present.
    for filename in ("continuity.md", "story.md", "structure.md",
                     "world.md", "voice.md"):
        assert (project_dir / filename).is_file(), (
            f"missing {filename}"
        )

    # characters/ + .gitkeep exist.
    characters_dir = project_dir / "characters"
    assert characters_dir.is_dir()
    gitkeep = characters_dir / ".gitkeep"
    assert gitkeep.is_file()
    assert gitkeep.read_text(encoding="utf-8") == ""

    # Result lists every created file (5 element files + .gitkeep).
    assert len(result.created_files) == 6


def test_scaffold_continuity_frontmatter(vault_root, fake_today):
    fiction.scaffold_fiction_project(
        vault_root, "The Glass Forest", today=fake_today,
    )
    continuity_path = (
        vault_root / "draft" / "fiction" / "the-glass-forest" / "continuity.md"
    )
    fm = frontmatter.load(str(continuity_path))
    assert fm["type"] == "fiction-continuity"
    assert fm["project"] == "The Glass Forest"  # human-readable, NOT slug
    assert fm["created"] == fake_today
    assert fm["fiction_slug"] == "the-glass-forest"


def test_scaffold_per_element_frontmatter(vault_root, fake_today):
    """Each element file carries type=fiction-<element> + project + slug."""
    fiction.scaffold_fiction_project(
        vault_root, "Storm's End", today=fake_today,
    )
    project_dir = vault_root / "draft" / "fiction" / "storms-end"

    for element_kind, filename in [
        ("story", "story.md"),
        ("structure", "structure.md"),
        ("world", "world.md"),
        ("voice", "voice.md"),
    ]:
        fm = frontmatter.load(str(project_dir / filename))
        assert fm["type"] == f"fiction-{element_kind}", (
            f"{filename} has wrong type: {fm['type']}"
        )
        assert fm["project"] == "Storm's End"
        assert fm["created"] == fake_today
        assert fm["fiction_slug"] == "storms-end"


def test_scaffold_continuity_contains_wikilinks(vault_root, fake_today):
    """continuity.md's body has wikilinks pointing at every sibling."""
    fiction.scaffold_fiction_project(
        vault_root, "The Glass Forest", today=fake_today,
    )
    continuity_text = (
        vault_root / "draft" / "fiction" / "the-glass-forest" / "continuity.md"
    ).read_text(encoding="utf-8")

    # Sections present.
    assert "## Synopsis" in continuity_text
    assert "## Characters" in continuity_text
    assert "## World" in continuity_text
    assert "## Voice" in continuity_text
    assert "## Structure" in continuity_text
    assert "## Plot state" in continuity_text
    assert "## Recent canonical updates" in continuity_text

    # Wikilinks point at sibling files using the slug.
    assert "[[draft/fiction/the-glass-forest/world]]" in continuity_text
    assert "[[draft/fiction/the-glass-forest/voice]]" in continuity_text
    assert "[[draft/fiction/the-glass-forest/structure]]" in continuity_text
    assert "[[draft/fiction/the-glass-forest/characters/]]" in continuity_text

    # READ THIS FIRST marker present (Hypatia's session-open cue).
    assert "READ THIS FIRST" in continuity_text


def test_scaffold_story_body_uses_title(vault_root, fake_today):
    fiction.scaffold_fiction_project(
        vault_root, "The Glass Forest", today=fake_today,
    )
    story_text = (
        vault_root / "draft" / "fiction" / "the-glass-forest" / "story.md"
    ).read_text(encoding="utf-8")
    assert "# The Glass Forest" in story_text


def test_scaffold_structure_body_references_frameworks(vault_root, fake_today):
    """structure.md placeholder names the 9 frameworks operator can pick."""
    fiction.scaffold_fiction_project(
        vault_root, "X", today=fake_today,
    )
    structure_text = (
        vault_root / "draft" / "fiction" / "x" / "structure.md"
    ).read_text(encoding="utf-8")
    # At least name a few of the frameworks so operator knows where
    # to look. Explicit list (not all 9 — keeps the test robust to
    # body-text edits) but the 3 most common.
    assert "3-act" in structure_text
    assert "Hero's Journey" in structure_text
    assert "Save the Cat" in structure_text


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_scaffold_second_invocation_is_noop(vault_root, fake_today):
    """Second call with same title returns already_exists and does not
    overwrite."""
    first = fiction.scaffold_fiction_project(
        vault_root, "The Glass Forest", today=fake_today,
    )
    assert first.status == "created"

    # Modify the manuscript so we can detect overwrites.
    story_path = (
        vault_root / "draft" / "fiction" / "the-glass-forest" / "story.md"
    )
    sentinel = "ANDREW'S WORK IN PROGRESS — DO NOT OVERWRITE\n"
    story_path.write_text(sentinel, encoding="utf-8")

    second = fiction.scaffold_fiction_project(
        vault_root, "The Glass Forest", today=fake_today,
    )
    assert second.status == "already_exists"
    assert second.slug == "the-glass-forest"
    assert "already exists" in second.detail.lower()
    # Critical: working manuscript untouched.
    assert story_path.read_text(encoding="utf-8") == sentinel


def test_scaffold_different_titles_are_separate_projects(vault_root, fake_today):
    """Two different titles → two separate directories. No collision."""
    a = fiction.scaffold_fiction_project(
        vault_root, "Project A", today=fake_today,
    )
    b = fiction.scaffold_fiction_project(
        vault_root, "Project B", today=fake_today,
    )
    assert a.status == "created"
    assert b.status == "created"
    assert a.rel_dir != b.rel_dir
    assert (vault_root / a.rel_dir).is_dir()
    assert (vault_root / b.rel_dir).is_dir()


def test_scaffold_titles_with_same_slug_collide(vault_root, fake_today):
    """Different titles that produce the SAME slug share the directory.

    Edge case worth pinning: ``"The Glass Forest"`` and
    ``"the glass forest"`` both slug to ``"the-glass-forest"``. The
    second invocation hits the idempotency branch — by design, the
    slug is the addressing key.
    """
    fiction.scaffold_fiction_project(
        vault_root, "The Glass Forest", today=fake_today,
    )
    second = fiction.scaffold_fiction_project(
        vault_root, "the glass forest", today=fake_today,
    )
    assert second.status == "already_exists"
    # The directory still has the FIRST title in its frontmatter
    # (the project field) — second invocation didn't overwrite.
    fm = frontmatter.load(
        str(vault_root / second.rel_dir / "continuity.md")
    )
    assert fm["project"] == "The Glass Forest"  # original title preserved


# ---------------------------------------------------------------------------
# Config gate — /fiction registration
# ---------------------------------------------------------------------------


def _make_config(
    tmp_path: Path,
    *,
    fiction_config: FictionConfig | None = None,
) -> TalkerConfig:
    """Build a minimal TalkerConfig for handler-registration tests."""
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir(exist_ok=True)
    return TalkerConfig(
        bot_token="test-token",
        allowed_users=[1],
        primary_users=["person/Andrew Newton"],
        anthropic=AnthropicConfig(
            api_key="test-key", model="claude-sonnet-4-6",
        ),
        stt=STTConfig(api_key="test-stt", model="whisper-large-v3"),
        session=SessionConfig(
            gap_timeout_seconds=1800,
            state_path=str(tmp_path / "state.json"),
        ),
        vault=VaultConfig(path=str(vault_dir)),
        logging=LoggingConfig(file=str(tmp_path / "talker.log")),
        instance=InstanceConfig(name="Hypatia", canonical="Hypatia"),
        fiction=fiction_config,
    )


def _build_app_and_get_commands(config: TalkerConfig) -> set[str]:
    """Construct an Application from the config + return the set of
    registered CommandHandler names (the strings in ``handler.commands``)."""
    from alfred.telegram import state as state_mod

    with tempfile.TemporaryDirectory() as tmp:
        mgr = state_mod.StateManager(Path(tmp) / "s.json")
        mgr.load()
        app = bot.build_app(
            config=config,
            state_mgr=mgr,
            anthropic_client=None,
            system_prompt="",
            vault_context_str="",
        )
        commands: set[str] = set()
        for group in app.handlers.values():
            for h in group:
                cmds = getattr(h, "commands", None)
                if cmds:
                    commands.update(cmds)
        return commands


def test_fiction_command_not_registered_when_block_absent(tmp_path):
    """Default config (no fiction block at all) → /fiction NOT
    registered. Salem-style instances see no surface."""
    config = _make_config(tmp_path, fiction_config=None)
    commands = _build_app_and_get_commands(config)
    assert "fiction" not in commands, (
        f"/fiction must NOT be registered without explicit opt-in. "
        f"Registered commands: {sorted(commands)}"
    )


def test_fiction_command_not_registered_when_disabled(tmp_path):
    """Explicit ``enabled=False`` block → /fiction NOT registered."""
    config = _make_config(
        tmp_path, fiction_config=FictionConfig(command_enabled=False),
    )
    commands = _build_app_and_get_commands(config)
    assert "fiction" not in commands


def test_fiction_command_registered_when_enabled(tmp_path):
    """Hypatia opts in → /fiction shows up as a CommandHandler."""
    config = _make_config(
        tmp_path, fiction_config=FictionConfig(command_enabled=True),
    )
    commands = _build_app_and_get_commands(config)
    assert "fiction" in commands, (
        f"/fiction must be registered when fiction.command_enabled=True. "
        f"Registered commands: {sorted(commands)}"
    )


def test_fiction_config_loaded_from_unified():
    """Config builder honors the YAML block."""
    from alfred.telegram.config import load_from_unified

    raw = {
        "telegram": {
            "bot_token": "x",
            "instance": {"name": "Hypatia"},
            "fiction": {"command_enabled": True},
        },
    }
    cfg = load_from_unified(raw)
    assert cfg.fiction is not None
    assert cfg.fiction.command_enabled is True


def test_fiction_config_absent_block_leaves_none():
    """No ``fiction`` block in YAML → cfg.fiction is None (sentinel)."""
    from alfred.telegram.config import load_from_unified

    raw = {
        "telegram": {
            "bot_token": "x",
            "instance": {"name": "Salem"},
            # No fiction block
        },
    }
    cfg = load_from_unified(raw)
    assert cfg.fiction is None
