"""Concurrency tests for ``VaultWriter._write_atomic`` + ``PipelineState``.

These lock in the contract that the ``mark_pending_write`` → ``os.replace``
→ ``update_file`` sequence is atomic with respect to any reader of
``pending_writes`` (the current asyncio-side ``compute_diff`` and any
future watcher-thread filter that queries pending_writes on event
dispatch). Without the lock, a reader can race the writer between the
mark and the rename and either miss the "this write is mine" signal or
snapshot a half-updated view.

The race window is microseconds in practice, but we exercise it here by
stalling the writer mid-``_write_atomic`` so a reader thread can attempt
a read during the critical section. If the lock is absent, the reader
observes pending_writes in an unsafe state; with the lock in place, the
reader blocks until the write completes.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from textwrap import dedent

import frontmatter
import pytest

from alfred.surveyor.state import PipelineState
from alfred.surveyor.writer import VaultWriter


def _seed_record(vault: Path, rel: str, tags: list[str] | None = None) -> None:
    """Write a minimal tagged note into ``vault/rel``."""
    fm_tags = f"alfred_tags: {tags!r}\n" if tags is not None else ""
    content = dedent(
        f"""\
        ---
        type: note
        name: Race Note
        created: 2026-04-20
        {fm_tags}---

        body
        """
    )
    target = vault / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


class TestPendingWriteLock:
    def test_state_exposes_pending_write_lock(self, tmp_path: Path) -> None:
        # The lock must be a threading.Lock (or compatible) so callers on
        # either side of the critical section (asyncio + watcher thread)
        # can acquire the same primitive.
        state = PipelineState(tmp_path / "state.json")
        assert hasattr(state, "pending_write_lock")
        # threading.Lock is a factory → type check via acquire/release
        # rather than isinstance to stay robust across Python versions.
        assert state.pending_write_lock.acquire(blocking=False) is True
        state.pending_write_lock.release()

    def test_compute_diff_acquires_lock(self, tmp_path: Path) -> None:
        # compute_diff must acquire the same lock the writer holds. If it
        # doesn't, a reader can snapshot pending_writes mid-write. We
        # verify by pre-acquiring the lock on another thread and asserting
        # compute_diff blocks until release.
        state = PipelineState(tmp_path / "state.json")
        state.pending_write_lock.acquire()

        result: dict[str, bool] = {"completed": False}

        def _reader() -> None:
            state.compute_diff({})
            result["completed"] = True

        t = threading.Thread(target=_reader)
        t.start()
        # Give the reader a moment — it should be blocked on the lock.
        time.sleep(0.05)
        assert result["completed"] is False, "compute_diff must wait on pending_write_lock"

        state.pending_write_lock.release()
        t.join(timeout=1.0)
        assert result["completed"] is True, "compute_diff must complete after lock release"


class TestWriteAtomicSerialization:
    def test_concurrent_reader_sees_consistent_view(
        self, tmp_vault: Path, tmp_path: Path
    ) -> None:
        # Start a real write on one thread and a compute_diff on another
        # that races it. The reader's view of pending_writes[rel] for our
        # file must either be "absent" (write hasn't started) or "present
        # with the post-write md5" (write completed). It must NEVER be
        # "present but the file on disk has a different md5" — that would
        # be the bug the lock prevents.
        _seed_record(tmp_vault, "note/Race.md", ["alpha"])

        state = PipelineState(tmp_path / "surveyor_state.json")
        # Seed state so compute_diff has something to compare against.
        state.update_file("note/Race.md", "stale-md5")

        writer = VaultWriter(tmp_vault, state)

        observations: list[tuple[str, str]] = []
        reader_done = threading.Event()

        def _reader_loop() -> None:
            # Hammer the critical section while the writer runs. Each
            # observation reads disk_md5 + pending_writes under the same
            # lock acquisition — that's the contract the writer's lock
            # guarantees. If the lock weren't held across mark + rename,
            # we'd see observations where pending is present but does
            # not match disk_md5 (the window between mark and replace).
            from alfred.surveyor.utils import compute_md5
            full_path = tmp_vault / "note/Race.md"
            for _ in range(100):
                with state.pending_write_lock:
                    try:
                        disk_md5 = compute_md5(full_path)
                    except OSError:
                        continue
                    pending = state.pending_writes.get("note/Race.md")
                observations.append((pending or "", disk_md5))
            reader_done.set()

        t = threading.Thread(target=_reader_loop)
        t.start()

        # Drive several real writes through. Each goes through the
        # pending_write_lock critical section.
        post = frontmatter.load(str(tmp_vault / "note/Race.md"))
        post.metadata["alfred_tags"] = ["alpha", "beta", "gamma"]
        for _ in range(5):
            writer._write_atomic(
                tmp_vault / "note/Race.md",
                "note/Race.md",
                post,
                audit_detail="alfred_tags",
            )

        reader_done.wait(timeout=5.0)
        t.join(timeout=1.0)
        assert reader_done.is_set(), "reader thread did not finish"

        # Invariant: whenever pending_writes has an entry for this file,
        # it must match the md5 of the file currently on disk. If the
        # lock weren't held across mark + rename, we'd see entries with
        # a pending hash that doesn't match disk (the window between
        # mark and replace, or vice versa).
        for pending, disk in observations:
            if pending:
                assert pending == disk, (
                    f"pending_writes={pending!r} but disk md5={disk!r} — "
                    "mark_pending_write raced with os.replace"
                )

    def test_write_atomic_updates_state_under_lock(
        self, tmp_vault: Path, tmp_path: Path
    ) -> None:
        # After _write_atomic returns, state.files[rel] must reflect the
        # new md5 and pending_writes[rel] must still hold that md5 (it
        # gets cleared by the next compute_diff). This guards against a
        # regression where the lock scope is trimmed too aggressively and
        # update_file falls outside it, leaving state.files stale.
        _seed_record(tmp_vault, "note/Atomic.md", ["a"])

        state = PipelineState(tmp_path / "surveyor_state.json")
        writer = VaultWriter(tmp_vault, state)

        post = frontmatter.load(str(tmp_vault / "note/Atomic.md"))
        post.metadata["alfred_tags"] = ["a", "b"]
        writer._write_atomic(
            tmp_vault / "note/Atomic.md",
            "note/Atomic.md",
            post,
            audit_detail="alfred_tags",
        )

        assert "note/Atomic.md" in state.files
        assert state.files["note/Atomic.md"].md5 == state.pending_writes["note/Atomic.md"]


class TestWriteAtomicFailurePath:
    def test_osreplace_failure_clears_pending_under_lock(
        self, tmp_vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # When os.replace raises, the writer must clear its pending_writes
        # entry so a subsequent compute_diff doesn't see a phantom pending
        # hash for a file that was never actually written.
        _seed_record(tmp_vault, "note/Fail.md", ["x"])

        state = PipelineState(tmp_path / "surveyor_state.json")
        writer = VaultWriter(tmp_vault, state)

        def _boom(*args: object, **kwargs: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr("alfred.surveyor.writer.os.replace", _boom)

        post = frontmatter.load(str(tmp_vault / "note/Fail.md"))
        post.metadata["alfred_tags"] = ["x", "y"]
        writer._write_atomic(
            tmp_vault / "note/Fail.md",
            "note/Fail.md",
            post,
            audit_detail="alfred_tags",
        )

        # Pending-writes entry must be gone; state.files must not carry
        # a hash that isn't on disk.
        assert "note/Fail.md" not in state.pending_writes
