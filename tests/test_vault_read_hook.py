"""Vault read-hook substrate pins (event-store design §7.1, integration map row 11).

The access-log substrate: ``register_read_hook`` + a fail-isolated fire loop at the end of
``vault_read``. The vault layer owns ONLY the registration point + fire loop — never who
registers or what the hook does (that is the scribe facade). These pins hold:

  * a registered hook fires with ``(vault_path, rel_path, frontmatter)`` on a successful read;
  * a hook exception NEVER fails the read (observability, not a gate) + emits the greppable
    ``vault.read_hook_failed`` signal;
  * registration is idempotent on function-identity;
  * NOTHING registered → the fire loop is a silent no-op (a platform instance never registers).
"""
from __future__ import annotations

import structlog

from alfred.vault import ops
from alfred.vault.ops import (
    clear_read_hooks,
    register_read_hook,
    vault_create,
    vault_read,
)


def _seed_note(tmp_path):
    """Create a minimal readable record and return (vault_path, rel_path)."""
    vault_path = tmp_path / "vault"
    vault_path.mkdir()
    res = vault_create(
        vault_path, "note", "A Note", set_fields={"type": "note"}, body="hello",
        scope="curator",
    )
    return vault_path, res["path"]


def test_read_hook_fires_with_frontmatter(tmp_path):
    clear_read_hooks()
    vault_path, rel = _seed_note(tmp_path)
    seen: list[tuple] = []
    register_read_hook(lambda vp, rp, fm: seen.append((rp, fm.get("type"))))
    vault_read(vault_path, rel)
    clear_read_hooks()
    assert seen == [(rel, "note")]  # exactly one fire, carrying the parsed frontmatter


def test_read_hook_exception_never_fails_the_read(tmp_path):
    clear_read_hooks()
    vault_path, rel = _seed_note(tmp_path)

    def _boom(vp, rp, fm):
        raise RuntimeError("hook blew up")

    register_read_hook(_boom)
    with structlog.testing.capture_logs() as cap:
        out = vault_read(vault_path, rel)  # must NOT raise
    clear_read_hooks()
    assert out["body"] == "hello"  # the read still returns normally
    matches = [c for c in cap if c.get("event") == "vault.read_hook_failed"]
    assert len(matches) == 1 and matches[0]["rel_path"] == rel  # greppable failure signal


def test_register_read_hook_idempotent_on_identity(tmp_path):
    clear_read_hooks()
    vault_path, rel = _seed_note(tmp_path)
    calls: list[int] = []
    hook = lambda vp, rp, fm: calls.append(1)  # noqa: E731
    register_read_hook(hook)
    register_read_hook(hook)  # same closure twice → registered once
    vault_read(vault_path, rel)
    clear_read_hooks()
    assert len(calls) == 1


def test_no_hook_registered_is_silent_noop(tmp_path):
    clear_read_hooks()
    vault_path, rel = _seed_note(tmp_path)
    # A platform instance never registers — the fire loop must be a no-op, not an error.
    assert ops._READ_HOOKS == []
    out = vault_read(vault_path, rel)
    assert out["frontmatter"]["type"] == "note"
