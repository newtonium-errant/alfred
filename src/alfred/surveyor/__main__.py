"""Entry point — asyncio.run + signal handling."""

from __future__ import annotations

import asyncio
import signal
import sys
from pathlib import Path

import yaml

from alfred.common.logging_handler import extract_rotation_config

from .config import load_config
from .daemon import Daemon
from .utils import setup_logging


def _load_rotation_kwargs(config_path: Path) -> dict[str, int]:
    """Pull rotation kwargs from the raw YAML's ``logging`` block.

    Mirror of curator's ``__main__._load_rotation_kwargs`` — surveyor's
    typed ``LoggingConfig`` is already schema-tolerant of unknown keys
    (its ``_build_dataclass`` filters by field name), but ``rotation``
    still doesn't reach ``setup_logging`` unless extracted here. Keeps
    ``python -m alfred.surveyor`` aligned with the orchestrator's
    rotation contract.
    """
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}
    log_cfg = raw.get("logging") if isinstance(raw, dict) else None
    if not isinstance(log_cfg, dict):
        return {}
    max_bytes, backup_count = extract_rotation_config(log_cfg)
    return {"max_bytes": max_bytes, "backup_count": backup_count}


def _load_env_file(env_path: Path | None = None) -> None:
    """Load a .env file into os.environ (without overriding existing vars).

    Thin shim over the canonical ``alfred._env.auto_load_dotenv`` so
    parser semantics stay byte-identical with the orchestrator and
    cli.py paths. See ``orchestrator._auto_load_dotenv_for_config`` for
    the contract; pre-consolidation (2026-05-05) this had its own
    parser that silently broke on ``export `` prefixes.
    """
    from alfred._env import auto_load_dotenv

    if env_path is None:
        env_path = Path(".env")
    auto_load_dotenv(env_path, override=False)


def main() -> None:
    _load_env_file()

    config_path = Path(__file__).resolve().parent.parent.parent / "config.yaml"
    if len(sys.argv) > 1:
        config_path = Path(sys.argv[1])

    cfg = load_config(config_path)
    setup_logging(
        level=cfg.logging.level,
        log_file=cfg.logging.file,
        **_load_rotation_kwargs(config_path),
    )

    daemon = Daemon(cfg)

    loop = asyncio.new_event_loop()

    def _shutdown(sig: signal.Signals) -> None:
        daemon.request_shutdown()

    if sys.platform != "win32":
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _shutdown, sig)
    else:
        # On Windows, use signal.signal for SIGINT (Ctrl+C)
        signal.signal(signal.SIGINT, lambda s, f: _shutdown(s))

    try:
        loop.run_until_complete(daemon.run())
    except KeyboardInterrupt:
        daemon.request_shutdown()
        loop.run_until_complete(daemon.shutdown())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
