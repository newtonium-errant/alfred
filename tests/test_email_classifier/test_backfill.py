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


# ---------------------------------------------------------------------------
# run_backfill — --reclassify flag (2026-05-31)
# ---------------------------------------------------------------------------
#
# Operator use case: after a corpus / few-shot fix lands, the historical
# vault holds records classified under the broken prompt. Default
# backfill skips records with priority already set (the c1 "fill in
# missing field" use case). --reclassify bypasses that gate so the
# improved classifier rewrites historical verdicts.
#
# Contract:
# - reclassify=False (default) → existing skip-on-existing-priority
#   behavior preserved (regression-pin)
# - reclassify=True → re-evaluate records with priority set, overwrite
#   on disk, count verdict CHANGES (not no-op re-confirms) on summary
#   .reclassified_verdict_changes
# - Verdict change → info-level email_classifier.backfill.reclassified
#   log event with old_priority + new_priority fields for grep
# - reclassify+dry_run → counts re-evaluations without LLM call or
#   write (dry-run beats reclassify when both passed)
#
# c6 quarantine isolation: the classifier shipped 2026-05-31 with a
# spam-quarantine layer that reads ``data/daily_sync_state.json`` and
# moves spam-classified records to ``quarantine/spam/<YYYY-MM>/`` when
# ``confidence.spam: true``. ``EmailClassifierConfig.quarantine_state_path``
# defaults to the relative ``./data/daily_sync_state.json``, which
# resolves against the test's cwd at runtime — if any prior test or
# live state has flipped the flag to true, reclassify tests whose
# classifier returns spam will see their records moved out from under
# them (review on 521e578 caught this with a FileNotFoundError on the
# post-test ``frontmatter.load(vault / rel)`` assertion).
#
# ``_isolate_quarantine_state`` writes an explicit ``confidence.spam:
# false`` to a tmp-path state file AND points the test's config at
# it, so the quarantine gate evaluates to False regardless of the
# live state. Every reclassify test that uses a spam classifier
# verdict MUST call this — the regression-pin
# ``test_backfill_reclassify_isolated_from_c6_quarantine_flag``
# below also pins the contract explicitly.


def _isolate_quarantine_state(
    tmp_path: Path,
    config: EmailClassifierConfig,
    *,
    spam_flag: bool = False,
) -> Path:
    """Write a daily_sync state file with the requested spam flag AND
    point the config's quarantine_state_path at it.

    Default ``spam_flag=False`` makes the quarantine layer a no-op —
    the configured-flag-false branch returns the record stays at its
    normal vault location even when the classifier returns spam.

    Returns the state-file path for tests that want to assert on its
    content (e.g., the isolation-pin test).
    """
    state_path = tmp_path / "data" / "daily_sync_state.json"
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
    config.quarantine_state_path = str(state_path)
    return state_path


def test_backfill_reclassify_processes_records_with_existing_priority(
    vault: Path,
    enabled_config: EmailClassifierConfig,
    tmp_path: Path,
) -> None:
    """Record with ``priority: low`` set; reclassify=True + classifier
    returns ``spam`` → frontmatter priority overwritten to ``spam``.

    Isolated from c6 quarantine (see ``_isolate_quarantine_state``
    docstring + the dedicated isolation-pin test below) — without
    this, a live ``data/daily_sync_state.json`` with ``spam: true``
    would trigger the c6 quarantine layer, MOVE the record to
    ``quarantine/spam/<YYYY-MM>/``, and break the post-test
    ``frontmatter.load(vault / rel)`` assertion with FileNotFoundError
    (review caught this on the original 521e578)."""
    _isolate_quarantine_state(tmp_path, enabled_config, spam_flag=False)
    rel = _seed_note(
        vault,
        "Mockingbird marketing — was misclassified low",
        body=_EMAIL_BODY,
        priority="low",  # pre-reclassify verdict from broken few-shot
    )
    fake = _FakeLLM(response=_classifier_response(
        priority="spam",
        reasoning="unsolicited commercial, retroactive verdict",
    ))

    summary = run_backfill(
        vault_path=vault,
        config=enabled_config,
        llm_caller=fake,
        reclassify=True,
    )

    # Re-evaluated, not skipped.
    assert summary.classified == 1
    assert summary.skipped_already_done == 0, (
        "reclassify mode must bypass the has_priority gate; "
        "skipped_already_done should stay 0"
    )
    # Verdict actually changed (low → spam) so the change counter ticks.
    assert summary.reclassified_verdict_changes == 1
    assert len(fake.calls) == 1

    # On-disk verification: priority overwritten.
    post = frontmatter.load(str(vault / rel))
    assert post.metadata["priority"] == "spam"
    assert "retroactive" in post.metadata.get("priority_reasoning", "")
    # Quarantine did NOT fire — confirms _isolate_quarantine_state
    # actually held the c6 layer off. If quarantine had fired, the
    # record would be at quarantine/spam/<YYYY-MM>/ and this
    # directory would exist; its absence pins isolation.
    assert not (vault / "quarantine").exists()


def test_backfill_reclassify_false_default_preserves_existing_behavior(
    vault: Path,
    enabled_config: EmailClassifierConfig,
    tmp_path: Path,
) -> None:
    """Regression-pin for the pre-2026-05-31 contract: default
    reclassify=False keeps the has_priority skip-gate, no LLM call
    fires for already-classified records. A future change that flips
    the default to True silently re-classifies the entire vault on
    every cron-driven backfill run — this test catches that drift.

    Isolated from c6 quarantine for defense-in-depth (cheap; cuts
    contamination risk if a future edit changes the fake LLM
    response to spam)."""
    _isolate_quarantine_state(tmp_path, enabled_config, spam_flag=False)
    rel = _seed_note(
        vault,
        "Already classified — must not re-evaluate",
        body=_EMAIL_BODY,
        priority="low",
    )
    fake = _FakeLLM(response="should not be called")

    # reclassify NOT passed → defaults to False
    summary = run_backfill(
        vault_path=vault,
        config=enabled_config,
        llm_caller=fake,
    )

    # Pre-2026-05-31 behavior preserved.
    assert summary.skipped_already_done == 1
    assert summary.classified == 0
    assert summary.reclassified_verdict_changes == 0
    assert len(fake.calls) == 0
    # On-disk: priority unchanged.
    post = frontmatter.load(str(vault / rel))
    assert post.metadata["priority"] == "low"


def test_backfill_reclassify_logs_verdict_change(
    vault: Path,
    enabled_config: EmailClassifierConfig,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """When a reclassify re-evaluation produces a DIFFERENT priority
    than the on-disk value, info-level
    ``email_classifier.backfill.reclassified`` fires with both
    priorities pinned so operator log review can grep verdict changes
    apart from no-op re-confirms.

    Mirrors the capsys-stdout pattern of
    ``test_backfill_progress_log_fires_at_interval`` (the project
    routes structlog through ConsoleRenderer; capsys captures
    rendered events from stdout). Isolated from c6 quarantine —
    classifier returns spam, which would otherwise trigger the move
    + pollute capsys output with the quarantine_spam log line.
    """
    _isolate_quarantine_state(tmp_path, enabled_config, spam_flag=False)
    _seed_note(
        vault, "Verdict change candidate",
        body=_EMAIL_BODY, priority="medium",
    )
    fake = _FakeLLM(response=_classifier_response(
        priority="spam",  # different from on-disk "medium"
        reasoning="retroactive spam under corrected few-shot",
    ))

    run_backfill(
        vault_path=vault,
        config=enabled_config,
        llm_caller=fake,
        reclassify=True,
        # Disable the progress log so its events don't pollute the
        # capsys output we're grepping.
        progress_every=0,
    )

    captured = capsys.readouterr()
    # Event name present in rendered output.
    assert "email_classifier.backfill.reclassified" in captured.out, (
        f"reclassified log event missing from capsys output:\n--- stdout ---\n{captured.out}"
    )
    # Both old + new priorities surfaced as structured fields. Pin
    # both values so a future refactor that drops either field fails
    # this test (per feedback_log_emission_test_pattern.md).
    assert "old_priority=medium" in captured.out or "old_priority='medium'" in captured.out
    assert "new_priority=spam" in captured.out or "new_priority='spam'" in captured.out


def test_backfill_reclassify_dry_run_does_not_write(
    vault: Path,
    enabled_config: EmailClassifierConfig,
    tmp_path: Path,
) -> None:
    """reclassify=True + dry_run=True → counts re-evaluations as
    candidates, but makes NO LLM call and writes NO frontmatter.
    dry-run beats reclassify when both passed (matches the
    existing dry-run-beats-real-call composition of --dry-run +
    --limit in non-reclassify mode).

    Isolated from c6 quarantine for defense-in-depth (cheap; dry-run
    skips the LLM call so the quarantine path can't fire today, but
    a future bug that flips the dry-run-beats-reclassify ordering
    would otherwise contaminate this test)."""
    _isolate_quarantine_state(tmp_path, enabled_config, spam_flag=False)
    rel = _seed_note(
        vault, "Dry-run reclassify candidate",
        body=_EMAIL_BODY, priority="low",
    )
    fake = _FakeLLM(response="should not be called")

    summary = run_backfill(
        vault_path=vault,
        config=enabled_config,
        llm_caller=fake,
        reclassify=True,
        dry_run=True,
    )

    # Record was a candidate (would be re-evaluated in a live run).
    assert summary.candidates == 1
    # But no LLM call, no classification, no verdict-change accounting.
    assert summary.classified == 0
    assert summary.reclassified_verdict_changes == 0
    assert len(fake.calls) == 0
    # On-disk: priority untouched.
    post = frontmatter.load(str(vault / rel))
    assert post.metadata["priority"] == "low"


def test_backfill_reclassify_isolated_from_c6_quarantine_flag(
    vault: Path,
    enabled_config: EmailClassifierConfig,
    tmp_path: Path,
) -> None:
    """Cross-isolation regression-pin (review-fix on 521e578).

    The c6 spam-quarantine layer (cab582c) reads
    ``data/daily_sync_state.json`` for the ``confidence.spam`` flag
    and, when true, moves spam-classified records to
    ``quarantine/spam/<YYYY-MM>/`` BEFORE this test's frontmatter
    assertions run. ``EmailClassifierConfig.quarantine_state_path``
    defaults to ``./data/daily_sync_state.json`` (relative path
    resolving against cwd at runtime), so a live state file OR
    earlier-test contamination could flip the gate.

    This test pins the contract: when the quarantine_state_path is
    explicitly seeded with ``spam: false``, a reclassify run whose
    classifier returns spam does NOT trigger the quarantine — the
    record stays at ``note/<file>.md``. Failure of this test means
    either (a) ``_isolate_quarantine_state`` is broken, or (b) the
    quarantine gate stopped honoring the spam=false branch.

    Counterpart test in tests/test_email_classifier/test_classifier.py
    (``test_classifier_does_not_quarantine_spam_when_flag_false``)
    pins the same gate at the classifier-call layer; this pins it at
    the backfill-via-classifier layer."""
    state_path = _isolate_quarantine_state(
        tmp_path, enabled_config, spam_flag=False,
    )
    rel = _seed_note(
        vault, "Spam classified during isolated reclassify",
        body=_EMAIL_BODY, priority="low",
    )
    fake = _FakeLLM(response=_classifier_response(
        priority="spam",
        reasoning="isolation-pin classifier verdict",
    ))

    summary = run_backfill(
        vault_path=vault,
        config=enabled_config,
        llm_caller=fake,
        reclassify=True,
    )

    # Classification ran (spam verdict reached the writer).
    assert summary.classified == 1
    assert summary.reclassified_verdict_changes == 1

    # On-disk: record is STILL at the original note/ path. Quarantine
    # did NOT fire because the state file we wrote has spam=false.
    assert (vault / rel).exists(), (
        "record was MOVED out of note/ — isolation broken; check that "
        "_isolate_quarantine_state actually points config at the "
        "tmp state file, and that the file contains spam=false"
    )
    # Frontmatter rewritten with new spam verdict.
    post = frontmatter.load(str(vault / rel))
    assert post.metadata["priority"] == "spam"

    # Defense-in-depth: no quarantine directory tree was created.
    assert not (vault / "quarantine").exists()

    # Sanity: the state file we wrote is the one the config points at.
    # Pin so a future change to _isolate_quarantine_state can't silently
    # break the wiring (e.g., if the helper stops updating the config).
    assert enabled_config.quarantine_state_path == str(state_path)
