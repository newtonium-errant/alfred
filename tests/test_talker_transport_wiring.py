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

The structural fix is :func:`alfred.transport.server.wire_transport_app`,
the single consolidation point that calls every ``register_*`` helper
conditionally based on what the daemon passes in. Adding a new
transport-app dependency means adding a kwarg there AND in the helper,
not threading another register call through this daemon.

These tests pin the wiring contract:

* ``register_vault_path`` correctly stashes the path under the storage
  key the handlers read from (sanity check on the helper itself).
* The daemon module's transport-setup block invokes
  :func:`wire_transport_app` and passes every kwarg the daemon
  legitimately needs (vault_path, send_fn, instance_name, ...). Source-
  text inspection is brittle-by-design: a refactor that removes the
  call must replace it with an equivalent wiring step, or this test
  fails and forces re-evaluation.
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
    """Daemon must wire the vault path onto the transport app at startup.

    Without this wiring, every /canonical/* handler returns 500
    ``vault_not_configured`` because ``_get_vault_path`` reads the
    storage key that ``register_vault_path`` (called from
    ``wire_transport_app``) sets.

    As of the centralized-wiring refactor, the daemon calls
    :func:`alfred.transport.server.wire_transport_app` exactly once,
    passing every wireable resource (vault_path, send_fn, pending-items
    callables, instance identity) as kwargs. The assertions below
    catch the "silently-dropped wiring" failure mode by pinning that
    the daemon source still routes through that single function.
    """
    source = _daemon_source()

    # The wire_transport_app call is the single consolidation point —
    # if it disappears, every per-resource registration disappears with
    # it. Pin it explicitly.
    assert "wire_transport_app" in source, (
        "alfred.telegram.daemon must call wire_transport_app on the "
        "transport app at startup; without it every /canonical/* "
        "handler 500s with vault_not_configured (vault_path is one "
        "of several resources wire_transport_app registers). If "
        "you've replaced this helper with an equivalent wiring path, "
        "update this test to assert the new shape."
    )

    # Strengthen the assertion: the call must explicitly pass
    # vault_path as a kwarg. A daemon that calls wire_transport_app
    # but omits vault_path still 500s on /canonical/* — the helper is
    # explicit-by-omission for exactly this reason.
    assert "vault_path=Path(config.vault.path)" in source, (
        "wire_transport_app must receive vault_path=Path(config.vault.path), "
        "not a hardcoded default and not omission. Each instance has its "
        "own vault path; hardcoding routes every instance through Salem's "
        "vault, and omission causes every /canonical/* handler to 500."
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
        "wire_transport_app must receive Path(config.vault.path), not "
        "a hardcoded default. Each instance has its own vault path; "
        "hardcoding routes every instance through Salem's vault."
    )


def test_talker_daemon_wires_send_fn_into_transport_app():
    """Daemon must wire the send callable through wire_transport_app.

    Without ``send_fn`` wired, /outbound/send returns 503
    ``telegram_not_configured`` for every immediate-send request.
    Adjacent regression to the vault_path bug — the structural fix
    deserves the same explicit pin as vault_path.
    """
    source = _daemon_source()
    assert "send_fn=_send_via_telegram" in source, (
        "wire_transport_app must receive send_fn=_send_via_telegram. "
        "Without it, /outbound/send returns 503 telegram_not_configured "
        "for every immediate-send request (the closure has the PTB Bot "
        "reference; nothing else can deliver)."
    )


def test_talker_daemon_wires_instance_identity():
    """Daemon must wire instance_name through wire_transport_app.

    Without ``instance_name`` wired, /peer/handshake responses return an
    empty ``instance`` field (handler defaults the missing app key to
    ""). Peers that depend on the handshake's ``instance`` field for
    routing decisions silently misbehave rather than fail loudly.
    """
    source = _daemon_source()
    assert "instance_name=config.instance.name" in source, (
        "wire_transport_app must receive instance_name=config.instance.name. "
        "Without it, /peer/handshake returns an empty 'instance' field "
        "and peers that route by instance silently misbehave."
    )


def test_talker_daemon_wires_gcal_client_when_enabled():
    """Daemon must construct + wire GCal client when ``gcal.enabled`` is true.

    Phase A+ inter-instance comms: without these wiring lines, even an
    instance that opted into ``gcal:`` in config.yaml would silently
    skip the GCal conflict-check + sync paths because the transport
    handler has no client to call. Default-disabled is the right
    behaviour, but config-enabled-but-not-wired is a silent failure.

    The pinned shape:
      * Daemon imports ``load_from_unified`` from gcal_config
      * Constructs ``GCalClient`` with the config's paths + scopes
      * Passes ``gcal_client=`` and ``gcal_config=`` to
        ``wire_transport_app``
    """
    source = _daemon_source()
    assert "from alfred.integrations.gcal_config import" in source, (
        "talker daemon must import gcal_config to read the GCal block"
    )
    assert "GCalClient" in source, (
        "talker daemon must construct a GCalClient when gcal.enabled "
        "(otherwise the conflict-check + sync paths silently skip)"
    )
    assert "gcal_client=gcal_client" in source, (
        "wire_transport_app must receive gcal_client=gcal_client; "
        "without it the transport handler has no client to call and "
        "GCal integration silently no-ops despite enabled config"
    )
    assert "gcal_config=gcal_config" in source, (
        "wire_transport_app must receive gcal_config=gcal_config; "
        "the handler reads calendar IDs from the typed config dataclass"
    )
