"""Process manager — `alfred up` starts all daemons via multiprocessing."""

from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from alfred.daemon import is_running, read_pid, remove_pid as _remove_pid_file, write_pid as _write_pid_file
from alfred.common.logging_handler import extract_rotation_config


def _rotation_kwargs(log_cfg: dict) -> dict[str, int]:
    """Return ``{"max_bytes": …, "backup_count": …}`` from a logging config dict.

    Thin wrapper around ``alfred.common.logging_handler.extract_rotation_config``
    that returns the result as a kwargs dict ready to splat into
    ``setup_logging(..., **kwargs)``. Every runner below grabs
    ``log_cfg`` already; this helper keeps the per-runner change one
    line ("``**_rotation_kwargs(log_cfg)``") instead of repeating the
    tuple-unpack inline.
    """
    max_bytes, backup_count = extract_rotation_config(log_cfg)
    return {"max_bytes": max_bytes, "backup_count": backup_count}


def _silence_stdio(log_file: str | None = None) -> None:
    """Redirect stdout/stderr away from the terminal in child processes for live mode.

    stderr goes to the log file (if given) so uncaught tracebacks are preserved
    for debugging. stdout goes to devnull.
    """
    sys.stdout = open(os.devnull, "w")  # noqa: SIM115 — kept open for process lifetime
    if log_file:
        sys.stderr = open(log_file, "a")  # noqa: SIM115
    else:
        sys.stderr = sys.stdout


def _run_curator(raw: dict[str, Any], skills_dir: str, suppress_stdout: bool = False) -> None:
    """Curator daemon process entry point."""
    log_cfg = raw.get("logging", {})
    log_file = f"{log_cfg.get('dir', './data')}/curator.log"
    if suppress_stdout:
        _silence_stdio(log_file)
    from alfred.curator.config import load_from_unified
    from alfred.curator.utils import setup_logging
    from alfred.email_classifier.config import load_from_unified as load_classifier
    config = load_from_unified(raw)
    # Per-instance opt-in: when ``email_classifier:`` is absent or
    # ``enabled: false``, this returns a disabled config and the daemon
    # short-circuits the post-processor. KAL-LE's config.kalle.yaml
    # leaves the block out by design.
    classifier_config = load_classifier(raw)
    setup_logging(level=log_cfg.get("level", "INFO"), log_file=log_file, suppress_stdout=suppress_stdout, **_rotation_kwargs(log_cfg))
    from alfred.curator.daemon import run
    asyncio.run(run(config, Path(skills_dir), email_classifier_config=classifier_config))


def _run_janitor(raw: dict[str, Any], skills_dir: str, suppress_stdout: bool = False) -> None:
    """Janitor watch daemon process entry point."""
    log_cfg = raw.get("logging", {})
    log_file = f"{log_cfg.get('dir', './data')}/janitor.log"
    if suppress_stdout:
        _silence_stdio(log_file)
    from alfred.janitor.config import load_from_unified
    from alfred.janitor.utils import setup_logging
    config = load_from_unified(raw)
    setup_logging(level=log_cfg.get("level", "INFO"), log_file=log_file, suppress_stdout=suppress_stdout, **_rotation_kwargs(log_cfg))
    from alfred.janitor.state import JanitorState
    from alfred.janitor.daemon import run_watch
    state = JanitorState(config.state.path, config.state.max_sweep_history)
    state.load()
    asyncio.run(run_watch(config, state, Path(skills_dir)))


def _run_distiller(raw: dict[str, Any], skills_dir: str, suppress_stdout: bool = False) -> None:
    """Distiller watch daemon process entry point."""
    log_cfg = raw.get("logging", {})
    log_file = f"{log_cfg.get('dir', './data')}/distiller.log"
    if suppress_stdout:
        _silence_stdio(log_file)
    from alfred.distiller.config import load_from_unified
    from alfred.distiller.utils import setup_logging
    config = load_from_unified(raw)
    setup_logging(level=log_cfg.get("level", "INFO"), log_file=log_file, suppress_stdout=suppress_stdout, **_rotation_kwargs(log_cfg))
    from alfred.distiller.state import DistillerState
    from alfred.distiller.daemon import run_watch
    state = DistillerState(config.state.path, config.state.max_run_history)
    state.load()
    asyncio.run(run_watch(config, state, Path(skills_dir)))


def _run_instructor(raw: dict[str, Any], skills_dir: str, suppress_stdout: bool = False) -> None:
    """Instructor watch daemon process entry point.

    Polls the vault for ``alfred_instructions`` directives and executes
    them in-process via the Anthropic SDK. Takes the same 3-arg
    signature as curator/janitor/distiller because the instructor also
    needs a ``skills_dir`` (its SKILL.md lives at
    ``vault-instructor/SKILL.md``).
    """
    log_cfg = raw.get("logging", {})
    log_file = f"{log_cfg.get('dir', './data')}/instructor.log"
    if suppress_stdout:
        _silence_stdio(log_file)
    from alfred.instructor.config import load_from_unified
    from alfred.instructor.utils import setup_logging
    config = load_from_unified(raw)
    setup_logging(level=log_cfg.get("level", "INFO"), log_file=log_file, suppress_stdout=suppress_stdout, **_rotation_kwargs(log_cfg))
    from alfred.instructor.state import InstructorState
    from alfred.instructor.daemon import run as run_instructor_daemon
    state = InstructorState(config.state.path)
    state.load()
    asyncio.run(run_instructor_daemon(
        config,
        state=state,
        suppress_stdout=suppress_stdout,
        skills_dir=Path(skills_dir),
    ))


_MISSING_DEPS_EXIT = 78  # exit code signaling missing optional dependencies


def _auto_load_dotenv_for_config(raw: dict[str, Any]) -> None:
    """Load the active config's sibling ``.env`` into ``os.environ``.

    Operator gotcha closer (P1 from QA 2026-05-05 401-fix validation):
    running ``alfred up`` from a fresh shell that hasn't
    ``set -a; source .env`` silently inherits whatever was already in
    the shell's env — typically Salem's ``ALFRED_TRANSPORT_TOKEN``
    from a prior session. Per-instance vars like
    ``ALFRED_KALLE_TRANSPORT_TOKEN`` aren't visible →
    ``_inject_transport_env_vars`` takes ``skipped_unresolved`` →
    daemons inherit Salem's token → KAL-LE's transport server returns
    401. Auto-loading ``.env`` BEFORE the injector closes the gap.

    Path resolution: the active config file's directory wins (so
    ``alfred --config /home/andrew/alfred/config.kalle.yaml up``
    looks for ``/home/andrew/alfred/.env``). Falls back to CWD if
    ``_config_path`` isn't on raw (legacy callers / tests). Per
    ``feedback_intentionally_left_blank.md`` emits a structured log
    on every call across three source paths (``loaded`` /
    ``empty`` / ``missing``) so an operator can grep
    ``orchestrator.dotenv_*`` post-restart to confirm env injection
    fired.

    Existing env vars WIN (``override=False`` semantics inside
    ``auto_load_dotenv``). An explicit ``export FOO=...`` in the
    parent shell still overrides — this is purely a gap-filler.

    Missing file → no-op + info log (production deployments use
    systemd / k8s secrets, not .env files; absence is the common
    case there).
    """
    import structlog
    from pathlib import Path
    from alfred._env import auto_load_dotenv, load_dotenv_file

    log = structlog.get_logger(__name__)

    config_path_raw = raw.get("_config_path")
    if isinstance(config_path_raw, str) and config_path_raw:
        env_path = Path(config_path_raw).resolve().parent / ".env"
    else:
        # Legacy callers / tests that build raw inline without going
        # through ``_load_unified_config``. Fall back to CWD-relative.
        env_path = Path(".env").resolve()

    if not env_path.is_file():
        log.info(
            "orchestrator.dotenv_missing",
            path=str(env_path),
            detail=(
                "no .env file at the config's sibling location. "
                "Production deploys (systemd, k8s) set env directly; "
                "this log is informational, not a warning."
            ),
        )
        return

    # Pre-count so we can distinguish "loaded N" from "loaded 0 because
    # file was empty / all comments" without re-parsing.
    parsed = load_dotenv_file(env_path)
    if not parsed:
        log.info(
            "orchestrator.dotenv_empty",
            path=str(env_path),
            detail=(
                ".env file present but parsed zero KEY=value lines. "
                "Either empty, all-comments, or all malformed."
            ),
        )
        return

    loaded, skipped = auto_load_dotenv(env_path, override=False)
    log.info(
        "orchestrator.dotenv_loaded",
        path=str(env_path),
        vars_loaded=loaded,
        vars_skipped_existing=skipped,
        # Deliberately NOT logging key names — secrets-shaped values
        # land in .env. Counts only.
    )


def _inject_transport_env_vars(raw: dict[str, Any]) -> None:
    """Set ``ALFRED_TRANSPORT_{HOST,PORT,TOKEN}`` in the current process env.

    Child processes inherit the current environment (``fork`` +
    ``multiprocessing.Process``), so setting these here means every
    tool's subprocess sees the values. Matches the ``MAIL_WEBHOOK_TOKEN``
    injection pattern — once injected, `alfred.transport.client`
    picks them up via ``os.environ.get()``.

    Values are read from the unsubstituted raw config dict (since
    ``_load_unified_config`` doesn't substitute env vars). For tokens
    written as ``${VARNAME}`` placeholders, this function resolves
    against ``os.environ`` at injection time via the canonical
    ``alfred._env.resolve_env_placeholders`` helper, then overrides
    any prior ``ALFRED_TRANSPORT_TOKEN`` value with the per-instance
    resolved token.

    The override is load-bearing for multi-instance deployments
    (per QA 2026-05-05): when a shared ``.env`` defines both
    ``ALFRED_TRANSPORT_TOKEN=<salem>`` AND
    ``ALFRED_KALLE_TRANSPORT_TOKEN=<kalle>``, KAL-LE's orchestrator
    starts with ``ALFRED_TRANSPORT_TOKEN`` already set to Salem's
    value (inherited from Salem's startup or .env). Without
    override, KAL-LE's subprocesses send Salem's token to KAL-LE's
    own transport server → 401 invalid_token. With override, the
    placeholder ``${ALFRED_KALLE_TRANSPORT_TOKEN}`` resolves to
    KAL-LE's token and replaces the inherited value.

    Defensive: if a placeholder fails to resolve (env var actually
    missing OR set-to-empty-string), the literal ``${VARNAME}``
    stays in the value and we decline to inject — same protection
    as before. The transport client's ``_resolve_token`` then raises
    ``TransportAuthMissing`` with a clear message rather than
    propagating an empty bearer header.

    Per ``feedback_intentionally_left_blank.md``: emits one structured
    info log per call (``orchestrator.transport_token.injected``)
    naming the source path (``placeholder_resolved`` /
    ``literal`` / ``skipped_unresolved`` / ``empty_config_token``),
    whether a prior env value was overridden, and an 8-char token
    fingerprint so the operator can confirm "KAL-LE booted with
    KAL-LE's token, not Salem's" from the orchestrator log alone.
    The fingerprint is the first 8 chars of the resolved token —
    enough to disambiguate between Salem and KAL-LE in practice
    without leaking the secret in full.
    """
    import structlog
    from alfred._env import resolve_env_placeholders

    log = structlog.get_logger(__name__)
    transport = raw.get("transport", {}) or {}

    server = transport.get("server", {}) or {}
    host = str(server.get("host", "") or "")
    port = server.get("port")
    if host and "ALFRED_TRANSPORT_HOST" not in os.environ:
        os.environ["ALFRED_TRANSPORT_HOST"] = host
    if port and "ALFRED_TRANSPORT_PORT" not in os.environ:
        os.environ["ALFRED_TRANSPORT_PORT"] = str(port)

    # Token — pull from auth.tokens.local.token, the v1 entry.
    auth = transport.get("auth", {}) or {}
    tokens = auth.get("tokens", {}) or {}
    local = tokens.get("local", {}) or {}
    raw_token = str(local.get("token", "") or "")
    prior_env_token = os.environ.get("ALFRED_TRANSPORT_TOKEN")

    if not raw_token:
        # No config token at all — nothing to inject. Log the
        # decision so operator can spot a misconfigured instance
        # that fell off the v1 entry.
        log.info(
            "orchestrator.transport_token.injected",
            source="empty_config_token",
            overrode_inherited=False,
            had_prior_env=prior_env_token is not None,
        )
        return

    resolved_token = resolve_env_placeholders(raw_token)
    if resolved_token.startswith("${"):
        # Placeholder failed to resolve (env var missing OR empty
        # string per the canonical helper's coalesce semantics).
        # Decline to inject — leak-prevention.
        log.info(
            "orchestrator.transport_token.injected",
            source="skipped_unresolved",
            overrode_inherited=False,
            had_prior_env=prior_env_token is not None,
            placeholder=raw_token,
        )
        return

    # OVERRIDE any prior ALFRED_TRANSPORT_TOKEN — the orchestrator's
    # intent is "this instance's daemons must use THIS instance's
    # token". Without override, KAL-LE-after-Salem-startup silently
    # uses Salem's token via inherited env (the QA 2026-05-05 bug).
    overrode = (
        prior_env_token is not None and prior_env_token != resolved_token
    )
    os.environ["ALFRED_TRANSPORT_TOKEN"] = resolved_token
    log.info(
        "orchestrator.transport_token.injected",
        source=(
            "placeholder_resolved"
            if "${" in raw_token else "literal"
        ),
        overrode_inherited=overrode,
        token_fingerprint=resolved_token[:8] + "...",
    )


def _run_surveyor(raw: dict[str, Any], suppress_stdout: bool = False) -> None:
    """Surveyor daemon process entry point."""
    log_cfg = raw.get("logging", {})
    if suppress_stdout:
        _silence_stdio(f"{log_cfg.get('dir', './data')}/surveyor.log")
    try:
        from alfred.surveyor.config import load_from_unified
        from alfred.surveyor.utils import setup_logging
        from alfred.surveyor.daemon import Daemon
    except ImportError as e:
        sys.exit(_MISSING_DEPS_EXIT)

    config = load_from_unified(raw)
    setup_logging(level=log_cfg.get("level", "INFO"), log_file=f"{log_cfg.get('dir', './data')}/surveyor.log", suppress_stdout=suppress_stdout, **_rotation_kwargs(log_cfg))
    daemon = Daemon(config)
    asyncio.run(daemon.run())


def _run_mail_webhook(raw: dict[str, Any], suppress_stdout: bool = False) -> None:
    """Mail webhook receiver process entry point."""
    log_cfg = raw.get("logging", {})
    if suppress_stdout:
        _silence_stdio(f"{log_cfg.get('dir', './data')}/mail_webhook.log")
    from alfred.mail.config import load_from_unified
    config = load_from_unified(raw)
    vault_path = Path(raw.get("vault", {}).get("path", "./vault"))
    inbox_path = vault_path / config.inbox_dir
    token = os.environ.get("MAIL_WEBHOOK_TOKEN", "")
    from alfred.mail.webhook import run_webhook
    # Idle-tick heartbeat — defaulted-on; emits ``mail.idle_tick`` so
    # the operator can distinguish "no traffic" from "daemon dead".
    run_webhook(
        inbox_path,
        token=token,
        idle_tick_enabled=config.idle_tick.enabled,
        idle_tick_interval_seconds=config.idle_tick.interval_seconds,
    )


def _run_talker(raw: dict[str, Any], skills_dir: str, suppress_stdout: bool = False) -> None:
    """Talker (Telegram) daemon process entry point.

    Mirrors the 3-arg curator runner: the talker needs ``skills_dir`` to
    locate ``vault-talker/SKILL.md`` for the system prompt.
    """
    log_cfg = raw.get("logging", {})
    log_file = f"{log_cfg.get('dir', './data')}/talker.log"
    if suppress_stdout:
        _silence_stdio(log_file)
    from alfred.telegram.daemon import run as talker_run
    exit_code = asyncio.run(
        talker_run(raw, skills_dir_str=skills_dir, suppress_stdout=suppress_stdout)
    )
    if exit_code:
        sys.exit(exit_code)


def _run_bit(raw: dict[str, Any], suppress_stdout: bool = False) -> None:
    """BIT daemon process entry point.

    Spawns the BIT scheduler. The BIT daemon writes to the vault without
    setting ``ALFRED_VAULT_SCOPE`` — unscoped writes pass the scope
    check in ``vault/scope.py`` (empty scope → unrestricted) — and runs
    at ``brief.schedule.time`` minus ``bit.schedule.lead_minutes`` (default
    5 minutes) so the Morning Brief can pick up a fresh BIT record.
    """
    log_cfg = raw.get("logging", {})
    log_file = f"{log_cfg.get('dir', './data')}/bit.log"
    if suppress_stdout:
        _silence_stdio(log_file)
    from alfred.bit.config import load_from_unified
    # Reuse brief's setup_logging — the signature matches and BIT
    # doesn't need a bespoke logger.
    from alfred.brief.utils import setup_logging
    config = load_from_unified(raw)
    setup_logging(level=log_cfg.get("level", "INFO"), log_file=log_file, suppress_stdout=suppress_stdout, **_rotation_kwargs(log_cfg))
    from alfred.bit.daemon import run_daemon
    asyncio.run(run_daemon(config, raw))


def _run_brief(raw: dict[str, Any], suppress_stdout: bool = False) -> None:
    """Brief daemon process entry point."""
    log_cfg = raw.get("logging", {})
    log_file = f"{log_cfg.get('dir', './data')}/brief.log"
    if suppress_stdout:
        _silence_stdio(log_file)
    from alfred.brief.config import load_from_unified
    from alfred.brief.utils import setup_logging
    config = load_from_unified(raw)
    setup_logging(level=log_cfg.get("level", "INFO"), log_file=log_file, suppress_stdout=suppress_stdout, **_rotation_kwargs(log_cfg))
    from alfred.brief.daemon import run_daemon
    asyncio.run(run_daemon(config))


def _run_brief_digest_push(raw: dict[str, Any], suppress_stdout: bool = False) -> None:
    """Brief-digest pusher daemon entry point (V.E.R.A. content arc sender).

    Runs on KAL-LE / STAY-C / future specialist instances. Auto-starts
    when ``brief_digest_push:`` is in the unified config AND
    ``enabled: true``. Salem intentionally omits the block — it is the
    receiver, not a sender.
    """
    log_cfg = raw.get("logging", {})
    log_file = f"{log_cfg.get('dir', './data')}/brief_digest_push.log"
    if suppress_stdout:
        _silence_stdio(log_file)
    # Reuse brief's setup_logging — same signature, no bespoke logger
    # needed. Keeps log format consistent with the receiver side.
    from alfred.brief.utils import setup_logging
    setup_logging(level=log_cfg.get("level", "INFO"), log_file=log_file, suppress_stdout=suppress_stdout, **_rotation_kwargs(log_cfg))
    from alfred.brief.kalle_brief_daemon import (
        load_brief_digest_push_config,
        run_daemon,
    )
    from alfred.transport.config import load_from_unified as load_transport
    config = load_brief_digest_push_config(raw)
    if not config.enabled:
        import sys
        import structlog
        log = structlog.get_logger(__name__)
        log.warning("kalle.brief_digest.daemon.disabled_in_config")
        sys.exit(78)
    if not config.self_name:
        import sys
        import structlog
        log = structlog.get_logger(__name__)
        log.warning("kalle.brief_digest.daemon.missing_self_name")
        sys.exit(78)
    transport_config = load_transport(raw)
    asyncio.run(run_daemon(config, transport_config))


def _run_digest(raw: dict[str, Any], suppress_stdout: bool = False) -> None:
    """Digest daemon entry — KAL-LE weekly cross-arc synthesis.

    Fires once per week at ``digest.schedule`` (default Sunday 07:00
    America/Halifax). Auto-skip with exit 78 when ``digest.enabled``
    is missing or false so the orchestrator's auto-restart doesn't
    spin a disabled daemon.
    """
    log_cfg = raw.get("logging", {})
    log_file = f"{log_cfg.get('dir', './data')}/digest.log"
    if suppress_stdout:
        _silence_stdio(log_file)
    from alfred.brief.utils import setup_logging
    setup_logging(
        level=log_cfg.get("level", "INFO"),
        log_file=log_file,
        suppress_stdout=suppress_stdout,
        **_rotation_kwargs(log_cfg),
    )
    from alfred.digest.config import load_from_unified as load_dg
    from alfred.digest.daemon import run_daemon as run_dg_daemon
    config = load_dg(raw)
    if not config.enabled:
        import sys
        import structlog
        log = structlog.get_logger(__name__)
        log.warning("digest.daemon.disabled_in_config")
        sys.exit(78)
    asyncio.run(run_dg_daemon(config, raw))


def _run_radar_day(raw: dict[str, Any], suppress_stdout: bool = False) -> None:
    """Daily radar daemon entry — auto-fires Phase 3a's run_daily_radar.

    Per-instance auto-start: any instance with
    ``distiller.radar_day.enabled: true`` runs this daemon. Default
    fire 08:00 ADT — 1h ahead of the Daily Sync at 09:00 ADT so the
    radar provider has a freshly-written daily file.

    Exit code 78 (orchestrator's "not configured" convention) when
    the block is absent / disabled so auto-restart skips us.
    """
    log_cfg = raw.get("logging", {})
    log_file = f"{log_cfg.get('dir', './data')}/radar_day.log"
    if suppress_stdout:
        _silence_stdio(log_file)
    from alfred.brief.utils import setup_logging
    setup_logging(
        level=log_cfg.get("level", "INFO"),
        log_file=log_file,
        suppress_stdout=suppress_stdout,
        **_rotation_kwargs(log_cfg),
    )
    from alfred.distiller.config import load_from_unified as load_distiller
    from alfred.distiller.radar_day_daemon import run_daemon as run_rd_daemon
    config = load_distiller(raw)
    if not config.radar_day.enabled:
        import sys
        import structlog
        log = structlog.get_logger(__name__)
        log.warning("radar_day.daemon.disabled_in_config")
        sys.exit(78)
    asyncio.run(run_rd_daemon(config))


def _run_friction_analyzer(
    raw: dict[str, Any], suppress_stdout: bool = False,
) -> None:
    """Friction analyzer daemon entry (K3 c1).

    Per-instance auto-start: any instance with
    ``daily_sync.friction_analyzer.enabled: true`` runs this daemon.
    Default fire 07:30 ADT — 1.5h ahead of the Daily Sync at 09:00 ADT
    so the friction log is fresh when the section provider reads it.

    Reads ``telegram.bash_exec.audit_path`` (KAL-LE's bash_exec audit
    log) and writes friction events to
    ``daily_sync.friction_analyzer.log_path``. The K3 c2 section
    provider reads the latter.

    Exit code 78 when the block is absent / disabled so auto-restart
    skips us.
    """
    log_cfg = raw.get("logging", {})
    log_file = f"{log_cfg.get('dir', './data')}/friction_analyzer.log"
    if suppress_stdout:
        _silence_stdio(log_file)
    from alfred.brief.utils import setup_logging
    setup_logging(
        level=log_cfg.get("level", "INFO"),
        log_file=log_file,
        suppress_stdout=suppress_stdout,
        **_rotation_kwargs(log_cfg),
    )
    from alfred.daily_sync.config import load_from_unified as load_ds
    from alfred.daily_sync.friction_analyzer_daemon import (
        run_daemon as run_fa_daemon,
    )
    config = load_ds(raw)
    if not config.friction_analyzer.enabled:
        import sys
        import structlog
        log = structlog.get_logger(__name__)
        log.warning("friction_analyzer.daemon.disabled_in_config")
        sys.exit(78)
    asyncio.run(run_fa_daemon(config, raw_config=raw))


def _run_pending_items_pusher(raw: dict[str, Any], suppress_stdout: bool = False) -> None:
    """Pending Items Queue periodic-flush daemon.

    Per-instance auto-start: any instance with a ``pending_items``
    block + ``enabled: true`` runs this daemon. Salem may also run
    it (with ``push.target_peer == ""``) to drive the local
    outbound-failure detector + view regeneration on its own queue;
    only the push step short-circuits when target_peer is empty.

    Exit code 78 (orchestrator's "not configured" convention) when
    the block is absent / disabled so auto-restart skips us.
    """
    log_cfg = raw.get("logging", {})
    log_file = f"{log_cfg.get('dir', './data')}/pending_items_pusher.log"
    if suppress_stdout:
        _silence_stdio(log_file)
    from alfred.brief.utils import setup_logging
    setup_logging(
        level=log_cfg.get("level", "INFO"),
        log_file=log_file,
        suppress_stdout=suppress_stdout,
        **_rotation_kwargs(log_cfg),
    )
    from alfred.pending_items.config import (
        load_from_unified as load_pending,
    )
    from alfred.pending_items.pusher import run_daemon as run_pi_daemon
    from alfred.transport.config import load_from_unified as load_transport
    pi_config = load_pending(raw)
    if not pi_config.enabled:
        import sys
        import structlog
        log = structlog.get_logger(__name__)
        log.warning("pending_items.pusher.disabled_in_config")
        sys.exit(78)
    transport_config = load_transport(raw)
    vault_path_str = (raw.get("vault") or {}).get("path", "./vault")
    # Resolve instance name — the talker's ``instance.name`` lives
    # under the ``telegram`` block. Fall back to ``"salem"`` to
    # match the agent_slug_for default behaviour. Normalise via
    # the shared compat helper so spaces / dots / the legacy
    # ``alfred → salem`` mapping work uniformly.
    from alfred.telegram._compat import _normalize_instance_name
    instance_name = "salem"
    telegram_raw = raw.get("telegram") or {}
    instance_raw = telegram_raw.get("instance") or {}
    if isinstance(instance_raw, dict):
        raw_name = str(instance_raw.get("name") or "")
        normalized = _normalize_instance_name(raw_name)
        if normalized:
            instance_name = normalized
    asyncio.run(
        run_pi_daemon(
            pi_config,
            transport_config,
            Path(vault_path_str),
            instance_name=instance_name,
        )
    )


def _run_cloudflared(raw: dict[str, Any], suppress_stdout: bool = False) -> None:
    """Cloudflared tunnel daemon process entry point.

    Wraps the ``cloudflared`` Go binary as a supervised child of
    ``alfred up`` so the Cloudflare tunnel auto-restarts with the
    other daemons. Replaces the manual ``nohup cloudflared tunnel run
    <id> &`` workflow.

    Per-instance auto-start: any instance with ``cloudflared:`` block
    AND ``enabled: true`` runs this daemon. When ``enabled: false`` or
    missing tunnel_id / binary, exits ``_MISSING_DEPS_EXIT`` (78) so
    the orchestrator's auto-restart loop skips us cleanly.

    Per ``feedback_intentionally_left_blank.md``: emits a structured
    ``cloudflared.disabled_in_config`` log when the block is present
    but disabled, distinguishing "operator opted out" from
    "daemon never registered" (the latter would not even reach this
    runner — the auto-start gate filters it).
    """
    log_cfg = raw.get("logging", {})
    log_file = f"{log_cfg.get('dir', './data')}/cloudflared_supervisor.log"
    if suppress_stdout:
        _silence_stdio(log_file)
    # Reuse brief's setup_logging — the signature matches and we don't
    # need a bespoke logger for this thin wrapper. The supervisor log
    # (this file) captures lifecycle structlog events;
    # ``cloudflared.log_path`` (a separate file) captures the binary's
    # own stdout/stderr.
    from alfred.brief.utils import setup_logging
    setup_logging(
        level=log_cfg.get("level", "INFO"),
        log_file=log_file,
        suppress_stdout=suppress_stdout,
        **_rotation_kwargs(log_cfg),
    )
    from alfred.cloudflared.config import load_from_unified
    from alfred.cloudflared.daemon import run as run_cloudflared
    config = load_from_unified(raw)
    if not config.enabled:
        import structlog
        log = structlog.get_logger(__name__)
        log.warning(
            "cloudflared.disabled_in_config",
            detail=(
                "cloudflared block present but enabled=false. Exiting 78 — "
                "orchestrator's auto-restart will skip this daemon."
            ),
        )
        sys.exit(_MISSING_DEPS_EXIT)
    exit_code = run_cloudflared(
        binary_path=config.binary_path,
        tunnel_id=config.tunnel_id,
        config_path=config.config_path,
        log_path=config.log_path,
        metrics_port=config.metrics_port,
    )
    if exit_code:
        sys.exit(exit_code)


def _run_daily_sync(raw: dict[str, Any], suppress_stdout: bool = False) -> None:
    """Daily Sync daemon process entry point.

    Per-instance 09:00 ADT push channel. Reads the unified config's
    ``daily_sync`` block (per email-surfacing c2). The orchestrator
    only spawns this entry point when ``daily_sync`` is in raw AND
    ``enabled: true`` — but we double-check here so a manual run via
    ``alfred up --only daily_sync`` against a misconfigured file
    fails fast with a clear log line instead of looping.
    """
    log_cfg = raw.get("logging", {})
    log_file = f"{log_cfg.get('dir', './data')}/daily_sync.log"
    if suppress_stdout:
        _silence_stdio(log_file)
    # Reuse brief's setup_logging — the signature matches and Daily
    # Sync doesn't need a bespoke logger.
    from alfred.brief.utils import setup_logging
    setup_logging(level=log_cfg.get("level", "INFO"), log_file=log_file, suppress_stdout=suppress_stdout, **_rotation_kwargs(log_cfg))
    from alfred.daily_sync.config import load_from_unified as load_ds
    from alfred.daily_sync.daemon import run_daemon as run_ds_daemon
    config = load_ds(raw)
    if not config.enabled:
        # Misconfiguration — return immediately rather than spinning
        # the loop. Matches the orchestrator's exit-78 convention so
        # auto-restart won't keep relaunching us.
        import sys
        import structlog
        log = structlog.get_logger(__name__)
        log.warning("daily_sync.daemon.disabled_in_config")
        sys.exit(78)
    vault_path_str = raw.get("vault", {}).get("path", "./vault")
    telegram_raw = raw.get("telegram", {}) or {}
    allowed = telegram_raw.get("allowed_users") or []
    user_id = 0
    if allowed:
        try:
            user_id = int(allowed[0])
        except (TypeError, ValueError):
            user_id = 0
    if not user_id:
        import sys
        import structlog
        log = structlog.get_logger(__name__)
        log.warning("daily_sync.daemon.no_telegram_user")
        sys.exit(78)
    asyncio.run(run_ds_daemon(config, Path(vault_path_str), user_id, raw_config=raw))


# ---------------------------------------------------------------------------
# Per-tool PID tracking — prevents zombie tool processes from surviving
# across alfred down / alfred up cycles.
# ---------------------------------------------------------------------------

def _tool_pid_path(data_dir: Path, tool: str) -> Path:
    """Return the PID file path for a specific tool."""
    return data_dir / f"{tool}.pid"


def _kill_stale_tool(data_dir: Path, tool: str) -> None:
    """If a previous instance of *tool* is still running, kill it.

    This catches zombie child processes that survived a previous
    ``alfred down`` (e.g., because the orchestrator was SIGKILL'd before
    it could terminate its children).
    """
    pid_file = _tool_pid_path(data_dir, tool)
    old_pid = read_pid(pid_file)
    if old_pid is None:
        return
    if old_pid == os.getpid():
        # Stale file pointing at ourselves — just clean up
        _remove_pid_file(pid_file)
        return
    if not is_running(old_pid):
        _remove_pid_file(pid_file)
        return
    # Process is alive — kill it
    print(f"  [{tool}] killing stale process (pid {old_pid})")
    try:
        os.kill(old_pid, signal.SIGTERM)
    except ProcessLookupError:
        _remove_pid_file(pid_file)
        return
    # Give it a moment to exit gracefully
    for _ in range(30):  # 3 seconds
        time.sleep(0.1)
        if not is_running(old_pid):
            break
    else:
        # Force kill
        try:
            os.kill(old_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    _remove_pid_file(pid_file)


def _record_tool_pid(data_dir: Path, tool: str, pid: int) -> None:
    """Write the tool's child-process PID to its PID file."""
    _write_pid_file(_tool_pid_path(data_dir, tool), pid)


def _cleanup_tool_pid(data_dir: Path, tool: str) -> None:
    """Remove the tool's PID file on shutdown."""
    _remove_pid_file(_tool_pid_path(data_dir, tool))


TOOL_RUNNERS = {
    "curator": _run_curator,
    "janitor": _run_janitor,
    "distiller": _run_distiller,
    "instructor": _run_instructor,
    "surveyor": _run_surveyor,
    "mail": _run_mail_webhook,
    "brief": _run_brief,
    "bit": _run_bit,
    "talker": _run_talker,
    "daily_sync": _run_daily_sync,
    "brief_digest_push": _run_brief_digest_push,
    "digest": _run_digest,
    "pending_items_pusher": _run_pending_items_pusher,
    "radar_day": _run_radar_day,
    "friction_analyzer": _run_friction_analyzer,
    "cloudflared": _run_cloudflared,
}


def run_all(
    raw: dict[str, Any],
    only: str | None = None,
    skills_dir: Path | None = None,
    pid_path: Path | None = None,
    live_mode: bool = False,
) -> None:
    """Start selected daemons as child processes with auto-restart."""
    if skills_dir is None:
        from alfred._data import get_skills_dir
        skills_dir = get_skills_dir()

    skills_dir_str = str(skills_dir)

    # Write PID file so ``alfred down`` can find us
    if pid_path is not None:
        from alfred.daemon import write_pid
        write_pid(pid_path, os.getpid())

    # Determine which tools to run
    if only:
        tools = [t.strip() for t in only.split(",")]
    else:
        # Configuration-by-presence: every daemon is opt-in by the presence
        # of its top-level config block. KAL-LE's config.kalle.yaml omits
        # curator/janitor/distiller (no inbox, no learn extraction); Salem's
        # config has them all. Required daemons would error loudly here, but
        # we currently have none — every tool can be absent on at least one
        # instance roster (see `project_multi_instance_design.md`).
        skipped: list[tuple[str, str]] = []
        tools = []
        for tool in ("curator", "janitor", "distiller"):
            if tool in raw:
                # Honor explicit ``enabled: false`` opt-out (currently only
                # wired for distiller — DistillerConfig.enabled). Block
                # present but disabled = skip cleanly with a distinct
                # reason so observers can tell intentional-off apart from
                # block-absent.
                block = raw.get(tool) or {}
                if isinstance(block, dict) and block.get("enabled") is False:
                    skipped.append((tool, "explicitly_disabled"))
                    continue
                tools.append(tool)
            else:
                skipped.append((tool, "no_config_block"))
        # Only add surveyor if config section exists AND not explicitly
        # disabled. Symmetric to the distiller opt-out above.
        if "surveyor" in raw:
            surveyor_block = raw.get("surveyor") or {}
            if isinstance(surveyor_block, dict) and surveyor_block.get("enabled") is False:
                skipped.append(("surveyor", "explicitly_disabled"))
            else:
                tools.append("surveyor")
        # Only add mail webhook if config section exists
        if "mail" in raw:
            tools.append("mail")
        if "brief" in raw:
            tools.append("brief")
        # BIT daemon auto-starts when the config has a ``bit`` section
        # OR when the brief is configured (BIT is a brief pre-check —
        # it makes no sense to have brief without BIT). Explicit
        # ``bit:`` section wins if present.
        if "bit" in raw or "brief" in raw:
            tools.append("bit")
        # Only add talker if config section exists — users without a Telegram
        # bot shouldn't have a daemon spinning in a retry loop on 78 exits.
        if "telegram" in raw:
            tools.append("talker")
        # Instructor auto-starts when ``instructor:`` is in config.
        # Without the section, the daemon has no Anthropic API key to
        # work with and would spin in a retry loop on every directive.
        if "instructor" in raw:
            tools.append("instructor")
        # Daily Sync (email-surfacing c2) auto-starts when ``daily_sync:``
        # is in config AND ``enabled: true``. KAL-LE intentionally omits
        # the block so it doesn't fire 09:00 conversations about coding.
        if "daily_sync" in raw and (raw.get("daily_sync") or {}).get("enabled"):
            tools.append("daily_sync")
        # Brief-digest pusher (V.E.R.A. content arc sender) auto-starts
        # when ``brief_digest_push:`` is in config AND ``enabled: true``.
        # KAL-LE turns this on; Salem leaves it absent (Salem is the
        # principal — receiver, not sender).
        if "brief_digest_push" in raw and (raw.get("brief_digest_push") or {}).get("enabled"):
            tools.append("brief_digest_push")
        # KAL-LE weekly cross-arc digest. Auto-starts when ``digest:``
        # is in config AND ``enabled: true``. Default off so subordinates
        # that don't write digests don't fire one.
        if "digest" in raw and (raw.get("digest") or {}).get("enabled"):
            tools.append("digest")
        # Pending Items Queue periodic flush + outbound-failure
        # detector daemon. Auto-starts when ``pending_items:`` is in
        # config AND ``enabled: true``. Salem runs it locally to drive
        # the outbound-failure scanner (push.target_peer empty); peer
        # instances run it with target_peer="salem" to flush their
        # local queue to Salem's aggregate.
        if "pending_items" in raw and (raw.get("pending_items") or {}).get("enabled"):
            tools.append("pending_items_pusher")
        # Daily radar auto-fire (distiller-radar Phase 3a → 3b feeder).
        # Auto-starts when ``distiller.radar_day:`` block is present
        # AND ``enabled: true``. KAL-LE is the first instance to flip
        # this on; Salem / Hypatia leave it absent (no radar corpus).
        # Nested under ``distiller`` because it's a distiller subsystem
        # — same vault, same state-dir, same scoring formula.
        radar_day_block = (
            (raw.get("distiller") or {}).get("radar_day") or {}
        )
        if radar_day_block.get("enabled"):
            tools.append("radar_day")
        # Friction analyzer (K3 c1 — Daily Sync friction queue feeder).
        # Auto-starts when ``daily_sync.friction_analyzer:`` block is
        # present AND ``enabled: true``. KAL-LE is the first instance
        # to flip this on; Salem / Hypatia have no bash_exec audit log
        # and leave the block absent.
        friction_block = (
            (raw.get("daily_sync") or {}).get("friction_analyzer") or {}
        )
        if friction_block.get("enabled"):
            tools.append("friction_analyzer")
        # Cloudflared tunnel supervisor. Auto-starts when
        # ``cloudflared:`` block is present AND ``enabled: true``.
        # Per-instance opt-in: only instances that need an exposed
        # tunnel (today: Salem for the Outlook → mail webhook bridge)
        # turn it on. Other instances leave the block absent.
        #
        # Conservative gate (enabled-true required, not enabled-by-
        # presence) because misconfigured cloudflared spins in a
        # restart loop on missing-credential errors that surface only
        # in cloudflared's own log file — keeping it opt-in prevents
        # accidental loops on instances that copied a template config.
        if "cloudflared" in raw and (raw.get("cloudflared") or {}).get("enabled"):
            tools.append("cloudflared")

        if skipped:
            import structlog
            _log = structlog.get_logger(__name__)
            for tool_name, reason in skipped:
                _log.info("orchestrator.daemon_skipped", tool=tool_name, reason=reason)

    # Validate tool names
    for tool in tools:
        if tool not in TOOL_RUNNERS:
            print(f"Unknown tool: {tool}")
            print(f"Available: {', '.join(TOOL_RUNNERS.keys())}")
            sys.exit(1)

    if not live_mode:
        print(f"Starting daemons: {', '.join(tools)}")

    # Resolve data directory for per-tool PID files
    data_dir = Path(raw.get("logging", {}).get("dir", "./data"))
    data_dir.mkdir(parents=True, exist_ok=True)

    # Kill any stale tool processes left over from a previous run
    for tool in tools:
        _kill_stale_tool(data_dir, tool)

    processes: dict[str, multiprocessing.Process] = {}
    restart_counts: dict[str, int] = {}

    suppress_stdout = live_mode

    # Sentinel file path — ``alfred down`` creates this to signal shutdown
    sentinel_path = pid_path.parent / "alfred.stop" if pid_path else None

    log_dir = Path(raw.get("logging", {}).get("dir", "./data"))
    workers_json_path = log_dir / "workers.json"
    started_at = datetime.now(timezone.utc).isoformat()

    # ---- Graceful SIGTERM/SIGINT handling --------------------------------
    # Installed BEFORE spawning children so that SIGTERM arriving during the
    # stagger sleep (10s between tool starts) sets the flag instead of killing
    # the orchestrator instantly and orphaning already-started children.
    shutdown_requested = False

    def _handle_shutdown(signum, frame):
        nonlocal shutdown_requested
        shutdown_requested = True

    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)

    # Auto-load the active config's sibling .env BEFORE the transport
    # env-var injection. The injector's placeholder resolver reads
    # ``os.environ``; if a per-instance token like
    # ``ALFRED_KALLE_TRANSPORT_TOKEN`` lives in .env but the operator
    # didn't ``set -a; source .env`` first, the resolver takes
    # ``skipped_unresolved`` and the daemons inherit the wrong token.
    # P1 from QA 2026-05-05 — see ``_auto_load_dotenv_for_config``.
    _auto_load_dotenv_for_config(raw)
    # Resolve transport env vars once — orchestrator injects these
    # into every tool's child environment so any subprocess can call
    # the outbound-push client without looking at config.yaml again.
    _inject_transport_env_vars(raw)

    def start_process(tool: str) -> multiprocessing.Process:
        runner = TOOL_RUNNERS[tool]
        # Tools whose runner signature is ``(raw, suppress_stdout)`` (no
        # skills_dir). BIT has no skill prompts — it drives the
        # aggregator directly — so it lives in this bucket. Same for
        # digest (renders a markdown summary, no agent prompt) and the
        # other no-agent daemons listed below. Pinned by
        # ``test_dispatcher_two_arg_branch_matches_two_arg_tools`` in
        # ``tests/orchestrator/test_tool_dispatch.py`` so a missing
        # entry trips a test rather than a TypeError on first spawn.
        if tool in ("surveyor", "mail", "brief", "bit", "daily_sync", "brief_digest_push", "digest", "pending_items_pusher", "radar_day", "friction_analyzer", "cloudflared"):
            p = multiprocessing.Process(target=runner, args=(raw, suppress_stdout), name=f"alfred-{tool}")
        else:
            p = multiprocessing.Process(target=runner, args=(raw, skills_dir_str, suppress_stdout), name=f"alfred-{tool}")
        p.daemon = True
        p.start()
        # Record per-tool PID so we can kill zombies on next startup
        _record_tool_pid(data_dir, tool, p.pid)
        if not live_mode:
            print(f"  [{tool}] started (pid {p.pid})")
        return p

    def _write_workers_json() -> None:
        """Write current process status to workers.json for the Ink TUI."""
        data = {
            "pid": os.getpid(),
            "started_at": started_at,
            "tools": {},
        }
        for tool in tools:
            p = processes.get(tool)
            if p is None:
                data["tools"][tool] = {"pid": None, "status": "stopped", "restarts": restart_counts.get(tool, 0)}
                continue
            alive = p.is_alive()
            data["tools"][tool] = {
                "pid": p.pid if alive else None,
                "status": "running" if alive else "stopped",
                "restarts": restart_counts.get(tool, 0),
            }
            if not alive and p.exitcode is not None:
                data["tools"][tool]["exit_code"] = p.exitcode
        try:
            workers_json_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError:
            pass

    try:
        # Start all — stagger by 10s to avoid thundering herd on shared infra.
        # Stagger sleep uses small increments so SIGTERM is noticed quickly.
        for i, tool in enumerate(tools):
            if i > 0:
                for _ in range(100):  # 10s in 0.1s increments
                    time.sleep(0.1)
                    if shutdown_requested:
                        break
                if shutdown_requested:
                    break
            processes[tool] = start_process(tool)
            restart_counts[tool] = 0

        if shutdown_requested:
            print("Shutdown requested during startup, stopping...")

        # Write initial workers.json
        _write_workers_json()
        last_workers_write = time.monotonic()

        if not shutdown_requested and live_mode:
            # Live TUI dashboard mode — prefer Textual, fall back to Rich Live
            # NOTE: Both TUI implementations check the sentinel file internally
            # (Textual via set_interval, Rich Live in its 0.25s loop).  The
            # SIGTERM handler + try/finally here ensures cleanup still runs if
            # the signal arrives while the TUI event loop is active.
            try:
                from alfred.tui import run_textual_dashboard
                run_textual_dashboard(
                    tools=tools,
                    processes=processes,
                    restart_counts=restart_counts,
                    start_process=start_process,
                    sentinel_path=sentinel_path,
                    log_dir=log_dir,
                    state_dir=log_dir,
                )
            except ImportError:
                from alfred.dashboard import run_live_dashboard
                run_live_dashboard(
                    tools=tools,
                    processes=processes,
                    restart_counts=restart_counts,
                    start_process=start_process,
                    sentinel_path=sentinel_path,
                    log_dir=log_dir,
                    state_dir=log_dir,
                )
        elif not shutdown_requested:
            # Plain text monitor loop
            try:
                while True:
                    # Sleep in small increments so the loop responds to
                    # SIGTERM within ~100ms instead of waiting up to 5s.
                    for _ in range(50):
                        time.sleep(0.1)
                        if shutdown_requested:
                            break

                    if shutdown_requested:
                        print("SIGTERM received, stopping...")
                        break

                    # Periodically write workers.json for the Ink TUI
                    now = time.monotonic()
                    if now - last_workers_write >= 2:
                        _write_workers_json()
                        last_workers_write = now

                    # Check for shutdown sentinel
                    if sentinel_path and sentinel_path.exists():
                        print("Shutdown sentinel detected, stopping...")
                        break

                    for tool in list(tools):
                        p = processes[tool]
                        if not p.is_alive():
                            exit_code = p.exitcode
                            if exit_code == _MISSING_DEPS_EXIT:
                                print(f"  [{tool}] missing dependencies, not restarting")
                                tools = [t for t in tools if t != tool]
                                continue
                            restart_counts[tool] += 1
                            if restart_counts[tool] <= 5:
                                print(f"  [{tool}] exited ({exit_code}), restarting ({restart_counts[tool]}/5)...")
                                processes[tool] = start_process(tool)
                            else:
                                print(f"  [{tool}] exceeded restart limit, giving up")
                                tools = [t for t in tools if t != tool]

                    if not tools:
                        print("All daemons failed, exiting.")
                        break
            except KeyboardInterrupt:
                print("\nShutting down...")
    finally:
        # Terminate child processes and clean up per-tool PID files.
        # This block runs on every exit path: normal break, SIGTERM,
        # KeyboardInterrupt, or unhandled exception.
        #
        # Strategy: SIGTERM all children at once, give them a brief window
        # to exit, then SIGKILL any survivors.  We must finish within the
        # ~5s window that ``_stop_unix`` allows before it SIGKILLs us.
        alive = {tool: p for tool, p in processes.items() if p.is_alive()}

        # Phase 1: SIGTERM all children simultaneously
        for tool, p in alive.items():
            p.terminate()

        # Phase 2: brief wait for graceful exit (1s total, not per-child)
        deadline = time.monotonic() + 1.0
        for tool, p in alive.items():
            remaining = max(0, deadline - time.monotonic())
            p.join(timeout=remaining)

        # Phase 3: SIGKILL any survivors
        for tool, p in alive.items():
            if p.is_alive():
                p.kill()
                p.join(timeout=0.5)
            print(f"  [{tool}] stopped")

        for tool in processes:
            _cleanup_tool_pid(data_dir, tool)
        print("All daemons stopped.")

        # Clean up PID file and sentinel
        if pid_path:
            from alfred.daemon import remove_pid
            remove_pid(pid_path)
        if sentinel_path:
            try:
                sentinel_path.unlink(missing_ok=True)
            except OSError:
                pass
        try:
            workers_json_path.unlink(missing_ok=True)
        except OSError:
            pass
