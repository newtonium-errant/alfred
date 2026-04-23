"""Peer Digests section — renders one-slide updates pushed by peer instances.

V.E.R.A. content-arc receiver pair: Salem's transport accepts a digest
from a named peer (c1: ``POST /peer/brief_digest``) and writes it to
``vault/run/Peer Digest {peer} {date}.md``. This module reads those
records on brief day and renders one section per peer.

Section shape:
    ## KAL-LE Update
    {digest_markdown verbatim}

When today has no digest from a known peer, the section renders the
intentionally-left-blank line per ``feedback_intentionally_left_blank``:
    No KAL-LE update today.

Multi-peer ready by design: the daemon iterates the configured peers
and asks this module for one rendered section per peer. STAY-C will
plug in via the same shape once it ships.

Source-of-truth choice:
    Records — not the transport state file — because the brief is a
    vault-rendered artifact, and the digest record IS the canonical
    history of what each peer reported. The transport state file is
    bookkeeping for the request/response cycle, not durable history.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import frontmatter

from .utils import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class PeerDigestRecord:
    """One peer-digest record read from ``vault/run/``."""

    peer: str
    date: str
    body: str
    received_at: str


def _scan_peer_digests(vault_path: Path, today_iso: str) -> list[PeerDigestRecord]:
    """Find all peer-digest records for ``today_iso``.

    Looks under ``vault/run/`` for records with frontmatter
    ``type: run``, ``source: peer``, ``created: <today_iso>``. Returns
    them in deterministic order keyed on ``(peer, received_at)`` so a
    same-peer re-push doesn't shuffle ordering between brief runs.
    """
    run_dir = vault_path / "run"
    if not run_dir.exists():
        return []

    out: list[PeerDigestRecord] = []
    for md_file in run_dir.glob("Peer Digest *.md"):
        try:
            post = frontmatter.load(str(md_file))
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "brief.peer_digests.parse_failed",
                path=str(md_file),
                error=str(exc),
            )
            continue
        fm = dict(post.metadata or {})
        if fm.get("type") != "run":
            continue
        if fm.get("source") != "peer":
            continue
        # ``created`` is the digest's logical date — frontmatter may
        # parse it as a date object (PyYAML) or a string. Normalise to
        # ISO string for the comparison.
        created = fm.get("created")
        created_iso = str(created) if created is not None else ""
        if created_iso[:10] != today_iso:
            continue
        peer = str(fm.get("peer") or "")
        if not peer:
            continue
        out.append(
            PeerDigestRecord(
                peer=peer,
                date=created_iso[:10],
                body=post.content.strip(),
                received_at=str(fm.get("received_at") or ""),
            )
        )
    out.sort(key=lambda r: (r.peer, r.received_at))
    return out


def _peer_canonical_name(peer: str, peer_canonical_names: dict[str, str] | None) -> str:
    """Resolve a peer name → human-facing section title.

    Looks up the override in ``peer_canonical_names`` first (caller
    pulls these from ``transport.peers`` config in v2; v1 uses an
    inline fallback table). Falls back to a sensible upper-cased form
    so an unconfigured peer still gets a readable section header.
    """
    if peer_canonical_names and peer in peer_canonical_names:
        return peer_canonical_names[peer]
    # Sensible default — kal-le → KAL-LE, stay-c → STAY-C.
    return peer.upper().replace("_", "-")


def render_peer_digests_section(
    vault_path: str | Path,
    today_iso: str,
    *,
    expected_peers: list[str] | None = None,
    peer_canonical_names: dict[str, str] | None = None,
) -> str:
    """Render the Peer Digests section body markdown.

    Args:
        vault_path: Path to the vault root.
        today_iso: ISO date string for the brief (caller resolves from
            the daemon's wall clock).
        expected_peers: Names of peers whose digest we expect to find.
            When a peer in this list has no record for today, the
            section emits "No {canonical} update today." as the
            intentionally-left-blank signal. When ``None`` or empty,
            only peers with actual records appear — useful for v1 when
            the daemon doesn't know which peers should be reporting.
        peer_canonical_names: Optional override map from short peer
            name (``"kal-le"``) to canonical display name
            (``"KAL-LE"``). Falls back to upper-case-with-dashes.

    Returns:
        Markdown string. Empty when ``expected_peers`` is empty AND no
        peer records were found — the daemon uses empty-string as the
        "omit the section entirely" signal (matching upcoming_events).
        When at least one expected peer was configured, ALWAYS returns
        a non-empty string (intentionally-left-blank invariant).
    """
    vault = Path(vault_path)
    records = _scan_peer_digests(vault, today_iso)
    records_by_peer: dict[str, list[PeerDigestRecord]] = {}
    for record in records:
        records_by_peer.setdefault(record.peer, []).append(record)

    expected = list(expected_peers or [])
    # Union of expected + actually-received — handles the case where a
    # peer reports without being in the expected list (still rendered).
    seen_peers = list(dict.fromkeys(expected + list(records_by_peer.keys())))

    if not seen_peers:
        # Section disabled by config (no peers expected) AND nothing
        # arrived — render nothing.
        return ""

    parts: list[str] = []
    for peer in seen_peers:
        canonical = _peer_canonical_name(peer, peer_canonical_names)
        parts.append(f"### {canonical} Update")
        peer_records = records_by_peer.get(peer, [])
        if not peer_records:
            # Intentionally-left-blank — operator sees the section ran.
            parts.append(f"No {canonical} update today.")
        else:
            # In the rare case a peer pushes more than once in a day
            # (e.g. operator rerun), render the latest by received_at.
            latest = peer_records[-1]
            parts.append(latest.body)
        parts.append("")

    # Drop trailing blank line for cleanliness.
    while parts and parts[-1] == "":
        parts.pop()
    return "\n".join(parts)
