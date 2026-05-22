"""Entry point: python -m curator"""

from __future__ import annotations

import asyncio
import signal
import sys
from pathlib import Path

import yaml

from alfred.common.logging_handler import extract_rotation_config

from .config import load_config
from .daemon import run
from .utils import setup_logging, get_logger


def _load_rotation_kwargs(config_path: str) -> dict[str, int]:
    """Pull rotation kwargs from the raw YAML's ``logging`` block.

    The typed ``LoggingConfig`` dataclass omits ``rotation`` (the
    orchestrator and CLI paths consume it directly via
    ``extract_rotation_config``). The ``python -m alfred.curator`` entry
    point also needs rotation honored — without this, the rotation
    policy in ``config.yaml`` is silently dropped on this code path and
    the bundled 100 MB × 5 default applies regardless of operator
    config. Re-reads the file (cheap, startup-only) rather than
    extending ``load_config``'s return shape.
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


def main() -> None:
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config.yaml"

    config = load_config(config_path)
    setup_logging(
        level=config.logging.level,
        log_file=config.logging.file,
        **_load_rotation_kwargs(config_path),
    )

    log = get_logger("curator")
    log.info("curator.starting", config=config_path)

    loop = asyncio.new_event_loop()

    # Graceful shutdown
    def _shutdown(sig: signal.Signals) -> None:
        log.info("curator.shutdown", signal=sig.name)
        for task in asyncio.all_tasks(loop):
            task.cancel()

    if sys.platform != "win32":
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _shutdown, sig)

    try:
        from alfred._data import get_skills_dir
        loop.run_until_complete(run(config, get_skills_dir()))
    except KeyboardInterrupt:
        log.info("curator.interrupted")
    except asyncio.CancelledError:
        pass
    finally:
        loop.close()
        log.info("curator.exited")


if __name__ == "__main__":
    main()
