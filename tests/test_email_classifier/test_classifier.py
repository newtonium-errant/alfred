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


def test_load_from_unified_resolves_primary_telegram_user_id() -> None:
    """c5 (2026-06-01) — ``primary_telegram_user_id`` is hydrated from
    the unified ``telegram.allowed_users[0]`` (mirror of brief's
    behaviour). Operator's top-level telegram config is the single
    source of truth — no per-tool duplication needed."""
    raw = {
        "telegram": {
            "allowed_users": [123456789, 987654321],
        },
        "email_classifier": {
            "enabled": True,
        },
    }
    cfg = load_from_unified(raw)
    assert cfg.primary_telegram_user_id == 123456789


def test_load_from_unified_telegram_user_id_none_when_absent() -> None:
    """No telegram block in the unified config → push user_id stays
    None → c5 push silently no-ops at runtime."""
    raw = {"email_classifier": {"enabled": True}}
    cfg = load_from_unified(raw)
    assert cfg.primary_telegram_user_id is None


def test_load_from_unified_telegram_user_id_with_classifier_disabled() -> None:
    """Even when the email_classifier block is absent (disabled path),
    the telegram user_id is still resolved — keeps the field's value
    consistent regardless of which load_from_unified branch fires.

    Mostly defensive: a future capability that pushes from a disabled
    classifier (e.g. a manual ``alfred email-classifier push <path>``
    CLI) should still see the operator's configured user_id."""
    raw = {
        "telegram": {"allowed_users": [555]},
        # No email_classifier block at all.
    }
    cfg = load_from_unified(raw)
    assert cfg.enabled is False
    assert cfg.primary_telegram_user_id == 555


def test_load_from_unified_telegram_user_id_malformed_falls_back_to_none() -> None:
    """``telegram.allowed_users[0]`` not an int-coercible value → field
    stays None rather than crashing the loader. Mirrors brief's
    defensive handling (an operator typo in YAML shouldn't break the
    daemon)."""
    raw = {
        "telegram": {"allowed_users": ["not-a-number"]},
        "email_classifier": {"enabled": True},
    }
    cfg = load_from_unified(raw)
    assert cfg.primary_telegram_user_id is None


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


# ---------------------------------------------------------------------------
# high_priority_sender override (2026-05-31)
#
# Operator-declarative override: a person record carrying
# ``high_priority_sender: true`` forces ``priority=high`` when the
# inbox sender matches the contact (by email OR alias substring on
# display name). Backward-compat: existing records without the field
# default to False; their behavior is unchanged.


def _seed_person_with_high_priority_flag(
    vault: Path,
    name: str,
    *,
    email: str | list[str] | None = None,
    aliases: list[str] | None = None,
    high_priority_sender: bool = True,
) -> None:
    """Variant of ``_seed_person`` that sets the override flag."""
    fm: dict[str, Any] = {
        "type": "person",
        "name": name,
        "created": "2026-04-21",
        "tags": [],
        "related": [],
        "high_priority_sender": high_priority_sender,
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


def test_high_priority_sender_flag_loaded_from_person_frontmatter(
    classifier_vault: Path,
) -> None:
    """``get_named_contacts`` populates ``high_priority_sender`` from
    the person record's frontmatter when the field is set."""
    _seed_person_with_high_priority_flag(
        classifier_vault, "Paul Chudnovsky",
        email="pchudnovsky@coxandpalmer.com",
        aliases=["Paul Chudnovsky", "P Chudnovsky"],
        high_priority_sender=True,
    )

    contacts = get_named_contacts(classifier_vault, config=None)
    assert len(contacts) == 1
    assert contacts[0].name == "Paul Chudnovsky"
    assert contacts[0].high_priority_sender is True


def test_high_priority_sender_default_false_when_absent(
    classifier_vault: Path,
) -> None:
    """Backward-compat: existing person records without the field still
    parse cleanly; ``high_priority_sender`` defaults to False. Pin so
    the ~30+ existing person records on Salem's vault stay unaffected
    by this ship until the operator explicitly flips the flag."""
    _seed_person(
        classifier_vault, "Jamie Newton",
        email="jamie@example.com",
    )  # no high_priority_sender field set

    contacts = get_named_contacts(classifier_vault, config=None)
    assert len(contacts) == 1
    assert contacts[0].high_priority_sender is False


def test_classifier_override_to_high_when_sender_matches_flagged_contact(
    classifier_vault: Path,
    enabled_config: EmailClassifierConfig,
) -> None:
    """End-to-end override pin: flagged contact + matching sender +
    LLM verdict ``medium`` → final ``priority=high`` + reason names
    override + audit fields populated.

    This is the canonical worked example from the dispatch — Paul
    Chudnovsky's email always lands high regardless of what the LLM
    picks."""
    _seed_person_with_high_priority_flag(
        classifier_vault, "Paul Chudnovsky",
        email="pchudnovsky@coxandpalmer.com",
        aliases=["Paul Chudnovsky"],
    )
    # Inbox content with the canonical Outlook → n8n display-name +
    # bracketed-address shape.
    inbox = dedent(
        """\
        From: Chudnovsky, Paul (Halifax) <pchudnovsky@coxandpalmer.com>
        **Subject:** Re: contract review

        Andrew — see attached redlines.
        """
    )
    rel = _seed_note(classifier_vault, "Chudnovsky contract review")
    fake = _FakeLLM(response=json.dumps({
        "priority": "medium",  # LLM thinks medium; override forces high
        "action_hint": None,
        "reasoning": "business correspondence",
    }))

    result = classify_record(
        vault_path=classifier_vault,
        note_rel_path=rel,
        inbox_content=inbox,
        config=enabled_config,
        llm_caller=fake,
    )

    # Priority forced to high.
    assert result.priority == "high"
    # Audit fields populated — both signals that override fired.
    assert result.override_applied is True
    assert result.llm_priority == "medium"
    # Reason carries the override marker AND preserves the LLM's
    # original rationale.
    from alfred.email_classifier.classifier import (
        HIGH_PRIORITY_SENDER_OVERRIDE_PREFIX,
    )
    assert HIGH_PRIORITY_SENDER_OVERRIDE_PREFIX in result.reasoning
    assert "Paul Chudnovsky" in result.reasoning
    assert "business correspondence" in result.reasoning  # LLM preserved
    # Frontmatter on disk reflects the override + the audit field.
    post = frontmatter.load(str(classifier_vault / rel))
    assert post.metadata["priority"] == "high"
    assert post.metadata["priority_llm_pre_override"] == "medium"
    assert HIGH_PRIORITY_SENDER_OVERRIDE_PREFIX in post.metadata[
        "priority_reasoning"
    ]


def test_classifier_no_override_when_sender_matches_unflagged_contact(
    classifier_vault: Path,
    enabled_config: EmailClassifierConfig,
) -> None:
    """Person record exists with matching email but flag is False →
    LLM verdict respected, no override fires. Pin the backward-compat
    invariant: existing person records without the flag don't
    suddenly start overriding."""
    _seed_person(
        classifier_vault, "Random Person",
        email="random@example.com",
    )  # NO high_priority_sender flag
    inbox = "**From:** random@example.com\n**Subject:** hi\nbody\n"
    rel = _seed_note(classifier_vault, "Random note")
    fake = _FakeLLM(response=json.dumps({
        "priority": "medium",
        "action_hint": None,
        "reasoning": "neutral",
    }))

    result = classify_record(
        vault_path=classifier_vault,
        note_rel_path=rel,
        inbox_content=inbox,
        config=enabled_config,
        llm_caller=fake,
    )

    # Priority unchanged from LLM verdict.
    assert result.priority == "medium"
    # Audit fields confirm no override.
    assert result.override_applied is False
    assert result.llm_priority is None
    # Frontmatter doesn't carry the audit field (only present when
    # override fires).
    post = frontmatter.load(str(classifier_vault / rel))
    assert "priority_llm_pre_override" not in post.metadata


def test_classifier_no_override_when_sender_does_not_match_any_flagged_contact(
    classifier_vault: Path,
    enabled_config: EmailClassifierConfig,
) -> None:
    """Flagged contact exists in the vault but the inbox sender is
    different. LLM verdict respected."""
    _seed_person_with_high_priority_flag(
        classifier_vault, "Paul Chudnovsky",
        email="pchudnovsky@coxandpalmer.com",
    )
    inbox = "**From:** newsletter@randomsite.com\n**Subject:** offer\nbody\n"
    rel = _seed_note(classifier_vault, "Newsletter note")
    fake = _FakeLLM(response=json.dumps({
        "priority": "low",
        "action_hint": "archive",
        "reasoning": "newsletter",
    }))

    result = classify_record(
        vault_path=classifier_vault,
        note_rel_path=rel,
        inbox_content=inbox,
        config=enabled_config,
        llm_caller=fake,
    )

    assert result.priority == "low"
    assert result.override_applied is False
    assert result.llm_priority is None


def test_classifier_alias_match_triggers_override(
    classifier_vault: Path,
    enabled_config: EmailClassifierConfig,
) -> None:
    """Alias substring match on the From-line display name fires the
    override. Fixture: contact with alias 'Paul Chudnovsky'; sender
    display name 'Chudnovsky, Paul (Halifax)' contains 'Chudnovsky'
    AND the contact's alias substring fires via the symmetric
    case-insensitive substring rule.

    Per dispatch's match semantics: alias match against display name
    is OUT OF SCOPE for the canonical 'Paul Chudnovsky' alias →
    'Chudnovsky, Paul' display name case (the substring check is in
    one direction — alias-IN-display). Pick an alias that IS a
    substring of the display name to exercise the path."""
    _seed_person_with_high_priority_flag(
        classifier_vault, "P Chudnovsky",
        email="different@some-other-domain.com",  # email does NOT match
        aliases=["Chudnovsky"],  # alias IS substring of display name
    )
    # Different domain than the contact's email, but display name
    # contains the alias.
    inbox = dedent(
        """\
        From: Chudnovsky, Paul (Halifax) <p.chudnovsky@coxandpalmer.com>
        **Subject:** Note from law office

        Andrew — see attached.
        """
    )
    rel = _seed_note(classifier_vault, "Law office note")
    fake = _FakeLLM(response=json.dumps({
        "priority": "low",  # LLM mis-classifies; alias override fixes
        "action_hint": None,
        "reasoning": "no familiar sender",
    }))

    result = classify_record(
        vault_path=classifier_vault,
        note_rel_path=rel,
        inbox_content=inbox,
        config=enabled_config,
        llm_caller=fake,
    )

    assert result.priority == "high"
    assert result.override_applied is True
    assert result.llm_priority == "low"


def test_classifier_case_insensitive_email_match(
    classifier_vault: Path,
    enabled_config: EmailClassifierConfig,
) -> None:
    """Email match is case-insensitive on BOTH sides. Fixture: contact
    email is lowercase ``pchudnovsky@coxandpalmer.com``; incoming
    From-line uses uppercase ``PCHUDNOVSKY@COXANDPALMER.COM`` →
    override still fires."""
    _seed_person_with_high_priority_flag(
        classifier_vault, "Paul Chudnovsky",
        email="pchudnovsky@coxandpalmer.com",  # lowercase
    )
    inbox = (
        "**From:** PCHUDNOVSKY@COXANDPALMER.COM\n"  # uppercase
        "**Subject:** redlines\nbody\n"
    )
    rel = _seed_note(classifier_vault, "Case test note")
    fake = _FakeLLM(response=json.dumps({
        "priority": "medium",
        "action_hint": None,
        "reasoning": "ok",
    }))

    result = classify_record(
        vault_path=classifier_vault,
        note_rel_path=rel,
        inbox_content=inbox,
        config=enabled_config,
        llm_caller=fake,
    )

    assert result.priority == "high"
    assert result.override_applied is True


def test_classifier_override_preserves_llm_priority_in_audit_field(
    classifier_vault: Path,
    enabled_config: EmailClassifierConfig,
) -> None:
    """The override audit must capture BOTH the final ``priority=high``
    AND the LLM's original verdict so calibration review can second-
    guess the override.

    The dispatch named this pin specifically — operator needs to be
    able to see ``classifier said: medium / operator-flagged →
    high`` post-override, otherwise the calibration loop loses the
    ability to ask 'should this contact still be flagged?'"""
    _seed_person_with_high_priority_flag(
        classifier_vault, "Paul Chudnovsky",
        email="pchudnovsky@coxandpalmer.com",
    )
    inbox = "**From:** pchudnovsky@coxandpalmer.com\nSubject\nbody\n"
    rel = _seed_note(classifier_vault, "Audit pin note")
    fake = _FakeLLM(response=json.dumps({
        "priority": "spam",  # extreme: LLM thought SPAM, override → high
        "action_hint": "archive",
        "reasoning": "looked like marketing",
    }))

    result = classify_record(
        vault_path=classifier_vault,
        note_rel_path=rel,
        inbox_content=inbox,
        config=enabled_config,
        llm_caller=fake,
    )

    # Final priority is the override value.
    assert result.priority == "high"
    # LLM's pre-override verdict captured exactly.
    assert result.llm_priority == "spam"
    assert result.override_applied is True
    # Frontmatter on disk carries the audit field.
    post = frontmatter.load(str(classifier_vault / rel))
    assert post.metadata["priority"] == "high"
    assert post.metadata["priority_llm_pre_override"] == "spam"
    # action_hint preserved from LLM per dispatch (operator can fix
    # via calibration if needed — out of scope for the override).
    assert post.metadata["action_hint"] == "archive"


def test_classifier_no_override_when_llm_already_high(
    classifier_vault: Path,
    enabled_config: EmailClassifierConfig,
) -> None:
    """No-op override path: LLM already picked ``high`` for a flagged-
    contact email → don't muddy the audit fields with a redundant
    override marker.

    The ``override_applied`` field is meant to signal 'override
    CHANGED the verdict'; firing it on already-high would make the
    field useless as a calibration filter."""
    _seed_person_with_high_priority_flag(
        classifier_vault, "Paul Chudnovsky",
        email="pchudnovsky@coxandpalmer.com",
    )
    inbox = "**From:** pchudnovsky@coxandpalmer.com\nSubject\nbody\n"
    rel = _seed_note(classifier_vault, "Already-high note")
    fake = _FakeLLM(response=json.dumps({
        "priority": "high",  # LLM AGREES with the override
        "action_hint": None,
        "reasoning": "named contact, looks important",
    }))

    result = classify_record(
        vault_path=classifier_vault,
        note_rel_path=rel,
        inbox_content=inbox,
        config=enabled_config,
        llm_caller=fake,
    )

    # Priority stayed high (was already, didn't change).
    assert result.priority == "high"
    # Audit fields signal NO override fired (because nothing changed).
    assert result.override_applied is False
    assert result.llm_priority is None
    # LLM's reasoning preserved verbatim, NO override marker.
    from alfred.email_classifier.classifier import (
        HIGH_PRIORITY_SENDER_OVERRIDE_PREFIX,
    )
    assert HIGH_PRIORITY_SENDER_OVERRIDE_PREFIX not in result.reasoning
    # Frontmatter doesn't carry the audit field.
    post = frontmatter.load(str(classifier_vault / rel))
    assert "priority_llm_pre_override" not in post.metadata


def test_extract_sender_handles_bare_address() -> None:
    """``_extract_sender`` correctly parses the ``**From:** addr``
    shape (no display name, no brackets)."""
    from alfred.email_classifier.classifier import _extract_sender

    email, display = _extract_sender("**From:** jamie@example.com\nbody\n")
    assert email == "jamie@example.com"
    assert display == ""


def test_extract_sender_handles_display_name_with_brackets() -> None:
    """``_extract_sender`` correctly parses the corporate-mail-client
    shape: ``From: Display Name <addr@host>``."""
    from alfred.email_classifier.classifier import _extract_sender

    email, display = _extract_sender(
        "From: Chudnovsky, Paul (Halifax) <pchudnovsky@coxandpalmer.com>\n"
    )
    assert email == "pchudnovsky@coxandpalmer.com"
    assert display == "Chudnovsky, Paul (Halifax)"


def test_extract_sender_empty_when_no_from_line() -> None:
    """No From-line at all → empty tuple. Override step's caller
    treats this as 'no override possible' + falls through to LLM."""
    from alfred.email_classifier.classifier import _extract_sender

    email, display = _extract_sender("Just a voice memo transcript.\n")
    assert email == ""
    assert display == ""


def test_extract_sender_lowercases_email() -> None:
    """Email is normalised to lowercase for case-insensitive match
    in the override step."""
    from alfred.email_classifier.classifier import _extract_sender

    email, _ = _extract_sender("**From:** Jamie@EXAMPLE.COM\n")
    assert email == "jamie@example.com"


# ---------------------------------------------------------------------------
# Word-boundary alias match (NOTE-1 fix on 6d85bc2, 2026-05-31)
#
# Pre-fix the alias check used substring ``in`` — a contact with alias
# ``"Pat"`` would falsely match "Patricia Smith" / "Pattern Recognition
# Weekly" / any display name with "pat" as a substring. The
# word-boundary regex (``\b<alias>\b``) closes the foot-gun for short
# aliases while preserving the legitimate matches.


def test_alias_match_uses_word_boundary_pat_does_not_match_patricia(
    classifier_vault: Path,
    enabled_config: EmailClassifierConfig,
) -> None:
    """Flagged contact with alias ``"Pat"``; incoming email from
    "Patricia Smith". Override MUST NOT fire — ``Patricia`` is not
    a word-boundary match for ``Pat`` (``Pat`` is a prefix of a
    longer word, no boundary between ``Pat`` and ``ricia``).

    Pre-fix this test would have FAILED — substring ``"pat" in
    "patricia smith"`` was True → override fired → priority forced
    to high on a stranger's email."""
    _seed_person_with_high_priority_flag(
        classifier_vault, "Pat O'Brien",
        email="po@example.com",  # email does NOT match
        aliases=["Pat"],  # short alias — foot-gun under substring match
    )
    inbox = "From: Patricia Smith <patricia@news.com>\nSubject\nbody\n"
    rel = _seed_note(classifier_vault, "Patricia note")
    fake = _FakeLLM(response=json.dumps({
        "priority": "low",
        "action_hint": None,
        "reasoning": "newsletter",
    }))

    result = classify_record(
        vault_path=classifier_vault,
        note_rel_path=rel,
        inbox_content=inbox,
        config=enabled_config,
        llm_caller=fake,
    )

    # Override DID NOT fire — LLM's "low" stands.
    assert result.priority == "low"
    assert result.override_applied is False
    assert result.llm_priority is None


def test_alias_match_uses_word_boundary_pat_does_match_pat_obrien(
    classifier_vault: Path,
    enabled_config: EmailClassifierConfig,
) -> None:
    """Flagged contact with alias ``"Pat"``; incoming email from
    "Pat O'Brien <po@example.com>". Override MUST fire — ``Pat`` is
    a word-boundary match in ``"Pat O'Brien"`` (boundary between
    start-of-string and ``P``, boundary between ``t`` and space).

    Sister test to the Patricia case above — same alias, same
    flagged contact, different display name. The legitimate match
    must still work after the word-boundary tightening."""
    _seed_person_with_high_priority_flag(
        classifier_vault, "Pat O'Brien",
        email="different@domain.com",  # email does NOT match
        aliases=["Pat"],
    )
    inbox = "From: Pat O'Brien <po@example.com>\nSubject\nbody\n"
    rel = _seed_note(classifier_vault, "Pat note")
    fake = _FakeLLM(response=json.dumps({
        "priority": "medium",
        "action_hint": None,
        "reasoning": "casual contact",
    }))

    result = classify_record(
        vault_path=classifier_vault,
        note_rel_path=rel,
        inbox_content=inbox,
        config=enabled_config,
        llm_caller=fake,
    )

    # Override DID fire — LLM's "medium" overridden to "high".
    assert result.priority == "high"
    assert result.override_applied is True
    assert result.llm_priority == "medium"


def test_alias_match_full_phrase_still_works(
    classifier_vault: Path,
    enabled_config: EmailClassifierConfig,
) -> None:
    """Regression pin for the canonical Paul Chudnovsky worked
    example — multi-word aliases still match under the word-boundary
    rule.

    Alias ``"Chudnovsky"`` matches display name ``"Chudnovsky, Paul
    (Halifax)"`` because the ``,`` after ``Chudnovsky`` is not a
    ``\\w`` character → word boundary. The shipped 6d85bc2 test
    ``test_classifier_alias_match_triggers_override`` already covers
    this; this is an explicit pin against the word-boundary tightening
    regressing it."""
    _seed_person_with_high_priority_flag(
        classifier_vault, "P Chudnovsky",
        email="different@some-other-domain.com",
        aliases=["Chudnovsky"],
    )
    inbox = (
        "From: Chudnovsky, Paul (Halifax) "
        "<p.chudnovsky@coxandpalmer.com>\nSubject\nbody\n"
    )
    rel = _seed_note(classifier_vault, "Law office note")
    fake = _FakeLLM(response=json.dumps({
        "priority": "low",
        "action_hint": None,
        "reasoning": "no familiar sender",
    }))

    result = classify_record(
        vault_path=classifier_vault,
        note_rel_path=rel,
        inbox_content=inbox,
        config=enabled_config,
        llm_caller=fake,
    )

    assert result.priority == "high"
    assert result.override_applied is True
    assert result.llm_priority == "low"


def test_alias_match_handles_regex_metachars(
    classifier_vault: Path,
    enabled_config: EmailClassifierConfig,
) -> None:
    """Alias containing regex metacharacters (apostrophe, parens,
    dots) MUST NOT be interpreted as regex syntax.

    Without ``re.escape``, an alias like ``"O'Brien"`` would pass
    through unescaped — apostrophe doesn't break Python regex (it
    has no special meaning), but other metachars like ``.`` (any
    char) or ``(`` (group-start, error) would. This test pins the
    safe behaviour: the alias is treated as a literal string,
    matched via word boundaries.

    Two assertions to cover both directions:
      * alias ``"O'Brien"`` matches display ``"O'Brien"`` → override
        fires (literal apostrophe handled correctly via escape).
      * alias ``"O'Brien"`` does NOT match display ``"XOBrienY"``
        (no apostrophe, different word stem) → no false positive
        from misinterpreting the apostrophe."""
    # --- Case 1: apostrophe in alias matches apostrophe in display ---
    _seed_person_with_high_priority_flag(
        classifier_vault, "Sean OBrien",
        email="different@somewhere.com",
        aliases=["O'Brien"],
    )
    inbox = "From: Sean O'Brien <so@example.com>\nSubject\nbody\n"
    rel = _seed_note(classifier_vault, "OBrien note")
    fake = _FakeLLM(response=json.dumps({
        "priority": "medium",
        "action_hint": None,
        "reasoning": "neutral",
    }))

    result = classify_record(
        vault_path=classifier_vault,
        note_rel_path=rel,
        inbox_content=inbox,
        config=enabled_config,
        llm_caller=fake,
    )
    assert result.priority == "high", (
        "Apostrophe-bearing alias must match literal apostrophe in "
        "display name (re.escape preserves the apostrophe as a "
        "literal, not as a regex metachar)"
    )
    assert result.override_applied is True

    # --- Case 2: same alias does NOT match an apostrophe-less display ---
    # Different note + different inbox; same vault carries the same
    # flagged contact from Case 1.
    inbox_no_apos = (
        "From: XOBrienY Smith <xy@spam.com>\nSubject\nbody\n"
    )
    rel2 = _seed_note(classifier_vault, "XOBrienY note")
    fake2 = _FakeLLM(response=json.dumps({
        "priority": "low",
        "action_hint": None,
        "reasoning": "stranger",
    }))

    result2 = classify_record(
        vault_path=classifier_vault,
        note_rel_path=rel2,
        inbox_content=inbox_no_apos,
        config=enabled_config,
        llm_caller=fake2,
    )
    # NO override — the apostrophe in the alias is a LITERAL that
    # must match. ``XOBrienY`` contains ``OBrien`` but NOT ``O'Brien``;
    # the literal apostrophe requirement prevents a false positive.
    # AND the word-boundary check would block it anyway (``XOBrienY``
    # has no boundary around the ``OBrien`` substring).
    assert result2.priority == "low"
    assert result2.override_applied is False


# ---------------------------------------------------------------------------
# c6 — spam quarantine layer (2026-05-31)
# ---------------------------------------------------------------------------
#
# Two gates: (a) classifier verdict == "spam", (b) operator has ratified
# spam surfacing via ``/calibration_ok spam`` (which flipped the
# ``confidence.spam`` flag in the daily_sync state file to true). When
# both fire, vault_move the record to ``<quarantine>/spam/<YYYY-MM>/``.
# Pre-ratification (the most common state during c1-c5 calibration) the
# spam frontmatter persists but the record stays at its normal vault
# location. See ``classifier._quarantine_spam_record`` for the path
# convention; see ``EmailClassifierConfig.quarantine_*`` for the config
# surface.


def _seed_confidence_state(state_path: Path, *, spam_flag: bool) -> None:
    """Write a minimal daily_sync state file with the confidence map.

    Mirrors the on-disk shape ``daily_sync.confidence.list_confidence``
    produces — see ``alfred/daily_sync/confidence.py::load_state`` for
    the canonical reader.
    """
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps({
            "confidence": {
                "high": True,
                "medium": True,
                "low": True,
                "spam": spam_flag,
            },
        }),
        encoding="utf-8",
    )


def test_classifier_quarantines_spam_when_flag_true(
    classifier_vault: Path,
    enabled_config: EmailClassifierConfig,
    tmp_path: Path,
) -> None:
    """Classifier verdict ``spam`` + ``confidence.spam: true`` →
    record gets moved to ``quarantine/spam/<YYYY-MM>/<filename>``."""
    state_path = tmp_path / "data" / "daily_sync_state.json"
    _seed_confidence_state(state_path, spam_flag=True)
    enabled_config.quarantine_state_path = str(state_path)

    rel = _seed_note(classifier_vault, "Obvious spam pitch")
    fake = _FakeLLM(response=json.dumps({
        "priority": "spam",
        "action_hint": None,
        "reasoning": "unsolicited commercial pitch",
    }))

    result = classify_record(
        vault_path=classifier_vault,
        note_rel_path=rel,
        inbox_content=_EMAIL_SAMPLE,
        config=enabled_config,
        llm_caller=fake,
    )

    # The result carries the quarantine destination.
    assert result.priority == "spam"
    assert result.quarantined_to != ""
    # Path format: quarantine/spam/YYYY-MM/<filename>
    assert result.quarantined_to.startswith("quarantine/spam/")
    assert result.quarantined_to.endswith("Obvious spam pitch.md")

    # On-disk: the record exists at the new location, not the old.
    new_full = classifier_vault / result.quarantined_to
    old_full = classifier_vault / rel
    assert new_full.exists(), "record missing from quarantine destination"
    assert not old_full.exists(), "record still present at original note/ path"

    # Frontmatter survives the move — priority + reasoning intact so
    # an operator reviewing the quarantine can see WHY the classifier
    # flagged it.
    post = frontmatter.load(str(new_full))
    assert post.metadata["priority"] == "spam"
    assert "unsolicited" in post.metadata.get("priority_reasoning", "")


def test_classifier_does_not_quarantine_spam_when_flag_false(
    classifier_vault: Path,
    enabled_config: EmailClassifierConfig,
    tmp_path: Path,
) -> None:
    """Classifier verdict ``spam`` + ``confidence.spam: false`` →
    record stays at normal path (current pre-c6 behavior preserved).

    This is the dominant state during c1-c5 calibration before the
    operator explicitly ratifies spam surfacing."""
    state_path = tmp_path / "data" / "daily_sync_state.json"
    _seed_confidence_state(state_path, spam_flag=False)
    enabled_config.quarantine_state_path = str(state_path)

    rel = _seed_note(classifier_vault, "Spam in calibration window")
    fake = _FakeLLM(response=json.dumps({
        "priority": "spam",
        "action_hint": None,
        "reasoning": "looks spammy",
    }))

    result = classify_record(
        vault_path=classifier_vault,
        note_rel_path=rel,
        inbox_content=_EMAIL_SAMPLE,
        config=enabled_config,
        llm_caller=fake,
    )

    # Quarantine did NOT fire — record stays at note/<file>.md
    assert result.priority == "spam"
    assert result.quarantined_to == ""
    # On-disk: record at original location, no quarantine tree created.
    assert (classifier_vault / rel).exists()
    assert not (classifier_vault / "quarantine").exists()


def test_classifier_does_not_quarantine_non_spam_when_flag_true(
    classifier_vault: Path,
    enabled_config: EmailClassifierConfig,
    tmp_path: Path,
) -> None:
    """Classifier verdict ``medium`` + ``confidence.spam: true`` →
    record stays at normal path. The flag gates spam quarantine ONLY;
    non-spam verdicts proceed unchanged regardless of the flag's
    state."""
    state_path = tmp_path / "data" / "daily_sync_state.json"
    _seed_confidence_state(state_path, spam_flag=True)
    enabled_config.quarantine_state_path = str(state_path)

    rel = _seed_note(classifier_vault, "Routine appointment confirm")
    fake = _FakeLLM(response=json.dumps({
        "priority": "medium",
        "action_hint": "calendar",
        "reasoning": "appointment confirm from a vendor",
    }))

    result = classify_record(
        vault_path=classifier_vault,
        note_rel_path=rel,
        inbox_content=_EMAIL_SAMPLE,
        config=enabled_config,
        llm_caller=fake,
    )

    assert result.priority == "medium"
    assert result.quarantined_to == ""
    assert (classifier_vault / rel).exists()
    assert not (classifier_vault / "quarantine").exists()


def test_classifier_does_not_quarantine_when_state_file_missing(
    classifier_vault: Path,
    enabled_config: EmailClassifierConfig,
    tmp_path: Path,
) -> None:
    """Edge case from the dispatch: state file missing → treat as
    flag=false (don't crash; don't quarantine if flag-mechanism state
    is unreadable).

    Operator-actionable: a missing state file usually means the
    daily_sync daemon hasn't been run yet, OR the operator deleted
    the state to reset calibration. Either way, the safest default
    is "stay at normal path" rather than silently quarantine on a
    state we can't read."""
    # Point at a non-existent path — load_state returns {} → flag is
    # treated as False.
    enabled_config.quarantine_state_path = str(
        tmp_path / "nonexistent" / "daily_sync_state.json"
    )

    rel = _seed_note(classifier_vault, "Spam with no state file")
    fake = _FakeLLM(response=json.dumps({
        "priority": "spam",
        "action_hint": None,
        "reasoning": "spammy",
    }))

    result = classify_record(
        vault_path=classifier_vault,
        note_rel_path=rel,
        inbox_content=_EMAIL_SAMPLE,
        config=enabled_config,
        llm_caller=fake,
    )

    assert result.priority == "spam"
    assert result.quarantined_to == ""
    assert (classifier_vault / rel).exists()


def test_classifier_does_not_quarantine_when_state_file_malformed(
    classifier_vault: Path,
    enabled_config: EmailClassifierConfig,
    tmp_path: Path,
) -> None:
    """Defensive: a corrupt state file (truncated JSON, garbage)
    falls back to flag=false rather than crashing the classifier.
    Mirrors the same fail-safe stance as the missing-file case.

    Pre-c6, the daily_sync ``load_state`` helper already had the
    "tolerant of malformed JSON" behavior (confidence.py:60); this
    test pins that the classifier's read path also tolerates."""
    state_path = tmp_path / "data" / "daily_sync_state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text("{not valid json at all", encoding="utf-8")
    enabled_config.quarantine_state_path = str(state_path)

    rel = _seed_note(classifier_vault, "Spam with garbage state")
    fake = _FakeLLM(response=json.dumps({
        "priority": "spam",
        "action_hint": None,
        "reasoning": "spammy",
    }))

    result = classify_record(
        vault_path=classifier_vault,
        note_rel_path=rel,
        inbox_content=_EMAIL_SAMPLE,
        config=enabled_config,
        llm_caller=fake,
    )

    assert result.priority == "spam"
    assert result.quarantined_to == ""


def test_classifier_does_not_quarantine_when_unclassified(
    classifier_vault: Path,
    enabled_config: EmailClassifierConfig,
    tmp_path: Path,
) -> None:
    """LLM parse failure → priority=unclassified (sentinel). Even when
    the spam flag is true, an unclassified record is NOT quarantined
    — quarantine is gated on the explicit ``"spam"`` verdict, not on
    any non-high/medium/low value. This prevents the calibration
    loop's "needs reclassification" records from being moved out of
    the active processing pipeline."""
    state_path = tmp_path / "data" / "daily_sync_state.json"
    _seed_confidence_state(state_path, spam_flag=True)
    enabled_config.quarantine_state_path = str(state_path)

    rel = _seed_note(classifier_vault, "Confused classifier output")
    fake = _FakeLLM(response="prose, not JSON")  # → sentinel

    result = classify_record(
        vault_path=classifier_vault,
        note_rel_path=rel,
        inbox_content=_EMAIL_SAMPLE,
        config=enabled_config,
        llm_caller=fake,
    )

    assert result.priority == "unclassified"
    assert result.quarantined_to == ""
    assert (classifier_vault / rel).exists()


# ---------------------------------------------------------------------------
# c5 — high-priority Telegram push (2026-06-01)
# ---------------------------------------------------------------------------
#
# Architectural sibling of c6 quarantine — same gate-on-confidence-flag
# pattern, different fire action. Two gates: (a) classifier verdict ==
# "high", (b) operator has ratified high-tier surfacing via
# ``/calibration_ok high`` (which flipped daily_sync ``confidence.high``
# to true). When both fire, dispatch a one-shot Telegram message via
# transport.client.send_outbound. Pre-ratification, the high frontmatter
# persists but no push fires.
#
# Tests monkeypatch ``alfred.transport.client.send_outbound`` to capture
# the call args without making real HTTP requests. The push helper
# imports the symbol LAZILY inside the function body, so patching the
# module attribute is sufficient — no need to patch the binding at the
# classifier module level.


def _seed_high_confidence_state(
    state_path: Path,
    *,
    high_flag: bool,
) -> None:
    """Write a minimal daily_sync state file with the high-confidence flag.

    Mirrors the on-disk shape ``daily_sync.confidence.list_confidence``
    produces (see ``_seed_confidence_state`` above — same writer, just
    a different flag-of-interest for c5 tests).
    """
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps({
            "confidence": {
                "high": high_flag,
                "medium": True,
                "low": True,
                "spam": True,
            },
        }),
        encoding="utf-8",
    )


@dataclass
class _CapturedSend:
    """Records the args passed to ``send_outbound`` for assertion."""

    calls: list[dict[str, Any]] = field(default_factory=list)
    raise_exc: BaseException | None = None

    async def __call__(
        self,
        user_id: int,
        text: str,
        *,
        scheduled_at: str | None = None,
        dedupe_key: str | None = None,
        client_name: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append({
            "user_id": user_id,
            "text": text,
            "scheduled_at": scheduled_at,
            "dedupe_key": dedupe_key,
            "client_name": client_name,
        })
        if self.raise_exc is not None:
            raise self.raise_exc
        return {"id": "fake-msg-id", "status": "sent"}


def _enabled_config_with_push_user(
    enabled_config: EmailClassifierConfig,
    user_id: int = 42,
) -> EmailClassifierConfig:
    """Stamp a primary_telegram_user_id on the shared enabled_config."""
    enabled_config.primary_telegram_user_id = user_id
    return enabled_config


def test_high_push_fires_when_gate_enabled(
    classifier_vault: Path,
    enabled_config: EmailClassifierConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Classifier verdict ``high`` + ``confidence.high: true`` →
    send_outbound called once with expected text + dedupe_key."""
    state_path = tmp_path / "data" / "daily_sync_state.json"
    _seed_high_confidence_state(state_path, high_flag=True)
    cfg = _enabled_config_with_push_user(enabled_config, user_id=42)
    cfg.c5_state_path = str(state_path)

    captured = _CapturedSend()
    monkeypatch.setattr(
        "alfred.transport.client.send_outbound", captured
    )

    rel = _seed_note(classifier_vault, "Urgent contract review")
    fake = _FakeLLM(response=json.dumps({
        "priority": "high",
        "action_hint": "respond",
        "reasoning": "named contact + urgent deadline",
    }))

    result = classify_record(
        vault_path=classifier_vault,
        note_rel_path=rel,
        inbox_content=_EMAIL_SAMPLE,
        config=cfg,
        llm_caller=fake,
    )

    assert result.priority == "high"
    assert result.pushed_to_telegram is True
    assert len(captured.calls) == 1
    call = captured.calls[0]
    assert call["user_id"] == 42
    assert call["dedupe_key"] == f"email-c5-{rel}"
    # Message contains the expected operator-readable lines. Subject
    # comes from the note's frontmatter (the seeded ``name`` field),
    # NOT from the inbox _EMAIL_SAMPLE — the classifier persists the
    # subject into frontmatter at curator-creation time, so c5 reads
    # the same source of truth as everything else downstream.
    assert "📬 High-priority email" in call["text"]
    assert "jamie@example.com" in call["text"]  # from _EMAIL_SAMPLE sender
    assert "Urgent contract review" in call["text"]  # subject (frontmatter name)
    assert "Action hint: respond" in call["text"]
    assert f"vault://{rel}" in call["text"]


def test_high_push_silent_when_gate_disabled(
    classifier_vault: Path,
    enabled_config: EmailClassifierConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Classifier verdict ``high`` + ``confidence.high: false`` →
    send_outbound NOT called. Frontmatter still persisted."""
    state_path = tmp_path / "data" / "daily_sync_state.json"
    _seed_high_confidence_state(state_path, high_flag=False)
    cfg = _enabled_config_with_push_user(enabled_config, user_id=42)
    cfg.c5_state_path = str(state_path)

    captured = _CapturedSend()
    monkeypatch.setattr(
        "alfred.transport.client.send_outbound", captured
    )

    rel = _seed_note(classifier_vault, "Important email pre-cal")
    fake = _FakeLLM(response=json.dumps({
        "priority": "high",
        "action_hint": "respond",
        "reasoning": "urgent",
    }))

    result = classify_record(
        vault_path=classifier_vault,
        note_rel_path=rel,
        inbox_content=_EMAIL_SAMPLE,
        config=cfg,
        llm_caller=fake,
    )

    assert result.priority == "high"
    assert result.pushed_to_telegram is False
    assert captured.calls == []
    # Frontmatter still got the high priority — push gate is independent
    # of the priority write.
    post = frontmatter.load(str(classifier_vault / rel))
    assert post.metadata["priority"] == "high"


def test_high_push_skipped_for_non_high_priority(
    classifier_vault: Path,
    enabled_config: EmailClassifierConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Classifier verdict ``medium`` + ``confidence.high: true`` →
    send_outbound NOT called. The gate fires ONLY on priority=high."""
    state_path = tmp_path / "data" / "daily_sync_state.json"
    _seed_high_confidence_state(state_path, high_flag=True)
    cfg = _enabled_config_with_push_user(enabled_config, user_id=42)
    cfg.c5_state_path = str(state_path)

    captured = _CapturedSend()
    monkeypatch.setattr(
        "alfred.transport.client.send_outbound", captured
    )

    rel = _seed_note(classifier_vault, "Routine medium-tier email")
    fake = _FakeLLM(response=json.dumps({
        "priority": "medium",
        "action_hint": None,
        "reasoning": "routine traffic",
    }))

    result = classify_record(
        vault_path=classifier_vault,
        note_rel_path=rel,
        inbox_content=_EMAIL_SAMPLE,
        config=cfg,
        llm_caller=fake,
    )

    assert result.priority == "medium"
    assert result.pushed_to_telegram is False
    assert captured.calls == []


def test_high_push_transport_failure_does_not_crash(
    classifier_vault: Path,
    enabled_config: EmailClassifierConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """send_outbound raises TransportUnavailable → classifier still
    returns the result with ``pushed_to_telegram=False``, no exception
    propagates, and the ``email_classifier.high_push_failed`` warning
    is logged.

    Uses ``structlog.testing.capture_logs`` rather than caplog because
    the push helper runs inside ``asyncio.run`` from the sync
    classifier — structlog's capture_logs is the canonical pattern
    for async/threaded code paths (see
    ``feedback_structlog_assertion_patterns.md``).
    """
    import structlog
    from alfred.transport.exceptions import TransportUnavailable

    state_path = tmp_path / "data" / "daily_sync_state.json"
    _seed_high_confidence_state(state_path, high_flag=True)
    cfg = _enabled_config_with_push_user(enabled_config, user_id=42)
    cfg.c5_state_path = str(state_path)

    captured = _CapturedSend(
        raise_exc=TransportUnavailable("upstream 503"),
    )
    monkeypatch.setattr(
        "alfred.transport.client.send_outbound", captured
    )

    rel = _seed_note(classifier_vault, "High when transport down")
    fake = _FakeLLM(response=json.dumps({
        "priority": "high",
        "action_hint": "respond",
        "reasoning": "urgent",
    }))

    with structlog.testing.capture_logs() as log_records:
        # The classifier must NEVER raise into the curator — even on
        # transport failure.
        result = classify_record(
            vault_path=classifier_vault,
            note_rel_path=rel,
            inbox_content=_EMAIL_SAMPLE,
            config=cfg,
            llm_caller=fake,
        )

    assert result.priority == "high"
    assert result.pushed_to_telegram is False
    # send_outbound was called once (the gate fired), it just failed.
    assert len(captured.calls) == 1

    failed = [
        r for r in log_records
        if r.get("event") == "email_classifier.high_push_failed"
    ]
    assert len(failed) == 1, (
        f"expected exactly one high_push_failed warning, "
        f"got {len(failed)}: {log_records}"
    )
    # Assert load-bearing fields per
    # feedback_log_emission_test_pattern.md — catches field renames or
    # drops, not just full-event drops.
    entry = failed[0]
    assert entry["path"] == rel
    assert entry["user_id"] == 42
    assert entry["error_type"] == "TransportUnavailable"
    assert "upstream 503" in entry.get("error", "")


def test_high_push_state_file_missing_treated_as_disabled(
    classifier_vault: Path,
    enabled_config: EmailClassifierConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Edge case mirroring the c6 fail-safe: state file missing → treat
    as flag=false (don't crash; don't push if state-mechanism is
    unreadable).

    A missing state file usually means daily_sync hasn't run yet, OR
    the operator reset calibration. Either way, the safest default is
    "don't push" rather than firing a push on a state we can't read.
    """
    cfg = _enabled_config_with_push_user(enabled_config, user_id=42)
    cfg.c5_state_path = str(
        tmp_path / "nonexistent" / "daily_sync_state.json"
    )

    captured = _CapturedSend()
    monkeypatch.setattr(
        "alfred.transport.client.send_outbound", captured
    )

    rel = _seed_note(classifier_vault, "High when state missing")
    fake = _FakeLLM(response=json.dumps({
        "priority": "high",
        "action_hint": "respond",
        "reasoning": "urgent",
    }))

    result = classify_record(
        vault_path=classifier_vault,
        note_rel_path=rel,
        inbox_content=_EMAIL_SAMPLE,
        config=cfg,
        llm_caller=fake,
    )

    assert result.priority == "high"
    assert result.pushed_to_telegram is False
    assert captured.calls == []


def test_high_push_message_format(
    classifier_vault: Path,
    enabled_config: EmailClassifierConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rendered Telegram text contains sender + subject + vault:// line
    and total length is under the 800-char operator-deliverable cap.

    Pins the message format so a future refactor that drops the
    sender line or the vault:// URL surfaces here rather than in
    operator-facing Telegram noise.
    """
    state_path = tmp_path / "data" / "daily_sync_state.json"
    _seed_high_confidence_state(state_path, high_flag=True)
    cfg = _enabled_config_with_push_user(enabled_config, user_id=42)
    cfg.c5_state_path = str(state_path)

    captured = _CapturedSend()
    monkeypatch.setattr(
        "alfred.transport.client.send_outbound", captured
    )

    rel = _seed_note(classifier_vault, "Format check")
    fake = _FakeLLM(response=json.dumps({
        "priority": "high",
        "action_hint": "respond",
        "reasoning": "urgent",
    }))

    classify_record(
        vault_path=classifier_vault,
        note_rel_path=rel,
        inbox_content=_EMAIL_SAMPLE,
        config=cfg,
        llm_caller=fake,
    )

    assert len(captured.calls) == 1
    text = captured.calls[0]["text"]
    # Required structural lines per the dispatch spec.
    assert "📬 High-priority email" in text
    assert "From:" in text
    assert "Subject:" in text
    assert "Action hint:" in text
    assert f"🔗 vault://{rel}" in text
    # Total length cap — operator-deliverable, well under Telegram's
    # 4096-char hard limit.
    assert len(text) < 800, (
        f"rendered message exceeds 800 chars: len={len(text)}"
    )


def test_high_push_skipped_when_no_user_id_configured(
    classifier_vault: Path,
    enabled_config: EmailClassifierConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ``primary_telegram_user_id`` on the config (i.e. operator has
    no telegram section configured) → push is silently skipped even
    when the gate is on. Mirrors brief's graceful-no-op behaviour."""
    state_path = tmp_path / "data" / "daily_sync_state.json"
    _seed_high_confidence_state(state_path, high_flag=True)
    # NOTE: deliberately NOT stamping primary_telegram_user_id — it
    # stays as the dataclass default of None.
    enabled_config.c5_state_path = str(state_path)

    captured = _CapturedSend()
    monkeypatch.setattr(
        "alfred.transport.client.send_outbound", captured
    )

    rel = _seed_note(classifier_vault, "High with no telegram configured")
    fake = _FakeLLM(response=json.dumps({
        "priority": "high",
        "action_hint": "respond",
        "reasoning": "urgent",
    }))

    result = classify_record(
        vault_path=classifier_vault,
        note_rel_path=rel,
        inbox_content=_EMAIL_SAMPLE,
        config=enabled_config,
        llm_caller=fake,
    )

    assert result.priority == "high"
    assert result.pushed_to_telegram is False
    assert captured.calls == []


# ---------------------------------------------------------------------------
# Synth-marker gate (Ship 5, empty-body arc 2026-06-09)
# ---------------------------------------------------------------------------
#
# The mail extract layer marks bodies it could not recover (image-only
# HTML, invisible-Unicode padding, upstream truncation) with one of two
# byte-strings as the first line of a synthesized body. The classifier
# runs as code, not an agent prompt, so Ship 4's curator + distiller
# SKILL gating doesn't reach it. These tests pin that the classifier
# short-circuits to ``priority: low`` WITHOUT calling the LLM (or the
# high-priority-sender override / c5 push / c6 quarantine) when a synth
# marker is present, and emits the grep-able
# ``email_classifier.skip_synth_marked`` log.


def _synth_email_inbox(marker: str) -> str:
    """Build an email-shaped inbox body carrying a synth ``marker``.

    Carries a ``**From:**`` line so ``is_email_inbox`` routes it, and the
    marker on the body line so ``_detect_synth_marker`` fires. Mirrors
    the shape the mail extract layer produces below the ``---`` header
    separator.
    """
    return dedent(
        f"""\
        **From:** newsletter@example.com
        **Subject:** Something happened

        {marker}

        Subject: Something happened
        From: newsletter@example.com
        Account: live
        """
    )


def test_synth_marked_image_only_skips_classification(
    classifier_vault: Path,
    enabled_config: EmailClassifierConfig,
) -> None:
    """Image-only synth marker → priority=low, LLM never called."""
    from alfred.mail import extract

    rel = _seed_note(
        classifier_vault,
        "Pizza Hut order update",
        body=f"# Pizza Hut order update\n\n{extract.SYNTH_MARKER_IMAGE_ONLY}\n",
    )
    fake = _FakeLLM(response=json.dumps({
        "priority": "high",
        "action_hint": "calendar",
        "reasoning": "should never be used",
    }))

    result = classify_record(
        vault_path=classifier_vault,
        note_rel_path=rel,
        inbox_content=_synth_email_inbox(extract.SYNTH_MARKER_IMAGE_ONLY),
        config=enabled_config,
        llm_caller=fake,
    )

    assert result.priority == "low"
    assert result.action_hint is None
    assert result.written_to == rel
    # LLM was NOT called — the gate short-circuits before the caller.
    assert len(fake.calls) == 0
    # On-disk frontmatter reflects the gate decision.
    post = frontmatter.load(str(classifier_vault / rel))
    assert post.metadata["priority"] == "low"
    assert post.metadata["action_hint"] is None
    assert "Synth-marked empty body" in post.metadata["priority_reasoning"]


def test_synth_marked_upstream_truncated_skips_classification(
    classifier_vault: Path,
    enabled_config: EmailClassifierConfig,
) -> None:
    """Upstream-truncated synth marker → priority=low, LLM never called."""
    from alfred.mail import extract

    rel = _seed_note(
        classifier_vault,
        "Newsletter blast",
        body=f"# Newsletter blast\n\n{extract.SYNTH_MARKER_UPSTREAM_TRUNCATED}\n",
    )
    fake = _FakeLLM(response=json.dumps({
        "priority": "medium",
        "action_hint": None,
        "reasoning": "should never be used",
    }))

    result = classify_record(
        vault_path=classifier_vault,
        note_rel_path=rel,
        inbox_content=_synth_email_inbox(extract.SYNTH_MARKER_UPSTREAM_TRUNCATED),
        config=enabled_config,
        llm_caller=fake,
    )

    assert result.priority == "low"
    assert len(fake.calls) == 0
    post = frontmatter.load(str(classifier_vault / rel))
    assert post.metadata["priority"] == "low"


def test_synth_marker_in_inbox_content_only_still_skips(
    classifier_vault: Path,
    enabled_config: EmailClassifierConfig,
) -> None:
    """Marker absent from note body but present in inbox content → skip.

    The curator may reshape the note body; the raw inbox content is the
    authoritative second signal. The gate checks both.
    """
    from alfred.mail import extract

    # Note body is clean (no marker) — simulate a curator that dropped
    # the marker line when reshaping the note.
    rel = _seed_note(
        classifier_vault,
        "Reshaped note",
        body="# Reshaped note\n\nSome curator-written summary text.\n",
    )
    fake = _FakeLLM(response=json.dumps({
        "priority": "high",
        "action_hint": None,
        "reasoning": "should never be used",
    }))

    result = classify_record(
        vault_path=classifier_vault,
        note_rel_path=rel,
        inbox_content=_synth_email_inbox(extract.SYNTH_MARKER_IMAGE_ONLY),
        config=enabled_config,
        llm_caller=fake,
    )

    assert result.priority == "low"
    assert len(fake.calls) == 0


def test_synth_marked_skips_high_priority_sender_override(
    classifier_vault: Path,
    enabled_config: EmailClassifierConfig,
) -> None:
    """Synth-marked body from a flagged priority sender → still low.

    We never escalate (or push / quarantine) on absent content, even
    from a ``high_priority_sender: true`` contact. The override must
    NOT fire because the gate short-circuits before the override step.
    """
    from alfred.mail import extract

    _seed_person_with_high_priority_flag(
        classifier_vault, "VIP Sender",
        email="vip@example.com",
        high_priority_sender=True,
    )
    rel = _seed_note(
        classifier_vault,
        "VIP image-only blast",
        body=f"# VIP image-only blast\n\n{extract.SYNTH_MARKER_IMAGE_ONLY}\n",
    )
    # Inbox content From-line matches the flagged contact's address.
    inbox = dedent(
        f"""\
        **From:** vip@example.com
        **Subject:** Exclusive

        {extract.SYNTH_MARKER_IMAGE_ONLY}

        Subject: Exclusive
        From: vip@example.com
        Account: live
        """
    )
    fake = _FakeLLM(response=json.dumps({
        "priority": "medium",
        "action_hint": None,
        "reasoning": "should never be used",
    }))

    result = classify_record(
        vault_path=classifier_vault,
        note_rel_path=rel,
        inbox_content=inbox,
        config=enabled_config,
        llm_caller=fake,
    )

    # Override did NOT fire — priority stays low, audit fields untouched.
    assert result.priority == "low"
    assert result.override_applied is False
    assert result.llm_priority is None
    assert result.pushed_to_telegram is False
    assert result.quarantined_to == ""
    assert len(fake.calls) == 0


def test_skip_synth_marked_log_emission(
    classifier_vault: Path,
    enabled_config: EmailClassifierConfig,
) -> None:
    """Pin the ``email_classifier.skip_synth_marked`` log + its fields.

    Per ``feedback_log_emission_test_pattern.md`` — the gate's
    intentionally-left-blank log must be driven by the production code
    path and its key fields pinned so a refactor that drops the event
    or renames a field is caught at test time.
    """
    import structlog

    from alfred.mail import extract

    rel = _seed_note(
        classifier_vault,
        "Logged synth note",
        body=f"# Logged synth note\n\n{extract.SYNTH_MARKER_UPSTREAM_TRUNCATED}\n",
    )
    fake = _FakeLLM(response="should never be used")

    with structlog.testing.capture_logs() as captured:
        classify_record(
            vault_path=classifier_vault,
            note_rel_path=rel,
            inbox_content=_synth_email_inbox(
                extract.SYNTH_MARKER_UPSTREAM_TRUNCATED
            ),
            config=enabled_config,
            llm_caller=fake,
        )

    matches = [
        c for c in captured
        if c.get("event") == "email_classifier.skip_synth_marked"
    ]
    assert len(matches) == 1, (
        f"expected exactly one skip_synth_marked event, got "
        f"{[c.get('event') for c in captured]!r}"
    )
    event = matches[0]
    assert event["path"] == rel
    assert event["marker"] == extract.SYNTH_MARKER_UPSTREAM_TRUNCATED
    assert event["priority"] == "low"


def test_non_synth_body_classifies_normally(
    classifier_vault: Path,
    enabled_config: EmailClassifierConfig,
) -> None:
    """Negative guard: a normal body still calls the LLM and tiers.

    Pins that the substring detection does NOT over-fire on ordinary
    email content — the gate only triggers on the actual marker strings.
    """
    rel = _seed_note(classifier_vault, "Ordinary email note")
    fake = _FakeLLM(response=json.dumps({
        "priority": "medium",
        "action_hint": "archive",
        "reasoning": "routine update from a known vendor",
    }))

    result = classify_record(
        vault_path=classifier_vault,
        note_rel_path=rel,
        inbox_content=_EMAIL_SAMPLE,
        config=enabled_config,
        llm_caller=fake,
    )

    # Normal path: LLM was called and its verdict stuck.
    assert result.priority == "medium"
    assert result.action_hint == "archive"
    assert len(fake.calls) == 1
