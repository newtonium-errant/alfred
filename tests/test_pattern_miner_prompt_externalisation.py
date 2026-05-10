"""Tests for the Phase 4 drafter-prompt externalisation
(``_load_draft_prompt_template`` in ``alfred.distiller.pattern_miner``).

Originally the prompt was an inline constant
(``DRAFT_PROMPT_TEMPLATE`` at pattern_miner.py:499). After the Phase 4
first-live-run review found severe W1 voice mismatch (9/10 proposals),
the prompt got externalised so the prompt-tuner could iterate against
a .md file rather than a hook-blocked Python literal. Precedent:
commit ``9904730`` 2026-05-09 voice-prompts externalisation; same
hook-blocking mechanism applied here.

This is the stage-1 mechanical refactor test surface — no content
changes; just pinning the loader contract so prompt-tuner's stage-2
edits land cleanly:

  1. Loader resolves to ``vault-distiller/prompts/draft_canonical_proposal.md``
     under the bundled skills dir (matches ``_load_stage_prompt`` precedent).
  2. Loader reads fresh per call — prompt-tuner edits take effect on
     the next miner invocation without a daemon restart.
  3. Loader degrades gracefully on a missing file (warning +
     empty string), mirroring the distiller-pipeline / voice-train
     precedents — the empty-string fallback falls through to
     ``str.format`` so the render-time KeyError is the diagnostic
     rather than a hard import-error.
  4. Format placeholders intact — ``{labels}``, ``{count}``,
     ``{members_with_previews}`` substitute the same way as the prior
     inline constant.
  5. The bundled .md file ships under the wheel — pin the path
     so a future packaging regression that drops the prompts/ dir
     fails loudly.

Tests run unconditionally per
``feedback_regression_pin_unconditional.md``.
"""

from __future__ import annotations

from pathlib import Path

import structlog

from alfred.distiller import pattern_miner


# ---------------------------------------------------------------------------
# 1. Bundled .md file ships in the wheel
# ---------------------------------------------------------------------------


class TestBundledFileShips:
    def test_draft_canonical_proposal_md_exists(self) -> None:
        # The .md file lives under
        # ``src/alfred/_bundled/skills/vault-distiller/prompts/``.
        # ``importlib.resources``-located so it works both from a
        # source checkout AND from an installed wheel.
        from alfred._data import get_skills_dir
        path = (
            get_skills_dir() / "vault-distiller" / "prompts"
            / "draft_canonical_proposal.md"
        )
        assert path.exists(), (
            f"Expected bundled draft prompt at {path}; missing means "
            f"the wheel won't ship it and the loader will fall back "
            f"to the empty-string degraded path in production."
        )

    def test_prompt_carries_required_format_placeholders(self) -> None:
        # The prompt MUST contain {labels}, {count},
        # {members_with_previews} — without them str.format() at the
        # call site would render with literal braces, producing a
        # broken LLM prompt and likely confusing the drafter.
        content = pattern_miner._load_draft_prompt_template()
        assert "{labels}" in content, (
            "Missing {labels} placeholder — call_drafter passes "
            "labels=<csv> to str.format and the rendered prompt "
            "would drop the cluster labels entirely."
        )
        assert "{count}" in content, (
            "Missing {count} placeholder — member count would be "
            "stripped from the rendered prompt."
        )
        assert "{members_with_previews}" in content, (
            "Missing {members_with_previews} placeholder — the "
            "cluster's member-file content (titles + body previews) "
            "would not reach the drafter."
        )


# ---------------------------------------------------------------------------
# 2. Loader resolves to the canonical bundled path
# ---------------------------------------------------------------------------


class TestLoaderPathResolution:
    def test_loader_reads_from_vault_distiller_prompts_dir(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        # The loader MUST hit
        # ``<skills_dir>/vault-distiller/prompts/draft_canonical_proposal.md``,
        # not e.g. ``vault-talker/`` or ``distiller/`` (which is what
        # the dispatch text suggested — but the existing convention is
        # ``vault-<tool>/`` and the new loader mirrors
        # ``_load_stage_prompt`` precisely).
        fake_skills_dir = tmp_path / "skills"
        (fake_skills_dir / "vault-distiller" / "prompts").mkdir(parents=True)
        target = (
            fake_skills_dir / "vault-distiller" / "prompts"
            / "draft_canonical_proposal.md"
        )
        target.write_text("PATH-RESOLUTION-PIN\n", encoding="utf-8")

        # Decoy: write a file at the wrong-but-plausible path. If the
        # loader resolves there instead, this content would leak into
        # the returned template and the assertion below fails.
        (fake_skills_dir / "distiller" / "prompts").mkdir(parents=True)
        (
            fake_skills_dir / "distiller" / "prompts"
            / "draft_canonical_proposal.md"
        ).write_text("WRONG-PATH-DECOY\n", encoding="utf-8")

        monkeypatch.setattr(
            "alfred._data.get_skills_dir", lambda: fake_skills_dir
        )

        result = pattern_miner._load_draft_prompt_template()
        assert result == "PATH-RESOLUTION-PIN\n"
        assert "WRONG-PATH-DECOY" not in result


# ---------------------------------------------------------------------------
# 3. Per-call freshness — prompt-tuner contract
# ---------------------------------------------------------------------------


class TestPerCallFreshness:
    def test_prompt_content_reads_fresh_per_call(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        # Edit-after-import test: changing the file on disk after the
        # module is loaded must be reflected on the NEXT helper call.
        # This is the load-bearing contract for prompt-tuner — edits
        # to draft_canonical_proposal.md must land without daemon
        # restart.
        fake_skills_dir = tmp_path / "skills"
        (fake_skills_dir / "vault-distiller" / "prompts").mkdir(parents=True)
        target = (
            fake_skills_dir / "vault-distiller" / "prompts"
            / "draft_canonical_proposal.md"
        )
        target.write_text("ORIGINAL\n", encoding="utf-8")

        monkeypatch.setattr(
            "alfred._data.get_skills_dir", lambda: fake_skills_dir
        )

        # First call sees ORIGINAL.
        assert pattern_miner._load_draft_prompt_template() == "ORIGINAL\n"

        # Edit the file on disk WITHOUT re-importing pattern_miner.
        target.write_text("EDITED\n", encoding="utf-8")

        # Next call sees EDITED — confirms no module-import cache,
        # no ``functools.cache``, no per-process memoisation.
        assert pattern_miner._load_draft_prompt_template() == "EDITED\n"


# ---------------------------------------------------------------------------
# 4. Missing-file graceful degradation (warning + empty string)
# ---------------------------------------------------------------------------


class TestMissingFileDegradation:
    def test_missing_file_warns_and_returns_empty_string(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        # If the .md file is missing (packaging regression, accidental
        # delete, or a future rename that drops the legacy path), the
        # loader returns "" and logs a warning. Mirrors
        # ``_load_stage_prompt`` precedent — the empty string falls
        # through to ``str.format`` which then raises a render-time
        # diagnostic (rather than silently producing a prompt missing
        # placeholders).
        fake_skills_dir = tmp_path / "skills"
        (fake_skills_dir / "vault-distiller" / "prompts").mkdir(parents=True)
        # Intentionally NOT writing draft_canonical_proposal.md here.

        monkeypatch.setattr(
            "alfred._data.get_skills_dir", lambda: fake_skills_dir
        )

        with structlog.testing.capture_logs() as captured:
            result = pattern_miner._load_draft_prompt_template()

        assert result == ""
        matches = [
            c for c in captured
            if c.get("event") == "pattern_miner.prompt_not_found"
        ]
        assert len(matches) == 1, (
            f"Expected exactly one prompt_not_found log, got "
            f"{len(matches)}: {captured}"
        )
        # Pin the path field — catches a silent rename/drop later.
        assert (
            "vault-distiller/prompts/draft_canonical_proposal.md"
            in matches[0]["path"]
        )


# ---------------------------------------------------------------------------
# 5. End-to-end content equivalence with the prior inline constant
# ---------------------------------------------------------------------------


class TestContentByteEquivalence:
    """Stage 1 is a mechanical refactor — the prompt body must be
    byte-identical with the prior inline ``DRAFT_PROMPT_TEMPLATE``
    constant. Pinning the bug-of-record diagnostic content (the
    "no preamble" instruction, the TYPE/SLUG suffix format, etc.)
    catches a regression where stage-1 accidentally drops content
    that stage-2 was supposed to iterate on rather than restore.

    Stage 2 (prompt-tuner content fix) WILL break these pins — that's
    intentional. When prompt-tuner ships, these byte-equivalence pins
    get replaced by structural pins ("contains the new refusal
    sentinel," "contains the split sentinel").
    """

    def test_prompt_contains_single_paragraph_instruction(self) -> None:
        # The "write a SINGLE-PARAGRAPH (3-5 sentences) summary"
        # directive is the core voice constraint that stage 2 will
        # rework. Stage 1 pins it byte-identical.
        content = pattern_miner._load_draft_prompt_template()
        assert (
            "write a SINGLE-PARAGRAPH (3-5 sentences) summary"
            in content
        )

    def test_prompt_contains_no_preamble_directive(self) -> None:
        # "No preamble, no 'the unifying theme is', just the claim."
        # Voice directive. Stage 2 reworks; stage 1 preserves.
        content = pattern_miner._load_draft_prompt_template()
        assert "No preamble" in content

    def test_prompt_contains_type_slug_suffix_format(self) -> None:
        # The TYPE: / SLUG: trailer is the structured-output contract
        # _parse_drafter_response in pattern_miner.py parses. Stage 1
        # MUST preserve this byte-for-byte or the parser regex fails
        # against the new prompt's output.
        content = pattern_miner._load_draft_prompt_template()
        assert "TYPE: architecture|principles" in content
        assert "SLUG: <kebab-case-slug-no-extension>" in content


# ---------------------------------------------------------------------------
# 6. call_drafter integration — format() round-trip works end-to-end
# ---------------------------------------------------------------------------


class TestFormatRoundTrip:
    def test_format_renders_with_required_placeholders(self) -> None:
        # Render the template with the same kwargs ``call_drafter``
        # uses. Catches a regression where the .md file gains a
        # spurious ``{...}`` token that confuses str.format.
        template = pattern_miner._load_draft_prompt_template()
        rendered = template.format(
            labels="topic/x, theme/y",
            count=5,
            members_with_previews="- doc1\n- doc2\n",
        )
        # All three placeholders substituted; no leftover ``{labels}``
        # / ``{count}`` / ``{members_with_previews}`` tokens.
        assert "topic/x, theme/y" in rendered
        assert "Document count: 5" in rendered
        assert "- doc1\n- doc2" in rendered
        assert "{labels}" not in rendered
        assert "{count}" not in rendered
        assert "{members_with_previews}" not in rendered
