"""#34 curator quarantine-count health probe.

The quarantine mechanism (retry-in-place-then-quarantine) prevents silent data
loss — but a quarantine dir that accumulates files unseen would recreate the
very silent-state anti-pattern #31/#32 exist to kill. So the BIT surfaces it:
WARN when anything is quarantined (with a retry-failed hint), OK with an
explicit "0 quarantined" otherwise. Per feedback_intentionally_left_blank.md.
"""
from __future__ import annotations

from pathlib import Path

from alfred.curator.health import _check_quarantine
from alfred.health.types import Status


def test_no_failed_dir_is_ok_zero(tmp_path: Path) -> None:
    r = _check_quarantine({"vault": {"path": str(tmp_path)}})
    assert r.status is Status.OK
    assert r.data["count"] == 0


def test_empty_failed_dir_is_ok_zero(tmp_path: Path) -> None:
    (tmp_path / "inbox" / "failed").mkdir(parents=True)
    r = _check_quarantine({"vault": {"path": str(tmp_path)}})
    assert r.status is Status.OK
    assert r.data["count"] == 0
    assert "0 quarantined" in r.detail


def test_quarantined_files_warn_with_retry_hint(tmp_path: Path) -> None:
    failed = tmp_path / "inbox" / "failed"
    failed.mkdir(parents=True)
    (failed / "email-gmail-x.md").write_text("x", encoding="utf-8")
    (failed / "email-live-y.md").write_text("y", encoding="utf-8")
    (failed / ".gitkeep").write_text("", encoding="utf-8")  # dotfile ignored
    r = _check_quarantine({"vault": {"path": str(tmp_path)}})
    assert r.status is Status.WARN
    assert r.data["count"] == 2
    assert "retry-failed" in r.detail


def test_custom_failed_dir_honored(tmp_path: Path) -> None:
    q = tmp_path / "inbox" / "q"
    q.mkdir(parents=True)
    (q / "email-gmail-x.md").write_text("x", encoding="utf-8")
    raw = {
        "vault": {"path": str(tmp_path)},
        "curator": {"on_failure": {"failed_dir": "inbox/q"}},
    }
    r = _check_quarantine(raw)
    assert r.status is Status.WARN
    assert r.data["count"] == 1
    assert r.data["failed_dir"] == "inbox/q"


def test_no_vault_path_skips() -> None:
    r = _check_quarantine({})
    assert r.status is Status.SKIP
