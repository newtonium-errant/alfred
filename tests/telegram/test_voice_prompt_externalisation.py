"""Regression pin + loader tests for the voice prompt externalisation.

The three voice prompts (extraction / cluster / overall) used to live as
inline triple-quoted constants in ``alfred.telegram.voice_train``. The
2026-05-09 refactor moved them to ``.md`` files under
``src/alfred/_bundled/skills/vault-hypatia/prompts/`` so prompt-tuner
can iterate without touching Python code.

These tests ensure:

1. Each ``.md`` file exists at the expected bundled path.
2. The loaded content is byte-for-byte identical to the SHA-256 hashes
   captured from the inlined constants immediately before the move.
   This is the load-bearing regression pin — if a future edit
   accidentally normalizes line endings, strips trailing whitespace,
   or otherwise mutates the file, the comparison breaks loudly.
3. Loaded content reaches the public helpers
   (``get_voice_extraction_prompt`` etc.) unchanged.
4. The loader uses ``alfred._data.get_skills_dir`` so it resolves
   correctly whether Alfred is installed from a checkout or a wheel
   (mirrors ``alfred.distiller.pipeline._load_stage_prompt``).
5. The legacy module-level constants (``VOICE_EXTRACTION_PROMPT`` etc.)
   are gone — anyone still importing them by name fails fast.
6. Per ``feedback_async_data_capture_freshness.md`` and the prompt-tuner
   refactor goal: prompts are read FRESH per call (not cached at module
   import time). Edits to the .md files must take effect on the next
   extraction without daemon restart.

Per ``feedback_regression_pin_unconditional.md``: this file is the
isolated regression-pin home. NO ``pytest.importorskip`` at module level
— these tests run on every pytest invocation regardless of optional
dependency availability. The only imports here are stdlib + ``alfred``
itself, so there's nothing to skip.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import structlog

from alfred._data import get_skills_dir
from alfred.telegram import voice_train


# ---------------------------------------------------------------------------
# Reference hashes — captured 2026-05-09 from the inlined constants
# IMMEDIATELY BEFORE the externalisation refactor.
#
# If these change, the refactor broke its own contract: prompt content
# was supposed to be relocated, not modified. A WARN-1 prose update or
# any other content change must update these hashes as part of the
# same commit, and that commit must be a CONTENT change (prompt-tuner
# domain), NOT a relocation change (builder domain).
# ---------------------------------------------------------------------------

EXPECTED_HASHES = {
    "voice_extraction.md": (
        "1b8357858f4bc7c22d1d3a6b98c83a16b720006b5ddf43c6a84ac750eb17cfc3"
    ),
    "voice_cluster.md": (
        "40b1a0e2915d55474ddb3a5e948757537de000169c62519f5a90e3b88c859663"
    ),
    "voice_overall.md": (
        "275a8b8d19473431d67bf24870ba5b4bddfc34483bfa1b84ee9deb1332bcaca0"
    ),
}

EXPECTED_LENGTHS = {
    "voice_extraction.md": 5505,
    "voice_cluster.md": 4037,
    "voice_overall.md": 4069,
}


# ---------------------------------------------------------------------------
# 1. Files exist at the expected bundled path
# ---------------------------------------------------------------------------


def test_prompt_files_exist_in_bundled_path() -> None:
    skills_dir = get_skills_dir()
    prompts_dir = skills_dir / "vault-hypatia" / "prompts"
    assert prompts_dir.is_dir(), f"Missing prompts dir at {prompts_dir}"
    for name in EXPECTED_HASHES:
        path = prompts_dir / name
        assert path.is_file(), f"Missing prompt file at {path}"


# ---------------------------------------------------------------------------
# 2. Regression pin — content matches the pre-refactor SHA-256 hashes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("filename", list(EXPECTED_HASHES.keys()))
def test_prompt_file_content_matches_pre_refactor_hash(filename: str) -> None:
    """Byte-for-byte regression pin against pre-refactor inlined content.

    If a future commit mutates the prompt content, this fails — and the
    failure surface forces the question "is this a relocation (forbidden
    here) or a content edit (which should update both file AND hash in
    lockstep)?"
    """
    skills_dir = get_skills_dir()
    path = skills_dir / "vault-hypatia" / "prompts" / filename
    content = path.read_text(encoding="utf-8")
    actual_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    assert actual_hash == EXPECTED_HASHES[filename], (
        f"{filename} content drifted from pre-refactor hash. "
        f"Expected {EXPECTED_HASHES[filename]}, got {actual_hash}. "
        f"If this is a deliberate prompt update, edit EXPECTED_HASHES "
        f"in this test in lockstep with the .md file change."
    )
    assert len(content) == EXPECTED_LENGTHS[filename]


# ---------------------------------------------------------------------------
# 3. Public helpers return the bundled file content unchanged
# ---------------------------------------------------------------------------


def test_get_voice_extraction_prompt_matches_bundled_file() -> None:
    skills_dir = get_skills_dir()
    raw = (skills_dir / "vault-hypatia" / "prompts" / "voice_extraction.md").read_text(
        encoding="utf-8"
    )
    assert voice_train.get_voice_extraction_prompt() == raw


def test_get_voice_cluster_prompt_matches_bundled_file() -> None:
    skills_dir = get_skills_dir()
    raw = (skills_dir / "vault-hypatia" / "prompts" / "voice_cluster.md").read_text(
        encoding="utf-8"
    )
    assert voice_train.get_voice_cluster_prompt() == raw


def test_get_voice_overall_prompt_matches_bundled_file() -> None:
    skills_dir = get_skills_dir()
    raw = (skills_dir / "vault-hypatia" / "prompts" / "voice_overall.md").read_text(
        encoding="utf-8"
    )
    assert voice_train.get_voice_overall_prompt() == raw


# ---------------------------------------------------------------------------
# 4. Loader works against the bundled importlib.resources path
#    (covers both source-tree-dev and installed-wheel modes — get_skills_dir
#    uses importlib.resources internally)
# ---------------------------------------------------------------------------


def test_loader_uses_importlib_resources_path() -> None:
    """The loader must hit a real path that ``get_skills_dir`` resolves.

    Whether installed from checkout or wheel, ``importlib.resources``
    points at the right tree. The contract here: the loader's resolved
    path lives under ``get_skills_dir() / vault-hypatia / prompts /``.
    """
    skills_dir = get_skills_dir()
    expected_dir = skills_dir / "vault-hypatia" / "prompts"
    # The loader must produce content for each file living under that dir.
    for filename in EXPECTED_HASHES:
        path = expected_dir / filename
        assert path.is_file()
        # The loader internal helper resolves to the same path.
        loaded = voice_train._load_voice_prompt(filename)
        assert loaded == path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 5. Legacy constants are gone
# ---------------------------------------------------------------------------


def test_legacy_voice_prompt_constants_removed() -> None:
    """Pin the deletion of the inlined constants.

    The refactor's whole point was to move these out of Python code into
    .md files prompt-tuner can edit. If a future commit re-introduces the
    constants ("just for backward compat"), this fails and forces the
    question of WHY — the rebuild against bundled files is the contract.
    """
    for name in (
        "VOICE_EXTRACTION_PROMPT",
        "VOICE_CLUSTER_PROMPT",
        "VOICE_OVERALL_PROMPT",
    ):
        assert not hasattr(voice_train, name), (
            f"{name} should no longer exist on the voice_train module — "
            f"externalised to vault-hypatia/prompts/. If you need the text, "
            f"call the appropriate get_voice_*_prompt() helper."
        )

    # METHOD_EXTRACTION_PROMPT stays inline (intentionally NOT in scope).
    assert hasattr(voice_train, "METHOD_EXTRACTION_PROMPT")


def test_voice_train_all_exports_use_loader_helpers() -> None:
    exports = set(voice_train.__all__)
    assert "get_voice_extraction_prompt" in exports
    assert "get_voice_cluster_prompt" in exports
    assert "get_voice_overall_prompt" in exports
    # The legacy constant names must NOT be in __all__ either.
    for name in (
        "VOICE_EXTRACTION_PROMPT",
        "VOICE_CLUSTER_PROMPT",
        "VOICE_OVERALL_PROMPT",
    ):
        assert name not in exports, (
            f"{name} still listed in __all__ but the constant is gone — "
            f"would crash on `from voice_train import *`."
        )


# ---------------------------------------------------------------------------
# 6. Per-call freshness — prompt content is read fresh from disk on every
#    helper call, not cached at module-import time. This is the load-bearing
#    contract for prompt-tuner: edits to the .md files must take effect on
#    the next extraction without a daemon restart.
# ---------------------------------------------------------------------------


def test_prompt_content_reads_fresh_per_call(tmp_path: Path, monkeypatch) -> None:
    """Edit-after-import test: changing the file on disk after the module
    is loaded is reflected on the next helper call."""
    fake_skills_dir = tmp_path / "skills"
    (fake_skills_dir / "vault-hypatia" / "prompts").mkdir(parents=True)
    target = fake_skills_dir / "vault-hypatia" / "prompts" / "voice_extraction.md"
    target.write_text("ORIGINAL\n", encoding="utf-8")

    monkeypatch.setattr(
        "alfred._data.get_skills_dir", lambda: fake_skills_dir
    )

    # First call sees ORIGINAL.
    assert voice_train.get_voice_extraction_prompt() == "ORIGINAL\n"

    # Edit the file on disk WITHOUT re-importing the module.
    target.write_text("EDITED\n", encoding="utf-8")

    # Next call sees EDITED — confirms no module-import cache.
    assert voice_train.get_voice_extraction_prompt() == "EDITED\n"


def test_prompt_loader_warns_on_missing_file(tmp_path: Path, monkeypatch) -> None:
    """If the .md file is missing the loader emits a warning + returns ''.

    Per ``feedback_intentionally_left_blank.md`` + the structlog assertion
    pattern: capture via ``structlog.testing.capture_logs`` (sync code, but
    structlog routes through proxy logger configured by alfred.utils so the
    sync caplog pattern is brittle here — capture_logs is the safe choice
    that works regardless of structlog config).
    """
    fake_skills_dir = tmp_path / "skills"
    (fake_skills_dir / "vault-hypatia" / "prompts").mkdir(parents=True)
    monkeypatch.setattr(
        "alfred._data.get_skills_dir", lambda: fake_skills_dir
    )

    with structlog.testing.capture_logs() as captured:
        result = voice_train._load_voice_prompt("does_not_exist.md")

    assert result == ""
    matches = [
        c for c in captured if c.get("event") == "voice_train.prompt_not_found"
    ]
    assert len(matches) == 1, (
        f"Expected exactly one prompt_not_found log, got {len(matches)}: "
        f"{captured}"
    )
    # Pin the structured fields — catches silent rename/drop later.
    assert matches[0]["prompt_file"] == "does_not_exist.md"
    assert "does_not_exist.md" in matches[0]["path"]
    # ``stdout_tail=""`` sentinel per builder.md subprocess-failure rule.
    assert matches[0]["stdout_tail"] == ""
