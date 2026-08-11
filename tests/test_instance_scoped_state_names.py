"""Instance-scoped state filenames (#84).

Seven state files carried a literal ``.salem.`` in their names on SHARED code
paths. On the box every instance runs the same code from one WorkingDirectory
and differs only by ``--config``, so KAL-LE running the email calibrator wrote
into a file named for Salem — the 2026-07-31 feed-store shape, made harder to
spot because the instance name in the filename made it look deliberate.

**THE MIGRATION PROPERTY, pinned first and hardest.** ``instance.name`` for
Salem is ``"Salem"``, which slugs to ``salem``, which reproduces every old
literal byte-for-byte. That is what makes this fix deployable with no rename
shim, no file moves and no operator step: Salem's live calibration corpora and
snooze store keep working because their paths do not change. If a future edit
breaks that identity, Salem silently starts from empty state — the corpora and
the snooze store are operator-visible, so the loss would be noticed late and
be unrecoverable. Hence the explicit per-file table below.
"""

from __future__ import annotations

import pytest

from alfred.common.instance_paths import (
    InstanceSlugError,
    instance_slug,
    instance_state_filename,
    instance_state_filename_or_unscoped,
)
from alfred.daily_sync.config import load_from_unified as load_daily_sync
from alfred.routine.config import load_from_unified as load_routine
from alfred.tier.snooze import default_snooze_path


def cfg_for(name: str | None) -> dict:
    raw: dict = {"daily_sync": {"enabled": True}, "routine": {}}
    if name is not None:
        raw["telegram"] = {"instance": {"name": name}}
    return raw


# ---------------------------------------------------------------------------
# THE MIGRATION PROPERTY — Salem's paths must not move
# ---------------------------------------------------------------------------


#: Every file #84 touched, with the EXACT name it had before the fix.
SALEM_LIVE_PATHS = {
    "email_calibration": "./data/email_calibration.salem.jsonl",
    "tier_recurrence_pending": "./data/tier_recurrence_pending.salem.jsonl",
    "tier_recurrence_decided": "./data/tier_recurrence_decided.salem.jsonl",
    "routine_match_pending": "./data/routine_match_pending.salem.jsonl",
    "routine_match_corpus": "./data/routine_match_corpus.salem.jsonl",
    "board_snooze": "./data/board_snooze.salem.json",
}


def test_salem_derived_paths_are_byte_identical_to_the_old_literals():
    """NO MIGRATION. Each derived path must equal the literal it replaced —
    that identity is the entire deploy story."""
    raw = cfg_for("Salem")
    ds = load_daily_sync(raw)
    routine = load_routine(raw)

    assert ds.corpus.path == SALEM_LIVE_PATHS["email_calibration"]
    assert ds.tier_recurrence.pending_path == \
        SALEM_LIVE_PATHS["tier_recurrence_pending"]
    assert ds.tier_recurrence.decided_path == \
        SALEM_LIVE_PATHS["tier_recurrence_decided"]
    assert routine.match_calibration.pending_path == \
        SALEM_LIVE_PATHS["routine_match_pending"]
    assert routine.match_calibration.corpus_path == \
        SALEM_LIVE_PATHS["routine_match_corpus"]
    assert default_snooze_path(raw) == SALEM_LIVE_PATHS["board_snooze"]


def test_the_slug_that_makes_the_identity_hold():
    """The single fact the whole migration story rests on."""
    assert instance_slug("Salem") == "salem"


# ---------------------------------------------------------------------------
# The actual fix — instances no longer collide
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name,slug", [
    ("KAL-LE", "kal-le"),
    ("Hypatia", "hypatia"),
    ("VERA", "vera"),
])
def test_each_instance_gets_its_own_files(name: str, slug: str):
    raw = cfg_for(name)
    ds = load_daily_sync(raw)
    routine = load_routine(raw)

    assert ds.corpus.path == f"./data/email_calibration.{slug}.jsonl"
    assert ds.tier_recurrence.pending_path == \
        f"./data/tier_recurrence_pending.{slug}.jsonl"
    assert ds.tier_recurrence.decided_path == \
        f"./data/tier_recurrence_decided.{slug}.jsonl"
    assert routine.match_calibration.pending_path == \
        f"./data/routine_match_pending.{slug}.jsonl"
    assert routine.match_calibration.corpus_path == \
        f"./data/routine_match_corpus.{slug}.jsonl"
    assert default_snooze_path(raw) == f"./data/board_snooze.{slug}.json"


def test_no_two_instances_share_any_of_the_seven_files():
    """The defect, stated as a property: the whole point is non-collision."""
    seen: dict[str, str] = {}
    for name in ("Salem", "KAL-LE", "Hypatia", "VERA"):
        raw = cfg_for(name)
        ds = load_daily_sync(raw)
        routine = load_routine(raw)
        for path in (
            ds.corpus.path,
            ds.tier_recurrence.pending_path,
            ds.tier_recurrence.decided_path,
            routine.match_calibration.pending_path,
            routine.match_calibration.corpus_path,
            default_snooze_path(raw),
        ):
            assert path not in seen, (
                f"{name} shares {path} with {seen[path]} — the #84 defect"
            )
            seen[path] = name


def test_no_production_default_names_an_instance_in_code():
    """The literals are gone from the modules that carried them.

    Checked on non-docstring string CONSTANTS via the AST, not on raw lines.
    Prose is allowed — and required — to explain the migration story, which
    means saying ``board_snooze.salem.json`` out loud; a line-based scan
    fires on its own explanation and gets deleted within a week. Comments
    are not string constants in the AST, so they are excluded for free.
    """
    import ast
    import inspect
    from pathlib import Path

    from alfred.daily_sync import config as ds_config
    from alfred.routine import match_calibration
    from alfred.tier import snooze

    for module in (ds_config, match_calibration, snooze):
        tree = ast.parse(
            Path(inspect.getfile(module)).read_text(encoding="utf-8"),
        )
        docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef))
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        }
        offenders = [
            node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
            and ".salem." in node.value
        ]
        assert offenders == [], (
            f"{module.__name__} still has instance-named string literals: "
            f"{offenders}"
        )


# ---------------------------------------------------------------------------
# Explicit config always wins
# ---------------------------------------------------------------------------


def test_an_explicit_corpus_path_is_honoured():
    raw = cfg_for("Salem")
    raw["daily_sync"]["corpus"] = {"path": "/srv/state/corpus.jsonl"}
    assert load_daily_sync(raw).corpus.path == "/srv/state/corpus.jsonl"


def test_explicit_tier_recurrence_paths_are_honoured():
    raw = cfg_for("Salem")
    raw["daily_sync"]["tier_recurrence"] = {
        "pending_path": "/srv/p.jsonl", "decided_path": "/srv/d.jsonl",
    }
    ds = load_daily_sync(raw)
    assert ds.tier_recurrence.pending_path == "/srv/p.jsonl"
    assert ds.tier_recurrence.decided_path == "/srv/d.jsonl"


def test_a_partial_override_leaves_the_sibling_derived():
    """Overriding one path must not strand the other on a placeholder."""
    raw = cfg_for("KAL-LE")
    raw["daily_sync"]["tier_recurrence"] = {"pending_path": "/srv/p.jsonl"}
    ds = load_daily_sync(raw)
    assert ds.tier_recurrence.pending_path == "/srv/p.jsonl"
    assert ds.tier_recurrence.decided_path == \
        "./data/tier_recurrence_decided.kal-le.jsonl"


def test_explicit_routine_match_paths_are_honoured():
    raw = cfg_for("Salem")
    raw["routine"] = {"match_calibration": {"pending_path": "/srv/rp.jsonl"}}
    assert load_routine(raw).match_calibration.pending_path == "/srv/rp.jsonl"


# ---------------------------------------------------------------------------
# A missing instance name never inherits Salem's files
# ---------------------------------------------------------------------------


def test_an_unnamed_instance_gets_an_UNSCOPED_name_not_salems():
    """The established pattern is resolve-to-empty and let the daemon-start
    guard refuse — NOT raise at config load, which would break every minimal
    fixture. What must not happen is the unnamed instance quietly adopting
    Salem's file, which is exactly what the literal did."""
    raw = cfg_for(None)
    ds = load_daily_sync(raw)
    routine = load_routine(raw)

    assert ds.corpus.path == "./data/email_calibration.jsonl"
    assert routine.match_calibration.pending_path == \
        "./data/routine_match_pending.jsonl"
    for path in (ds.corpus.path, routine.match_calibration.pending_path):
        assert "salem" not in path


def test_config_loading_does_not_raise_on_a_missing_instance_name():
    load_daily_sync({"daily_sync": {"enabled": True}})
    load_routine({"routine": {}})


# ---------------------------------------------------------------------------
# The shared helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw_name,slug", [
    ("Salem", "salem"), ("KAL-LE", "kal-le"), ("  Hypatia  ", "hypatia"),
    ("Two Words", "two-words"), ("VERA", "vera"),
])
def test_slug_normalisation(raw_name: str, slug: str):
    assert instance_slug(raw_name) == slug


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_a_blank_instance_is_a_hard_error_in_the_strict_helper(blank):
    with pytest.raises(InstanceSlugError):
        instance_slug(blank)  # type: ignore[arg-type]


def test_the_tolerant_helper_degrades_instead_of_raising():
    assert instance_state_filename_or_unscoped("x", "", suffix="jsonl") == "x.jsonl"
    assert instance_state_filename_or_unscoped("x", "Salem", suffix="jsonl") == \
        "x.salem.jsonl"


def test_filename_shape():
    assert instance_state_filename("board_snooze", "Salem", suffix="json") == \
        "board_snooze.salem.json"


def test_batch_slug_delegates_to_the_shared_one():
    """ONE derivation. The files that carry an instance in their NAME and the
    directories that carry one in their PATH must agree about what 'Salem'
    slugs to — two spellings is how a writer and a reader diverge."""
    from alfred.batch.paths import BatchPathError, instance_slug as batch_slug

    for name in ("Salem", "KAL-LE", "Two Words"):
        assert batch_slug(name) == instance_slug(name)

    # ...and the module's own error contract is unchanged.
    with pytest.raises(BatchPathError):
        batch_slug("")
