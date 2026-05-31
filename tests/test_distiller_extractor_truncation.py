"""Tests for the distiller extractor's max_tokens truncation handling.

Filed 2026-05-31 after the Path C Phase 1.5 spike surfaced a live
production bug: ``AnthropicConfig.max_tokens=4096`` default silently
truncated extractor JSON on long records (e.g.,
``session/Voice Chat and Calibration Design 2026-04-15.md`` at
650 lines: 0 learnings extracted at 4096; 26 learnings at 16384).
Every daily distiller run since the 4096 default landed has been
losing learnings on long records — empty ``learnings=[]`` looked
identical to "nothing extractable" in operator logs.

This file covers two surfaces added in the fix:

  1. **Default bump pin** — ``AnthropicConfig.max_tokens`` defaults
     to 16384. Regression-pin so a future drift back to 4096 fails
     this test.
  2. **Truncation-discrimination warning** — extractor emits
     ``extractor.truncated_drop`` (WARNING level) when an empty
     learnings list comes back AFTER a truncated stop_reason
     (``"max_tokens"`` Anthropic / ``"length"`` OpenAI-compat).
     Distinct from ``extractor.extract_empty`` (info) which fires on
     genuine "model said nothing extractable" results.
     Two paths covered: validation succeeded + empty learnings;
     validation failed twice + empty fallback. Negative case pinned:
     legitimate empty result with ``stop_reason="end_turn"`` does
     NOT fire the warning.

Per ``feedback_log_emission_test_pattern.md``: log emissions are
pinned via ``structlog.testing.capture_logs()`` so a future refactor
that drops the truncation warning fails this test instead of
silently regressing operator observability.

Per ``feedback_intentionally_left_blank.md``: the truncation warning
is the operator's "ran, dropped real work" signal — distinct from
both "ran, model said nothing" (extract_empty) and "ran, JSON
parse failed for non-truncation reason" (validation_failed).
"""

from __future__ import annotations

import json

import pytest
import structlog

from alfred.distiller import extractor as extractor_mod
from alfred.distiller.candidates import CandidateSignal
from alfred.distiller.config import (
    AnthropicConfig,
    DistillerConfig,
    ExtractionConfig,
)


# --- Helpers ---------------------------------------------------------------


def _config() -> DistillerConfig:
    """Minimal DistillerConfig — extract() reads anthropic.max_tokens +
    extraction.backend only (the dispatcher routes to anthropic backend
    by default; we monkeypatch ``_call_extraction_llm`` so no real API
    call ever happens)."""
    return DistillerConfig(
        extraction=ExtractionConfig(backend="anthropic"),
        anthropic=AnthropicConfig(
            api_key="DUMMY_ANTHROPIC_TEST_KEY",
            model="claude-opus-4-7",
            max_tokens=16384,
        ),
    )


def _signals() -> CandidateSignal:
    """All-default CandidateSignal — extract() passes through to the
    user-prompt renderer; none of the signal fields affect the
    truncation-warning path under test."""
    return CandidateSignal()


def _frontmatter(title: str = "Test Source", type_: str = "session") -> dict:
    """Minimal source_frontmatter — extract() reads ``title`` + ``type``
    for the truncated_drop log. Other fields are present in real
    frontmatter but unused on the warning path."""
    return {"title": title, "type": type_}


# --- Default-bump pin ------------------------------------------------------


class TestAnthropicConfigDefault:
    def test_anthropic_config_default_max_tokens_is_16384(self) -> None:
        """Pin the post-2026-05-31 default. Drift back to 4096 (or any
        value below the truncation threshold the spike measured)
        silently reintroduces the production truncation bug."""
        cfg = AnthropicConfig()
        assert cfg.max_tokens == 16384, (
            "AnthropicConfig.max_tokens default regressed — Path C "
            "Phase 1.5 spike (2026-05-31) confirmed 4096 silently "
            "truncates long records (0 learnings vs 26 at 16384). "
            "Update the test ONLY if the new value is justified by "
            "fresh measurement, not as a side-effect of a drift."
        )


# --- Truncation warning — empty learnings + truncated stop_reason ---------


class TestTruncationWarningOnEmpty:
    async def test_extractor_emits_truncation_warning_when_stop_reason_max_tokens_and_empty(
        self, monkeypatch,
    ) -> None:
        """Empty ``learnings=[]`` AFTER ``stop_reason="max_tokens"`` is
        a likely-dropped-output. Extractor emits
        ``extractor.truncated_drop`` WARNING (NOT the info-level
        ``extract_empty`` that fires on genuine empties)."""

        # Mock the dispatcher: return valid empty-learnings JSON +
        # max_tokens stop_reason. ExtractionResult parses
        # {"learnings": []} successfully → triggers the
        # learnings==0 + stop_reason check.
        async def _fake_dispatch(*, prompt, system, config):
            return (json.dumps({"learnings": []}), {"stop_reason": "max_tokens"})

        monkeypatch.setattr(
            extractor_mod, "_call_extraction_llm", _fake_dispatch,
        )

        with structlog.testing.capture_logs() as captured:
            result = await extractor_mod.extract(
                source_body="long source body content...",
                source_frontmatter=_frontmatter(
                    title="Voice Chat and Calibration Design 2026-04-15",
                    type_="session",
                ),
                existing_learn_titles=[],
                signals=_signals(),
                config=_config(),
            )

        # Sanity: returns empty result (validation succeeded, just empty).
        assert result.learnings == []

        # Truncation warning fired with operator-actionable fields.
        truncated = [
            c for c in captured
            if c.get("event") == "extractor.truncated_drop"
        ]
        assert len(truncated) == 1, (
            f"expected exactly one extractor.truncated_drop event; "
            f"got events: {[c.get('event') for c in captured]}"
        )
        ev = truncated[0]
        assert ev["log_level"] == "warning"
        assert ev["stop_reason"] == "max_tokens"
        assert ev["source_title"] == "Voice Chat and Calibration Design 2026-04-15"
        assert ev["source_type"] == "session"
        # Note field names the actionable config knob — operator
        # grepping "max_tokens" in the daemon log finds the actionable
        # hint without needing to read the agent docs.
        assert "max_tokens" in ev["note"]

        # Negative: the info-level extract_empty does NOT fire on the
        # truncation path (this is the discrimination — empties without
        # truncation get the info log; empties WITH truncation get the
        # warning instead, not both).
        info_empties = [
            c for c in captured
            if c.get("event") == "extractor.extract_empty"
        ]
        assert info_empties == [], (
            f"extract_empty fired alongside truncated_drop — the "
            f"discrimination path is broken; got: {info_empties}"
        )

    async def test_extractor_emits_truncation_warning_on_attempt_1_path(
        self, monkeypatch,
    ) -> None:
        """Regression pin for the attempt=1 discrimination (added
        2026-05-31 after the original ship covered only attempt=2 +
        validation-failed paths — review caught the gap when these
        tests failed with empty captured logs).

        The attempt=1 path fires when the model returns valid
        ``{"learnings": []}`` JSON on the FIRST call with a truncated
        stop_reason — the most common production shape per the
        spike's Voice Chat record. Pin the ``attempt=1`` field
        explicitly so a future refactor that moves the discrimination
        back to attempt=2-only re-fails this test."""

        async def _fake_dispatch(*, prompt, system, config):
            return (json.dumps({"learnings": []}), {"stop_reason": "max_tokens"})

        monkeypatch.setattr(
            extractor_mod, "_call_extraction_llm", _fake_dispatch,
        )

        with structlog.testing.capture_logs() as captured:
            await extractor_mod.extract(
                source_body="x",
                source_frontmatter=_frontmatter(),
                existing_learn_titles=[],
                signals=_signals(),
                config=_config(),
            )

        truncated = [
            c for c in captured
            if c.get("event") == "extractor.truncated_drop"
        ]
        assert len(truncated) == 1
        # Pin attempt=1 explicitly — the bug that caused the original
        # ship's tests to fail was the discrimination being absent
        # from this attempt. If a future refactor accidentally
        # restores the gap, this assertion catches it.
        assert truncated[0]["attempt"] == 1
        assert truncated[0]["stop_reason"] == "max_tokens"

    async def test_extractor_emits_truncation_warning_on_length_stop_reason(
        self, monkeypatch,
    ) -> None:
        """OpenAI-compat backends (Ollama, Together) emit ``"length"``
        instead of Anthropic's ``"max_tokens"`` for the same
        truncation condition. The warning fires for both."""

        async def _fake_dispatch(*, prompt, system, config):
            return (json.dumps({"learnings": []}), {"stop_reason": "length"})

        monkeypatch.setattr(
            extractor_mod, "_call_extraction_llm", _fake_dispatch,
        )

        with structlog.testing.capture_logs() as captured:
            await extractor_mod.extract(
                source_body="x",
                source_frontmatter=_frontmatter(),
                existing_learn_titles=[],
                signals=_signals(),
                config=_config(),
            )

        truncated = [
            c for c in captured
            if c.get("event") == "extractor.truncated_drop"
        ]
        assert len(truncated) == 1
        assert truncated[0]["stop_reason"] == "length"

    async def test_extractor_no_truncation_warning_on_legitimate_empty_result(
        self, monkeypatch,
    ) -> None:
        """Genuine "model said no learnings" path: empty result with
        ``stop_reason="end_turn"`` (Anthropic) → info-level
        ``extract_empty``, NOT ``truncated_drop``. Pins the
        discrimination so the warning doesn't fire on every empty."""

        async def _fake_dispatch(*, prompt, system, config):
            return (json.dumps({"learnings": []}), {"stop_reason": "end_turn"})

        monkeypatch.setattr(
            extractor_mod, "_call_extraction_llm", _fake_dispatch,
        )

        with structlog.testing.capture_logs() as captured:
            await extractor_mod.extract(
                source_body="smoke test placeholder",
                source_frontmatter=_frontmatter(),
                existing_learn_titles=[],
                signals=_signals(),
                config=_config(),
            )

        # No truncation warning on the legitimate-empty path.
        truncated = [
            c for c in captured
            if c.get("event") == "extractor.truncated_drop"
        ]
        assert truncated == [], (
            f"truncated_drop fired on a legitimate-empty result "
            f"(stop_reason=end_turn) — the discrimination is broken; "
            f"got: {truncated}"
        )

        # Info-level extract_empty DID fire (so the existing path
        # stays unaffected).
        info_empties = [
            c for c in captured
            if c.get("event") == "extractor.extract_empty"
        ]
        assert len(info_empties) == 1
        assert info_empties[0]["stop_reason"] == "end_turn"


# --- Truncation warning — validation-failed path -------------------------


class TestTruncationWarningOnValidationFailure:
    async def test_extractor_emits_truncation_warning_on_validation_failure_with_truncated_stop_reason(
        self, monkeypatch,
    ) -> None:
        """When BOTH attempts return malformed JSON (truncated
        mid-string) AND stop_reason is truncation-shaped, fire
        ``truncated_drop`` alongside the existing
        ``validation_failed``. Pre-2026-05-31 the truncation case was
        indistinguishable from a "model produced bad JSON" failure
        in logs — different operator action (raise max_tokens vs
        tune prompt)."""

        # Both attempts return malformed JSON (cut mid-string) +
        # max_tokens stop_reason — the canonical truncation shape
        # observed in the 2026-05-31 spike on the Voice Chat record.
        async def _fake_dispatch(*, prompt, system, config):
            truncated_json = (
                '{"learnings": [{"type": "decision", '
                '"title": "Use message-level routing", '
                '"claim": "Salem routes peer messages at the message layer '
                'rather than tool layer because'  # cuts off mid-string
            )
            return (truncated_json, {"stop_reason": "max_tokens"})

        monkeypatch.setattr(
            extractor_mod, "_call_extraction_llm", _fake_dispatch,
        )

        with structlog.testing.capture_logs() as captured:
            result = await extractor_mod.extract(
                source_body="long source body",
                source_frontmatter=_frontmatter(title="A Long Source"),
                existing_learn_titles=[],
                signals=_signals(),
                config=_config(),
            )

        # Validation failed twice → empty fallback.
        assert result.learnings == []

        # Existing validation_failed log still fires (preserves
        # the prior diagnostic surface).
        validation_failures = [
            c for c in captured
            if c.get("event") == "extractor.validation_failed"
        ]
        assert len(validation_failures) == 1
        assert validation_failures[0]["stop_reason"] == "max_tokens"

        # NEW: truncated_drop fires alongside, with operator-actionable
        # fields — this is the discrimination that pre-2026-05-31 was
        # missing (every truncation looked like a "bad JSON" failure).
        truncated = [
            c for c in captured
            if c.get("event") == "extractor.truncated_drop"
        ]
        assert len(truncated) == 1
        ev = truncated[0]
        assert ev["log_level"] == "warning"
        assert ev["stop_reason"] == "max_tokens"
        assert ev["source_title"] == "A Long Source"
        # Note field actionable hint identifies the fix path.
        assert "max_tokens" in ev["note"]


# --- source_path threading (2026-05-31 followup) -------------------------


class TestSourcePathThreading:
    """Pin the source_path kwarg surface added 2026-05-31 followup.

    Operator log review needs to identify WHICH source record dropped
    output. Pre-fix, only ``source_title`` + ``source_type`` from
    frontmatter were logged; the path was only available on the
    upper-layer daemon error log (``distiller.v2.extract_error``),
    which required timestamp-correlation between two logs to identify
    a truncated record. Post-fix, ``extractor.truncated_drop``
    carries ``source_path`` directly when the caller threads it
    through.

    Three tests cover the contract:
      - Caller supplies source_path → field populated in log
      - Caller omits source_path → field present with value ``None``
        (so grep ``source_path=`` matches both cases; absence-of-
        value is a discoverable state, not a missing key)
      - Existing callers that don't pass source_path still work
        (back-compat regression pin)
    """

    async def test_extract_passes_source_path_to_truncation_warning(
        self, monkeypatch,
    ) -> None:
        """Caller passes ``source_path="some/path.md"`` → the
        ``extractor.truncated_drop`` log emits
        ``source_path="some/path.md"`` so operator grep can identify
        the affected record without cross-log correlation."""

        async def _fake_dispatch(*, prompt, system, config):
            return (json.dumps({"learnings": []}), {"stop_reason": "max_tokens"})

        monkeypatch.setattr(
            extractor_mod, "_call_extraction_llm", _fake_dispatch,
        )

        with structlog.testing.capture_logs() as captured:
            await extractor_mod.extract(
                source_body="x",
                source_frontmatter=_frontmatter(),
                existing_learn_titles=[],
                signals=_signals(),
                config=_config(),
                source_path="session/Voice Chat and Calibration Design 2026-04-15.md",
            )

        truncated = [
            c for c in captured
            if c.get("event") == "extractor.truncated_drop"
        ]
        assert len(truncated) == 1
        ev = truncated[0]
        # Path threaded verbatim — no normalization, the caller's
        # representation is authoritative (matches the daemon-layer
        # log fields ``source=sc.record.rel_path`` /
        # ``source=str(source_file)``).
        assert (
            ev["source_path"]
            == "session/Voice Chat and Calibration Design 2026-04-15.md"
        )

    async def test_extract_omits_source_path_when_caller_does_not_supply(
        self, monkeypatch,
    ) -> None:
        """Caller omits source_path → field present with value
        ``None`` (back-compat for tests / direct callers; absence-of-
        value is grep-able)."""

        async def _fake_dispatch(*, prompt, system, config):
            return (json.dumps({"learnings": []}), {"stop_reason": "max_tokens"})

        monkeypatch.setattr(
            extractor_mod, "_call_extraction_llm", _fake_dispatch,
        )

        with structlog.testing.capture_logs() as captured:
            await extractor_mod.extract(
                source_body="x",
                source_frontmatter=_frontmatter(),
                existing_learn_titles=[],
                signals=_signals(),
                config=_config(),
                # Deliberately omit source_path to exercise default.
            )

        truncated = [
            c for c in captured
            if c.get("event") == "extractor.truncated_drop"
        ]
        assert len(truncated) == 1
        ev = truncated[0]
        # Field present in log with None value — pinned so a future
        # refactor that drops the field entirely (rather than logging
        # None) fails this test. Grep-able as ``source_path=None``.
        assert "source_path" in ev
        assert ev["source_path"] is None

    async def test_extract_default_kwarg_does_not_break_existing_callers(
        self, monkeypatch,
    ) -> None:
        """Back-compat regression pin: extract() called without
        source_path (the pre-followup signature) returns normally and
        emits the truncated_drop log without raising TypeError. The
        original 6 tests in this file all exercise this path
        implicitly; this test makes the contract explicit so a future
        change that promotes source_path to positional-required fails
        this test instead of silently breaking unit-test callers."""

        async def _fake_dispatch(*, prompt, system, config):
            # Return a non-empty valid result so we exercise the
            # success-path code instead of the truncation path.
            return (
                json.dumps({"learnings": []}),
                {"stop_reason": "end_turn"},
            )

        monkeypatch.setattr(
            extractor_mod, "_call_extraction_llm", _fake_dispatch,
        )

        # Pre-followup call signature — no source_path kwarg.
        result = await extractor_mod.extract(
            source_body="smoke test",
            source_frontmatter=_frontmatter(),
            existing_learn_titles=[],
            signals=_signals(),
            config=_config(),
        )

        # Returns normally — back-compat preserved.
        assert result.learnings == []
