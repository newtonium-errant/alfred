"""#74 batch 1 — the four allowlisted debris leakers, anchored per-instance.

Each was a cwd-relative DEFAULT applied when a config omits the field: tests
set ``logging.dir`` to a tmp dir, the module ignored it and resolved against
the process cwd, and the suite wrote into the repo tree. On the box the same
defect is a cross-instance collision — one WorkingDirectory shared by Salem,
KAL-LE, Hypatia and VERA, so "./data/x" is ONE file for all four (#53's shape,
and the 2026-07-31 feed-store incident for real).

Every fix here has the same two obligations, pinned per leaker:

  * **byte-identity** — Salem's ``logging.dir`` IS ``./data``, so the derived
    default must reproduce the exact string the literal produced. That is what
    makes this a zero-migration change: no file moves on the box.
  * **distinctness** — two instances must resolve DIFFERENT paths, which is
    the property the literal did not have.

Tests run unconditionally per ``feedback_regression_pin_unconditional.md``.
"""
from __future__ import annotations

# A KAL-LE-shaped absolute data dir — a second, co-located instance.
_KALLE_DATA_DIR = "/home/andrew/.alfred/kalle/data"
# Salem's data dir, verified on the box 2026-08-11: logging.dir is the
# cwd-relative "./data" (WorkingDirectory makes it /data/algernon/alfred/data).
_SALEM_DATA_DIR = "./data"


# ===========================================================================
# Leaker 1 — scribe.input_dir  (data/scribe/scribe/{negation_candidates,
#            notegen_edit}.jsonl + their two lock files)
# ===========================================================================

def _scribe(raw: dict) -> object:
    from alfred.scribe.config import load_from_unified

    return load_from_unified(raw)


def test_scribe_input_dir_anchors_on_logging_dir() -> None:
    assert _scribe({"logging": {"dir": _KALLE_DATA_DIR}}).input_dir == (
        f"{_KALLE_DATA_DIR}/scribe/inbox"
    )


def test_scribe_salem_default_is_byte_identical() -> None:
    # The pre-#74 literal was exactly "./data/scribe/inbox".
    assert _scribe({"logging": {"dir": _SALEM_DATA_DIR}}).input_dir == "./data/scribe/inbox"


def test_scribe_default_unchanged_when_no_logging_block() -> None:
    # Minimal fixtures (and any config without a logging block) keep the old
    # value — the retrofit must not change behaviour where there is no anchor.
    assert _scribe({}).input_dir == "./data/scribe/inbox"
    assert _scribe({"scribe": {"mode": "synthetic"}}).input_dir == "./data/scribe/inbox"


def test_scribe_blank_logging_dir_does_not_root_anchor() -> None:
    # The hole the shared resolver closes: a blank dir must NOT produce the
    # root-anchored "/scribe/inbox".
    assert _scribe({"logging": {"dir": "   "}}).input_dir == "./data/scribe/inbox"


def test_scribe_explicit_input_dir_wins() -> None:
    cfg = _scribe({
        "logging": {"dir": _KALLE_DATA_DIR},
        "scribe": {"input_dir": "/x/inbox"},
    })
    assert cfg.input_dir == "/x/inbox"


def test_scribe_input_dir_is_instance_scoped_and_distinct() -> None:
    salem = _scribe({"logging": {"dir": _SALEM_DATA_DIR}}).input_dir
    kalle = _scribe({"logging": {"dir": _KALLE_DATA_DIR}}).input_dir
    # Mutation check — revert the default to the cwd-relative literal and both
    # collapse to "./data/scribe/inbox", reddening this.
    assert salem != kalle


def test_scribe_derived_default_carries_the_resolvers_with_it(tmp_path) -> None:
    """The write sinks — the files that actually leaked — follow the anchor.

    ``resolve_candidates_dir`` / ``resolve_notegen_feedback_dir`` derive from
    ``input_dir``, so anchoring the default is what moves the four leaked files
    out of the repo tree and into the instance's data dir.
    """
    from alfred.scribe.negation_suppression import resolve_candidates_dir
    from alfred.scribe.notegen_feedback import resolve_notegen_feedback_dir

    cfg = _scribe({"logging": {"dir": str(tmp_path)}})
    for resolved in (resolve_candidates_dir(cfg), resolve_notegen_feedback_dir(cfg)):
        # The load-bearing assertion: the sink is under the configured dir, so
        # nothing lands in the cwd.
        assert str(resolved).startswith(str(tmp_path))


def test_scribe_doubling_is_preserved_deliberately(tmp_path) -> None:
    """BATCH 1 DOES NOT FIX THE DOUBLING — this pin says so out loud.

    The resolvers compute ``<input_dir>.parent / "scribe"`` on the contract
    that ``input_dir`` is ``<DATA>/inbox``. The default's parent is already
    ``<DATA>/scribe``, so they land on ``<DATA>/scribe/scribe``. Anchoring the
    default (batch 1) moves the whole structure under the configured data dir
    without re-siting anything inside it, which is the point: the doubling is
    preserved EXACTLY, so this commit cannot move an operator's files.

    Batch 2 re-sites it and FLIPS this pin — that is the intended signal, not
    a regression. Until then the doubling is harmless: it doubles inside
    whatever data dir is configured, never in the cwd.
    """
    from alfred.scribe.negation_suppression import resolve_candidates_dir

    cfg = _scribe({"logging": {"dir": str(tmp_path)}})
    assert resolve_candidates_dir(cfg) == tmp_path / "scribe" / "scribe"
