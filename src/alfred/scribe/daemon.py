"""Sovereign scribe daemon — the STAY-C slot standup (scribe P1-d).

Minimal standup: the slot comes UP boundary-enforced + guard-armed + idle-ready.
NO audio→note pipeline (that is P2). On boot the daemon:

  (a) REQUIRES ``sovereign: {enabled: true}`` — refuses to boot a clinical
      scribe without the no-egress boundary (fail-closed; a scribe block alone
      is a misconfiguration, not a licence to run unguarded).
  (b) RE-VALIDATES the sovereign boundary in its OWN process. The run_all
      parent gate already validated + fork-installs the guard, but a
      spawn-launched child would NOT inherit it — so the daemon self-checks
      (the P1-a r2 belt: parent gate + daemon self-check).
  (c) SELF-INSTALLS the SovereignHttpGuard in its own process — the REAL
      per-process coverage (spawn children don't inherit the parent's httpx
      monkeypatch; this is the load-bearing install, not the fork inheritance).
  (d) boots in synthetic mode and emits ``scribe.daemon.up`` (intentionally-
      left-blank: up + sovereign_ok + synthetic + no-input, so idle-ready is
      distinguishable from broken).
  (e) sits in an idle loop emitting ``scribe.daemon.idle_tick``.

A boundary breach raises :class:`SovereignBoundaryError`; the orchestrator
runner maps it to exit 79 (NON-RESTARTABLE — refuse to re-enter a
cloud-reachable state).
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path

import structlog

from alfred.scribe.config import ScribeConfig, load_from_unified
from alfred.sovereign import (
    SovereignBoundaryError,
    install_sovereign_http_guard,
    is_aiohttp_guard_installed,
    is_requests_guard_installed,
    is_sovereign_http_guard_installed,
    validate_sovereign_boundary,
)

log = structlog.get_logger(__name__)

# How often the pipeline scans input_dir for new sources (P2-d). Short enough
# to be responsive to a dropped encounter, long enough to be cheap when idle.
_SWEEP_INTERVAL_SECONDS = 30


def startup(
    raw: dict,
    *,
    env: Mapping[str, str] | None = None,
) -> ScribeConfig:
    """Boot steps (a)-(d) — synchronous, fail-closed. Returns the loaded
    :class:`ScribeConfig`.

    Raises :class:`SovereignBoundaryError` if the sovereign block is absent
    (``scribe_requires_sovereign``) or any of the four barriers is breached
    (``barrier_a`` .. ``barrier_d``). ``env`` defaults to ``os.environ`` (read
    live by the boundary's barrier c); injectable for tests.
    """
    config = load_from_unified(raw)

    # (a) fail-closed: a clinical scribe MUST run behind the boundary.
    sovereign = raw.get("sovereign") or {}
    if not (isinstance(sovereign, dict) and sovereign.get("enabled")):
        raise SovereignBoundaryError(
            "scribe_requires_sovereign",
            "the scribe daemon refuses to boot without sovereign:{enabled:true} "
            "— a clinical scribe must run behind the no-egress boundary. Add "
            "the sovereign block and launch cloud-key-scrubbed (env -u ...).",
        )

    # (b) re-validate the boundary in THIS process (belt for spawn children).
    validate_sovereign_boundary(raw, env=env)  # raises on breach

    # (c) self-install the per-call HTTP guard in THIS process (real coverage).
    install_sovereign_http_guard()

    # (c.1) STT backend availability (scribe P2-b). A real-model provider
    # (faster-whisper / local-whisper) needs the [scribe] extra; if it's
    # missing, raise MissingSTTDependency → the runner exits 78 (missing deps,
    # no-restart) rather than boot a scribe that cannot transcribe. The ``fake``
    # provider needs no dep and passes.
    from alfred.scribe.stt import ensure_backend_available
    ensure_backend_available(config)

    # (c.1b) Diarize backend availability (scribe P4). The ``pyannote`` engine
    # needs the [scribe-diarize] extra; if it's missing, raise
    # MissingDiarizeDependency → the runner exits 78 (missing deps, no-restart),
    # mirroring the STT dep-guard. The ``off`` (default) / ``fake`` providers need
    # no dep and pass, so the daemon boots torch-free.
    from alfred.scribe.diarize import ensure_diarize_backend_available
    ensure_diarize_backend_available(config)

    # (c.2) Non-gating kernel-egress belt probe (#42). Best-effort only: the
    # load-bearing egress control is the boundary gate + the http guard armed
    # above — this just PROBES the systemd IPAddressDeny belt and LOGS
    # enforced|unverified (+ a loopback-severed WARN if IPAddressAllow
    # over-blocked Ollama). It NEVER gates boot, so any exception is swallowed
    # (observability-only). Gated on ``scribe.egress_probe.enabled`` (default
    # true) — when enabled it fires ONE payload-free canary SYN toward a
    # NON-ROUTABLE TEST-NET-1 address on the unverified path (never a real
    # host); set it false to suppress that SYN. NOTE: EPERM is synchronous, so
    # when the belt IS enforced no packet ever leaves the box.
    # A malformed (non-dict) egress_probe scalar is coerced to {} so a bad
    # config can never crash boot (the probe is observability-only).
    probe_cfg = (raw.get("scribe") or {}).get("egress_probe")
    if not isinstance(probe_cfg, dict):
        probe_cfg = {}
    if probe_cfg.get("enabled", True):
        try:
            from alfred.sovereign.egress_probe import probe_kernel_egress_firewall
            probe_kernel_egress_firewall(
                canary=probe_cfg.get("canary", "192.0.2.1:443"),
                loopback=probe_cfg.get("loopback", "127.0.0.1:11434"),
                logger=log,
            )
        except Exception:  # noqa: BLE001 — probe is observability-only, never gates boot
            log.warning(
                "scribe.egress_firewall.probe_skipped",
                detail="egress probe raised unexpectedly — swallowed (non-gating, observability-only)",
            )
    else:
        log.info(
            "scribe.egress_firewall.probe_disabled",
            detail="scribe.egress_probe.enabled:false — kernel-belt probe skipped, NO off-box canary SYN fired",
        )

    # (d) ILB up signal.
    log.info(
        "scribe.daemon.up",
        sovereign_ok=True,
        http_guard_installed=is_sovereign_http_guard_installed(),
        aiohttp_guard_installed=is_aiohttp_guard_installed(),  # #40 web-transport coverage
        requests_guard_installed=is_requests_guard_installed(),  # audit: hf_hub/requests coverage
        mode=config.mode,
        input_dir=config.input_dir,
        has_input=False,
        detail=(
            "sovereign scribe up — boundary validated, http guard armed, "
            "synthetic mode, NO input pipeline yet (P2). Idle-ready."
        ),
    )
    return config


def _state_path(raw: dict, config) -> str:
    """The pipeline state file — under the instance's logging.dir (filesystem
    only). Mirrors the per-tool ``data/<tool>_state.json`` convention."""
    log_dir = (raw.get("logging") or {}).get("dir", "./data")
    return str(Path(log_dir) / "scribe_state.json")


async def _maybe_start_ingest_server(config: ScribeConfig, *, events=None):
    """Start the loopback PWA ingest server IFF ``ingest_web.enabled``.

    Returns the started :class:`~alfred.scribe.ingest_web.IngestWebServer`, or
    ``None`` when the server is INERT (the default). Attests
    ``scribe.ingest_web.up`` either way (intentionally-left-blank: an inert
    server is distinguishable from a broken one). The bind host is already proven
    loopback by barrier (e) at load — this trusts that gate. ``events`` (the
    event-store facade) rides ``app["scribe_events"]`` for the encounter.* emits.
    """
    web_cfg = config.ingest_web
    if not web_cfg.enabled:
        log.info(
            "scribe.ingest_web.up",
            enabled=False,
            detail="ingest server INERT (scribe.ingest_web.enabled:false) — no socket bound",
        )
        return None
    from alfred.scribe.ingest_web import IngestWebServer

    server = IngestWebServer(config, events=events)
    await server.start()
    log.info(
        "scribe.ingest_web.up",
        enabled=True,
        host=web_cfg.host,
        port=web_cfg.port,
        detail="sovereign loopback PWA ingest server bound (barrier-e validated).",
    )
    return server


async def run(
    raw: dict,
    *,
    suppress_stdout: bool = False,
    env: Mapping[str, str] | None = None,
) -> None:
    """Async entry point. Runs :func:`startup`, then the pipeline sweep loop.

    Never returns under normal operation — the orchestrator terminates the
    process on shutdown. A boundary breach in :func:`startup` propagates out
    (the runner maps it to exit 79); a missing STT dep → exit 78.
    """
    config = startup(raw, env=env)

    # Import here (not at module load) so the daemon's boundary/dep checks in
    # startup() run before the pipeline stack is imported.
    from alfred.scribe.events import ScribeEvents
    from alfred.scribe.events_maintenance import ScribeEventMaintenance
    from alfred.scribe.pipeline import run_sweep
    from alfred.scribe.retention_sweep import RetentionSweep
    from alfred.scribe.state import ScribeState
    from alfred.vault import ops as vault_ops

    state = ScribeState(_state_path(raw, config))
    state.load()
    vault_path = Path((raw.get("vault") or {}).get("path", "./vault"))
    log_dir = Path((raw.get("logging") or {}).get("dir", "./data"))

    # The medico-legal event store (event-store design §2.4 / §8 row 14). ALWAYS-ON
    # with scribe — no enabled knob. Clinical mode fails LOUD at open → REFUSE boot
    # (ordinary RESTARTABLE exit, not 78/79 — most likely a perms/disk problem on the
    # events dir); non-clinical degrades to inactive (dev exercises the store; the
    # emitters no-op). ``legacy_audit_path`` sha-pins the legacy attest-audit into the
    # clinical genesis (§3.3).
    try:
        events = ScribeEvents.from_config(
            raw, log_dir, legacy_audit_path=log_dir / "clinical_attest_audit.jsonl")
    except Exception:
        log.error(
            "scribe.daemon.event_store_open_failed",
            detail="the medico-legal event store failed to open — REFUSING boot (clinical mode "
                   "requires a durable audit trail; likely a perms/disk problem on the events dir). "
                   "Restartable exit.")
        raise
    # PHIA s.63 access log (§7.1.2a / §7.1.3): register the read hook so vault reads
    # are logged; the daemon's OWN sweep reads run under a "pipeline" access context
    # → SUPPRESSED + counted (the daily summary makes the suppression itself auditable).
    vault_ops.register_read_hook(events.make_read_hook())
    maint = ScribeEventMaintenance(events)
    # The retention sweep (#13 §3, slice 13b) — sibling of ScribeEventMaintenance, one per daemon
    # lifetime. Seals READY / defensively-seals abandoned encounters + rolling-prunes the diarize_stats
    # telemetry sink, best-effort + off-loop (tar/crypto via asyncio.to_thread). Same clinical-store
    # gate as the emitters: a retained seal needs the durable retention.sealed record.
    retention_sweep = RetentionSweep(config, events)

    log.info(
        "scribe.daemon.pipeline_watching",
        mode=config.mode,
        input_dir=config.input_dir,
        events_active=events.active,
        detail="sovereign scribe pipeline watching input_dir (synthetic-gated).",
    )

    # The loopback PWA ingest server (#49) rides THIS event loop alongside the
    # sweep — the guard is already installed (startup step c), so the server
    # comes up AFTER it. INERT unless enabled. Stopped in the shutdown finally so
    # a graceful terminate tears the socket down cleanly. Exit/restart semantics
    # are UNCHANGED: a bind error propagates like any sweep-stack error (generic
    # restartable exit), NOT a sovereign/dep exit (79/78).
    server = await _maybe_start_ingest_server(config, events=events)
    try:
        # The daemon's reads are pipeline reads — bind the suppression context for the
        # whole loop (incl. the boot scan below), so s.63 logs person-views, not the
        # system operating.
        with events.access_context("stayc_scribe", "pipeline", "daemon"):
            # BOOT: the FULL post-attest-edit comparison (design §5.3 — bounded per-sweep,
            # full at boot). Best-effort — a scan error must never wedge the daemon.
            try:
                maint.post_attest_edit_scan(vault_path, full=True)
            except Exception:  # noqa: BLE001 — observability scan, never gates the daemon
                log.exception("scribe.daemon.boot_post_attest_scan_error")
            while True:
                try:
                    await run_sweep(config, state, vault_path, events=events)
                    # Event-store maintenance (design §4/§5.3/§5.5): all self-latched +
                    # best-effort. Independent of sweep work (VAULT-STATE observations run
                    # every tick, not gated behind an idle early-return).
                    maint.heartbeat_if_due()
                    maint.post_attest_edit_scan(vault_path)
                    maint.flush_suppressed_if_new_day()
                    # Retention sweep (#13 §3, slice 13b) — own try/except so a retention failure is
                    # cleanly attributed and never masks the maintenance calls above; the sweep also
                    # isolates per-encounter failures internally, so an exception never wedges the loop.
                    try:
                        await retention_sweep.run(state)
                    except Exception:  # noqa: BLE001 — best-effort; a retention error never kills the loop
                        log.exception("scribe.daemon.retention_sweep_error")
                except Exception:  # noqa: BLE001 — a sweep-level error must not kill the loop
                    log.exception("scribe.daemon.sweep_error")
                await asyncio.sleep(_SWEEP_INTERVAL_SECONDS)
    finally:
        if server is not None:
            await server.stop()
            log.info("scribe.ingest_web.down", detail="ingest server stopped (graceful shutdown)")
        vault_ops.clear_read_hooks()  # a one-process daemon leaves no process-global hook
