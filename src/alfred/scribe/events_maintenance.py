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
        self, vault_path: Path, *, full: bool = False, now: str | None = None,
    ) -> list[dict]:
        """Compare each attested encounter's CURRENT note body against the pinned attested
        ``body_sha`` (the attested-digest index). On mismatch: emit ``note.post_attest_edit_detected``
        + a loud structlog, latched per (encounter, current_sha). Returns the mismatch list.

        ``full=True`` (boot / ``verify --deep``) scans every attested encounter; otherwise the
        per-sweep check is HOT-WINDOW-bounded — encounters attested within ``hot_window_days`` OR
        whose note file mtime is within that window. Detection ONLY — never a status mutation."""
        ev = self._ev
        if not ev.active:
            return []
        now_dt = _parse_ts(now) or datetime.now(timezone.utc)
        cutoff = now_dt - timedelta(days=self._hot_window_days)
        # subject_id → attested ts (chain-derived; the index carries rel_path per subject).
        subjects: dict[str, str] = {}
        for e in ev.query("clinical", kind="attest.recorded"):
            sid = str(e.get("subject_id") or "")
            if sid:
                subjects[sid] = str(e.get("ts") or "")
        edits: list[dict] = []
        for sid, attested_ts in subjects.items():
            dig = ev.attested_digest(sid)
            if not dig or not dig.get("rel_path"):
                continue  # rebuilt index (rel_path="") can't locate the note — skip
            rel_path = dig["rel_path"]
            if not full and not self._in_hot_window(vault_path, rel_path, attested_ts, cutoff):
                continue
            try:
                body = vault_read(vault_path, rel_path)["body"]
            except (VaultError, OSError):
                continue
            current_sha = _body_sha(body)
            if current_sha == dig["body_sha"]:
                continue
            key = (sid, current_sha)
            if key in self._edit_latch:
                continue
            self._edit_latch.add(key)
            log.warning(
                "scribe.events.post_attest_edit_detected",
                subject_id=sid,
                attested_body_sha=dig["body_sha"],
                current_body_sha=current_sha,
                detail="the signed note's body no longer matches its attested digest — a post-attest "
                       "edit visibly re-opens review (detection only; the sanctioned supersede is amend)",
            )
            ev.note_post_attest_edit_detected(
                subject_id=sid, attested_body_sha=dig["body_sha"], current_body_sha=current_sha)
            edits.append({"subject_id": sid, "attested_body_sha": dig["body_sha"],
                          "current_body_sha": current_sha, "rel_path": rel_path})
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
