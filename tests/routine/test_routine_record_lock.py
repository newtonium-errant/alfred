"""Load-bearing pins for the #34-sibling routine-record RMW lock.

The routine CLI writers (cmd_done + the cmd_undone/add/remove/edit handlers via
``_atomic_item_mutate``) RMW ``routine/<Name>.md`` records that ``tier.promote``
already flocks — but pre-fix they bypassed the lock, so a promote-append racing
a cmd_done (or two cmd_done) could clobber each other's field (lost update). The
fix: every writer holds ``file_rmw_lock`` (same sidecar as promote) across its
whole read→mutate→write, and writes atomically (``.tmp`` → ``os.replace``).

These pin: (1) each writer ACQUIRES the lock on the correct record path (the
deterministic regression guard — fails if a writer's lock is dropped);
(2) the writes are atomic (``os.replace``); and (3) the gold standard — a real
two-writer concurrency test proving NO lost update (both changes survive). Tests
run unconditionally.
"""
from __future__ import annotations

import contextlib
import os
import threading
import time
from pathlib import Path

import frontmatter  # type: ignore[import-untyped]
import yaml

from alfred.common.file_lock import file_rmw_lock
from alfred.routine.cli import cmd_done
from alfred.routine.cli_items import _MutationResult, _atomic_item_mutate
from alfred.routine.config import RoutineConfig


def _config(vault: Path, tmp_path: Path) -> RoutineConfig:
    config = RoutineConfig(vault_path=str(vault), instance_name="salem")
    config.state.path = str(tmp_path / "routine_state.json")
    return config


def _write_routine(vault: Path, name: str, payload: dict) -> Path:
    routine_dir = vault / "routine"
    routine_dir.mkdir(parents=True, exist_ok=True)
    fm_str = yaml.dump(payload, default_flow_style=False, sort_keys=False)
    path = routine_dir / f"{name}.md"
    path.write_text(f"---\n{fm_str}---\n\n# {name}\n", encoding="utf-8")
    return path


def _chores(vault: Path) -> Path:
    return _write_routine(vault, "Chores", {
        "type": "routine",
        "name": "Chores",
        "cadence": {"type": "daily"},
        "items": [
            {"text": "A", "priority": "tracked"},
            {"text": "B", "priority": "tracked"},
        ],
        "completion_log": {},
    })


# --- acquisition pins (deterministic regression guard) ---------------------


def test_cmd_done_acquires_lock_on_the_record_path(tmp_path: Path, monkeypatch) -> None:
    vault = tmp_path / "vault"
    record = _chores(vault)
    calls: list[Path] = []

    @contextlib.contextmanager
    def _spy(path):
        calls.append(path)
        yield

    monkeypatch.setattr("alfred.routine.cli.file_rmw_lock", _spy)
    cmd_done(_config(vault, tmp_path), "Chores", "A", today_override="2026-07-25")
    assert calls == [record]  # locked the record's own sidecar (== promote's)


def test_atomic_item_mutate_acquires_lock_on_the_record_path(tmp_path: Path, monkeypatch) -> None:
    # The chokepoint for cmd_undone / cmd_item_add / cmd_item_remove / cmd_item_edit.
    vault = tmp_path / "vault"
    record = _chores(vault)
    calls: list[Path] = []

    @contextlib.contextmanager
    def _spy(path):
        calls.append(path)
        yield

    monkeypatch.setattr("alfred.routine.cli_items.file_rmw_lock", _spy)

    def _mut(items, completion_log):
        return _MutationResult(items=items, completion_log=completion_log, payload_extras={}, aborted=True)

    _atomic_item_mutate(record, _mut)
    assert calls == [record]


# --- atomic-write pins (torn-read fix) -------------------------------------


def test_cmd_done_writes_atomically_via_os_replace(tmp_path: Path, monkeypatch) -> None:
    vault = tmp_path / "vault"
    record = _chores(vault)
    real_replace = os.replace
    replaced: list[tuple] = []

    def _spy(src, dst, *a, **k):
        replaced.append((str(src), str(dst)))
        return real_replace(src, dst, *a, **k)

    monkeypatch.setattr(os, "replace", _spy)
    cmd_done(_config(vault, tmp_path), "Chores", "A", today_override="2026-07-25")
    assert any(dst.endswith("Chores.md") for _src, dst in replaced)


def test_atomic_item_mutate_writes_atomically_via_os_replace(tmp_path: Path, monkeypatch) -> None:
    vault = tmp_path / "vault"
    record = _chores(vault)
    real_replace = os.replace
    replaced: list[tuple] = []

    def _spy(src, dst, *a, **k):
        replaced.append((str(src), str(dst)))
        return real_replace(src, dst, *a, **k)

    monkeypatch.setattr(os, "replace", _spy)

    def _mut(items, completion_log):
        completion_log = dict(completion_log)
        completion_log["A"] = ["2026-07-25"]
        return _MutationResult(items=items, completion_log=completion_log, payload_extras={}, aborted=False)

    _atomic_item_mutate(record, _mut)
    assert any(dst.endswith("Chores.md") for _src, dst in replaced)


# --- GOLD STANDARD: real two-writer concurrency, NO lost update -------------


def test_no_lost_update_concurrent_writers(tmp_path: Path) -> None:
    """Two concurrent writers append DISTINCT completions to the SAME record;
    the flock must serialize them so BOTH survive. Deterministic BOTH ways via
    the lock's blocking + a stale-read ordering:

    Main holds ``file_rmw_lock(record)`` and reads FIRST (captures an empty
    completion_log — its stale view {A}). It starts a worker running a REAL
    cmd_done on item B, sleeps, then writes its STALE view (no B) and releases.

      * WITH the lock: the worker BLOCKS on the flock the whole time, so it
        can't read/write until main releases; it then reads main's {A}, appends
        B → {A, B}. Both survive. ✓
      * WITHOUT the lock (the pre-fix bypass): the worker reads {} during the
        sleep and writes {B}; main then writes its stale {A}, CLOBBERING B →
        {A} only. This pin FAILS.

    So it is a non-vacuous regression guard (mutation-verified: a no-op lock
    loses B).
    """
    vault = tmp_path / "vault"
    record = _chores(vault)
    config = _config(vault, tmp_path)
    date = "2026-07-25"

    def _worker_b() -> None:
        cmd_done(config, "Chores", "B", today_override=date)

    with file_rmw_lock(record):
        # Main reads FIRST — its stale view has an empty completion_log.
        post = frontmatter.load(str(record))
        fm = dict(post.metadata or {})
        stale_cl = dict(fm.get("completion_log") or {})
        stale_cl["A"] = [date]

        t = threading.Thread(target=_worker_b)
        t.start()
        time.sleep(0.3)  # unlocked worker reads {} + writes B here; locked worker blocks

        # Main writes its STALE view (no B) — clobbers B iff the worker ran unlocked.
        fm["completion_log"] = stale_cl
        fm_yaml = yaml.dump(fm, default_flow_style=False, allow_unicode=True, sort_keys=False)
        record.write_text(f"---\n{fm_yaml}---\n\n{post.content}\n", encoding="utf-8")
    # lock released → the worker's cmd_done proceeds (blocked until now if locked)
    t.join(timeout=5)
    assert not t.is_alive(), "worker cmd_done did not complete"

    cl = frontmatter.load(str(record)).metadata["completion_log"]
    assert cl.get("A") == [date], "main's write (A) must survive"
    assert date in cl.get("B", []), "cmd_done(B) must preserve A — no lost update"
