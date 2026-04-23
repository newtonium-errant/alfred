"""Tests for the brief's Peer Digests section (V.E.R.A. receiver).

Covers:
- Single-peer happy path: a digest record renders verbatim under
  ``### {CANONICAL} Update``.
- Multiple peers render in deterministic order.
- Missing-peer intentionally-left-blank fallback.
- Stale records (yesterday's date) are excluded.
- Disabled config returns empty string.
- ``peer_canonical_names`` overrides the auto-uppercase header.
- Same-peer multi-push uses the latest record.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from alfred.brief.config import PeerDigestsConfig, load_from_unified
from alfred.brief.peer_digests import render_peer_digests_section


TODAY = "2026-04-23"
YESTERDAY = "2026-04-22"


def _write_peer_digest(
    vault: Path,
    *,
    peer: str,
    date: str,
    body: str,
    received_at: str = "2026-04-23T05:30:00+00:00",
) -> Path:
    """Materialise a peer-digest record under ``vault/run/``."""
    fm = {
        "type": "run",
        "name": f"Peer Digest {peer} {date}",
        "source": "peer",
        "peer": peer,
        "received_at": received_at,
        "created": date,
        "correlation_id": f"{peer}-brief-{date.replace('-', '')}",
        "content_length": len(body.encode("utf-8")),
        "tags": ["peer-digest", peer],
    }
    fm_str = yaml.dump(fm, default_flow_style=False, allow_unicode=True, sort_keys=False)
    text = f"---\n{fm_str}---\n\n{body}\n"
    run_dir = vault / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / f"Peer Digest {peer} {date}.md"
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    v = tmp_path / "vault"
    v.mkdir()
    return v


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_single_peer_digest_renders_verbatim(vault: Path) -> None:
    body = (
        "**Yesterday:**\n"
        "- 4 commits to aftermath-lab\n"
        "- BIT all green\n\n"
        "**Today:**\n"
        "- Polish the daily sync replies\n\n"
        "**Posture:** green — all systems nominal."
    )
    _write_peer_digest(vault, peer="kal-le", date=TODAY, body=body)

    out = render_peer_digests_section(vault, TODAY, expected_peers=["kal-le"])
    assert "### KAL-LE Update" in out
    assert "4 commits to aftermath-lab" in out
    assert "**Posture:** green" in out


def test_multiple_peers_rendered_in_expected_order(vault: Path) -> None:
    """``expected_peers`` controls the section order."""
    _write_peer_digest(
        vault, peer="kal-le", date=TODAY,
        body="kal-le content here",
    )
    _write_peer_digest(
        vault, peer="stay-c", date=TODAY,
        body="stay-c content here",
    )
    out = render_peer_digests_section(
        vault, TODAY, expected_peers=["stay-c", "kal-le"],
    )
    pos_stayc = out.index("STAY-C Update")
    pos_kalle = out.index("KAL-LE Update")
    assert pos_stayc < pos_kalle


def test_no_record_for_expected_peer_renders_blank_line(vault: Path) -> None:
    """Expected peer + no record → intentionally-left-blank line."""
    out = render_peer_digests_section(
        vault, TODAY, expected_peers=["kal-le"],
    )
    assert "### KAL-LE Update" in out
    assert "No KAL-LE update today." in out


def test_unexpected_peer_pushed_still_renders(vault: Path) -> None:
    """A peer not in expected_peers still appears when it pushed today."""
    _write_peer_digest(vault, peer="surprise", date=TODAY, body="surprise body")
    out = render_peer_digests_section(vault, TODAY, expected_peers=[])
    assert "### SURPRISE Update" in out
    assert "surprise body" in out


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


def test_yesterday_record_excluded(vault: Path) -> None:
    """Stale records (yesterday) should not appear in today's section."""
    _write_peer_digest(
        vault, peer="kal-le", date=YESTERDAY,
        body="this should not appear",
    )
    out = render_peer_digests_section(vault, TODAY, expected_peers=["kal-le"])
    assert "this should not appear" not in out
    # Falls back to intentionally-left-blank because no record found
    # for today.
    assert "No KAL-LE update today." in out


def test_non_peer_run_record_excluded(vault: Path) -> None:
    """A regular run record (e.g. Morning Brief) without source=peer is skipped."""
    run_dir = vault / "run"
    run_dir.mkdir(parents=True)
    fm = {
        "type": "run",
        "name": "Morning Brief 2026-04-23",
        "created": TODAY,
        # No source: peer, no peer field.
    }
    fm_str = yaml.dump(fm, default_flow_style=False, sort_keys=False)
    (run_dir / "Morning Brief 2026-04-23.md").write_text(
        f"---\n{fm_str}---\n\nbrief body\n", encoding="utf-8"
    )

    out = render_peer_digests_section(vault, TODAY, expected_peers=["kal-le"])
    # Doesn't pull in the brief body — only the missing-peer line.
    assert "brief body" not in out
    assert "No KAL-LE update today." in out


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_no_peers_configured_and_no_records_returns_empty(vault: Path) -> None:
    """Daemon uses empty string as the omit-entirely signal."""
    out = render_peer_digests_section(vault, TODAY, expected_peers=[])
    assert out == ""


def test_canonical_name_override_used(vault: Path) -> None:
    _write_peer_digest(vault, peer="kalle", date=TODAY, body="text")
    out = render_peer_digests_section(
        vault, TODAY,
        expected_peers=["kalle"],
        peer_canonical_names={"kalle": "K.A.L.L.E."},
    )
    assert "### K.A.L.L.E. Update" in out


def test_same_peer_multiple_pushes_uses_latest(vault: Path) -> None:
    """Two pushes for the same peer-day — the latest received_at wins."""
    # First push (earlier received_at).
    _write_peer_digest(
        vault, peer="kal-le", date=TODAY,
        body="EARLIER PUSH",
        received_at="2026-04-23T05:30:00+00:00",
    )
    # Overwrite the file but with a later received_at + different body.
    _write_peer_digest(
        vault, peer="kal-le", date=TODAY,
        body="LATER PUSH WINS",
        received_at="2026-04-23T05:45:00+00:00",
    )
    out = render_peer_digests_section(vault, TODAY, expected_peers=["kal-le"])
    assert "LATER PUSH WINS" in out
    assert "EARLIER PUSH" not in out


def test_corrupt_record_is_skipped(vault: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Malformed YAML in a peer-digest record doesn't break the section."""
    run_dir = vault / "run"
    run_dir.mkdir(parents=True)
    (run_dir / "Peer Digest broken 2026-04-23.md").write_text(
        "---\nnot: valid: yaml: here\n: bad\n---\n\nbody\n",
        encoding="utf-8",
    )
    # Add a clean record alongside.
    _write_peer_digest(vault, peer="kal-le", date=TODAY, body="clean digest")
    out = render_peer_digests_section(vault, TODAY, expected_peers=["kal-le"])
    assert "clean digest" in out


# ---------------------------------------------------------------------------
# Config plumbing
# ---------------------------------------------------------------------------


def test_load_from_unified_defaults(tmp_path: Path) -> None:
    raw = {"vault": {"path": str(tmp_path)}, "brief": {}}
    cfg = load_from_unified(raw)
    assert cfg.peer_digests.enabled is True
    assert cfg.peer_digests.expected_peers == []
    assert cfg.peer_digests.peer_canonical_names == {}


def test_load_from_unified_overrides(tmp_path: Path) -> None:
    raw = {
        "vault": {"path": str(tmp_path)},
        "brief": {
            "peer_digests": {
                "enabled": True,
                "expected_peers": ["kal-le", "stay-c"],
                "peer_canonical_names": {"kalle": "K.A.L.L.E."},
            },
        },
    }
    cfg = load_from_unified(raw)
    assert cfg.peer_digests.enabled is True
    assert cfg.peer_digests.expected_peers == ["kal-le", "stay-c"]
    assert cfg.peer_digests.peer_canonical_names == {"kalle": "K.A.L.L.E."}


def test_load_from_unified_disabled(tmp_path: Path) -> None:
    raw = {
        "vault": {"path": str(tmp_path)},
        "brief": {"peer_digests": {"enabled": False}},
    }
    cfg = load_from_unified(raw)
    assert cfg.peer_digests.enabled is False
