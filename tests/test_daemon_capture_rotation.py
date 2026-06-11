"""S5 pins (2026-06-11): spawn-time rollover of the stdout-capture log.

The original S5 premise ("alfred.log truncates per `alfred up`") was
FALSIFIED on deeper reading — spawn capture has always appended and the
parent's handler rotates. The real defect the deep-read exposed: the
capture fd is opened BEFORE the handler's first-emit rollover, so the
fd follows the rename and a whole run's child-stdout fattens the ``.1``
sibling past any policy bound (observed: 954MB/911MB rotated files
against a 100MB policy, ~2.4GB total). The fix rolls an oversized
capture BEFORE the fd opens, so each run starts on a fresh file and
history survives in the numbered siblings.

All filesystem-only — no daemon spawn, no orchestration harness (the
live restart is the acceptance test).
"""

from __future__ import annotations

from pathlib import Path

from alfred.daemon import rotate_capture_log_if_oversized


def _grow(path: Path, size: int) -> None:
    path.write_bytes(b"x" * size)


def test_oversized_capture_rolls_to_sibling(tmp_path) -> None:
    log = tmp_path / "alfred.log"
    _grow(log, 2048)
    rolled = rotate_capture_log_if_oversized(log, max_bytes=1024, backup_count=3)
    assert rolled is True
    assert not log.exists()  # fresh file will be created by the append-open
    assert (tmp_path / "alfred.log.1").stat().st_size == 2048


def test_undersized_capture_left_alone(tmp_path) -> None:
    log = tmp_path / "alfred.log"
    _grow(log, 100)
    assert rotate_capture_log_if_oversized(log, max_bytes=1024, backup_count=3) is False
    assert log.stat().st_size == 100
    assert not (tmp_path / "alfred.log.1").exists()


def test_missing_capture_is_noop(tmp_path) -> None:
    assert rotate_capture_log_if_oversized(
        tmp_path / "alfred.log", max_bytes=1024, backup_count=3,
    ) is False


def test_cascade_preserves_history_and_evicts_oldest(tmp_path) -> None:
    """RotatingFileHandler-style cascade: live → .1, .1 → .2, oldest out."""
    log = tmp_path / "alfred.log"
    _grow(log, 2048)
    (tmp_path / "alfred.log.1").write_text("run-minus-1", encoding="utf-8")
    (tmp_path / "alfred.log.2").write_text("run-minus-2", encoding="utf-8")

    rolled = rotate_capture_log_if_oversized(log, max_bytes=1024, backup_count=2)
    assert rolled is True
    # Previous live capture became .1 ...
    assert (tmp_path / "alfred.log.1").stat().st_size == 2048
    # ... prior .1 cascaded to .2 ...
    assert (tmp_path / "alfred.log.2").read_text(encoding="utf-8") == "run-minus-1"
    # ... and the old .2 (beyond backup_count) was evicted.
    assert not (tmp_path / "alfred.log.3").exists()


def test_rotation_disabled_semantics_mirror_handler(tmp_path) -> None:
    """maxBytes=0 / backupCount=0 mean 'never roll' in RotatingFileHandler —
    the spawn-time roll honors a rotation-disabled config the same way."""
    log = tmp_path / "alfred.log"
    _grow(log, 2048)
    assert rotate_capture_log_if_oversized(log, max_bytes=0, backup_count=3) is False
    assert rotate_capture_log_if_oversized(log, max_bytes=1024, backup_count=0) is False
    assert log.stat().st_size == 2048


def test_bundled_policy_defaults_apply_when_unspecified(tmp_path) -> None:
    """None/None resolves through the bundled policy (100MB/5) — a small
    file never rolls under defaults."""
    log = tmp_path / "alfred.log"
    _grow(log, 4096)
    assert rotate_capture_log_if_oversized(log) is False
