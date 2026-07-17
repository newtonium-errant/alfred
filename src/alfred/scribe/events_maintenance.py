"""Daemon-side event-store maintenance (event-store design §4 / §5.3 / §5.5 / §8 rows 8-9).

The stateless :class:`~alfred.scribe.events.ScribeEvents` facade deliberately does NOT own the
cross-sweep LATCHES this maintenance needs — the >24h heartbeat cadence, the per-UTC-day
suppression summary, and the per-(encounter, sha) post-attest-edit dedup. This helper owns those,
consuming ONLY the facade's frozen public API (``latest`` / ``query`` / ``attested_digest`` /
``store_heartbeat`` / ``flush_suppressed_reads`` / ``note_post_attest_edit_detected``). It never
mutates the facade.

The post-attest-edit scan is the ONLY working mechanism for the silent-edit case (design §5.3): the
clobber detector's attested/amended branch short-circuits BEFORE the sha compare and only runs on
NEW audio, so it can never see an out-of-band edit of a signed note. This index-driven scan compares
the current note body against the pinned attested ``body_sha`` every sweep (hot-window-bounded), and
runs FULL at boot / ``events verify --deep``. Detection only — NEVER a status mutation (anti-spoliation;
the sanctioned supersede path is ``amended``).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from alfred.scribe.attest import _body_sha  # the SAME sha attest pinned into the index
from alfred.vault.ops import VaultError, vault_read

if TYPE_CHECKING:
    from alfred.scribe.events import ScribeEvents

log = structlog.get_logger("scribe.events.maintenance")

# The clinical families the heartbeat counts (design §4/§5.1) — meta is EXCLUDED (the heartbeat
# row's own existence is the liveness signal; it never self-counts).
_HEARTBEAT_FAMILIES = ("attestation", "note", "encounter", "consent", "retention")
_HEARTBEAT_INTERVAL = timedelta(hours=24)
_DEFAULT_HOT_WINDOW_DAYS = 30


def _parse_ts(ts: Any) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _build_source_id_index(vault_path: Path) -> dict[str, str]:
    """Map ``source_id → rel_path`` by scanning the vault's ``clinical_note`` records — the
    re-derivation for attested-index entries whose ``rel_path`` is empty (the index was rebuilt from
    the chain, where ``rel_path`` is never carried, §7.4, or lost). Read-only, frontmatter only;
    built lazily at most once per scan. Mirrors ``vault_list``'s internal enumeration but returns
    the ``source_id`` the digest index keys on."""
    import frontmatter

    out: dict[str, str] = {}
    clinical_dir = Path(vault_path) / "clinical_note"
    if not clinical_dir.is_dir():
        return out
    for md in clinical_dir.rglob("*.md"):
        try:
            post = frontmatter.load(str(md))
        except Exception:  # noqa: BLE001 — an unreadable/half-written note is skipped, never fatal
            continue
        if post.metadata.get("type") != "clinical_note":
            continue
        sid = str(post.metadata.get("source_id") or "")
        if sid:
            out[sid] = str(md.relative_to(vault_path)).replace("\\", "/")
    return out


class ScribeEventMaintenance:
    """Owns the daemon's event-store latches. One instance per daemon lifetime."""

    def __init__(self, events: "ScribeEvents", *, hot_window_days: int = _DEFAULT_HOT_WINDOW_DAYS):
        self._ev = events
        self._hot_window_days = hot_window_days
        # (subject_id, current_body_sha) already surfaced — the per-(encounter, sha) latch so an
        # unfixed post-attest edit is emitted ONCE, not every 30s.
        self._edit_latch: set[tuple[str, str]] = set()
        self._last_summary_day: str | None = None

    # --- daily heartbeat (§4/§5.1) ---------------------------------------

    def heartbeat_if_due(self, *, now: str | None = None):
        """Emit one ``store.heartbeat`` to the clinical stream when >24h since the last (tail-region
        latch — the store IS the latch, so this survives a daemon restart). Payload = per-family
        counts since the last heartbeat, explicit-zero (intentionally-left-blank — 'no events today'
        becomes provable rather than ambiguous)."""
        ev = self._ev
        if not ev.active:
            return None
        now_dt = _parse_ts(now) or datetime.now(timezone.utc)
        last = ev.latest("clinical", family="meta", kind="store.heartbeat")
        last_ts = _parse_ts(last.get("ts")) if last else None
        if last_ts is not None and (now_dt - last_ts) < _HEARTBEAT_INTERVAL:
            return None
        since = last.get("ts") if last else None
        counts = {fam: 0 for fam in _HEARTBEAT_FAMILIES}
        for e in ev.query("clinical", since=since):
            fam = e.get("family")
            if fam in counts:  # meta (the boundary heartbeat itself) is never in counts
                counts[fam] += 1
        return ev.store_heartbeat(counts=counts, now=now)

    # --- daily suppressed-reads summary (§5.5) ---------------------------

    def flush_suppressed_if_new_day(self, *, now: str | None = None):
        """Emit the daily ``access.system_reads_summary`` once per UTC day (in-memory day latch) —
        so 'hook alive, zero human views' is provable from the access chain. Emits even a zero
        count (intentionally-left-blank)."""
        ev = self._ev
        if not ev.active:
            return None
        day = (_parse_ts(now) or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
        if self._last_summary_day == day:
            return None
        self._last_summary_day = day
        return ev.flush_suppressed_reads(now=now)

    # --- bounded post-attest-edit scan (§5.3) ----------------------------

    def post_attest_edit_scan(
        self, vault_path: Path, *, full: bool = False, emit: bool = True, now: str | None = None,
    ) -> list[dict]:
        """Compare each attested encounter's CURRENT note body against the pinned attested
        ``body_sha`` (the attested-digest index). On mismatch: emit ``note.post_attest_edit_detected``
        + a loud structlog, latched per (encounter, current_sha). Returns the mismatch list.

        ``full=True`` (boot / ``verify --deep``) scans every attested encounter; otherwise the
        per-sweep check is HOT-WINDOW-bounded — encounters attested within ``hot_window_days`` OR
        whose note file mtime is within that window. ``emit=False`` (the ``events verify --deep``
        query surface, §8 row 15 — query verbs append ONLY ``store.verified``, never a note event)
        REPORTS mismatches without emitting or latching. Detection ONLY — never a status mutation.

        INDEX-DRIVEN (§5.3 / adjudication item 5): iterates the attested-digest index ONCE, not a
        full clinical-stream scan + a per-subject index re-parse. When an index entry's ``rel_path``
        is empty (the index was REBUILT from the chain — ``rel_path`` is index-only, never chained,
        §7.4 — or lost), the note path is RE-DERIVED from ``subject_id`` by matching the vault's
        clinical_note ``source_id`` frontmatter (lazily, one vault scan). Without this, a rebuild
        silently blinds detection and ``verify --deep --rebuild-index`` prints a FALSE all-clear on
        the exact AG-Rec-6 'prove any post-signature change is visible' query."""
        ev = self._ev
        if not ev.active:
            return []
        now_dt = _parse_ts(now) or datetime.now(timezone.utc)
        cutoff = now_dt - timedelta(days=self._hot_window_days)
        index = ev.attested_index()  # ONE read (index-driven), body_sha/attested_at/rel_path per subject
        source_id_map: dict[str, str] | None = None  # built lazily ONLY if a rel_path is empty
        edits: list[dict] = []
        for sid, dig in index.items():
            if not sid or not isinstance(dig, dict):
                continue
            body_sha = str(dig.get("body_sha") or "")
            if not body_sha:
                continue
            attested_ts = str(dig.get("attested_at") or "")
            rel_path = str(dig.get("rel_path") or "")
            if not rel_path:
                # RE-DERIVE (R-A): the rebuilt/lost index dropped rel_path — locate the note by its
                # source_id, not `continue` (which is the silent false all-clear).
                if source_id_map is None:
                    source_id_map = _build_source_id_index(vault_path)
                rel_path = source_id_map.get(sid, "")
                if not rel_path:
                    continue  # the note is genuinely gone (deleted) — nothing to compare
            if not full and not self._in_hot_window(vault_path, rel_path, attested_ts, cutoff):
                continue
            try:
                body = vault_read(vault_path, rel_path)["body"]
            except (VaultError, OSError):
                continue
            current_sha = _body_sha(body)
            if current_sha == body_sha:
                continue
            record = {"subject_id": sid, "attested_body_sha": body_sha,
                      "current_body_sha": current_sha, "rel_path": rel_path}
            if not emit:
                edits.append(record)  # REPORT-only (verify --deep): no emit, no latch
                continue
            key = (sid, current_sha)
            if key in self._edit_latch:
                continue
            self._edit_latch.add(key)
            log.warning(
                "scribe.events.post_attest_edit_detected",
                subject_id=sid,
                attested_body_sha=body_sha,
                current_body_sha=current_sha,
                detail="the signed note's body no longer matches its attested digest — a post-attest "
                       "edit visibly re-opens review (detection only; the sanctioned supersede is amend)",
            )
            ev.note_post_attest_edit_detected(
                subject_id=sid, attested_body_sha=body_sha, current_body_sha=current_sha)
            edits.append(record)
        return edits

    def _in_hot_window(self, vault_path: Path, rel_path: str, attested_ts: str, cutoff: datetime) -> bool:
        attested_dt = _parse_ts(attested_ts)
        if attested_dt is not None and attested_dt >= cutoff:
            return True  # attested within the window
        try:
            mtime = datetime.fromtimestamp((vault_path / rel_path).stat().st_mtime, tz=timezone.utc)
        except OSError:
            return False
        return mtime >= cutoff  # OR the note file was modified within the window
