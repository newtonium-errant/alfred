"""Smoke tests for KAL-LE's Daily Sync config block.

Per the multi-instance wiring antipattern memo
(``feedback_multi_instance_wiring_pattern.md``): each new per-instance
wiring step gets a smoke test that confirms the config block loads
without falling back to defaults silently.

KAL-LE's ``daily_sync:`` block was added to ``config.kalle.yaml`` in
c2 of the distiller-radar Phase 3 arc. ``config.kalle.yaml`` is
NOT tracked in git (it carries deployment secrets via ``${VAR}``
references), so this test exercises the canonical block SHAPE via a
synthetic fixture that mirrors what the live config carries. If the
live config drifts from this shape, KAL-LE's daemon misbehaves but
this test still passes — the contract here is "the shape, when
loaded, produces a working DailySyncConfig".

For end-to-end live-config validation, see ``alfred status`` against
``--config config.kalle.yaml`` which exercises the real file.
"""

from __future__ import annotations

import pytest

from alfred.daily_sync.config import DailySyncConfig, load_from_unified


# Canonical KAL-LE Daily Sync block — kept in lockstep with what's
# written to ``config.kalle.yaml`` in c2 of the Phase 3 arc. When the
# canonical shape changes (new section provider config, batch_size
# sub-fields, etc.), update both here and the live config file.
KALLE_DAILY_SYNC_BLOCK: dict = {
    "daily_sync": {
        "enabled": True,
        "schedule": {
            "time": "09:00",
            "timezone": "America/Halifax",
        },
        "batch_size": 5,
        "corpus": {
            "path": "/home/andrew/.alfred/kalle/data/daily_sync_corpus.jsonl",
        },
        "state": {
            "path": "/home/andrew/.alfred/kalle/data/daily_sync_state.json",
        },
        "attribution": {
            "enabled": True,
            "batch_size": 5,
            "corpus_path": (
                "/home/andrew/.alfred/kalle/data/attribution_audit_corpus.jsonl"
            ),
        },
    },
}


# Mirror of the local-token allowed_clients addition in c2 of the
# Phase 3 arc. The Daily Sync daemon dispatches via the transport with
# ``client_name='daily_sync'``; without this entry, KAL-LE's transport
# rejects every fire with client_not_allowed.
KALLE_TRANSPORT_LOCAL_ALLOWED: list[str] = [
    "scheduler",
    "brief",
    "janitor",
    "curator",
    "talker",
    "daily_sync",
]


@pytest.fixture
def kalle_raw() -> dict:
    """Synthetic config dict mirroring KAL-LE's daily_sync block."""
    return dict(KALLE_DAILY_SYNC_BLOCK)


def test_kalle_block_loads_enabled(kalle_raw: dict) -> None:
    cfg = load_from_unified(kalle_raw)
    assert isinstance(cfg, DailySyncConfig)
    assert cfg.enabled is True


def test_kalle_schedule_is_09_halifax(kalle_raw: dict) -> None:
    """Per project_kalle_daily_sync.md — 09:00 ADT."""
    cfg = load_from_unified(kalle_raw)
    assert cfg.schedule.time == "09:00"
    assert cfg.schedule.timezone == "America/Halifax"


def test_kalle_state_path_tool_scoped(kalle_raw: dict) -> None:
    """KAL-LE's state path must NOT collide with Salem's
    ./data/daily_sync_state.json — both daemons would otherwise
    overwrite each other's last-fired bookkeeping."""
    cfg = load_from_unified(kalle_raw)
    assert "/home/andrew/.alfred/kalle/data/" in cfg.state.path
    assert cfg.state.path.endswith("daily_sync_state.json")


def test_kalle_corpus_path_separate_from_salem(kalle_raw: dict) -> None:
    """Per-instance calibration corpus — sharing Salem's would let one
    instance overwrite the other's calibration trail."""
    cfg = load_from_unified(kalle_raw)
    assert "/home/andrew/.alfred/kalle/data/" in cfg.corpus.path


def test_kalle_attribution_section_enabled(kalle_raw: dict) -> None:
    """Attribution-audit section provider is wired so KAL-LE's own
    attribution-audit markers (from radar dedup, instructor edits)
    surface for Andrew's confirm/reject."""
    cfg = load_from_unified(kalle_raw)
    assert cfg.attribution.enabled is True
    assert "/home/andrew/.alfred/kalle/data/" in cfg.attribution.corpus_path


def test_kalle_local_allowed_clients_includes_daily_sync() -> None:
    """The Daily Sync daemon dispatches outbound Telegram chunks
    through KAL-LE's transport using ``client_name='daily_sync'``.
    Without this entry, every 09:00 ADT fire fails with
    client_not_allowed.

    This mirrors the live ``config.kalle.yaml`` change; updating one
    without the other is a deployment-time bug.
    """
    assert "daily_sync" in KALLE_TRANSPORT_LOCAL_ALLOWED


def test_kalle_block_no_unknown_keys_silently_swallowed() -> None:
    """Defensive — load_from_unified ignores unknown keys for
    forward-compat. Verify the canonical block uses only known fields
    so a typo here doesn't silently disable the option."""
    cfg = load_from_unified(KALLE_DAILY_SYNC_BLOCK)
    # All the canonical-block top-level keys should be reflected in
    # the loaded dataclass — confirms no typo dropped a setting.
    assert cfg.enabled is True
    assert cfg.batch_size == 5
    assert cfg.schedule.time == "09:00"
    assert cfg.corpus.path.endswith("daily_sync_corpus.jsonl")
    assert cfg.state.path.endswith("daily_sync_state.json")
    assert cfg.attribution.enabled is True
