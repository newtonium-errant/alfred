"""Tests for the pending-items branch of the Daily Sync reply dispatcher.

Covers:

- ``self_instance=""`` raises an explicit ``ValueError`` (no silent
  fallback to "salem"). Per ``feedback_hardcoding_and_alfred_naming.md``
  the fallback would route Hypatia / KAL-LE pending items as if they
  were Salem.
- Dispatcher works with a non-default cwd when ``raw_config`` is plumbed
  through. The legacy fallback path opens ``config.yaml`` from cwd; the
  new ``raw_config`` parameter avoids that fragility.
- ``raw_config`` is forwarded into the local + peer helpers so callers
  on a hot path don't pay the per-call file-open cost.

The dispatcher's success / failure paths beyond the parameter contract
are exercised in :mod:`test_reply_dispatch` and the executor's own
test suite — these tests target the new wiring only.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from alfred.daily_sync.reply_dispatch import (
    _PendingItemResolveFailure,
    _load_raw_config_lazy,
    _resolve_pending_item_correction,
    _resolve_pending_item_locally,
)
from alfred.daily_sync.assembler import ReplyCorrection


def _item(num: int = 1) -> dict[str, Any]:
    """Synthetic pending-item dict matching the dispatcher's schema."""
    return {
        "item_number": num,
        "id": f"item-{num}",
        "category": "outbound_failure",
        "created_by_instance": "salem",
        "session_id": "abc",
        "context": "test context",
        "resolution_options": [
            {"id": "noted", "label": "Noted, no action"},
            {"id": "show_me", "label": "Show me the text"},
        ],
    }


def test_self_instance_empty_raises_value_error():
    """Empty self_instance must raise rather than silently fall back to "salem"."""
    correction = ReplyCorrection(item_number=1, ok=True, consumed_token="noted")
    with pytest.raises(ValueError, match="self_instance must be a non-empty"):
        _resolve_pending_item_correction(
            correction, _item(),
            self_instance="",
        )


def test_self_instance_none_raises_value_error():
    """``None`` collapses to empty under ``or ""`` and must raise."""
    correction = ReplyCorrection(item_number=1, ok=True, consumed_token="noted")
    with pytest.raises(ValueError, match="self_instance must be a non-empty"):
        _resolve_pending_item_correction(
            correction, _item(),
            self_instance=None,  # type: ignore[arg-type]
        )


def test_self_instance_whitespace_raises_value_error():
    """Whitespace-only self_instance is not a valid identity."""
    correction = ReplyCorrection(item_number=1, ok=True, consumed_token="noted")
    with pytest.raises(ValueError, match="self_instance must be a non-empty"):
        _resolve_pending_item_correction(
            correction, _item(),
            self_instance="   ",
        )


def test_load_raw_config_lazy_uses_provided_dict_without_opening_file(tmp_path: Path):
    """When ``raw_config`` is provided, the helper MUST NOT open config.yaml.

    Hot-path guarantee: the bot's reply handler plumbs ``raw_config``
    through from ``bot_data``; the dispatcher's local + peer helpers
    should reuse that pre-loaded dict instead of re-reading the file
    every Telegram reply.
    """
    raw = {"vault": {"path": "/some/path"}, "pending_items": {"enabled": True}}
    cwd = os.getcwd()
    try:
        # Move to a directory with no config.yaml. If the helper
        # falls through to the open() path, this will raise.
        os.chdir(tmp_path)
        result = _load_raw_config_lazy(raw)
        assert result is raw, "Provided raw_config must be returned by reference"
    finally:
        os.chdir(cwd)


def test_load_raw_config_lazy_falls_back_to_cwd_when_none(tmp_path: Path):
    """When ``raw_config`` is None, the helper opens ``config.yaml`` from cwd.

    Legacy / direct-test caller compatibility — production callers
    should pass ``raw_config`` to avoid the round-trip.
    """
    config_path = tmp_path / "config.yaml"
    config_path.write_text("vault:\n  path: /from/file\n")

    cwd = os.getcwd()
    try:
        os.chdir(tmp_path)
        result = _load_raw_config_lazy(None)
        assert result["vault"]["path"] == "/from/file"
    finally:
        os.chdir(cwd)


def test_load_raw_config_lazy_raises_when_no_file_and_no_dict(tmp_path: Path):
    """No ``raw_config`` AND no config.yaml on disk → explicit failure.

    Tests the dispatcher's no-config-found error path. The fallback
    open() raises OSError, which is wrapped in
    ``_PendingItemResolveFailure``.
    """
    cwd = os.getcwd()
    try:
        os.chdir(tmp_path)  # no config.yaml here
        with pytest.raises(_PendingItemResolveFailure):
            _load_raw_config_lazy(None)
    finally:
        os.chdir(cwd)


def _fake_resolve(*_a: Any, **_kw: Any) -> dict[str, Any]:
    """Sync stand-in for the executor's ``resolve_local_item`` factory.

    The real function returns a coroutine; the dispatcher passes
    that coroutine to ``_run_coro_sync`` which awaits it. In these
    tests we ALSO patch ``_run_coro_sync`` to short-circuit, so the
    factory's return value is discarded — but if the factory itself
    returns a coroutine object, Python still emits the
    ``coroutine ... was never awaited`` warning at GC time. Returning
    a plain dict avoids that noise.
    """
    return {"ok": True, "summary": "noted"}


def test_dispatcher_uses_raw_config_from_non_default_cwd(tmp_path: Path):
    """Regression: dispatcher must work with cwd != alfred-repo-root.

    Pre-fix, the dispatcher hard-coded ``open("config.yaml", ...)``
    which is cwd-dependent. A daemon started from /tmp would crash.
    With ``raw_config`` plumbed through, cwd doesn't matter.
    """
    # Use a queue file in tmp_path so the executor has somewhere to
    # write. Doesn't matter for this test — we patch the executor.
    raw_config = {
        "vault": {"path": str(tmp_path / "vault")},
        "telegram": {"allowed_users": [12345]},
        "pending_items": {
            "enabled": True,
            "queue_path": str(tmp_path / "pending_items.jsonl"),
        },
    }
    correction = ReplyCorrection(item_number=1, ok=True, consumed_token="noted")

    cwd = os.getcwd()
    try:
        os.chdir(tmp_path)  # no config.yaml here
        with patch(
            "alfred.pending_items.executor.resolve_local_item",
            new=_fake_resolve,
        ), patch(
            "alfred.daily_sync.reply_dispatch._run_coro_sync",
            return_value={"ok": True, "summary": "noted"},
        ):
            err, did_resolve, summary = _resolve_pending_item_correction(
                correction, _item(),
                self_instance="salem",
                raw_config=raw_config,
            )
        # Cwd has no config.yaml, but raw_config was plumbed through —
        # so the dispatcher succeeds rather than raising "config.yaml
        # not readable".
        assert err is None
        assert did_resolve is True
        assert summary == "noted"
    finally:
        os.chdir(cwd)


def test_resolve_local_skips_config_open_when_raw_provided(tmp_path: Path):
    """``_resolve_pending_item_locally`` must not open() when raw_config provided."""
    raw_config = {
        "vault": {"path": str(tmp_path / "vault")},
        "telegram": {"allowed_users": [12345]},
        "pending_items": {
            "enabled": True,
            "queue_path": str(tmp_path / "queue.jsonl"),
        },
    }
    cwd = os.getcwd()
    try:
        os.chdir(tmp_path)  # no config.yaml here
        with patch(
            "alfred.pending_items.executor.resolve_local_item",
            new=_fake_resolve,
        ), patch(
            "alfred.daily_sync.reply_dispatch._run_coro_sync",
            return_value={"ok": True, "summary": "ok"},
        ):
            result = _resolve_pending_item_locally(
                item_id="abc",
                resolution_id="noted",
                raw_config=raw_config,
            )
        assert result == "ok"
    finally:
        os.chdir(cwd)
