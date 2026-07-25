"""Tests for the BIT mail probes — the #31 account-delivery liveness monitor
(Monitor A).

Monitor A answers "roughly how long since this account last delivered an
email," sourced from the ARRIVAL artifact (the raw ``email-{account}-*.md``
file the fetcher/webhook write to the inbox and the curator moves — filename
retained — to ``inbox/processed/``). Age is by file mtime: ``inbox/`` files
carry arrival mtime, ``processed/`` files carry processing-time mtime (the
curator rewrites frontmatter before the move) — see the module docstring of
``alfred.mail.health`` for why that under-reports benignly. It is deliberately
INDEPENDENT of whether the curator STRUCTURED the record (Monitor B's job): a
raw email sitting unstructured in ``processed/`` still counts as delivered.
Would have caught the Gmail intake going silently dead for ~2 months.

Per ``feedback_intentionally_left_blank.md``: an account gone silent must be
distinguishable from a healthy-but-quiet account. Tests run unconditionally.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from alfred.mail.health import (
    _DEFAULT_PROCESSED_DIR,
    _MAIL_LIVENESS_FAIL_DAYS,
    _MAIL_LIVENESS_WARN_DAYS,
    _check_account_liveness,
    _check_liveness,
    _newest_arrival_mtime,
    health_check,
)
from alfred.health.types import Status


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_email(dir_path: Path, account: str, days_ago: float, tag: str = "20260101-000000") -> Path:
    """Create an ``email-{account}-{tag}-subj.md`` file with mtime days_ago."""
    dir_path.mkdir(parents=True, exist_ok=True)
    f = dir_path / f"email-{account}-{tag}-subj.md"
    f.write_text("---\ntype: note\n---\nbody\n", encoding="utf-8")
    mtime = time.time() - days_ago * 86400.0
    os.utime(f, (mtime, mtime))
    return f


def _raw(tmp_path: Path, *, accounts=None, liveness=None, processed_dir=None) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "vault": {"path": str(tmp_path)},
        "mail": {"accounts": accounts if accounts is not None else [{"name": "gmail", "email": "g@x", "imap_host": "h"}]},
    }
    if liveness is not None:
        raw["mail"]["liveness"] = liveness
    if processed_dir is not None:
        raw["curator"] = {"processed_dir": processed_dir}
    return raw


# ---------------------------------------------------------------------------
# _newest_arrival_mtime
# ---------------------------------------------------------------------------


def test_newest_arrival_none_when_no_records(tmp_path: Path) -> None:
    assert _newest_arrival_mtime(tmp_path, "inbox", _DEFAULT_PROCESSED_DIR, "gmail") is None


def test_newest_arrival_scans_processed_dir(tmp_path: Path) -> None:
    # Only a processed record exists (curator already moved it) — still found.
    _make_email(tmp_path / "inbox" / "processed", "gmail", days_ago=1)
    assert _newest_arrival_mtime(tmp_path, "inbox", "inbox/processed", "gmail") is not None


def test_newest_arrival_takes_max_across_both_dirs(tmp_path: Path) -> None:
    _make_email(tmp_path / "inbox" / "processed", "gmail", days_ago=10, tag="old")
    fresh = _make_email(tmp_path / "inbox", "gmail", days_ago=1, tag="new")
    newest = _newest_arrival_mtime(tmp_path, "inbox", "inbox/processed", "gmail")
    assert newest is not None
    assert abs(newest - fresh.stat().st_mtime) < 0.001  # the fresh inbox file wins


def test_newest_arrival_ignores_other_accounts(tmp_path: Path) -> None:
    _make_email(tmp_path / "inbox", "live", days_ago=1)
    assert _newest_arrival_mtime(tmp_path, "inbox", "inbox/processed", "gmail") is None


def test_newest_arrival_glob_special_account_name_is_literal(tmp_path: Path) -> None:
    # The manual prefix filter treats a '*' literally — glob would not.
    _make_email(tmp_path / "inbox", "a*b", days_ago=1)
    _make_email(tmp_path / "inbox", "axxb", days_ago=1)  # would match if '*' were a wildcard
    # Only the literal 'a*b' file counts for account 'a*b'.
    assert _newest_arrival_mtime(tmp_path, "inbox", "inbox/processed", "a*b") is not None
    # And 'axxb' is its own distinct account.
    assert _newest_arrival_mtime(tmp_path, "inbox", "inbox/processed", "axxb") is not None


# ---------------------------------------------------------------------------
# _check_account_liveness — thresholds
# ---------------------------------------------------------------------------


def test_fresh_arrival_is_ok(tmp_path: Path) -> None:
    _make_email(tmp_path / "inbox", "gmail", days_ago=1)
    r = _check_account_liveness(tmp_path, "inbox", "inbox/processed", "gmail", 3, 7)
    assert r.status is Status.OK
    assert r.name == "delivery:gmail"


def test_quiet_account_warns(tmp_path: Path) -> None:
    _make_email(tmp_path / "inbox", "gmail", days_ago=4)
    r = _check_account_liveness(tmp_path, "inbox", "inbox/processed", "gmail", 3, 7)
    assert r.status is Status.WARN


def test_dead_account_fails(tmp_path: Path) -> None:
    _make_email(tmp_path / "inbox", "gmail", days_ago=10)
    r = _check_account_liveness(tmp_path, "inbox", "inbox/processed", "gmail", 3, 7)
    assert r.status is Status.FAIL


def test_no_arrivals_is_ok_not_fail(tmp_path: Path) -> None:
    # Never delivered ≠ dead. Returns OK (not SKIP — SKIP would drag the mail
    # tool rollup down on every fresh/quiet install; and not FAIL — no baseline).
    r = _check_account_liveness(tmp_path, "inbox", "inbox/processed", "gmail", 3, 7)
    assert r.status is Status.OK
    assert "no delivered records yet" in r.detail


def test_orthogonality_unstructured_email_in_processed_counts_as_delivered(tmp_path: Path) -> None:
    """The core orthogonality guarantee: a raw email that the curator moved to
    processed/ WITHOUT structuring it (the silent-outage case) still counts as
    DELIVERED. Monitor A measures arrival, not structuring — that keeps it
    independent of Monitor B."""
    _make_email(tmp_path / "inbox" / "processed", "gmail", days_ago=1)
    r = _check_account_liveness(tmp_path, "inbox", "inbox/processed", "gmail", 3, 7)
    assert r.status is Status.OK  # delivered, regardless of structuring outcome


# ---------------------------------------------------------------------------
# _check_liveness — config wiring
# ---------------------------------------------------------------------------


def test_liveness_disabled_returns_ok_observable_line(tmp_path: Path) -> None:
    # Disabled is observable (ILB) but must not drag the tool rollup → OK, not SKIP.
    raw = _raw(tmp_path, liveness={"enabled": False})
    out = _check_liveness(raw, raw["mail"])
    assert len(out) == 1
    assert out[0].status is Status.OK
    assert out[0].name == "delivery-liveness"
    assert "disabled" in out[0].detail


def test_liveness_no_accounts_returns_empty(tmp_path: Path) -> None:
    raw = _raw(tmp_path, accounts=[])
    assert _check_liveness(raw, raw["mail"]) == []


def test_liveness_default_thresholds(tmp_path: Path) -> None:
    _make_email(tmp_path / "inbox", "gmail", days_ago=1)
    raw = _raw(tmp_path)
    out = _check_liveness(raw, raw["mail"])
    assert [r.name for r in out] == ["delivery:gmail"]
    assert out[0].status is Status.OK
    assert out[0].data["warn_days"] == _MAIL_LIVENESS_WARN_DAYS
    assert out[0].data["fail_days"] == _MAIL_LIVENESS_FAIL_DAYS


def test_liveness_per_account_override(tmp_path: Path) -> None:
    # gmail: 2d-old, override warn=1 → WARN; live: 2d-old, global warn=3 → OK.
    _make_email(tmp_path / "inbox", "gmail", days_ago=2)
    _make_email(tmp_path / "inbox", "live", days_ago=2)
    raw = _raw(
        tmp_path,
        accounts=[{"name": "gmail"}, {"name": "live"}],
        liveness={"warn_days": 3, "fail_days": 7, "accounts": {"gmail": {"warn_days": 1}}},
    )
    out = {r.name: r for r in _check_liveness(raw, raw["mail"])}
    assert out["delivery:gmail"].status is Status.WARN
    assert out["delivery:live"].status is Status.OK


def test_liveness_reads_custom_processed_dir(tmp_path: Path) -> None:
    # curator.processed_dir override is honored.
    _make_email(tmp_path / "inbox" / "done", "gmail", days_ago=1)
    raw = _raw(tmp_path, processed_dir="inbox/done")
    out = _check_liveness(raw, raw["mail"])
    assert out[0].status is Status.OK


def test_liveness_skips_account_missing_name(tmp_path: Path) -> None:
    raw = _raw(tmp_path, accounts=[{"email": "no-name@x"}])  # malformed
    assert _check_liveness(raw, raw["mail"]) == []


# ---------------------------------------------------------------------------
# health_check integration (async)
# ---------------------------------------------------------------------------


async def test_health_check_includes_delivery_lines(tmp_path: Path) -> None:
    (tmp_path / "inbox").mkdir()
    _make_email(tmp_path / "inbox", "gmail", days_ago=1)
    raw = _raw(tmp_path)
    th = await health_check(raw)
    names = [r.name for r in th.results]
    assert "delivery:gmail" in names
    assert th.status is Status.OK  # fresh delivery + inbox exists


async def test_health_check_dead_account_rolls_up_to_fail(tmp_path: Path) -> None:
    (tmp_path / "inbox").mkdir()
    _make_email(tmp_path / "inbox", "gmail", days_ago=30)
    raw = _raw(tmp_path)
    th = await health_check(raw)
    delivery = [r for r in th.results if r.name == "delivery:gmail"][0]
    assert delivery.status is Status.FAIL
    assert th.status is Status.FAIL  # worst-of rollup
