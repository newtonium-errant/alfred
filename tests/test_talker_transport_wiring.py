"""Regression tests for the talker daemon's transport-app wiring.

Background: the talker daemon builds the outbound-push transport app via
``build_transport_app`` then registers a series of callables/values on
the aiohttp application object (vault path, pending-items aggregate
path, pending-items resolver, etc.) before the server starts accepting
requests. Each registration is a separate function call — easy to add a
new one and forget the wiring step on the daemon side.

That's exactly what happened with ``register_vault_path``: defined in
``alfred.transport.peer_handlers`` and used by every ``/canonical/*``
handler (plus the brief_digest endpoint), but never actually called
from the daemon. Result: every canonical request 500'd with
``vault_not_configured``. Repro confirmed 2026-05-01 when Hypatia's
``/canonical/event/propose-create`` push hit Salem.

These tests pin the wiring contract:

* ``register_vault_path`` correctly stashes the path under the storage
  key the handlers read from (sanity check on the helper itself).
* The daemon module's transport-setup block actually invokes
  ``register_vault_path`` against ``transport_app`` with the configured
  vault path. Source-text inspection is brittle-by-design: a refactor
  that removes the call must replace it with an equivalent wiring
  step, or this test fails and forces re-evaluation.
"""

from __future__ import annotations

from pathlib import Path

from aiohttp import web

from alfred.transport.peer_handlers import (
    _KEY_VAULT_PATH,
    register_vault_path,
)


def _daemon_source() -> str:
    """Read the talker daemon source from this checkout, not the installed copy.

    Why: the editable-install pin can resolve ``alfred.telegram.daemon``
    to the main-repo copy even when tests run from a worktree (see
    CLAUDE.md "Worktree + editable-install gotcha"). We want to assert
    against the source on disk *next to this test*, so a worktree fix
    validates against itself rather than against whatever master had
    installed at venv-creation time.
    """
    here = Path(__file__).resolve().parent
    daemon_path = here.parent / "src" / "alfred" / "telegram" / "daemon.py"
    return daemon_path.read_text(encoding="utf-8")


def test_register_vault_path_sets_storage_key(tmp_path):
    """Helper writes the vault path under the key handlers read from."""
    app = web.Application()
    vault_root = tmp_path / "vault"
    vault_root.mkdir()

    register_vault_path(app, vault_root)

    assert app[_KEY_VAULT_PATH] == str(vault_root)


def test_talker_daemon_wires_vault_path_into_transport_app():
    """Daemon must call register_vault_path on the transport app at startup.

    Without this call, every /canonical/* handler returns 500
    ``vault_not_configured`` because ``_get_vault_path`` reads the same
    key the helper sets. Source-text assertion catches refactors that
    accidentally drop the wiring; the equivalent wiring (whatever
    helper or builder is used) must restore the storage key, in which
    case this test should be updated to assert against the new shape.
    """
    source = _daemon_source()

    # The import path is the canonical surface — anyone refactoring is
    # likely to either keep this exact string or replace it with an
    # equivalent helper. The assertion below catches the
    # "silently-dropped wiring" failure mode.
    assert "register_vault_path" in source, (
        "alfred.telegram.daemon must call register_vault_path on the "
        "transport app at startup; without it every /canonical/* "
        "handler 500s with vault_not_configured. If you've replaced "
        "this helper with an equivalent wiring path, update this test "
        "to assert the new shape."
    )

    # Strengthen the assertion: the call must reference the transport
    # app object built earlier in the same scope. Catches a partial
    # refactor that leaves a dangling import.
    assert "register_vault_path(transport_app" in source, (
        "register_vault_path must be invoked against transport_app "
        "(the aiohttp.Application built by build_transport_app), not "
        "some other app object. Without this, the canonical handlers "
        "served by transport_app see no vault path."
    )


def test_talker_daemon_wires_vault_path_with_configured_value():
    """The vault path passed in must come from ``config.vault.path``.

    Hardcoding a wrong path (e.g. defaulting to ``./vault``) would let
    the test above pass while still serving the wrong vault for
    non-Salem instances (KAL-LE → ~/aftermath-lab/, Hypatia →
    ~/library-alexandria/). Pin that the wiring threads the configured
    value through.
    """
    source = _daemon_source()
    assert "Path(config.vault.path)" in source, (
        "register_vault_path must receive Path(config.vault.path), not "
        "a hardcoded default. Each instance has its own vault path; "
        "hardcoding routes every instance through Salem's vault."
    )
