"""Tests for the email classifier module.

Covers:
- Per-instance config loading (block present → enabled; absent → disabled).
- Email-only filter (non-email inbox content short-circuits).
- Note-only filter (person/org/task records are NOT classified).
- Classifier output schema validation (mock LLM response → parsed correctly).
- Frontmatter mutation (record gets ``priority`` + ``action_hint`` fields).
- Disabled path (``enabled: false`` → no LLM call, no mutation).
- JSON parse failure → record gets sentinel value, no crash.
- Named-contact lookup helper (loads ``person/*.md`` records).

No real LLM call happens — the classifier accepts an ``llm_caller``
callable that tests inject directly. No anthropic SDK monkeypatching.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from textwrap import dedent
from typing import Any

import frontmatter
import pytest

from alfred.email_classifier import (
    ClassificationResult,
    EmailClassifierConfig,
    classify_record,
    classify_records_for_inbox,
    is_email_inbox,
    load_from_unified,
)
from alfred.email_classifier.classifier import (
    _coerce_result,
    _parse_classification,
)
from alfred.email_classifier.vault_helpers import (
    NamedContact,
    get_named_contacts,
    render_contacts_for_prompt,
    reset_contacts_cache,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def classifier_vault(tmp_path: Path) -> Path:
    """Minimal vault with person/, note/, org/, task/ dirs."""
    vault = tmp_path / "vault"
    vault.mkdir()
    for sub in ("person", "note", "org", "task", "inbox"):
        (vault / sub).mkdir()
    return vault


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    """Reset the contacts cache between every test to keep state clean."""
    reset_contacts_cache()
    yield
    reset_contacts_cache()


@pytest.fixture
def enabled_config() -> EmailClassifierConfig:
    """Default-enabled config with a real-ish API key placeholder."""
    cfg = EmailClassifierConfig(enabled=True)
    cfg.anthropic.api_key = "DUMMY_ANTHROPIC_TEST_KEY"
    return cfg


# Helper: build a fake llm_caller that returns a canned response.
@dataclass
class _FakeLLM:
    response: str = ""
    calls: list[dict[str, Any]] = field(default_factory=list)

    def __call__(
        self,
        system: str,
        user: str,
        config: EmailClassifierConfig,
    ) -> str:
        self.calls.append({"system": system, "user": user})
        return self.response


def _seed_note(
    vault: Path,
    name: str,
    *,
    description: str = "Test note",
    body: str = "# Test note\n",
) -> str:
    """Create a note record on disk; return its rel path."""
    rel = f"note/{name}.md"
    post = frontmatter.Post(
        body,
        type="note",
        name=name,
        description=description,
        created="2026-04-21",
        tags=[],
        related=[],
    )
    (vault / "note" / f"{name}.md").write_text(
        frontmatter.dumps(post) + "\n",
        encoding="utf-8",
    )
    return rel


def _seed_person(
    vault: Path,
    name: str,
    *,
    email: str | list[str] | None = None,
    aliases: list[str] | None = None,
) -> None:
    """Create a person record on disk."""
    fm: dict[str, Any] = {
        "type": "person",
        "name": name,
        "created": "2026-04-21",
        "tags": [],
        "related": [],
    }
    if email is not None:
        fm["email"] = email
    if aliases is not None:
        fm["aliases"] = aliases
    post = frontmatter.Post(f"# {name}\n", **fm)
    (vault / "person" / f"{name}.md").write_text(
        frontmatter.dumps(post) + "\n",
        encoding="utf-8",
    )


# Sample email content the curator would have produced from
# Outlook → n8n → webhook. Always carries a **From:** line.
_EMAIL_SAMPLE = dedent(
    """\
    **From:** jamie@example.com
    **Subject:** Quick question about Friday

    Hey — can you confirm the meeting at 3pm Friday?

    -- Jamie
    """
)

_NON_EMAIL_SAMPLE = dedent(
    """\
    Voice memo transcript:

    Andrew just rambling about kitchen renovation budget options.
    No senders, no subjects.
    """
)


# ---------------------------------------------------------------------------
# Config loading — per-instance contract
# ---------------------------------------------------------------------------


def test_load_from_unified_block_absent_returns_disabled() -> None:
    """No ``email_classifier`` block ⇒ disabled config (Salem when KAL-LE
    inherits from a base, or any instance without an email pipeline)."""
    cfg = load_from_unified({"vault": {"path": "/tmp"}})
    assert cfg.enabled is False


def test_load_from_unified_block_present_enabled() -> None:
    """Salem-style block ⇒ enabled config with cue groups intact."""
    raw = {
        "email_classifier": {
            "enabled": True,
            "anthropic": {
                "api_key": "DUMMY_ANTHROPIC_TEST_KEY",
                "model": "claude-sonnet-4-6",
                "max_tokens": 1024,
            },
            "prompt": {
                "high": ["From Jamie"],
                "spam": ["unsolicited"],
            },
        },
    }
    cfg = load_from_unified(raw)
    assert cfg.enabled is True
    assert cfg.anthropic.api_key == "DUMMY_ANTHROPIC_TEST_KEY"
    assert cfg.anthropic.model == "claude-sonnet-4-6"
    assert cfg.prompt.high == ["From Jamie"]
    assert cfg.prompt.spam == ["unsolicited"]
    # Unspecified groups fall back to defaults
    assert cfg.prompt.medium  # non-empty default
    assert cfg.prompt.low  # non-empty default


def test_load_from_unified_explicit_disabled() -> None:
    """``enabled: false`` ⇒ post-processor is a no-op."""
    raw = {"email_classifier": {"enabled": False}}
    cfg = load_from_unified(raw)
    assert cfg.enabled is False


def test_load_from_unified_env_substitution(monkeypatch: pytest.MonkeyPatch) -> None:
    """``${VAR}`` placeholders are substituted at load time."""
    monkeypatch.setenv("MY_TEST_KEY", "DUMMY_ANTHROPIC_TEST_KEY_FROM_ENV")
    raw = {
        "email_classifier": {
            "enabled": True,
            "anthropic": {"api_key": "${MY_TEST_KEY}"},
        },
    }
    cfg = load_from_unified(raw)
    assert cfg.anthropic.api_key == "DUMMY_ANTHROPIC_TEST_KEY_FROM_ENV"


# ---------------------------------------------------------------------------
# Email detection
# ---------------------------------------------------------------------------


def test_is_email_inbox_from_header() -> None:
    assert is_email_inbox(_EMAIL_SAMPLE) is True


def test_is_email_inbox_account_subject() -> None:
    content = "**Account:** andrew@example.com\n**Subject:** notice\nbody"
    assert is_email_inbox(content) is True


def test_is_email_inbox_voice_memo() -> None:
    assert is_email_inbox(_NON_EMAIL_SAMPLE) is False


def test_is_email_inbox_empty() -> None:
    assert is_email_inbox("") is False


# ---------------------------------------------------------------------------
# Vault helpers — named contact lookup
# ---------------------------------------------------------------------------


def test_get_named_contacts_reads_person_records(classifier_vault: Path) -> None:
    _seed_person(classifier_vault, "Jamie Newton", email="jamie@example.com")
    _seed_person(
        classifier_vault,
        "RRTS Customer",
        email=["cust@rrts.ca", "alt@rrts.ca"],
        aliases=["RRTS"],
    )

    contacts = get_named_contacts(classifier_vault, config=None)
    by_name = {c.name: c for c in contacts}

    assert "Jamie Newton" in by_name
    assert by_name["Jamie Newton"].emails == ["jamie@example.com"]
    assert by_name["RRTS Customer"].emails == ["cust@rrts.ca", "alt@rrts.ca"]
    assert by_name["RRTS Customer"].aliases == ["RRTS"]


def test_get_named_contacts_strips_brackets_and_mailto(classifier_vault: Path) -> None:
    _seed_person(
        classifier_vault,
        "Bob",
        email="<mailto:bob@example.com>",
    )
    contacts = get_named_contacts(classifier_vault, config=None)
    assert contacts[0].emails == ["bob@example.com"]


def test_get_named_contacts_caches(classifier_vault: Path) -> None:
    """Second call within TTL returns same list without re-scanning."""
    _seed_person(classifier_vault, "Alice", email="alice@example.com")
    cfg = EmailClassifierConfig(enabled=True, named_contact_cache_seconds=60)

    first = get_named_contacts(classifier_vault, config=cfg, now=1000.0)
    # Add a record AFTER the cache primes — the cache must still
    # return the original list because the second call is within TTL.
    _seed_person(classifier_vault, "Bob", email="bob@example.com")
    second = get_named_contacts(classifier_vault, config=cfg, now=1010.0)

    assert [c.name for c in first] == ["Alice"]
    assert [c.name for c in second] == ["Alice"]


def test_get_named_contacts_missing_person_dir(tmp_path: Path) -> None:
    """No person/ dir ⇒ empty list, not a crash."""
    contacts = get_named_contacts(tmp_path, config=None)
    assert contacts == []


def test_render_contacts_for_prompt_empty() -> None:
    rendered = render_contacts_for_prompt([])
    assert "no named contacts" in rendered.lower()


def test_render_contacts_for_prompt_one() -> None:
    rendered = render_contacts_for_prompt(
        [NamedContact(name="Jamie", emails=["j@x.com"], aliases=["J"])],
    )
    assert "Jamie" in rendered
    assert "j@x.com" in rendered
    assert "J" in rendered


# ---------------------------------------------------------------------------
# JSON parse + coerce
# ---------------------------------------------------------------------------


def test_parse_classification_clean_json() -> None:
    raw = '{"priority": "high", "action_hint": "calendar", "reasoning": "rsvp"}'
    parsed = _parse_classification(raw)
    assert parsed == {
        "priority": "high",
        "action_hint": "calendar",
        "reasoning": "rsvp",
    }


def test_parse_classification_fenced_json() -> None:
    raw = "```json\n{\"priority\": \"low\", \"action_hint\": null, \"reasoning\": \"newsletter\"}\n```"
    parsed = _parse_classification(raw)
    assert parsed["priority"] == "low"
    assert parsed["action_hint"] is None


def test_parse_classification_with_prose_fallback() -> None:
    raw = "Sure, here's the JSON:\n{\"priority\": \"medium\", \"action_hint\": null}"
    parsed = _parse_classification(raw)
    assert parsed is not None
    assert parsed["priority"] == "medium"


def test_parse_classification_garbage() -> None:
    assert _parse_classification("nope just prose") is None
    assert _parse_classification("") is None


def test_coerce_result_valid() -> None:
    result = _coerce_result(
        {"priority": "spam", "action_hint": "archive", "reasoning": "phish"},
        sentinel="unclassified",
    )
    assert result.priority == "spam"
    assert result.action_hint == "archive"
    assert result.reasoning == "phish"


def test_coerce_result_unknown_priority_falls_back_to_sentinel() -> None:
    result = _coerce_result(
        {"priority": "URGENT!!", "action_hint": None},
        sentinel="unclassified",
    )
    assert result.priority == "unclassified"


def test_coerce_result_null_input_returns_sentinel() -> None:
    result = _coerce_result(None, sentinel="unclassified")
    assert result.priority == "unclassified"
    assert result.action_hint is None


def test_coerce_result_empty_action_hint_normalised_to_none() -> None:
    result = _coerce_result(
        {"priority": "low", "action_hint": ""},
        sentinel="unclassified",
    )
    assert result.action_hint is None


# ---------------------------------------------------------------------------
# classify_record — frontmatter mutation
# ---------------------------------------------------------------------------


def test_classify_record_writes_priority_and_action_hint(
    classifier_vault: Path,
    enabled_config: EmailClassifierConfig,
) -> None:
    rel = _seed_note(classifier_vault, "Jamie Friday meeting")
    fake = _FakeLLM(response=json.dumps({
        "priority": "high",
        "action_hint": "calendar",
        "reasoning": "RSVP requested by named contact",
    }))

    result = classify_record(
        vault_path=classifier_vault,
        note_rel_path=rel,
        inbox_content=_EMAIL_SAMPLE,
        config=enabled_config,
        llm_caller=fake,
    )

    assert result.priority == "high"
    assert result.action_hint == "calendar"
    assert result.written_to == rel
    assert len(fake.calls) == 1

    # Verify on-disk frontmatter
    post = frontmatter.load(str(classifier_vault / rel))
    assert post.metadata["priority"] == "high"
    assert post.metadata["action_hint"] == "calendar"
    assert "RSVP" in post.metadata["priority_reasoning"]


def test_classify_record_handles_null_action_hint(
    classifier_vault: Path,
    enabled_config: EmailClassifierConfig,
) -> None:
    rel = _seed_note(classifier_vault, "Generic newsletter")
    fake = _FakeLLM(response=json.dumps({
        "priority": "low",
        "action_hint": None,
        "reasoning": "newsletter",
    }))

    result = classify_record(
        vault_path=classifier_vault,
        note_rel_path=rel,
        inbox_content=_EMAIL_SAMPLE,
        config=enabled_config,
        llm_caller=fake,
    )

    assert result.priority == "low"
    assert result.action_hint is None
    post = frontmatter.load(str(classifier_vault / rel))
    assert post.metadata["priority"] == "low"
    assert post.metadata["action_hint"] is None


def test_classify_record_malformed_json_yields_sentinel(
    classifier_vault: Path,
    enabled_config: EmailClassifierConfig,
) -> None:
    rel = _seed_note(classifier_vault, "Confused note")
    fake = _FakeLLM(response="this is not JSON, it's prose, sorry")

    result = classify_record(
        vault_path=classifier_vault,
        note_rel_path=rel,
        inbox_content=_EMAIL_SAMPLE,
        config=enabled_config,
        llm_caller=fake,
    )

    assert result.priority == "unclassified"
    post = frontmatter.load(str(classifier_vault / rel))
    assert post.metadata["priority"] == "unclassified"


def test_classify_record_empty_llm_response_yields_sentinel(
    classifier_vault: Path,
    enabled_config: EmailClassifierConfig,
) -> None:
    """LLM returned nothing (e.g. SDK error swallowed) → sentinel, no crash."""
    rel = _seed_note(classifier_vault, "Quiet note")
    fake = _FakeLLM(response="")

    result = classify_record(
        vault_path=classifier_vault,
        note_rel_path=rel,
        inbox_content=_EMAIL_SAMPLE,
        config=enabled_config,
        llm_caller=fake,
    )
    assert result.priority == "unclassified"


# ---------------------------------------------------------------------------
# classify_records_for_inbox — post-processor entry point
# ---------------------------------------------------------------------------


def test_classify_records_for_inbox_disabled_short_circuits(
    classifier_vault: Path,
) -> None:
    rel = _seed_note(classifier_vault, "Should not be touched")
    cfg = EmailClassifierConfig(enabled=False)
    fake = _FakeLLM(response="should not be called")

    results = classify_records_for_inbox(
        vault_path=classifier_vault,
        inbox_content=_EMAIL_SAMPLE,
        note_paths=[rel],
        config=cfg,
        llm_caller=fake,
    )

    assert results == []
    assert len(fake.calls) == 0
    # Frontmatter unchanged
    post = frontmatter.load(str(classifier_vault / rel))
    assert "priority" not in post.metadata


def test_classify_records_for_inbox_non_email_short_circuits(
    classifier_vault: Path,
    enabled_config: EmailClassifierConfig,
) -> None:
    rel = _seed_note(classifier_vault, "Voice memo note")
    fake = _FakeLLM(response="should not be called")

    results = classify_records_for_inbox(
        vault_path=classifier_vault,
        inbox_content=_NON_EMAIL_SAMPLE,
        note_paths=[rel],
        config=enabled_config,
        llm_caller=fake,
    )

    assert results == []
    assert len(fake.calls) == 0
    post = frontmatter.load(str(classifier_vault / rel))
    assert "priority" not in post.metadata


def test_classify_records_for_inbox_filters_to_notes_only(
    classifier_vault: Path,
    enabled_config: EmailClassifierConfig,
) -> None:
    """person/, org/, task/ records are NOT classified — only note/ records."""
    rel_note = _seed_note(classifier_vault, "Real note")
    fake = _FakeLLM(response=json.dumps({
        "priority": "medium",
        "action_hint": None,
        "reasoning": "test",
    }))

    file_paths = [
        rel_note,
        "person/Jamie Newton.md",
        "org/RRTS.md",
        "task/Reply to Jamie.md",
    ]
    results = classify_records_for_inbox(
        vault_path=classifier_vault,
        inbox_content=_EMAIL_SAMPLE,
        note_paths=file_paths,
        config=enabled_config,
        llm_caller=fake,
    )

    # Only the note got classified — one LLM call, one result
    assert len(results) == 1
    assert len(fake.calls) == 1
    assert results[0].written_to == rel_note


def test_classify_records_for_inbox_skips_missing_record(
    classifier_vault: Path,
    enabled_config: EmailClassifierConfig,
) -> None:
    """If a note path doesn't exist on disk, skip it without crashing."""
    rel_existing = _seed_note(classifier_vault, "Real one")
    rel_missing = "note/Phantom note.md"
    fake = _FakeLLM(response=json.dumps({
        "priority": "medium",
        "action_hint": None,
    }))

    results = classify_records_for_inbox(
        vault_path=classifier_vault,
        inbox_content=_EMAIL_SAMPLE,
        note_paths=[rel_missing, rel_existing],
        config=enabled_config,
        llm_caller=fake,
    )

    # Phantom skipped, real one classified
    assert len(results) == 1
    assert results[0].written_to == rel_existing


def test_classify_records_for_inbox_no_notes_returns_empty(
    classifier_vault: Path,
    enabled_config: EmailClassifierConfig,
) -> None:
    """Non-note paths only ⇒ no classification, no crash."""
    fake = _FakeLLM(response="should not be called")
    results = classify_records_for_inbox(
        vault_path=classifier_vault,
        inbox_content=_EMAIL_SAMPLE,
        note_paths=["person/Bob.md", "task/something.md"],
        config=enabled_config,
        llm_caller=fake,
    )
    assert results == []
    assert len(fake.calls) == 0


def test_classify_records_for_inbox_includes_contacts_in_prompt(
    classifier_vault: Path,
    enabled_config: EmailClassifierConfig,
) -> None:
    """Salem's high cue requires the named-contact list to flow into the prompt."""
    _seed_person(classifier_vault, "Jamie Newton", email="jamie@example.com")
    rel = _seed_note(classifier_vault, "Jamie note")
    fake = _FakeLLM(response=json.dumps({
        "priority": "high",
        "action_hint": None,
    }))

    classify_records_for_inbox(
        vault_path=classifier_vault,
        inbox_content=_EMAIL_SAMPLE,
        note_paths=[rel],
        config=enabled_config,
        llm_caller=fake,
    )

    assert len(fake.calls) == 1
    user_prompt = fake.calls[0]["user"]
    assert "Jamie Newton" in user_prompt
    assert "jamie@example.com" in user_prompt


def test_classify_records_for_inbox_swallows_unexpected_exception(
    classifier_vault: Path,
    enabled_config: EmailClassifierConfig,
) -> None:
    """If the LLM caller itself throws, the post-processor logs and continues."""
    rel = _seed_note(classifier_vault, "Note A")

    def _boom(system: str, user: str, config: EmailClassifierConfig) -> str:
        raise RuntimeError("simulated SDK explosion")

    # No crash — empty results because the one record errored out
    results = classify_records_for_inbox(
        vault_path=classifier_vault,
        inbox_content=_EMAIL_SAMPLE,
        note_paths=[rel],
        config=enabled_config,
        llm_caller=_boom,
    )
    assert results == []


# ---------------------------------------------------------------------------
# System prompt content — sanity check on cue interpolation
# ---------------------------------------------------------------------------


def test_system_prompt_contains_all_cue_groups(
    classifier_vault: Path,
    enabled_config: EmailClassifierConfig,
) -> None:
    rel = _seed_note(classifier_vault, "Some note")
    fake = _FakeLLM(response=json.dumps({"priority": "low", "action_hint": None}))

    classify_records_for_inbox(
        vault_path=classifier_vault,
        inbox_content=_EMAIL_SAMPLE,
        note_paths=[rel],
        config=enabled_config,
        llm_caller=fake,
    )

    sys_prompt = fake.calls[0]["system"]
    for tier in ("high:", "medium:", "low:", "spam:"):
        assert tier in sys_prompt
    # Salem-specific seed lines
    assert "Jamie Newton" in sys_prompt
    assert "RRTS customer" in sys_prompt
