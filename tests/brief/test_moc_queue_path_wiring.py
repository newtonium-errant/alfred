"""The MOC suggestion queue path — one file, resolved by both sides.

WHAT THIS MODULE EXISTS TO CATCH, stated plainly because the obvious pin does
not catch it. The MOC reader shipped and never executed on any instance:
``brief.daemon._moc_queue_path_for`` and ``daily_sync.action_router.
_moc_queue_path`` both walked ``getattr(config, "surveyor", None)`` against
typed dataclasses that never declared a ``surveyor`` field. ``getattr`` with a
default cannot raise, so both manufactured ``None`` for every config forever —
the brief short-circuited before its producer was ever called, and an operator
affirm wrote the vault and then silently failed to flip the queue row.

Three properties of that bug decide how these tests are written:

* **A test tolerating ``None`` certifies nothing.** ``None`` is exactly what a
  correct-but-unreachable resolver returns. Every pin here asserts a POSITIVE
  path value.
* **A hand-built stub is worse than no fixture.** The pre-existing act test
  did ``cfg.surveyor = _Surveyor()`` — the dataclass is not frozen, so the
  assignment succeeded and manufactured the very shape production lacks. It
  proved the resolver worked on a config that cannot exist. Every config here
  is built by the PRODUCTION loader from a real YAML file.
* **A mirrored bug is self-consistent.** Both sides being wrong the same way
  is invisible to any pin that checks one side. The headline pin drives BOTH
  loaders from ONE raw dict and asserts they agree AND that they agree with
  the surveyor's own enqueue-side derivation.

The fixture's SHAPE is sampled from the live Hypatia config rather than
invented: ``surveyor.moc_suggestion`` sets ``enabled: true`` and NO
``queue_path``, and ``surveyor.state.path`` is set. That matters — it means
the FALLBACK branch is the one production runs, and a pin that only exercised
the explicit ``queue_path`` branch (as the pre-existing one did) would leave
the live path unpinned. ``test_live_shape_premise_the_fallback_branch_is_the_
one_that_runs`` pins that claim against the schema so it cannot rot silently.

No test here reads ``config.yaml.example`` — that file belongs to another lane
this cycle, and a pin that reads it would couple this module's colour to that
lane's edits.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import structlog
import yaml

from alfred.brief.config import load_from_unified as load_brief
from alfred.brief.daemon import _moc_queue_path_for
from alfred.daily_sync.action_router import _moc_queue_path
from alfred.daily_sync.config import load_from_unified as load_ds
from alfred.surveyor.config import (
    MocSuggestionConfig,
    StateConfig,
    resolve_moc_queue_path,
)
from alfred.surveyor.config import load_from_unified as load_surveyor

# The queue file's name, spelled as a literal ON PURPOSE. Importing
# ``derive_default_queue_path`` and comparing against its own output would make
# every fixture here move with the bug: change the derivation and the
# expectation follows it, scoring a comfortable green on a broken build. A
# literal is an expectation the change cannot touch.
QUEUE_FILENAME = "moc_suggestions.jsonl"

# The surveyor's documented state-path default. Pinned against the schema in
# ``test_live_shape_premise...`` below rather than trusted here.
SURVEYOR_STATE_DEFAULT = "./data/surveyor_state.json"


def _write_config(tmp_path: Path, *, surveyor: dict | None) -> Path:
    """A real config FILE, parsed by real YAML, in the live instance's shape.

    ``surveyor=None`` omits the block entirely — the one shape for which
    ``None`` is the correct answer, and the negative half of the exclusion
    pins below.
    """
    raw: dict = {
        "vault": {"path": str(tmp_path / "vault")},
        "telegram": {"instance": {"name": "Hypatia"}},
        "logging": {"dir": str(tmp_path / "data")},
        "brief": {"schedule": {"time": "06:00"}},
        "daily_sync": {"enabled": True},
    }
    if surveyor is not None:
        raw["surveyor"] = surveyor
    path = tmp_path / "config.hypatia.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return path


def _load_raw(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# The headline pin — one config, both sides, one file
# ---------------------------------------------------------------------------


def test_reader_and_writer_resolve_to_the_same_file(tmp_path: Path) -> None:
    """THE pin. Both production loaders, one raw dict, one positive path.

    A mirrored defect is self-consistent and therefore invisible to a
    single-sided check, so this asserts three things at once: the reader
    resolves a REAL path, the writer resolves a REAL path, and they are the
    SAME path. Driven through the fallback branch, which is what live configs
    use.
    """
    state_path = tmp_path / "instance-data" / "surveyor_state.json"
    cfg_file = _write_config(tmp_path, surveyor={
        "state": {"path": str(state_path)},
        # Live Hypatia shape: enabled, and NO queue_path.
        "moc_suggestion": {"enabled": True},
    })
    raw = _load_raw(cfg_file)

    expected = state_path.parent / QUEUE_FILENAME

    reader = _moc_queue_path_for(load_brief(raw))
    writer = _moc_queue_path(load_ds(raw))

    # POSITIVE values, asserted before the equality — two ``None``s are equal
    # to each other, and that is precisely the bug this module exists to
    # catch. Equality alone would pass against the broken build.
    assert reader == expected, "the brief's reader resolved the wrong file"
    assert writer == expected, "the act router resolved the wrong file"
    assert reader == writer


def test_resolved_path_matches_the_surveyors_own_enqueue_derivation(
    tmp_path: Path,
) -> None:
    """PREMISE PIN — the readers agree with the side that WRITES the rows.

    The two readers sharing one helper only proves they cannot disagree with
    each other; it says nothing about whether they agree with the surveyor,
    which is the only process that creates a row. The expectation here is
    rebuilt from the surveyor's FULL typed config by a literal spelling of the
    sibling-file convention, so it is independent of the helper under test:
    if ``resolve_queue_path`` and the surveyor's state config ever drift, this
    goes red while the same-file pin above stays green.
    """
    state_path = tmp_path / "instance-data" / "surveyor_state.json"
    raw = _load_raw(_write_config(tmp_path, surveyor={
        "state": {"path": str(state_path)},
        "moc_suggestion": {"enabled": True},
    }))

    surveyor_cfg = load_surveyor(raw)
    enqueue_target = Path(surveyor_cfg.state.path).parent / QUEUE_FILENAME

    assert enqueue_target.name == QUEUE_FILENAME
    assert _moc_queue_path_for(load_brief(raw)) == enqueue_target
    assert _moc_queue_path(load_ds(raw)) == enqueue_target


def test_live_shape_premise_the_fallback_branch_is_the_one_that_runs() -> None:
    """PREMISE PIN for this module's fixtures, against the SCHEMA.

    Every fixture above omits ``moc_suggestion.queue_path`` because the live
    configs do — the explicit branch never fires in the field, so pinning only
    that branch (which is what the pre-existing act test did) leaves the
    production path uncovered. That is a claim about the schema, so it is
    checked against the schema rather than assumed:

    * ``queue_path`` defaults to ``None`` — so an operator who does not set it
      lands on the fallback.
    * ``state.path`` carries a non-empty default — so a ``surveyor:`` block
      ALWAYS resolves to something, which is what lets the callers' idle logs
      claim "no surveyor block" as the single cause of a ``None``.
    """
    assert MocSuggestionConfig().queue_path is None
    assert StateConfig().path == SURVEYOR_STATE_DEFAULT


# ---------------------------------------------------------------------------
# Branch coverage — the fallback, the explicit, and the default
# ---------------------------------------------------------------------------


def test_surveyor_block_without_a_state_key_still_resolves(
    tmp_path: Path,
) -> None:
    """The drift a raw-dict read would have introduced.

    ``StateConfig.path`` has a default, so a config carrying ``surveyor:`` with
    no ``state:`` key still enqueues — to the default's sibling. A resolver
    that read ``surveyor["state"]["path"]`` off the raw dict would answer "no
    queue" about a queue that is actively being written, which is the same
    class of silent-``None`` failure this whole module is about, just one
    level down.
    """
    raw = _load_raw(_write_config(tmp_path, surveyor={
        "moc_suggestion": {"enabled": True},
    }))

    expected = Path(SURVEYOR_STATE_DEFAULT).parent / QUEUE_FILENAME
    assert _moc_queue_path_for(load_brief(raw)) == expected
    assert _moc_queue_path(load_ds(raw)) == expected


def test_explicit_queue_path_wins_over_the_derivation(tmp_path: Path) -> None:
    """The explicit branch still works — it is just not the live one."""
    explicit = tmp_path / "elsewhere" / "custom_queue.jsonl"
    raw = _load_raw(_write_config(tmp_path, surveyor={
        "state": {"path": str(tmp_path / "data" / "surveyor_state.json")},
        "moc_suggestion": {"enabled": True, "queue_path": str(explicit)},
    }))

    assert _moc_queue_path_for(load_brief(raw)) == explicit
    assert _moc_queue_path(load_ds(raw)) == explicit


def test_no_surveyor_block_resolves_to_none_but_a_real_one_does_not(
    tmp_path: Path,
) -> None:
    """EXCLUSION PIN WITH ITS POSITIVE CONTROL, in one test on purpose.

    "No surveyor block -> None" is vacuous on its own: it passes identically
    against a resolver that returns ``None`` for everything, which is the
    build this module was written against. The control is the second half —
    the nearest admissible neighbour, a config identical but for the
    ``surveyor:`` block, must resolve to a real path.
    """
    without = _load_raw(_write_config(tmp_path, surveyor=None))
    with_block = _load_raw(_write_config(tmp_path, surveyor={
        "state": {"path": str(tmp_path / "data" / "surveyor_state.json")},
        "moc_suggestion": {"enabled": True},
    }))

    assert resolve_moc_queue_path(without) is None
    assert _moc_queue_path_for(load_brief(without)) is None
    assert _moc_queue_path(load_ds(without)) is None

    # THE CONTROL — without this, every assertion above passes on a build
    # whose resolver is dead.
    assert resolve_moc_queue_path(with_block) is not None
    assert _moc_queue_path_for(load_brief(with_block)) is not None
    assert _moc_queue_path(load_ds(with_block)) is not None


def test_env_substitution_is_applied_to_the_state_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``${VAR}`` expands, exactly as the surveyor's own loader expands it.

    A resolver reading the raw dict without substitution would hand the
    literal string ``${MOC_TEST_DATA_DIR}/surveyor_state.json`` downstream and
    the reader would look for a directory named ``${MOC_TEST_DATA_DIR}``.
    """
    monkeypatch.setenv("MOC_TEST_DATA_DIR", str(tmp_path / "expanded"))
    raw = _load_raw(_write_config(tmp_path, surveyor={
        "state": {"path": "${MOC_TEST_DATA_DIR}/surveyor_state.json"},
        "moc_suggestion": {"enabled": True},
    }))

    expected = tmp_path / "expanded" / QUEUE_FILENAME
    assert _moc_queue_path_for(load_brief(raw)) == expected
    assert _moc_queue_path(load_ds(raw)) == expected


# ---------------------------------------------------------------------------
# The declared-field contract — why the bug cannot recur silently
# ---------------------------------------------------------------------------


def test_both_configs_declare_the_field_rather_than_growing_it(
    tmp_path: Path,
) -> None:
    """The structural half of the fix, pinned.

    The defect's mechanism was a lookup that COULD NOT FAIL: ``getattr(cfg,
    "surveyor", None)`` on a class with no such field returns ``None`` rather
    than raising, so a wiring error wore the costume of an unset option. Both
    configs now DECLARE ``moc_queue_path``, so a rename or a drop is an
    AttributeError at the read site instead of another silent deployment.

    Asserting on ``__dataclass_fields__`` rather than on ``hasattr``: these
    dataclasses are not frozen, so ``hasattr`` is satisfied by an instance
    attribute glued on after construction — which is precisely how the
    pre-existing act fixture manufactured a ``surveyor`` attribute production
    never had. Only field DECLARATION distinguishes the two.
    """
    from alfred.brief.config import BriefConfig
    from alfred.daily_sync.config import DailySyncConfig

    assert "moc_queue_path" in BriefConfig.__dataclass_fields__
    assert "moc_queue_path" in DailySyncConfig.__dataclass_fields__

    # And the loader populates it — a declared field nobody stamps is the
    # threading failure one layer over.
    raw = _load_raw(_write_config(tmp_path, surveyor={
        "state": {"path": str(tmp_path / "data" / "surveyor_state.json")},
        "moc_suggestion": {"enabled": True},
    }))
    assert load_brief(raw).moc_queue_path
    assert load_ds(raw).moc_queue_path


def test_daily_sync_resolves_the_queue_without_a_daily_sync_block(
    tmp_path: Path,
) -> None:
    """The stamp sits OUTSIDE the ``daily_sync:``-absent branch, and must.

    ``POST /feed/act`` loads this config whether or not the instance runs a
    daily_sync block, so stamping the queue path only on the populated branch
    would leave the ledger write dead on exactly the instances that act on
    cards. Hypatia is the instance with the MOC data.
    """
    raw = _load_raw(_write_config(tmp_path, surveyor={
        "state": {"path": str(tmp_path / "data" / "surveyor_state.json")},
        "moc_suggestion": {"enabled": True},
    }))
    raw.pop("daily_sync")

    cfg = load_ds(raw)
    assert cfg.enabled is False
    assert _moc_queue_path(cfg) == tmp_path / "data" / QUEUE_FILENAME


# ---------------------------------------------------------------------------
# The idle signal — a reason that is true, and a positive observation
# ---------------------------------------------------------------------------


def test_no_surveyor_idle_log_states_a_checkable_reason(tmp_path: Path) -> None:
    """ILB, pinned at the emission — including the field that PROVES it.

    The predecessor of this log fired on every instance claiming the queue
    path was "unresolved in this config", which reads as a healthy opt-out and
    is indistinguishable from the resolver being broken. It WAS broken. So the
    pin asserts the reason field's content, not merely that some line was
    emitted: an idle signal separates idle from broken only while the reason
    it gives is true.
    """
    from alfred.brief.daemon import _emit_moc_suggestions

    raw = _load_raw(_write_config(tmp_path, surveyor=None))
    cfg = load_brief(raw)

    with structlog.testing.capture_logs() as captured:
        _emit_moc_suggestions(cfg, store=None, instance="hypatia")

    events = [c for c in captured if c.get("event") == "brief.moc_suggestion_no_surveyor"]
    assert len(events) == 1
    assert events[0]["surveyor_configured"] is False
    assert events[0]["instance"] == "hypatia"
    assert "no surveyor: block" in events[0]["reason"]
    assert events[0]["check"]

    # THE CONTROL for the assertion above: the same call on a surveyor-bearing
    # config must NOT take this branch. Without it, the pin is green on a
    # build that short-circuits unconditionally — which is the build that
    # shipped.
    with_block = _load_raw(_write_config(tmp_path, surveyor={
        "state": {"path": str(tmp_path / "data" / "surveyor_state.json")},
        "moc_suggestion": {"enabled": True},
    }))
    with structlog.testing.capture_logs() as captured2:
        _emit_moc_suggestions(load_brief(with_block), store=None, instance="hypatia")

    assert not [
        c for c in captured2 if c.get("event") == "brief.moc_suggestion_no_surveyor"
    ]
    # Stronger than the absence above: name what it reached instead. The
    # ``store=None`` here is deliberate — getting as far as the store read is
    # itself the evidence that the queue-path branch was passed rather than
    # taken, which absence alone cannot show.
    assert [
        c for c in captured2
        if c.get("event") == "brief.moc_suggestion_store_unreadable"
    ]
