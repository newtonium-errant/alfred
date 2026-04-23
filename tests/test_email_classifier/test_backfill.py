"""Tests for the email_classifier backfill command (c1.5).

Covers the contract from the c1.5 spec:

- Skips records that already carry a ``priority`` frontmatter field.
- Email-only filter excludes notes that don't look email-derived.
- ``dry_run=True`` makes no LLM calls and writes nothing.
- ``limit`` caps the number of *classified* records (skips don't count).
- Frontmatter write produces ``priority`` + ``action_hint`` fields.
- Classifier failure (LLM raises) is logged + counted as error + run continues.
- Progress logging fires every N records.

No real LLM call happens — the backfill takes the same ``llm_caller``
callable the classifier accepts. Tests inject fakes directly.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import frontmatter
import pytest

from alfred.email_classifier import (
    BackfillSummary,
    EmailClassifierConfig,
    is_email_derived_note,
    run_backfill,
)
from alfred.email_classifier.vault_helpers import reset_contacts_cache


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    reset_contacts_cache()
    yield
    reset_contacts_cache()


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    """Minimal vault with the dirs the classifier touches."""
    v = tmp_path / "vault"
    v.mkdir()
    for sub in ("note", "person", "org", "task", "inbox"):
        (v / sub).mkdir()
    return v


@pytest.fixture
def enabled_config() -> EmailClassifierConfig:
    cfg = EmailClassifierConfig(enabled=True)
    cfg.anthropic.api_key = "DUMMY_ANTHROPIC_TEST_KEY"
    return cfg


@dataclass
class _FakeLLM:
    """Records calls and returns a canned response (or raises if set)."""

    response: str = ""
    raise_on_call: Exception | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    def __call__(
        self,
        system: str,
        user: str,
        config: EmailClassifierConfig,
    ) -> str:
        self.calls.append({"system": system, "user": user})
        if self.raise_on_call is not None:
            raise self.raise_on_call
        return self.response


def _seed_note(
    vault: Path,
    name: str,
    *,
    body: str = "",
    subtype: str | None = "reference",
    description: str = "",
    priority: str | None = None,
    extra_meta: dict[str, Any] | None = None,
) -> str:
    """Write a note record. Return the rel path."""
    fm: dict[str, Any] = {
        "type": "note",
        "name": name,
        "created": "2026-04-22",
        "tags": [],
        "related": [],
        "description": description,
    }
    if subtype is not None:
        fm["subtype"] = subtype
    if priority is not None:
        fm["priority"] = priority
    if extra_meta:
        fm.update(extra_meta)
    post = frontmatter.Post(body or f"# {name}\n", **fm)
    (vault / "note" / f"{name}.md").write_text(
        frontmatter.dumps(post) + "\n",
        encoding="utf-8",
    )
    return f"note/{name}.md"


def _classifier_response(
    priority: str = "medium",
    action_hint: str | None = None,
    reasoning: str = "",
) -> str:
    return json.dumps({
        "priority": priority,
        "action_hint": action_hint,
        "reasoning": reasoning,
    })


# Email-shape body content the curator might have produced.
_EMAIL_BODY = (
    "**From:** newsletter@example.com\n"
    "**Subject:** Weekly digest\n\n"
    "Marketing newsletter content. Click here to unsubscribe."
)


# ---------------------------------------------------------------------------
# is_email_derived_note — the per-record filter
# ---------------------------------------------------------------------------


def test_is_email_derived_note_subtype_reference_qualifies() -> None:
    """``subtype: reference`` is the canonical email-note marker."""
    assert is_email_derived_note({"subtype": "reference"}, "any body") is True


def test_is_email_derived_note_body_with_from_header_qualifies() -> None:
    body = "**From:** somebody@example.com\nSome body content."
    assert is_email_derived_note({"subtype": None}, body) is True


def test_is_email_derived_note_body_with_email_address_qualifies() -> None:
    body = "Andrew received a note from joe@example.com about RRTS."
    assert is_email_derived_note({"subtype": None}, body) is True


def test_is_email_derived_note_body_with_keyword_qualifies() -> None:
    body = "This newsletter is about kettlebell training."
    assert is_email_derived_note({"subtype": None}, body) is True


def test_is_email_derived_note_pure_voice_memo_does_not_qualify() -> None:
    """No subtype, no email markers ⇒ skip."""
    body = "Andrew rambling about kitchen renovation budget options."
    assert is_email_derived_note({"subtype": None}, body) is False


def test_is_email_derived_note_description_can_qualify() -> None:
    """Email signal in the description (not body) still qualifies."""
    metadata = {"subtype": None, "description": "Forwarded email from Jamie."}
    assert is_email_derived_note(metadata, "short body") is True


# ---------------------------------------------------------------------------
# run_backfill — already-classified records skipped
# ---------------------------------------------------------------------------


def test_backfill_skips_already_classified(
    vault: Path, enabled_config: EmailClassifierConfig
) -> None:
    """Records with a non-empty ``priority`` field are skipped, no LLM call."""
    _seed_note(vault, "Old classified", body=_EMAIL_BODY, priority="high")
    fake = _FakeLLM(response="should not be called")

    summary = run_backfill(
        vault_path=vault,
        config=enabled_config,
        llm_caller=fake,
    )

    assert summary.skipped_already_done == 1
    assert summary.classified == 0
    assert summary.candidates == 0
    assert len(fake.calls) == 0


def test_backfill_does_classify_unclassified_sentinel_records(
    vault: Path, enabled_config: EmailClassifierConfig
) -> None:
    """A record carrying the ``unclassified`` sentinel is treated as already-done.

    c3 calibration handles re-classification of the sentinel — backfill
    should NOT compete with it. Once any non-empty ``priority`` exists,
    the record is out of scope.
    """
    _seed_note(vault, "Sentinel note", body=_EMAIL_BODY, priority="unclassified")
    fake = _FakeLLM(response="should not be called")

    summary = run_backfill(
        vault_path=vault,
        config=enabled_config,
        llm_caller=fake,
    )

    assert summary.skipped_already_done == 1
    assert summary.classified == 0
    assert len(fake.calls) == 0


# ---------------------------------------------------------------------------
# run_backfill — email-only filter
# ---------------------------------------------------------------------------


def test_backfill_skips_non_email_notes(
    vault: Path, enabled_config: EmailClassifierConfig
) -> None:
    """Voice-memo style notes (no subtype, no email markers) are skipped."""
    _seed_note(
        vault,
        "Voice memo",
        body="Andrew talking about kitchen renovation options.",
        subtype=None,
    )
    fake = _FakeLLM(response="should not be called")

    summary = run_backfill(
        vault_path=vault,
        config=enabled_config,
        llm_caller=fake,
    )

    assert summary.skipped_not_email == 1
    assert summary.classified == 0
    assert len(fake.calls) == 0


def test_backfill_classifies_email_derived_only(
    vault: Path, enabled_config: EmailClassifierConfig
) -> None:
    """Mixed vault: only the email-derived note gets the LLM call."""
    rel_email = _seed_note(vault, "Email note", body=_EMAIL_BODY)
    _seed_note(
        vault,
        "Voice note",
        body="Pure rambling about workouts.",
        subtype=None,
    )
    fake = _FakeLLM(response=_classifier_response("low", "archive"))

    summary = run_backfill(
        vault_path=vault,
        config=enabled_config,
        llm_caller=fake,
    )

    assert summary.classified == 1
    assert summary.skipped_not_email == 1
    assert len(fake.calls) == 1

    # Verify on-disk write happened on the email note ONLY
    email_post = frontmatter.load(str(vault / rel_email))
    assert email_post.metadata["priority"] == "low"
    assert email_post.metadata["action_hint"] == "archive"

    voice_post = frontmatter.load(str(vault / "note" / "Voice note.md"))
    assert "priority" not in voice_post.metadata


# ---------------------------------------------------------------------------
# run_backfill — dry-run
# ---------------------------------------------------------------------------


def test_backfill_dry_run_makes_no_llm_calls(
    vault: Path, enabled_config: EmailClassifierConfig
) -> None:
    rel = _seed_note(vault, "Email note", body=_EMAIL_BODY)
    fake = _FakeLLM(response="should not be called")

    summary = run_backfill(
        vault_path=vault,
        config=enabled_config,
        llm_caller=fake,
        dry_run=True,
    )

    assert summary.candidates == 1
    assert summary.classified == 0
    assert len(fake.calls) == 0

    # Frontmatter unchanged
    post = frontmatter.load(str(vault / rel))
    assert "priority" not in post.metadata


def test_backfill_dry_run_respects_limit(
    vault: Path, enabled_config: EmailClassifierConfig
) -> None:
    """In dry-run, --limit caps the *candidate* count."""
    for i in range(5):
        _seed_note(vault, f"Email note {i}", body=_EMAIL_BODY)
    fake = _FakeLLM(response="unused")

    summary = run_backfill(
        vault_path=vault,
        config=enabled_config,
        llm_caller=fake,
        dry_run=True,
        limit=2,
    )

    assert summary.candidates == 2
    assert summary.classified == 0


# ---------------------------------------------------------------------------
# run_backfill — limit
# ---------------------------------------------------------------------------


def test_backfill_limit_caps_classifications(
    vault: Path, enabled_config: EmailClassifierConfig
) -> None:
    """Limit caps actual classifications; skips don't burn the budget."""
    # 2 already-classified, 5 backfill candidates
    for i in range(2):
        _seed_note(
            vault, f"Already done {i}", body=_EMAIL_BODY, priority="high",
        )
    for i in range(5):
        _seed_note(vault, f"Pending {i}", body=_EMAIL_BODY)

    fake = _FakeLLM(response=_classifier_response("medium"))

    summary = run_backfill(
        vault_path=vault,
        config=enabled_config,
        llm_caller=fake,
        limit=3,
    )

    # Stops after 3 classifications even though 5 candidates exist
    assert summary.classified == 3
    assert summary.skipped_already_done == 2
    assert len(fake.calls) == 3


# ---------------------------------------------------------------------------
# run_backfill — frontmatter write
# ---------------------------------------------------------------------------


def test_backfill_writes_priority_and_action_hint(
    vault: Path, enabled_config: EmailClassifierConfig
) -> None:
    rel = _seed_note(vault, "Email A", body=_EMAIL_BODY)
    fake = _FakeLLM(response=_classifier_response(
        "high", "calendar", "RSVP requested",
    ))

    summary = run_backfill(
        vault_path=vault,
        config=enabled_config,
        llm_caller=fake,
    )

    assert summary.classified == 1

    post = frontmatter.load(str(vault / rel))
    assert post.metadata["priority"] == "high"
    assert post.metadata["action_hint"] == "calendar"
    assert "RSVP" in post.metadata["priority_reasoning"]


def test_backfill_writes_sentinel_on_malformed_llm_output(
    vault: Path, enabled_config: EmailClassifierConfig
) -> None:
    """Garbled LLM output ⇒ unclassified sentinel written. Run continues."""
    _seed_note(vault, "Garbled note", body=_EMAIL_BODY)
    fake = _FakeLLM(response="not even close to JSON")

    summary = run_backfill(
        vault_path=vault,
        config=enabled_config,
        llm_caller=fake,
    )

    # classify_record returned a sentinel result — backfill counts it as
    # classified (vs. errored). The classifier itself wrote the sentinel.
    assert summary.classified == 1
    post = frontmatter.load(str(vault / "note" / "Garbled note.md"))
    assert post.metadata["priority"] == "unclassified"


# ---------------------------------------------------------------------------
# run_backfill — error path
# ---------------------------------------------------------------------------


def test_backfill_continues_when_llm_caller_raises(
    vault: Path, enabled_config: EmailClassifierConfig
) -> None:
    """LLM caller raising ⇒ logged + counted as error + run continues."""
    _seed_note(vault, "First", body=_EMAIL_BODY)
    _seed_note(vault, "Second", body=_EMAIL_BODY)

    # Caller that always raises
    fake = _FakeLLM(raise_on_call=RuntimeError("simulated SDK failure"))

    summary = run_backfill(
        vault_path=vault,
        config=enabled_config,
        llm_caller=fake,
    )

    # Both records errored; run did not abort
    assert summary.errors == 2
    assert summary.classified == 0
    assert len(summary.error_paths) == 2


# ---------------------------------------------------------------------------
# Misc — vault has no note dir
# ---------------------------------------------------------------------------


def test_backfill_missing_note_dir_returns_empty_summary(
    tmp_path: Path, enabled_config: EmailClassifierConfig
) -> None:
    """No vault/note/ ⇒ empty summary, no crash."""
    fake = _FakeLLM(response="unused")
    summary = run_backfill(
        vault_path=tmp_path,
        config=enabled_config,
        llm_caller=fake,
    )

    assert isinstance(summary, BackfillSummary)
    assert summary.classified == 0
    assert summary.skipped_already_done == 0
    assert summary.skipped_not_email == 0


def test_backfill_progress_log_fires_at_interval(
    vault: Path,
    enabled_config: EmailClassifierConfig,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Progress log fires every N records. Use small interval for the test.

    structlog renders to stdout via the default ConsoleRenderer; capture
    via ``capsys`` rather than ``caplog`` since the project uses
    structlog's processor chain (see curator/utils.py setup_logging).
    """
    # Seed enough records that progress fires at least once
    for i in range(6):
        _seed_note(
            vault, f"Email {i}", body=_EMAIL_BODY, priority="low",  # already done
        )
    fake = _FakeLLM(response="unused")

    run_backfill(
        vault_path=vault,
        config=enabled_config,
        llm_caller=fake,
        progress_every=3,
    )

    captured = capsys.readouterr()
    # 6 records, every 3 → progress fires at idx=3 and idx=6
    progress_count = captured.out.count("email_classifier.backfill.progress")
    assert progress_count >= 2, (
        f"expected ≥2 progress logs, got {progress_count}\n--- stdout ---\n{captured.out}"
    )
