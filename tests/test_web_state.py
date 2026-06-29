"""Tests for ``alfred.web.state`` — the single-use magic-link nonce store."""

from __future__ import annotations

import json
from pathlib import Path

from alfred.web.state import WebAuthState


def _store(tmp_path: Path) -> WebAuthState:
    s = WebAuthState.create(tmp_path / "web_auth_state.json")
    s.load()
    return s


def test_record_and_consume_returns_entry(tmp_path) -> None:
    s = _store(tmp_path)
    s.record_nonce("abc", "andrew", exp=10_000_000_000)  # far future
    entry = s.consume_nonce("abc", now=1_000_000_000)
    assert entry == {"name": "andrew", "exp": 10_000_000_000}


def test_consume_is_single_use(tmp_path) -> None:
    s = _store(tmp_path)
    s.record_nonce("abc", "andrew", exp=10_000_000_000)
    assert s.consume_nonce("abc", now=1_000_000_000) is not None
    # Replay → already consumed → None.
    assert s.consume_nonce("abc", now=1_000_000_000) is None


def test_consume_expired_returns_none_and_removes(tmp_path) -> None:
    s = _store(tmp_path)
    s.record_nonce("abc", "andrew", exp=1_000)
    assert s.consume_nonce("abc", now=2_000) is None  # expired
    # And it's gone (consumed even though expired) → second call also None.
    assert "abc" not in s.nonces


def test_consume_absent_returns_none(tmp_path) -> None:
    s = _store(tmp_path)
    assert s.consume_nonce("never-recorded", now=1_000) is None


def test_prune_expired(tmp_path) -> None:
    s = _store(tmp_path)
    s.record_nonce("old", "a", exp=1_000)
    s.record_nonce("fresh", "b", exp=10_000_000_000)
    removed = s.prune_expired(now=2_000)
    assert removed == 1
    assert "old" not in s.nonces
    assert "fresh" in s.nonces


def test_save_load_roundtrip(tmp_path) -> None:
    s = _store(tmp_path)
    s.record_nonce("abc", "andrew", exp=10_000_000_000)
    s.save()
    s2 = _store(tmp_path)
    assert s2.nonces == {"abc": {"name": "andrew", "exp": 10_000_000_000}}


def test_load_tolerates_corrupt_file(tmp_path) -> None:
    path = tmp_path / "web_auth_state.json"
    path.write_text("{ not json", encoding="utf-8")
    s = WebAuthState.create(path)
    s.load()  # must not raise
    assert s.nonces == {}


def test_load_schema_tolerance_extra_keys_and_bad_entries(tmp_path) -> None:
    path = tmp_path / "web_auth_state.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "future_field": "ignored",
                "nonces": {
                    "good": {"name": "andrew", "exp": 10_000_000_000},
                    "bad": "not-a-dict",  # dropped
                },
            }
        ),
        encoding="utf-8",
    )
    s = WebAuthState.create(path)
    s.load()
    assert "good" in s.nonces
    assert "bad" not in s.nonces


def test_missing_file_is_clean(tmp_path) -> None:
    s = WebAuthState.create(tmp_path / "does_not_exist.json")
    s.load()  # no-op, no raise
    assert s.nonces == {}
