"""Poll loop + detector for the instructor daemon.

The daemon watches every ``*.md`` record in the vault for a populated
``alfred_instructions`` frontmatter field. For each pending directive,
the executor (commit 4) runs the natural-language instruction in-process
via the Anthropic SDK and archives the result to
``alfred_instructions_last``.

Detection model: **poll + content-hash gate.** Every
``poll_interval_seconds`` we walk the vault, parse frontmatter, and
enqueue any record whose
  - ``alfred_instructions`` list is non-empty AND
  - whose full-file SHA-256 does not match the state's cached hash, OR
  - has no cached hash yet (newly-seen file).

The gate keeps the poll cheap on steady-state vaults where no directives
are pending. Once a directive lands, the executor updates the record
(clearing the queue + appending to ``alfred_instructions_last``), the
file's hash changes, and the next poll correctly sees no pending work.

Commit 3 scope: the poll loop + ``detect_pending`` pure function that
returns what to execute. Actual execution is wired in commit 4 —
``run`` currently logs the enqueued directives at INFO level and bumps
the run timestamp so operators can observe the detector in isolation.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

import frontmatter

from alfred.vault.ops import is_ignored_path
from alfred.vault.schema import INSTRUCTION_FIELDS

from .config import InstructorConfig
from .state import InstructorState
from .utils import file_hash, get_logger

log = get_logger(__name__)


# The field on the record that carries the pending directive queue.
# Exported here as a module-level symbol so the executor + CLI can
# import the same name without re-indexing INSTRUCTION_FIELDS.
PENDING_FIELD = INSTRUCTION_FIELDS[0]
ARCHIVE_FIELD = INSTRUCTION_FIELDS[1]


@dataclass(frozen=True)
class PendingInstruction:
    """One directive, ready to execute.

    ``rel_path`` is vault-relative (POSIX-style). ``directive`` is the
    raw natural-language text. ``record_hash`` is the file's SHA-256 at
    detection time — the executor re-reads the file and can compare
    against this to detect concurrent edits (the operator mid-typing
    a new directive while the executor is running).
    """

    rel_path: str
    directive: str
    record_hash: str


def _iter_vault_md(vault_path: Path, ignore_dirs: list[str]) -> list[Path]:
    """Yield every ``*.md`` file under ``vault_path`` not in ``ignore_dirs``.

    Walks via ``rglob`` + ``is_ignored_path`` so multi-component
    ignore entries (e.g. ``inbox/processed``) work the same as the
    other tools. Sorts the output so detection is deterministic across
    runs — important for any future diff/test comparison.
    """
    if not vault_path.exists():
        return []
    results: list[Path] = []
    for md in vault_path.rglob("*.md"):
        rel = md.relative_to(vault_path)
        if is_ignored_path(rel, ignore_dirs):
            continue
        results.append(md)
    return sorted(results)


def _read_pending_directives(md_path: Path) -> list[str]:
    """Return the list of pending directive strings on this record.

    Handles the common YAML oddities: scalar instead of list (single
    directive typed without dash), ``None`` / empty list, non-string
    entries. Non-string entries are dropped with a warning rather than
    coerced — the executor needs a directive text, not whatever weird
    YAML shape an operator might have written by accident.
    """
    try:
        post = frontmatter.load(str(md_path))
    except Exception as exc:  # noqa: BLE001 — malformed YAML, skip this record
        log.warning(
            "instructor.detect.parse_failed",
            path=str(md_path),
            error=str(exc),
        )
        return []

    raw = post.metadata.get(PENDING_FIELD)
    if raw is None:
        return []
    # YAML coerces a one-entry list with a scalar string into a string.
    # Promote to single-entry list so downstream code has uniform shape.
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        log.warning(
            "instructor.detect.bad_shape",
            path=str(md_path),
            got_type=type(raw).__name__,
        )
        return []

    directives: list[str] = []
    for entry in raw:
        if isinstance(entry, str) and entry.strip():
            directives.append(entry.strip())
        else:
            log.warning(
                "instructor.detect.skipping_non_string_entry",
                path=str(md_path),
                got_type=type(entry).__name__,
            )
    return directives


def detect_pending(
    vault_path: Path,
    state: InstructorState,
    ignore_dirs: list[str] | None = None,
) -> list[PendingInstruction]:
    """Return every pending directive in the vault.

    Pure function: no state mutation, no filesystem writes. Reads the
    vault, hashes each ``*.md``, and returns one ``PendingInstruction``
    per directive on each record whose hash changed (or was never seen).

    The hash-unchanged-skip is the cheap steady-state path: a vault
    with zero pending directives costs one ``file_hash`` per record
    per poll. Records with a populated ``alfred_instructions`` list
    always re-hash to a new value on the next poll (because the
    executor either clears the list on success or stamps an error
    field on failure — either way the file content changes), so we
    don't need a second "list non-empty" special case here.
    """
    ignore = list(ignore_dirs or [])
    pending: list[PendingInstruction] = []

    for md_path in _iter_vault_md(vault_path, ignore):
        rel = md_path.relative_to(vault_path).as_posix()
        try:
            current_hash = file_hash(md_path)
        except OSError as exc:
            log.warning(
                "instructor.detect.hash_failed",
                path=rel,
                error=str(exc),
            )
            continue

        if state.hash_unchanged(rel, current_hash):
            continue

        directives = _read_pending_directives(md_path)
        if not directives:
            # Hash changed but no pending queue — just refresh the
            # cached hash so we don't re-parse this file every poll.
            state.record_hash(rel, current_hash)
            continue

        for directive in directives:
            pending.append(
                PendingInstruction(
                    rel_path=rel,
                    directive=directive,
                    record_hash=current_hash,
                )
            )

    return pending


async def run(
    config: InstructorConfig,
    state: InstructorState | None = None,
    suppress_stdout: bool = False,
) -> None:
    """Run the instructor poll loop until cancelled.

    Signature matches the other tools' ``run()`` entry points so the
    orchestrator's dispatch table can be extended uniformly.
    ``suppress_stdout`` is accepted for parity but the actual stdout
    suppression is done in the orchestrator runner before this
    function is called.

    Commit 3 scope: the loop observes ``detect_pending`` results and
    logs them. Commit 4 replaces the placeholder block with the
    executor dispatch.
    """
    if state is None:
        state = InstructorState(config.state.path)
        state.load()

    vault_path = config.vault.vault_path
    interval = config.poll_interval_seconds

    log.info(
        "instructor.daemon.starting",
        vault=str(vault_path),
        poll_interval_seconds=interval,
    )

    while True:
        try:
            pending = detect_pending(
                vault_path, state, ignore_dirs=config.vault.ignore_dirs
            )
            if pending:
                log.info(
                    "instructor.daemon.pending_detected",
                    count=len(pending),
                    paths=sorted({p.rel_path for p in pending}),
                )
                # NB: actual executor dispatch lands in commit 4. For
                # now we just refresh the cached hash so we don't
                # re-log the same pending directives every cycle —
                # the record content hasn't changed yet (the executor
                # would normally edit it), so without this the next
                # poll would detect the same hash mismatch.
                # Commit 4 replaces this block with the real executor
                # call, which mutates the record and naturally
                # advances the hash.
                for p in pending:
                    state.record_hash(p.rel_path, p.record_hash)
            state.stamp_run()
            state.save()
        except Exception as exc:  # noqa: BLE001 — never break the loop
            log.warning("instructor.daemon.poll_error", error=str(exc))

        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            log.info("instructor.daemon.shutdown")
            raise
