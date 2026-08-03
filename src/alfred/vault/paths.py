"""Vault path containment — the shared resolve-and-verify gate.

Arc #18. Every writer that composes a filesystem path from a record name, a
relative-path string, or any other caller-supplied value routes the composed
path through :func:`resolve_in_vault` BEFORE taking a lock and BEFORE writing.
The path is admissible iff it *resolves* to a location inside the vault root.

## Why location-verification and not character-sanitization

The 2026-08-03 audit measured the real vault (8,614 records): 5 record names
contain ``/``, 233 stems contain ``.``, 3,209 records live at legitimately
nested paths (``inbox/processed/``, ``quarantine/spam/2026-05/``), and names
routinely carry ``$ + * = ~``, em-dashes and apostrophes. A character blocklist
would reject real vault content. ZERO record names contain ``..``.

So the gate tests WHERE THE PATH LANDS, not what it is spelled with. Every
legitimate shape above resolves inside the vault and is accepted unchanged;
every escape resolves outside and is refused.

## What the four properties each close

* ``.resolve()`` on the candidate — ``..`` traversal AND symlink escape (the
  residual ``vault/scope.py``'s ``_delete_target_class`` docstring self-declares
  as "the boarded resolve-against-vault arc" — this is that arc).
* ``is_relative_to`` rather than a ``str.startswith`` prefix compare — the
  sibling-directory escape. With vault ``/home/andrew/vault``, the path
  ``../vault-evil/pwned.md`` resolves to ``/home/andrew/vault-evil/pwned.md``,
  which PASSES a prefix compare and FAILS ``is_relative_to``. This is exactly
  the live defect in ``vault/ops.py``'s ``_resolve_vault_path``, which this
  module replaces (M5).
* verifying the joined result — pathlib's absolute-path override. ``Path("/vault")
  / "/etc/passwd"`` is ``/etc/passwd``, silently discarding the root; an absolute
  ``rel_path`` simply fails the containment check.
* raising rather than returning a sentinel — no caller can proceed on a falsy
  return by accident.

## Contract notes

* **Fail-closed.** Any doubt (empty path, embedded NUL, unresolvable) is a
  refusal, never a pass-through.
* **Returns the RESOLVED path**, so callers write exactly what was verified —
  no gap between the path that was checked and the path that is used. It also
  normalises the path STRING, which is what logs, canary payloads and equality
  comparisons see.

  It does NOT change lock identity, and an earlier draft of this docstring
  claimed otherwise ("two writers spelling the same record differently now take
  the SAME lock, which they previously did not"). That was wrong. ``flock``
  keys on the INODE via the open file description, not on the path string, so
  two spellings that name one file — ``routine/F.md`` vs
  ``routine/../routine/F.md``, or a symlinked vault root vs its realpath —
  ALWAYS contended, before this module existed and after. Measured
  cross-process; pinned in ``tests/routine/test_routine_record_lock.py``
  (``test_mixed_spellings_take_the_SAME_lock_no_37_regression``).

  The practical consequence, and the reason this paragraph is worth its length:
  ``tier.promote.append_promoted_item`` still composes the CONFIGURED spelling
  while the gated writers compose the resolved one, and that split is SAFE.
  #37's lost-update guarantee holds across the gated/ungated boundary, so
  containment can be rolled out writer-by-writer without an atomic
  commit-group.
* **No success log.** A per-write "allowed" line would be pure spam on the hot
  path; the REFUSAL is the signal (intentionally-left-blank — the interesting
  event is the one that stops work, and it is always emitted).
* ``writer`` is a REQUIRED keyword argument, not an optional one. Per
  builder.md's optional-gate rule: a defaulted gate parameter is a standing trap.
  Requiring it means the refusal log always names its origin.
"""

from __future__ import annotations

from pathlib import Path

import structlog

log = structlog.get_logger(__name__)


class VaultContainmentError(Exception):
    """A composed path did not resolve to a location inside the vault root.

    Carries the structured detail so a caller that maps this onto its own
    result vocabulary can still log or surface specifics. Callers in the board
    writer family deliberately map this onto their EXISTING "can't address that
    record" result kind rather than widening their cross-agent kind vocabulary
    — the distinct signal lives in the log event, not in the operator/model
    facing enum.
    """

    def __init__(
        self,
        *,
        rel_path: str,
        resolved: str,
        vault_root: str,
        writer: str,
        reason: str,
    ) -> None:
        super().__init__(
            f"path escapes the vault root: {rel_path!r} -> {resolved!r} "
            f"(vault root {vault_root!r}, writer {writer!r}, reason {reason!r})"
        )
        self.rel_path = rel_path
        self.resolved = resolved
        self.vault_root = vault_root
        self.writer = writer
        self.reason = reason


def resolve_in_vault(
    vault_path: Path | str,
    rel_path: Path | str,
    *,
    writer: str,
) -> Path:
    """Resolve ``rel_path`` against the vault root and REQUIRE it stay inside.

    ``vault_path`` is the vault root. ``rel_path`` is the caller-composed
    vault-relative path (``"routine/For Self Health.md"``). ``writer`` names the
    calling site for the refusal log (e.g. ``"routine.completion.done"``) and is
    required — see the module docstring.

    Returns the RESOLVED absolute path on success. Raises
    :class:`VaultContainmentError` on any escape, emitting exactly one
    ``vault.containment.escape_denied`` warning first.

    An absolute ``rel_path`` is refused rather than honoured: pathlib would
    silently discard the vault root, so honouring it would be the escape.
    """
    vault_root = Path(vault_path).resolve()
    raw = str(rel_path)

    # Fail-closed on the degenerate inputs before touching the filesystem. An
    # empty path would join to the vault ROOT itself — technically "inside", but
    # never a valid record target, and a caller reaching here with one has a bug
    # we should surface rather than absorb.
    if not raw.strip():
        _deny(
            rel_path=raw, resolved="", vault_root=str(vault_root),
            writer=writer, reason="empty_path",
        )

    try:
        candidate = (vault_root / rel_path).resolve()
    except (ValueError, OSError) as exc:
        # Embedded NUL raises ValueError; a resolution loop (symlink cycle)
        # raises OSError. Both are refusals, not crashes.
        _deny(
            rel_path=raw, resolved="", vault_root=str(vault_root),
            writer=writer, reason=f"unresolvable:{type(exc).__name__}",
        )

    if not candidate.is_relative_to(vault_root):
        _deny(
            rel_path=raw, resolved=str(candidate), vault_root=str(vault_root),
            writer=writer, reason="outside_vault_root",
        )

    return candidate


def vault_relative(vault_path: Path | str, abs_path: Path | str) -> str:
    """The vault-relative POSIX string for ``abs_path`` — symlink-tolerant.

    The companion to :func:`resolve_in_vault`, and NOT optional once that gate
    is in place. ``Path.relative_to`` is purely LEXICAL: it compares path
    components without resolving anything. So the moment a writer holds a
    RESOLVED path (what ``resolve_in_vault`` returns) while ``vault_path`` is
    still the CONFIGURED spelling, ``relative_to`` raises — the two no longer
    share a prefix even though they name the same directory:

        configured : /home/andrew/alfred/vault          (a symlink)
        resolved   : /data/algernon/alfred/vault
        ValueError : '/data/.../routine/Chores.md' is not in the subpath of
                     '/home/andrew/alfred/vault'

    That is production's exact shape (``/home/andrew/alfred`` has been a symlink
    to ``/data/algernon/alfred`` since 2026-06-25, and the vault is configured
    via the symlink spelling), and it took down every routine verb when arc #18
    made the writers resolve. It was invisible to the whole test suite because
    ``tmp_path`` fixtures are never symlinked, so ``.resolve()`` is a no-op and
    the two spellings coincide.

    Resolving BOTH sides makes the comparison mean what callers assume it means.
    Falls back to the absolute string when the path genuinely lies outside the
    vault — this helper is for DISPLAY/logging, so it must not raise; containment
    is :func:`resolve_in_vault`'s job and has already run by the time we get here.
    """
    root = Path(vault_path).resolve()
    target = Path(abs_path).resolve()
    try:
        return target.relative_to(root).as_posix()
    except ValueError:
        # Outside the vault: report the absolute path rather than crashing a
        # log line or a canary payload.
        return target.as_posix()


def _deny(
    *, rel_path: str, resolved: str, vault_root: str, writer: str, reason: str,
) -> None:
    """Emit the refusal signal and raise. Never returns."""
    log.warning(
        "vault.containment.escape_denied",
        writer=writer,
        rel_path=rel_path,
        resolved=resolved,
        vault_root=vault_root,
        reason=reason,
    )
    raise VaultContainmentError(
        rel_path=rel_path, resolved=resolved, vault_root=vault_root,
        writer=writer, reason=reason,
    )


__all__ = [
    "VaultContainmentError",
    "resolve_in_vault",
    "vault_relative",
]
