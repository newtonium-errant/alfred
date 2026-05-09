"""Regression: ``_check_directory`` must not fire false-positive
warnings for canonical sub-path TYPE_DIRECTORY entries.

Background (Hypatia voice-profile rebuild 2026-05-09, NOTE-1): the
2026-05-09 08:00 voice-profile session created four ``voice-cluster``
records at ``voice/cluster/<name>.md`` (the canonical path per
``TYPE_DIRECTORY["voice-cluster"] = "voice/cluster"``). The files
landed correctly, but each create returned a warning:

    voice-cluster expected in voice/cluster/, found in voice/

The warning was a false positive: the path-validation logic compared
the first directory segment against the full expected directory string,
so any path of the form ``voice/cluster/<name>.md`` was rejected with
``"voice" != "voice/cluster"``. Same class affects every type in
``TYPE_DIRECTORY`` whose value contains a slash (currently ``essay`` →
``document/essay`` and ``voice-cluster`` → ``voice/cluster``).

This regression pin runs unconditionally — no module-level
``pytest.importorskip`` per ``feedback_regression_pin_unconditional.md``.
``vault.ops`` and ``vault.schema`` are pure-Python with stdlib-only
deps so no skip is warranted in any case; the explicit "no skip"
discipline is the load-bearing convention.
"""

from __future__ import annotations

from pathlib import Path

from alfred.vault.ops import _check_directory, vault_create
from alfred.vault.schema import TYPE_DIRECTORY


# --- Unit tests on _check_directory directly ----------------------------


def test_subpath_canonical_path_returns_none_voice_cluster():
    # voice-cluster → voice/cluster. Canonical write returns no warning.
    assert _check_directory(
        "voice-cluster", "voice/cluster/masculinity-accountability.md"
    ) is None


def test_subpath_canonical_path_returns_none_essay():
    # essay → document/essay. Canonical write returns no warning.
    assert _check_directory(
        "essay", "document/essay/some-essay-slug.md"
    ) is None


def test_subpath_wrong_first_segment_voice_cluster_returns_warning():
    # voice-cluster placed at note/<name>.md is wrong — first segment
    # mismatch. Warn naming the canonical sub-path.
    msg = _check_directory("voice-cluster", "note/cluster/foo.md")
    assert msg is not None
    assert "voice/cluster/" in msg
    assert "note/cluster/" in msg


def test_subpath_right_first_segment_wrong_second_voice_cluster_warns():
    # voice-cluster placed at voice/leaf/foo.md (correct first segment,
    # wrong second) is still wrong; warn naming the actual prefix.
    msg = _check_directory("voice-cluster", "voice/leaf/foo.md")
    assert msg is not None
    assert "voice/cluster/" in msg
    assert "voice/leaf/" in msg


def test_subpath_partial_depth_voice_cluster_at_voice_root():
    # voice-cluster placed directly at voice/<name>.md (one level too
    # shallow for the sub-path). Treat as wrong: not canonically placed.
    # Pre-fix this fired with the wrong message; post-fix it should
    # either return a clean message OR no message — the contract is "no
    # false-positive on canonical AND canonical here is two levels".
    # We accept either no warning or a warning that names the correct
    # expected sub-path. The principal contract is that a true canonical
    # path returns None (covered above).
    result = _check_directory("voice-cluster", "voice/foo.md")
    if result is not None:
        assert "voice/cluster/" in result


def test_top_level_canonical_still_returns_none_post_fix():
    # Sanity: simple types (no sub-path) keep their pre-fix behavior.
    assert _check_directory("task", "task/Some Task.md") is None
    assert _check_directory("person", "person/Andrew Newton.md") is None


def test_top_level_wrong_dir_still_warns_post_fix():
    # Sanity: simple types still warn when placed wrong.
    msg = _check_directory("task", "person/Some Task.md")
    assert msg is not None
    assert "task/" in msg
    assert "person/" in msg


def test_known_type_with_no_directory_entry_returns_none():
    # If a type is missing from TYPE_DIRECTORY, the check returns None
    # rather than crashing. Source/session live like that historically.
    # Pick a known type that is intentionally NOT in TYPE_DIRECTORY.
    # ``session`` is documented as flexible-placement.
    assert "session" not in TYPE_DIRECTORY
    assert _check_directory("session", "session/some-conv.md") is None


# --- End-to-end: vault_create on voice-cluster returns no path warning --


def test_vault_create_voice_cluster_emits_no_path_warning(tmp_vault: Path):
    """The Hypatia 2026-05-09 repro: ``vault_create`` with type
    ``voice-cluster`` MUST NOT include a directory-routing warning in
    its return payload. The file lands at ``voice/cluster/<name>.md``
    per TYPE_DIRECTORY, which is the canonical placement.

    ``voice-cluster`` is a Hypatia-scope extension type (lives in
    ``KNOWN_TYPES_HYPATIA``), so the call passes ``scope="hypatia"``
    to clear the type gate. The path-warning behaviour under test is
    in ``_check_directory``, which runs after both gates.
    """
    result = vault_create(
        tmp_vault,
        "voice-cluster",
        "masculinity-accountability",
        set_fields={
            "cluster": "masculinity-accountability",
            "leaf_count": 3,
        },
        body="# Cluster Summary\n\nTest body.\n",
        scope="hypatia",
    )
    assert result["path"] == "voice/cluster/masculinity-accountability.md"
    # Pre-fix this assertion would have failed with the warning
    # "Type 'voice-cluster' expected in 'voice/cluster/', found in 'voice/'".
    path_warnings = [
        w for w in result.get("warnings", []) if "expected in" in w
    ]
    assert path_warnings == [], (
        f"unexpected path-routing warnings on canonical write: "
        f"{path_warnings}"
    )


def test_vault_create_essay_emits_no_path_warning(tmp_vault: Path):
    """Same regression class for ``essay`` (TYPE_DIRECTORY entry
    ``document/essay``). Confirms the fix isn't voice-cluster-specific."""
    result = vault_create(
        tmp_vault,
        "essay",
        "test-essay-slug",
        set_fields={"status": "draft"},
        body="# Test Essay\n",
        scope="hypatia",
    )
    assert result["path"] == "document/essay/test-essay-slug.md"
    path_warnings = [
        w for w in result.get("warnings", []) if "expected in" in w
    ]
    assert path_warnings == [], (
        f"unexpected path-routing warnings on canonical write: "
        f"{path_warnings}"
    )
