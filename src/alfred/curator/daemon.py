"""Main daemon loop — orchestrates the full inbox processing pipeline.

Architecture: agent-writes-via-CLI. The agent uses ``alfred vault`` commands
(via Bash tool) to create/modify vault files. Curator orchestrates:
detect inbox → create session → invoke agent → read mutation log → mark processed → track state.

For OpenClaw backends, uses a 4-stage pipeline (pipeline.py) for better quality.
For non-CLI backends (Zo HTTP), falls back to snapshot/diff.
"""

from __future__ import annotations

import asyncio
import json
import os
import time as _time
from pathlib import Path

from alfred.vault.mutation_log import append_to_audit_log, cleanup_session_file, create_session_file, read_mutations

from .backends import BaseBackend
from .backends.cli import ClaudeBackend
from .backends.hermes import HermesBackend
from .backends.http import ZoBackend
from .backends.openclaw import OpenClawBackend
from .config import CuratorConfig
from .context import build_vault_context, extract_sender_email, gather_sender_context
from .pipeline import run_pipeline
from .state import StateManager
from .utils import get_logger
from .watcher import InboxWatcher
from .writer import diff_vault, mark_processed, snapshot_vault

log = get_logger(__name__)


def _load_skill(skills_dir: Path) -> str:
    """Load SKILL.md and all reference templates into a single text block."""
    skill_path = skills_dir / "vault-curator" / "SKILL.md"
    if not skill_path.exists():
        log.warning("daemon.skill_not_found", path=str(skill_path))
        return ""

    parts: list[str] = [skill_path.read_text(encoding="utf-8")]

    # Inline all reference templates so the agent has the full schema
    refs_dir = skills_dir / "vault-curator" / "references"
    if refs_dir.is_dir():
        for ref_file in sorted(refs_dir.glob("*.md")):
            content = ref_file.read_text(encoding="utf-8")
            parts.append(f"\n---\n### Reference Template: {ref_file.name}\n```\n{content}\n```")

    return "\n".join(parts)


def _create_backend(config: CuratorConfig) -> BaseBackend:
    """Instantiate the configured backend with vault env vars for CLI backends."""
    backend_name = config.agent.backend
    if backend_name == "claude":
        return ClaudeBackend(config.agent.claude)
    elif backend_name == "zo":
        return ZoBackend(config.agent.zo)
    elif backend_name == "openclaw":
        return OpenClawBackend(config.agent.openclaw)
    elif backend_name == "hermes":
        # Hermes is HTTP-based like Zo. No env-var-based vault access;
        # the Hermes agent is expected to drive the alfred vault CLI
        # itself. Ref upstream 8e2673c.
        return HermesBackend(
            config.agent.hermes,
            vault_path=config.vault.path,
            scope="curator",
        )
    else:
        raise ValueError(f"Unknown backend: {backend_name}")


def _is_cli_backend(backend: BaseBackend) -> bool:
    """Check if backend supports env-var-based vault access (CLI backends)."""
    return isinstance(backend, (ClaudeBackend, OpenClawBackend))


def _use_pipeline(config: CuratorConfig) -> bool:
    """Check if the 4-stage pipeline should be used (OpenClaw backend only)."""
    return config.agent.backend == "openclaw"


# ---------------------------------------------------------------------------
# Cross-process file locking — prevents two curator daemons from processing
# the same inbox file simultaneously (root cause of duplicate records).
# ---------------------------------------------------------------------------

_LOCK_STALE_SECONDS = 600  # 10 minutes


def _claim_file(inbox_file: Path, _retry: bool = True) -> bool:
    """Atomically claim an inbox file for processing.

    Creates ``{inbox_file}.lock`` containing our PID and timestamp.
    Returns True if the lock was acquired, False if another live process
    already holds it.
    """
    lock_path = inbox_file.with_suffix(inbox_file.suffix + ".lock")
    my_pid = os.getpid()
    payload = json.dumps({"pid": my_pid, "ts": _time.time()})

    # Fast path — try exclusive create (O_CREAT | O_EXCL)
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        os.write(fd, payload.encode())
        os.close(fd)
        log.debug("daemon.lock_acquired", file=inbox_file.name, pid=my_pid)
        return True
    except FileExistsError:
        pass

    # Lock file exists — inspect it
    try:
        data = json.loads(lock_path.read_text(encoding="utf-8"))
        lock_pid = int(data["pid"])
        lock_ts = float(data["ts"])
    except (OSError, json.JSONDecodeError, KeyError, ValueError):
        # Corrupt lock file — break it and retry (once)
        log.warning("daemon.lock_corrupt", file=inbox_file.name)
        _release_file(inbox_file)
        if _retry:
            return _claim_file(inbox_file, _retry=False)
        return False

    # Same process? (shouldn't happen, but be safe)
    if lock_pid == my_pid:
        return True

    # Is the holder still alive?
    try:
        os.kill(lock_pid, 0)
    except ProcessLookupError:
        # Dead process — break stale lock
        log.info("daemon.lock_stale_dead_pid", file=inbox_file.name, dead_pid=lock_pid)
        _release_file(inbox_file)
        if _retry:
            return _claim_file(inbox_file, _retry=False)
        return False
    except PermissionError:
        pass  # process exists but owned by another user — treat as alive

    # Alive but stale timestamp?
    if (_time.time() - lock_ts) > _LOCK_STALE_SECONDS:
        log.warning("daemon.lock_stale_timeout", file=inbox_file.name, holder_pid=lock_pid)
        _release_file(inbox_file)
        if _retry:
            return _claim_file(inbox_file, _retry=False)
        return False

    # Another live process legitimately holds the lock
    log.info("daemon.lock_held", file=inbox_file.name, holder_pid=lock_pid)
    return False


def _release_file(inbox_file: Path) -> None:
    """Remove the lock file for an inbox file (idempotent)."""
    lock_path = inbox_file.with_suffix(inbox_file.suffix + ".lock")
    try:
        lock_path.unlink(missing_ok=True)
    except OSError:
        pass


async def _process_file(
    inbox_file: Path,
    backend: BaseBackend,
    skill_text: str,
    config: CuratorConfig,
    state_mgr: StateManager,
) -> None:
    """Process a single inbox file through the full pipeline."""
    filename = inbox_file.name
    log.info("daemon.processing", file=filename)

    # Always pass the file to the LLM — read as text if possible, otherwise point to the file
    try:
        inbox_content = inbox_file.read_text(encoding="utf-8")
    except (UnicodeDecodeError, ValueError):
        inbox_content = f"[File: {filename} — read it directly from: {inbox_file}]"

    # Build vault context
    vault_context = build_vault_context(
        config.vault.vault_path,
        ignore_dirs=config.vault.ignore_dirs,
    )
    context_text = vault_context.to_prompt_text()

    # Inject sender-specific context for emails
    sender_email = extract_sender_email(inbox_content)
    if sender_email:
        sender_ctx = gather_sender_context(
            config.vault.vault_path,
            sender_email,
            ignore_dirs=config.vault.ignore_dirs,
        )
        if sender_ctx:
            context_text = context_text + "\n\n" + sender_ctx

    vault_path_str = str(config.vault.vault_path)
    session_path = create_session_file()

    if _use_pipeline(config):
        # 4-stage pipeline for OpenClaw backend
        pipeline_result = await run_pipeline(
            inbox_file=inbox_file,
            inbox_content=inbox_content,
            vault_context_text=context_text,
            config=config,
            session_path=session_path,
        )

        mutations = read_mutations(session_path)
        files_created = mutations["files_created"]
        files_modified = mutations["files_modified"]
        cleanup_session_file(session_path)

        # Audit log
        audit_path = str(Path(config.state.path).parent / "vault_audit.log")
        append_to_audit_log(audit_path, "curator", mutations, detail=filename)

        if not pipeline_result.success:
            log.error("daemon.pipeline_failed", file=filename, summary=pipeline_result.summary[:500])

        if not files_created and not files_modified:
            log.warning("daemon.no_changes", file=filename)
    else:
        # Legacy path for Claude and Zo backends
        use_mutation_log = _is_cli_backend(backend)

        if use_mutation_log:
            backend.env_overrides = {
                "ALFRED_VAULT_PATH": vault_path_str,
                "ALFRED_VAULT_SCOPE": "curator",
                "ALFRED_VAULT_SESSION": session_path,
            }
        else:
            before = snapshot_vault(config.vault.vault_path, ignore_dirs=config.vault.ignore_dirs)

        result = await backend.process(
            inbox_content=inbox_content,
            skill_text=skill_text,
            vault_context=context_text,
            inbox_filename=filename,
            vault_path=vault_path_str,
        )

        if use_mutation_log:
            mutations = read_mutations(session_path)
            files_created = mutations["files_created"]
            files_modified = mutations["files_modified"]
            cleanup_session_file(session_path)
        else:
            after = snapshot_vault(config.vault.vault_path, ignore_dirs=config.vault.ignore_dirs)
            files_created, files_modified = diff_vault(before, after)
            mutations = {"files_created": files_created, "files_modified": files_modified, "files_deleted": []}
            cleanup_session_file(session_path)

        # Audit log
        audit_path = str(Path(config.state.path).parent / "vault_audit.log")
        append_to_audit_log(audit_path, "curator", mutations, detail=filename)

        if not result.success:
            log.error("daemon.agent_failed", file=filename, summary=result.summary[:500])

        if not files_created and not files_modified:
            log.warning("daemon.no_changes", file=filename)

    # Mark processed and move (skip if agent already moved the file)
    if inbox_file.exists():
        mark_processed(inbox_file, config.vault.processed_path)

    # Update state
    state_mgr.state.mark_processed(
        filename=filename,
        inbox_path=str(inbox_file),
        files_created=files_created,
        files_modified=files_modified,
        backend_used=config.agent.backend,
    )
    state_mgr.save()

    log.info(
        "daemon.completed",
        file=filename,
        created=len(files_created),
        modified=len(files_modified),
    )


async def run(config: CuratorConfig, skills_dir: Path) -> None:
    """Main daemon entry point."""
    log.info("daemon.starting", backend=config.agent.backend)

    # Load skill text
    skill_text = _load_skill(skills_dir)
    if not skill_text:
        log.warning("daemon.no_skill", msg="Running without SKILL.md — agent may not produce correct output")

    # Init backend
    backend = _create_backend(config)

    # Init state
    state_mgr = StateManager(config.state.path)
    state_mgr.load()

    # Init watcher
    watcher = InboxWatcher(
        inbox_path=config.vault.inbox_path,
        debounce_seconds=config.watcher.debounce_seconds,
    )

    # Startup scan for unprocessed files — process concurrently up to
    # watcher.max_concurrent. Each file retains its own claim + release and
    # the mark_processed-on-failure fallback (Batch B item 1). One file's
    # failure does not cancel peers (return_exceptions=True is implicit in
    # the per-task exception handler). Ref upstream 163b7f9.
    max_concurrent = config.watcher.max_concurrent
    unprocessed = watcher.full_scan()
    if unprocessed:
        log.info(
            "daemon.startup_scan",
            files=len(unprocessed),
            max_concurrent=max_concurrent,
        )
        startup_sem = asyncio.Semaphore(max_concurrent)

        async def _process_startup(inbox_file: Path) -> None:
            async with startup_sem:
                if not _claim_file(inbox_file):
                    log.info("daemon.skip_locked", file=inbox_file.name)
                    return
                try:
                    await _process_file(inbox_file, backend, skill_text, config, state_mgr)
                except Exception:
                    log.exception("daemon.process_error", file=inbox_file.name)
                    # Always move to processed — even on failure — to prevent
                    # infinite reprocessing loops. The error is logged above.
                    if inbox_file.exists():
                        try:
                            mark_processed(inbox_file, config.vault.processed_path)
                        except Exception:
                            log.exception("daemon.mark_processed_fallback_failed", file=inbox_file.name)
                finally:
                    _release_file(inbox_file)

        await asyncio.gather(
            *[_process_startup(f) for f in unprocessed],
            return_exceptions=True,
        )

    # Start watching
    watcher.start()
    log.info("daemon.watching", inbox=str(config.vault.inbox_path))

    import time
    last_rescan = time.monotonic()
    rescan_interval = config.watcher.rescan_interval
    _processing: set[str] = set()  # guard against concurrent processing

    try:
        while True:
            await asyncio.sleep(config.watcher.poll_interval)
            ready = watcher.collect_ready()

            # Periodic full_scan fallback (inotify may not work on all kernels/mounts)
            now = time.monotonic()
            if now - last_rescan >= rescan_interval:
                last_rescan = now
                rescan_hits = watcher.full_scan()
                for f in rescan_hits:
                    if f not in ready:
                        ready.append(f)

            # Filter to files not already being processed in this loop, and
            # claim them cross-process under the lock. Anything we claim must
            # hit the _release_file path, so we do the claim inside the task.
            to_process = [
                f for f in ready
                if f.exists() and str(f) not in _processing
            ]

            if to_process:
                for f in to_process:
                    _processing.add(str(f))

                sem = asyncio.Semaphore(max_concurrent)

                async def _watch_process(inbox_file: Path) -> None:
                    async with sem:
                        # Cross-process lock — prevents duplicate processing
                        # by zombie daemons. Must be INSIDE the semaphore so
                        # the in-memory _processing set stays consistent with
                        # the filesystem lock.
                        if not _claim_file(inbox_file):
                            log.info("daemon.skip_locked", file=inbox_file.name)
                            _processing.discard(str(inbox_file))
                            return
                        try:
                            await _process_file(inbox_file, backend, skill_text, config, state_mgr)
                        except Exception:
                            log.exception("daemon.process_error", file=inbox_file.name)
                            # Always move to processed — even on failure — to
                            # prevent infinite reprocessing loops.
                            if inbox_file.exists():
                                try:
                                    mark_processed(inbox_file, config.vault.processed_path)
                                except Exception:
                                    log.exception("daemon.mark_processed_fallback_failed", file=inbox_file.name)
                        finally:
                            _processing.discard(str(inbox_file))
                            _release_file(inbox_file)

                # return_exceptions=True: one file's failure must not cancel
                # the gather (per-task handler already logs + marks processed).
                await asyncio.gather(
                    *[_watch_process(f) for f in to_process],
                    return_exceptions=True,
                )
    finally:
        watcher.stop()
        log.info("daemon.stopped")
