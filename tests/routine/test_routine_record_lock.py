"""Load-bearing pins for the #34-sibling routine-record RMW lock.

The routine CLI writers RMW ``routine/<Name>.md`` records that ``tier.promote``
already flocks — but pre-fix they bypassed the lock, so a promote-append racing
a cmd_done (or two cmd_done) could clobber each other's field (lost update). The
fix: every writer holds ``file_rmw_lock`` (same sidecar as promote) across its
whole read→mutate→write, and writes atomically (``.tmp`` → ``os.replace``).

Phase C slice 1 relocated the cmd_done / cmd_undone completion_log WRITE into
the shared ``alfred.routine.completion`` leaf (single writer per lane, shared
with the board's ``/feed/act`` path) — so those two acquire the lock in
``completion.py``; cmd_item_add/remove/edit still lock via
``cli_items._atomic_item_mutate``. Both paths lock the SAME record sidecar.

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

    # The completion_log write now locks inside the shared completion writer.
    monkeypatch.setattr("alfred.routine.completion.file_rmw_lock", _spy)
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

    _atomic_item_mutate(record, _mut, vault_path=vault)
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

    _atomic_item_mutate(record, _mut, vault_path=vault)
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


# ---------------------------------------------------------------------------
# #37 x #18 convergence — the lock survives MIXED PATH SPELLINGS
# ---------------------------------------------------------------------------
#
# Arc #18 made the routine writers compose their target through
# ``vault.paths.resolve_in_vault``, which returns a RESOLVED path.
# ``tier.promote.append_promoted_item`` still composes the CONFIGURED spelling
# (``Path(vault_path) / "routine" / f"{record}.md"``, promote.py:349). On the
# box those strings DIFFER — /home/andrew/alfred is a symlink to
# /data/algernon/alfred and the vault is configured via the symlink.
#
# The open question that gated this arc's merge: does that spelling split
# de-serialize the sidecar and reopen the #37 lost-update race?
#
# It does not, and the reason is mechanical rather than incidental: ``flock``
# locks the INODE (via the open file description), not the path string. Two
# spellings that name the same file through a symlinked parent open the same
# inode, so they contend correctly. These pins prove it through the REAL
# ``file_rmw_lock`` rather than by argument, so the guarantee is checkable and
# a future change that breaks it fails here.


def _symlinked_vault(tmp_path: Path) -> tuple[Path, Path]:
    """Return ``(configured, real)`` — a vault reached via a symlink, the
    production topology. ``configured`` is what config.yaml would carry."""
    real = tmp_path / "data" / "algernon" / "alfred" / "vault"
    (real / "routine").mkdir(parents=True)
    configured = tmp_path / "home_alfred_vault"
    configured.symlink_to(real, target_is_directory=True)
    return configured, real


def test_promote_and_routine_writer_lock_paths_differ_in_spelling(tmp_path: Path) -> None:
    """Precondition for the pin below. If these two ever converge, the
    contention test stops proving anything and silently becomes a tautology."""
    from alfred.vault.paths import resolve_in_vault

    configured, _real = _symlinked_vault(tmp_path)
    promote_target = Path(configured) / "routine" / "Chores.md"   # promote.py:349 verbatim
    writer_target = resolve_in_vault(configured, "routine/Chores.md", writer="test")

    assert str(promote_target) != str(writer_target), (
        "the two composers must produce DIFFERENT strings for this pin to mean anything"
    )
    assert promote_target.with_suffix(".lock") != writer_target.with_suffix(".lock")


def test_mixed_spellings_take_the_SAME_lock_no_37_regression(tmp_path: Path) -> None:
    """The gating question, answered by measurement.

    Holding ``file_rmw_lock`` via promote's CONFIGURED spelling must block a
    contender using the #18 writers' RESOLVED spelling. If it did not, a
    promote-append racing a routine write on the box would lost-update exactly
    as it did before #37.
    """
    import fcntl

    from alfred.vault.paths import resolve_in_vault

    configured, _real = _symlinked_vault(tmp_path)
    promote_target = Path(configured) / "routine" / "Chores.md"
    writer_target = resolve_in_vault(configured, "routine/Chores.md", writer="test")

    with file_rmw_lock(promote_target):
        # Same sidecar name the migrated writers' file_rmw_lock would open.
        contender_lock = writer_target.with_suffix(".lock")
        held_lock = promote_target.with_suffix(".lock")
        assert contender_lock.stat().st_ino == held_lock.stat().st_ino, (
            "different inodes would mean genuinely different locks"
        )

        fd = open(contender_lock, "a", encoding="utf-8")
        try:
            with contextlib.suppress(BlockingIOError):
                fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                raise AssertionError(
                    "resolved spelling acquired the lock while the configured "
                    "spelling held it — the sidecars de-serialized and #37's "
                    "lost-update race is reopen"
                )
        finally:
            fd.close()


def test_no_lost_update_across_mixed_spellings(tmp_path: Path) -> None:
    """The gold-standard form of the same guarantee: two writers, two
    spellings, one record — both changes must survive.

    Mirrors the two-writer test above it, but the worker locks via the RESOLVED
    spelling while main holds the CONFIGURED one.
    """
    configured, real = _symlinked_vault(tmp_path)
    record = real / "routine" / "Chores.md"
    record.write_text(
        "---\ntype: routine\nname: Chores\n"
        "items:\n- text: A\n  priority: tracked\ncompletion_log: {}\n---\n\n# Chores\n",
        encoding="utf-8",
    )

    from alfred.vault.paths import resolve_in_vault

    resolved = resolve_in_vault(configured, "routine/Chores.md", writer="test")
    done: list[str] = []

    def _worker() -> None:
        # Locks via the RESOLVED spelling (what the #18 writers now compose).
        with file_rmw_lock(resolved):
            post = frontmatter.load(str(record))
            fm = dict(post.metadata or {})
            cl = dict(fm.get("completion_log") or {})
            cl["B"] = ["2026-07-25"]
            fm["completion_log"] = cl
            fm_yaml = yaml.dump(fm, default_flow_style=False, allow_unicode=True, sort_keys=False)
            record.write_text(f"---\n{fm_yaml}---\n\n{post.content}\n", encoding="utf-8")
            done.append("B")

    # Main holds via the CONFIGURED spelling (what promote composes).
    with file_rmw_lock(Path(configured) / "routine" / "Chores.md"):
        post = frontmatter.load(str(record))
        fm = dict(post.metadata or {})
        stale = dict(fm.get("completion_log") or {})
        stale["A"] = ["2026-07-25"]

        t = threading.Thread(target=_worker)
        t.start()
        time.sleep(0.3)  # an unlocked worker would write B here and get clobbered

        fm["completion_log"] = stale
        fm_yaml = yaml.dump(fm, default_flow_style=False, allow_unicode=True, sort_keys=False)
        record.write_text(f"---\n{fm_yaml}---\n\n{post.content}\n", encoding="utf-8")

    t.join(timeout=5)
    assert not t.is_alive(), "worker did not complete"
    assert done == ["B"]

    cl = frontmatter.load(str(record)).metadata["completion_log"]
    assert cl.get("A") == ["2026-07-25"], "main's write (A) must survive"
    assert "B" in cl, "worker's write (B) must preserve A — no lost update across spellings"
