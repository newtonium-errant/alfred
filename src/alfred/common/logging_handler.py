"""Shared log rotation handler for every tool's ``setup_logging``.

Background: prior to this module, every tool's ``utils.py`` stamped out an
identical ``setup_logging`` that wired a vanilla ``logging.FileHandler``
pointing at ``data/<tool>.log``. With no rotation, ``data/alfred.log`` had
grown to 15 GB and ``data/surveyor.log`` to 14 GB in routine operation —
disk had headroom, but ``grep`` over a 15 GB file is operationally painful
and ``tail`` is slow enough to be a friction surface. There was no
truncation gate of any kind.

The fix consolidates the handler-construction logic here so all 8
``setup_logging`` clones (curator, janitor, distiller, instructor,
surveyor, telegram, brief, transport) call the same builder. Each clone
keeps its own ``setup_logging`` signature (the operator-facing contract
is unchanged), but the handler internals route through
``build_rotating_file_handler``.

Multi-writer safety: ``RotatingFileHandler`` is NOT multi-process safe.
The Alfred orchestrator spawns one ``multiprocessing.Process`` per tool
and each tool writes only to its OWN log file
(``data/<tool>.log``) via ``setup_logging``'s FileHandler. No two
processes share a FileHandler target, so the multi-process rotation race
is not exercised in production. The single exception — ``data/alfred.log``
— receives bytes from the orchestrator parent's ``setup_logging`` AND
from child processes via inherited stdout/stderr (the
``spawn_daemon`` redirect in ``daemon.py``). Those stdio bytes bypass
the FileHandler entirely; rotation will only rotate the FileHandler
portion, and the stdout-fd-bound writes will continue landing in
whatever inode the fd points at (the post-rotation backup, not the new
live file). This is documented in the ship report; operators should
externally ``logrotate`` ``alfred.log`` if the stdio-redirect bytes need
strict bounds.

Defaults — 100 MB per file × 5 backups = ~500 MB max per tool, ~7.5 GB
worst case across ~15 tools. Acceptable disk budget on a host with
918 GB free.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

import structlog

# Default rotation policy — applied when the config omits the
# ``logging.rotation`` block entirely. Picked from the original Tier A #2
# task: 100 MB × 5 backups bounds disk use without rotating so often that
# a noisy daemon's recent context is lost.
DEFAULT_MAX_BYTES = 100_000_000  # 100 MB per file
DEFAULT_BACKUP_COUNT = 5


def build_rotating_file_handler(
    log_file: str | Path,
    *,
    max_bytes: int | None = None,
    backup_count: int | None = None,
    encoding: str = "utf-8",
) -> logging.Handler:
    """Construct a ``RotatingFileHandler`` for a tool's log file.

    Creates the parent directory if it doesn't exist (mirrors the
    pre-existing behavior of every ``setup_logging`` clone). If
    ``max_bytes`` or ``backup_count`` is ``None`` the module-level
    defaults apply — operator-facing config can omit the rotation block
    entirely and get the bundled policy.

    A non-positive ``max_bytes`` disables rotation (``RotatingFileHandler``
    treats ``maxBytes=0`` as "never rotate"). This is the documented
    escape hatch for an operator who wants to manage rotation externally
    (logrotate, journald, etc.) without touching code — set
    ``logging.rotation.max_bytes: 0`` in config.yaml.

    Per ``feedback_intentionally_left_blank.md``: a future refactor that
    silently drops rotation should leave the operator's ``ls -lh data/``
    output looking obviously wrong (no ``.1`` / ``.2`` / ``.N`` backup
    files appearing). The shared helper centralizes the decision so
    "missing rotation" is one bug class, not eight.
    """
    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    if max_bytes is None:
        max_bytes = DEFAULT_MAX_BYTES
    if backup_count is None:
        backup_count = DEFAULT_BACKUP_COUNT

    # Defensive coercion: a config value loaded from YAML could be a
    # string ("100000000") on a misconfigured deploy. ``int(...)`` raises
    # ``ValueError`` on garbage, which surfaces in the daemon's startup
    # log — preferred over silently using the default.
    max_bytes = int(max_bytes)
    backup_count = int(backup_count)

    # Negative values are nonsensical; clamp to zero (rotation disabled)
    # rather than letting RotatingFileHandler crash with a confusing
    # OSError on first write.
    if max_bytes < 0:
        max_bytes = 0
    if backup_count < 0:
        backup_count = 0

    return RotatingFileHandler(
        str(log_path),
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding=encoding,
    )


def extract_rotation_config(log_cfg: dict) -> tuple[int, int]:
    """Pull (max_bytes, backup_count) out of a ``logging`` config dict.

    Schema-tolerant: an absent ``rotation`` block, an empty
    ``rotation: {}``, or extra/unknown keys all yield the bundled
    defaults. Caller passes the result through to
    ``setup_logging(..., max_bytes=..., backup_count=...)``.

    Pulled out as a separate helper so the orchestrator's per-tool
    dispatchers + the top-level ``cli._setup_logging_from_config``
    can share the same extraction logic — keeps the two call sites
    from drifting on the YAML schema interpretation.
    """
    rotation = log_cfg.get("rotation") if isinstance(log_cfg, dict) else None
    if not isinstance(rotation, dict):
        return DEFAULT_MAX_BYTES, DEFAULT_BACKUP_COUNT
    max_bytes = rotation.get("max_bytes", DEFAULT_MAX_BYTES)
    backup_count = rotation.get("backup_count", DEFAULT_BACKUP_COUNT)
    return max_bytes, backup_count


def resolve_rotation_policy(
    max_bytes: int | None,
    backup_count: int | None,
) -> tuple[int, int]:
    """Resolve ``setup_logging``'s rotation kwargs to concrete ints.

    Mirrors the resolution chain inside ``build_rotating_file_handler``:
    ``None`` → module-level default → ``int(...)`` coercion → clamp
    negatives to zero. Pulled out as a separate helper so the per-tool
    ``setup_logging`` clones can emit the
    ``logging.rotation.policy_applied`` event with the SAME resolved
    values the handler ended up using — caller computes them once,
    passes them to both ``build_rotating_file_handler`` and
    ``emit_rotation_policy_log``.

    Schema-tolerance: a stringy YAML value (``"100000000"``) coerces
    cleanly; garbage raises ``ValueError`` at the conversion site, same
    as the handler builder.
    """
    if max_bytes is None:
        max_bytes = DEFAULT_MAX_BYTES
    if backup_count is None:
        backup_count = DEFAULT_BACKUP_COUNT
    max_bytes = int(max_bytes)
    backup_count = int(backup_count)
    if max_bytes < 0:
        max_bytes = 0
    if backup_count < 0:
        backup_count = 0
    return max_bytes, backup_count


def emit_rotation_policy_log(
    log_file: str | Path | None,
    max_bytes: int,
    backup_count: int,
) -> None:
    """Emit a one-shot ``logging.rotation.policy_applied`` structlog event.

    Called from the tail of every tool's ``setup_logging`` after
    ``structlog.configure`` runs. Records the resolved rotation policy
    (``max_bytes``, ``backup_count``, ``rotation_enabled``) so an
    operator can grep ``data/<tool>.log`` and confirm whether their
    config was honored.

    Per ``feedback_intentionally_left_blank.md``: silence on rotation
    policy is operationally ambiguous — an operator who sets
    ``rotation.max_bytes: 0`` (the disable-rotation escape hatch) cannot
    distinguish "config honored, disabled" from "config silently
    dropped, default applied, not yet rotated." The explicit event
    makes the two cases distinguishable via
    ``rotation_enabled=True/False``.

    ``log_file=None`` (the suppress-handler path) is a no-op — no file
    handler was built, nothing to report. ``rotation_enabled`` is
    explicit (``max_bytes > 0``) so the disabled case shows up as a
    field, not as absence.
    """
    if log_file is None:
        return
    log = structlog.get_logger("alfred.logging")
    log.info(
        "logging.rotation.policy_applied",
        max_bytes=int(max_bytes),
        backup_count=int(backup_count),
        log_file=str(log_file),
        rotation_enabled=int(max_bytes) > 0,
    )
