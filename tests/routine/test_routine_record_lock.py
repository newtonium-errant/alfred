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
import structlog
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


def test_promote_lock_path_is_the_RESOLVED_path(tmp_path: Path, monkeypatch) -> None:
    """After #18 M4, promote composes through ``resolve_in_vault`` like every
    other gated writer, so its lock path is the RESOLVED one.

    This pin DRIVES the real ``append_promoted_item`` and captures what it
    actually locks. The version this replaces hand-built
    ``Path(configured) / "routine" / f"{record}.md"`` with a "promote.py:349
    verbatim" comment — a MIRROR of production, not production. When M4 migrated
    promote, the mirror kept asserting the old world and stayed GREEN. It did not
    fail; it went stale silently, which is worse, and it is exactly the failure
    the pin existed to prevent (an assertion that no longer describes anything).
    Hence: capture from the writer, never re-implement it.
    """
    from alfred.tier.promote import append_promoted_item
    from alfred.vault.paths import resolve_in_vault

    configured, _real = _symlinked_vault(tmp_path)
    locked: list[Path] = []

    @contextlib.contextmanager
    def _spy(path):
        locked.append(path)
        yield

    monkeypatch.setattr("alfred.tier.promote.file_rmw_lock", _spy)
    append_promoted_item(configured, "Chores", text="Sweep", cadence_days=7)

    expected = resolve_in_vault(configured, "routine/Chores.md", writer="test")
    assert locked == [expected], (
        "promote must lock the resolved path — if this fails, either the gate "
        "was dropped or promote stopped routing through resolve_in_vault"
    )


def test_promote_refuses_an_escaping_record_before_taking_the_lock(
    tmp_path: Path,
) -> None:
    """promote is the family's most powerful primitive — no ``.exists()`` gate,
    because absence is a supported branch (it seeds a new record). So an escape
    here was arbitrary-file-CREATE. Assert on DEBRIS as well as contents: the
    gate must precede ``file_rmw_lock``, whose ``mkdir(parents=True)`` would
    otherwise build the directory chain at the out-of-vault target."""
    from alfred.tier.promote import REFUSED_UNSAFE_TARGET, append_promoted_item

    configured, _real = _symlinked_vault(tmp_path)
    # Where the escape ACTUALLY lands: "<vault>/routine/../../escaped" climbs
    # out of routine/ then out of the vault, so it resolves relative to the
    # REAL vault's parent — not to tmp_path. An earlier draft asserted on
    # tmp_path/"escaped" and therefore passed under the gate-after-lock
    # mutation for the wrong reason; mutation testing caught it.
    outside = configured.resolve().parent / "escaped"

    with structlog.testing.capture_logs() as cap:
        res = append_promoted_item(
            configured, "../../escaped/Evil", text="x", cadence_days=7,
        )

    assert res == REFUSED_UNSAFE_TARGET
    assert not outside.exists(), "containment ran after the lock — directories created"
    denials = [c for c in cap if c.get("event") == "tier.promote.path_escape_denied"]
    assert len(denials) == 1


# --- the inode property, with the divergence CONSTRUCTED -------------------
#
# Before M4 the two spellings diverged because promote and the routine writers
# composed differently. M4 converged them — which is correct, and which means
# the divergence can no longer be borrowed from a code asymmetry.
#
# That is fine, because the property under test was never about the code: flock
# keys on the INODE, which is a filesystem fact. So the tests now CONSTRUCT the
# two spellings directly from the symlinked vault. This is strictly more honest
# than the old form — it tests the property rather than a coincidence, and it
# cannot rot when a writer migrates.


def _both_spellings(tmp_path: Path) -> tuple[Path, Path]:
    """``(configured_spelling, resolved_spelling)`` for one record."""
    from alfred.vault.paths import resolve_in_vault

    configured, _real = _symlinked_vault(tmp_path)
    via_symlink = Path(configured) / "routine" / "Chores.md"
    via_resolved = resolve_in_vault(configured, "routine/Chores.md", writer="test")
    assert str(via_symlink) != str(via_resolved), "fixture must produce two spellings"
    return via_symlink, via_resolved


def test_mixed_spellings_take_the_SAME_lock_no_37_regression(tmp_path: Path) -> None:
    """Holding ``file_rmw_lock`` via one spelling must block the other. If it
    did not, any writer still composing the configured spelling (today: none in
    the routine family; tomorrow: whatever M6 has not migrated yet) would
    lost-update against a migrated one exactly as before #37."""
    import fcntl

    via_symlink, via_resolved = _both_spellings(tmp_path)

    with file_rmw_lock(via_symlink):
        held = via_symlink.with_suffix(".lock")
        contender = via_resolved.with_suffix(".lock")
        assert contender.stat().st_ino == held.stat().st_ino, (
            "different inodes would mean genuinely different locks"
        )
        fd = open(contender, "a", encoding="utf-8")
        try:
            with contextlib.suppress(BlockingIOError):
                fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                raise AssertionError(
                    "the second spelling acquired the lock while the first held "
                    "it — the sidecars de-serialized and #37's lost-update race "
                    "is reopen"
                )
        finally:
            fd.close()


def test_no_lost_update_across_mixed_spellings(tmp_path: Path) -> None:
    """Gold-standard form: two writers, two spellings, one record — both
    changes must survive."""
    via_symlink, via_resolved = _both_spellings(tmp_path)
    record = via_resolved
    record.write_text(
        "---\ntype: routine\nname: Chores\n"
        "items:\n- text: A\n  priority: tracked\ncompletion_log: {}\n---\n\n# Chores\n",
        encoding="utf-8",
    )
    done: list[str] = []

    def _worker() -> None:
        with file_rmw_lock(via_resolved):
            post = frontmatter.load(str(record))
            fm = dict(post.metadata or {})
            cl = dict(fm.get("completion_log") or {})
            cl["B"] = ["2026-07-25"]
            fm["completion_log"] = cl
            fm_yaml = yaml.dump(fm, default_flow_style=False, allow_unicode=True, sort_keys=False)
            record.write_text(f"---\n{fm_yaml}---\n\n{post.content}\n", encoding="utf-8")
            done.append("B")

    with file_rmw_lock(via_symlink):
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
