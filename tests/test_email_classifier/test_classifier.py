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
