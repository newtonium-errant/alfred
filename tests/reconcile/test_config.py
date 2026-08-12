"""The ``reconcile:`` config block.

The properties under test:

  1. **The store path is INSTANCE-scoped and CONFIG-derived.** Two
     instances sharing a data dir must not share a ledger — that is the
     2026-07-31 cross-contamination shape, and it is the whole reason the
     slug is in the path.
  2. **No instance name is hardcoded anywhere.** The default is wrong on
     every instance but one if it names one.
  3. **An unanchored store means reconcile is OFF**, loudly — never a
     cwd-relative guess.
  4. **The config-derived path and the paths-module path agree.** They are
     two entry points to one location and drift between them would put a
     writer and a reader on different files.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
import structlog

from alfred.reconcile import config as config_mod
from alfred.reconcile.attention import CLASS_DUPLICATE_DENIAL
from alfred.reconcile.config import (
    DEFAULT_MIN_CORRECTION_SUPPORT,
    ReconcileConfig,
    load_from_unified,
)
from alfred.reconcile.paths import remittance_root


@pytest.fixture(autouse=True)
def _reset_unanchored_latch():
    """The unanchored warning is latched once per process. Reset it so each
    test that expects the warning actually sees it — a latched-out warning
    would make the pin vacuously pass."""
    config_mod._unanchored_logged = False
    yield
    config_mod._unanchored_logged = False


def _raw(instance="VERA", data_dir="/srv/vera/data", **reconcile):
    out = {
        "logging": {"dir": data_dir},
        "telegram": {"instance": {"name": instance}},
    }
    if reconcile:
        out["reconcile"] = reconcile
    return out


def test_store_dir_defaults_under_the_instance_data_dir():
    cfg = load_from_unified(_raw())
    assert cfg.enabled
    assert cfg.store_dir == "/srv/vera/data/remittance/vera"
    assert cfg.instance_name == "VERA"


def test_two_instances_sharing_a_data_dir_do_not_share_a_ledger():
    """The cross-contamination guard. On the box every instance shares one
    WorkingDirectory and differs only by --config."""
    a = load_from_unified(_raw(instance="VERA", data_dir="./data"))
    b = load_from_unified(_raw(instance="Salem", data_dir="./data"))
    assert a.store_dir != b.store_dir


def test_the_config_path_and_the_paths_module_agree():
    """One location, two entry points. Drift here would put the CLI writer
    and any (data_dir, instance) reader on different ledgers."""
    cfg = load_from_unified(_raw(instance="VERA", data_dir="/srv/vera/data"))
    assert Path(cfg.store_dir) == remittance_root("/srv/vera/data", "VERA")


def test_a_relative_data_dir_keeps_its_exact_string():
    """Byte-identity with the legacy literal: a pathlib join would normalise
    ``./data`` to ``data`` and move an existing store."""
    cfg = load_from_unified(_raw(data_dir="./data"))
    assert cfg.store_dir.startswith("./data/")


def test_an_explicit_store_dir_always_wins():
    cfg = load_from_unified(_raw(store_dir="/elsewhere/remit"))
    assert cfg.store_dir == "/elsewhere/remit"


def _executable_string_constants(path: Path) -> list[str]:
    """Every string literal in a module EXCEPT its docstrings.

    Docstrings are excluded deliberately. The per-instance-defaults rule is
    about default VALUES, not about prose: this codebase names Salem and
    KAL-LE constantly in explanatory comments (they are the incidents the
    rules came from), and a check that failed on those would be noise that
    teaches people to delete the explanation. What must not exist is an
    instance name a code path can actually RETURN.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstring_nodes: set[int] = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            docstring_nodes.add(id(body[0].value))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstring_nodes
    ]


def test_no_instance_name_is_a_value_any_code_path_can_return():
    """The hardcoding guard, at the source level rather than hoping a test
    happened to exercise the path that would have returned it."""
    literals = _executable_string_constants(Path(config_mod.__file__))
    for literal in literals:
        lowered = literal.lower()
        for name in ("salem", "kal-le", "kalle", "hypatia", "vera", "bluebird"):
            assert name not in lowered, (
                f"instance name {name!r} appears in an executable string "
                f"literal {literal!r} — a default that names one instance is "
                f"wrong on every other one"
            )


def test_the_hardcoding_guard_can_actually_fire(tmp_path):
    """The positive control for the pin above. A check that cannot fail is
    not a check: this proves the helper DOES see an instance name when one
    is present in executable code, and does NOT see one that appears only
    in a docstring."""
    module = tmp_path / "sample.py"
    module.write_text(
        '"""A docstring mentioning Salem, which must be ignored."""\n'
        'DEFAULT = "salem"\n',
        encoding="utf-8",
    )
    literals = [s.lower() for s in _executable_string_constants(module)]
    assert "salem" in literals
    assert not any("docstring mentioning" in s for s in literals)


def test_a_missing_data_dir_disables_reconcile_loudly():
    """Fail-safe, not a cwd guess. Guessing the process cwd IS the defect."""
    with structlog.testing.capture_logs() as captured:
        cfg = load_from_unified({"telegram": {"instance": {"name": "VERA"}}})
    assert cfg.store_dir == ""
    assert cfg.enabled is False
    events = [
        c for c in captured
        if c.get("event") == "reconcile.config.disabled_no_store_dir"
    ]
    assert len(events) == 1
    assert "NOT fall back to the process cwd" in events[0]["detail"]


def test_a_missing_instance_name_disables_reconcile():
    cfg = load_from_unified({"logging": {"dir": "/srv/data"}})
    assert cfg.store_dir == ""
    assert cfg.enabled is False


def test_a_blank_instance_name_does_not_raise_at_load():
    """A blank name reaches config loading legitimately — a minimal fixture,
    an operator mid-setup. The established pattern is to resolve empty and
    let the command refuse, not to raise at load time."""
    cfg = load_from_unified(_raw(instance="   "))
    assert cfg.store_dir == ""
    assert cfg.enabled is False


def test_a_bare_config_is_disabled_not_cwd_relative():
    """The dataclass carries the coercion, so a bare construction (the
    shape a fixture or a default_factory reaches) is covered by the same
    mechanism as the loaded one."""
    assert ReconcileConfig().enabled is False
    assert ReconcileConfig().store_dir == ""


def test_missing_reconcile_block_still_yields_a_usable_config():
    cfg = load_from_unified(_raw())
    assert cfg.enabled
    assert cfg.eob_classes == {}
    assert cfg.min_correction_support == DEFAULT_MIN_CORRECTION_SUPPORT


def test_unknown_keys_in_the_block_are_ignored():
    """Schema-tolerant: a config written for a newer build loads unchanged."""
    raw = _raw(date_order="dmy")
    raw["reconcile"]["a_future_setting"] = True
    cfg = load_from_unified(raw)
    assert cfg.enabled
    assert not hasattr(cfg, "a_future_setting")


def test_a_non_dict_reconcile_block_is_tolerated():
    raw = _raw()
    raw["reconcile"] = "not a mapping"
    cfg = load_from_unified(raw)
    assert cfg.store_dir == "/srv/vera/data/remittance/vera"


def test_eob_codes_are_normalised_to_upper_case():
    cfg = load_from_unified(_raw(eob_classes={" zz14 ": CLASS_DUPLICATE_DENIAL}))
    assert cfg.eob_classes == {"ZZ14": CLASS_DUPLICATE_DENIAL}


def test_the_default_eob_map_is_empty():
    """Deliberate: the provider's real code list is not in this repo, and a
    fabricated mapping would classify real money with invented authority."""
    assert load_from_unified(_raw()).eob_classes == {}
    assert ReconcileConfig().eob_classes == {}


def test_a_code_mapped_to_an_unknown_class_is_kept_and_warned_not_dropped():
    """Kept, because classification fails OPEN on it — dropping the entry
    would SILENCE the lines instead, which is the wrong direction for
    money. The positive control: a valid mapping is kept without a warning.
    """
    with structlog.testing.capture_logs() as captured:
        cfg = load_from_unified(_raw(eob_classes={
            "ZZ99": "not_a_real_class", "ZZ14": CLASS_DUPLICATE_DENIAL,
        }))
    assert cfg.eob_classes["ZZ99"] == "not_a_real_class"
    assert cfg.eob_classes["ZZ14"] == CLASS_DUPLICATE_DENIAL
    events = [
        c for c in captured
        if c.get("event") == "reconcile.config.unknown_eob_classes"
    ]
    assert len(events) == 1
    assert "ZZ99" in events[0]["mappings"]
    assert "ZZ14" not in events[0]["mappings"]


@pytest.mark.parametrize("order", ["dmy", "mdy", "DMY", " Mdy "])
def test_valid_date_orders_are_accepted(order):
    assert load_from_unified(_raw(date_order=order)).date_order in ("dmy", "mdy")


def test_an_invalid_date_order_falls_back_to_refusing_and_says_so():
    """Falling back to REFUSING is the safe direction: an unreadable
    setting must not become a guess about what a date means."""
    with structlog.testing.capture_logs() as captured:
        cfg = load_from_unified(_raw(date_order="ymd"))
    assert cfg.date_order == ""
    events = [
        c for c in captured
        if c.get("event") == "reconcile.config.bad_date_order"
    ]
    assert len(events) == 1
    assert "REFUSED rather than guessed" in events[0]["detail"]


def test_date_order_is_unset_by_default():
    assert load_from_unified(_raw()).date_order == ""


@pytest.mark.parametrize("bad", [0, -3, "x", None])
def test_min_correction_support_is_floored_at_one(bad):
    cfg = load_from_unified(_raw(min_correction_support=bad))
    assert cfg.min_correction_support >= 1


def test_min_correction_support_default_is_two():
    """One ruling is as likely a one-off as a pattern."""
    assert DEFAULT_MIN_CORRECTION_SUPPORT == 2
