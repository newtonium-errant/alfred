"""STAY-C bug-report capture + triage (task #4).

Box-local, PHI-cautious bug reports for the sovereign scribe. ``POST /scribe/bug`` (loopback,
INGEST-token gated, NOT bearer-exempt) writes ``<ts>-<hex>.md`` files (an OPAQUE id — the
reporter's free-text summary is NEVER in the filename, only inside the file body) under the
resolved bug dir; ``alfred scribe bugs {list|show|resolve}`` triages them on-box.

SOVEREIGN POSTURE — the daemon NEVER egresses a report. Surfacing them off-box is the separate
box-watcher component's job (a systemd path-unit outside the clinical unit). Reports are
treated as **PHI-until-a-human-says-otherwise**. The auto-context the page attaches is PHI-FREE
by construction (view/hash, serverState, clinician COUNT, clinician SLUG — a staff id, never a
name, the attribution chip, UA, timestamps) plus a memory-only diagnostic ring buffer of
UI-event traces (code-path breadcrumbs, never content). The free-text summary/detail carry the
page's "don't include patient details" caution — but because a human can still paste PHI, the
file posture is PHI-grade regardless, and the OPAQUE id is what makes the locked-mode watcher
ping + the daemon log PHI-safe (the summary lives only in the 0640 file body).

CUSTODY MODES (explicit, NOT umask-dependent — the clinical unit's UMask=0077 would force
0700/0600 and lock the different-user box watcher out): dir :data:`_BUG_DIR_MODE` (2750 — setgid
+ group r-x, so a shared-group watcher can iterdir + read but not write/delete), files
:data:`_BUG_FILE_MODE` (0640 — owner rw, group r, so full mode can read the body). The shared
group + chgrp is the operator's install step (see the watcher .service template); the watcher
keeps its OWN forwarded-state OUTSIDE this dir, so the dir need not be group-writable.

CAPS (a stuck/abusive client must never fill the disk, and the file must stay bounded):
  * per-POST body ≤ ``bug.max_body_bytes`` (enforced at the route, on the raw body);
  * ≤ ``bug.max_open_reports`` UNRESOLVED (top-level) reports — over that, the route 429s;
  * the diagnostic ring is truncated to :data:`_MAX_EVENTS` events × :data:`_MAX_EVENT_LEN`
    chars each when written, so an oversized client ring cannot bloat the file.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)

# A bug id is the filename stem — a strict, traversal-proof charset (no '/', no '..', no
# leading dot). The CLI + route resolve a report BY this id, so it gates path safety.
BUG_ID_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._-]{0,127}$")

# Explicit permission modes (NOT umask-dependent — the clinical unit runs UMask=0077, which
# would otherwise force 0700/0600 and lock the DIFFERENT-user box watcher out entirely). Set by
# explicit chmod AFTER create. The dir is setgid + group r-x so a shared-group watcher can
# iterdir the dir and READ bodies (0640 files) in full mode; the group is NOT given write on
# the dir (the watcher keeps its own forwarded-state OUTSIDE the dir), so a group member can
# neither add nor delete reports. The shared group + chgrp is the operator's install step
# (documented in the watcher .service template). File CONTENTS stay owner-write, group-read.
_BUG_DIR_MODE = 0o2750       # setgid + owner rwx + group r-x (NO group write)
_BUG_FILE_MODE = 0o640       # owner rw + group r — the watcher (group) reads bodies (full mode)

_RESOLVED_SUBDIR = "resolved"
_MAX_EVENTS = 40                 # diagnostic ring truncation (the page keeps ~20; be generous)
_MAX_EVENT_LEN = 300             # per-event char cap
_MAX_SUMMARY_LEN = 200           # single-line frontmatter summary cap
_MAX_CONTEXT_VALUE_LEN = 400     # per auto-context value cap

# The PHI-free auto-context keys the page is allowed to attach. An unknown key is DROPPED
# (never written) — the report file can only ever carry this closed, enumerated, PHI-free set.
_ALLOWED_CONTEXT_KEYS: tuple[str, ...] = (
    "view", "server_state", "clinicians_len", "user", "attribution", "ua", "client_ts",
)


class BugCapRefused(Exception):
    """A cap was hit — the report was NOT written. ``reason`` is an opaque code the route
    maps to a 4xx the UI renders (never a filesystem path / PHI)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def resolve_bug_dir(config) -> Path:
    """The bug-report directory. ``bug.dir`` if set, else ``<input_dir parent>/bugs`` — so an
    operator who points ``input_dir`` at ``<STAYC_DATA>/inbox`` gets ``<STAYC_DATA>/bugs`` for
    free (per-instance-correct, never a single-instance literal)."""
    configured = getattr(config.bug, "dir", "") or ""
    if configured:
        return Path(configured).expanduser()
    return Path(config.input_dir).expanduser().parent / "bugs"


def _sanitize_line(value: Any, cap: int) -> str:
    """One safe frontmatter line: coerce to str, collapse ALL whitespace (incl. newlines) to
    single spaces so a value can't break the ``k: v`` frontmatter or inject a new key, cap
    length. (The reader is a tolerant line parser, but keeping the writer clean is the belt.)"""
    s = re.sub(r"\s+", " ", str(value)).strip()
    return s[:cap]


def _chmod_quiet(path: Path, mode: int) -> None:
    """Best-effort explicit chmod. Never crash a write on a chmod failure (e.g. the operator
    hasn't yet chgrp'd the dir) — the perms are a deployment concern, not a write-blocker; the
    watcher fails LOUD on its own side if it then can't read (R2)."""
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def _count_open_reports(bug_dir: Path) -> int:
    if not bug_dir.is_dir():
        return 0
    return sum(1 for p in bug_dir.iterdir() if p.is_file() and p.suffix == ".md")


def _unique_path(bug_dir: Path, stem: str) -> tuple[Path, str]:
    """A non-colliding ``<stem>.md`` path (append -2, -3, … if needed). Returns (path, id)."""
    candidate = stem
    n = 1
    while (bug_dir / f"{candidate}.md").exists():
        n += 1
        candidate = f"{stem}-{n}"
    return bug_dir / f"{candidate}.md", candidate


def _write_report_file(path: Path, text: str) -> None:
    """Write ``text`` atomically (0600 temp → replace) then chmod to :data:`_BUG_FILE_MODE`
    (0640) EXPLICITLY — never a window where the file is other-readable, and the explicit chmod
    (not the create mode) is what survives the clinical unit's UMask=0077 so the shared-group
    watcher can read the body in full mode. Other (world) never gets any access."""
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, path)
    os.chmod(path, _BUG_FILE_MODE)


def _render_report(*, bug_id: str, summary: str, detail: str,
                   context: dict[str, Any], events: list[Any]) -> str:
    """Render the report markdown: a tolerant ``k: v`` frontmatter (PHI-free auto-context) + a
    Detail section + the truncated diagnostic ring. Every value single-lined + capped."""
    fm: list[str] = [
        f"id: {bug_id}",
        f"created: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}",
        f"summary: {_sanitize_line(summary, _MAX_SUMMARY_LEN)}",
    ]
    ctx = context if isinstance(context, dict) else {}
    for key in _ALLOWED_CONTEXT_KEYS:                    # closed, enumerated, PHI-free set only
        if key in ctx and ctx[key] is not None:
            fm.append(f"{key}: {_sanitize_line(ctx[key], _MAX_CONTEXT_VALUE_LEN)}")

    ev = events if isinstance(events, list) else []
    trimmed = [str(e)[:_MAX_EVENT_LEN] for e in ev[-_MAX_EVENTS:]]
    ev_block = "\n".join(f"- {e}" for e in trimmed) if trimmed else "_(none captured)_"
    detail_text = (str(detail).strip() or "_(no detail provided)_")

    return (
        "---\n" + "\n".join(fm) + "\n---\n\n"
        "## Detail\n\n" + detail_text + "\n\n"
        "## Diagnostic events (RAM ring buffer, PHI-free UI trace)\n\n" + ev_block + "\n"
    )


def write_bug_report(config, *, summary: str, detail: str,
                     context: dict[str, Any] | None = None,
                     events: list[Any] | None = None) -> tuple[Path, str]:
    """Write one bug report → (path, bug_id). Enforces the open-report disk backstop
    (``BugCapRefused('report_cap')``). The per-POST BODY cap is the route's job (on the raw
    bytes, before JSON parse); the ring truncation is applied here."""
    bug_dir = resolve_bug_dir(config)
    bug_dir.mkdir(parents=True, exist_ok=True)
    _chmod_quiet(bug_dir, _BUG_DIR_MODE)         # explicit dir mode (setgid, group r-x) — R3
    if _count_open_reports(bug_dir) >= config.bug.max_open_reports:
        raise BugCapRefused("report_cap")

    # OPAQUE id — timestamp + short random hex. The reporter's free-text summary is NOT in the
    # id (it lives ONLY inside the 0600/0640 file body), so the id is PHI-safe to log AND to
    # egress: the locked-mode watcher ping sends this id, and the daemon log records it. (R1 —
    # the prior slug-in-filename leaked reporter free text off-box in locked mode + into the log.)
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    stem = f"{ts}-{secrets.token_hex(4)}"
    path, bug_id = _unique_path(bug_dir, stem)
    text = _render_report(bug_id=bug_id, summary=summary, detail=detail,
                          context=context or {}, events=events or [])
    _write_report_file(path, text)
    # PHI-safe log — the OPAQUE id + count only, NEVER the summary/detail/context (which may
    # carry PHI). The id is a random token, NOT a transform of the summary (R1/R13).
    log.info("scribe.bug.written", bug_id=bug_id, open_reports=_count_open_reports(bug_dir))
    return path, bug_id


# --- triage (CLI surface) ---------------------------------------------------

def _parse_frontmatter(text: str) -> dict[str, str]:
    """Tolerant reader of the ``k: v`` frontmatter WE wrote (not a full YAML parser — the
    writer single-lines every value, so a naive split is exact and injection-proof)."""
    out: dict[str, str] = {}
    if not text.startswith("---\n"):
        return out
    body = text[4:]
    end = body.find("\n---")
    block = body[:end] if end != -1 else body
    for line in block.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            out[k.strip()] = v.strip()
    return out


def _report_path(config, bug_id: str) -> Path | None:
    """The on-disk path for ``bug_id`` (top-level OR resolved/), or None. Traversal-guarded:
    a non-:data:`BUG_ID_RE` id resolves to None (never a filesystem escape)."""
    if not BUG_ID_RE.fullmatch(bug_id or ""):
        return None
    bug_dir = resolve_bug_dir(config)
    for candidate in (bug_dir / f"{bug_id}.md", bug_dir / _RESOLVED_SUBDIR / f"{bug_id}.md"):
        if candidate.is_file():
            return candidate
    return None


def list_bugs(config, *, include_resolved: bool = False) -> list[dict[str, Any]]:
    """List reports (unresolved top-level; ``include_resolved`` adds resolved/), newest first
    by id (ts-prefixed). Each: ``{id, created, summary, resolved}``."""
    bug_dir = resolve_bug_dir(config)
    rows: list[dict[str, Any]] = []

    def _collect(d: Path, resolved: bool) -> None:
        if not d.is_dir():
            return
        for p in sorted(d.iterdir()):
            if p.is_file() and p.suffix == ".md":
                fm = _parse_frontmatter(p.read_text(encoding="utf-8", errors="replace"))
                rows.append({
                    "id": p.stem,
                    "created": fm.get("created", ""),
                    "summary": fm.get("summary", ""),
                    "resolved": resolved,
                })

    _collect(bug_dir, False)
    if include_resolved:
        _collect(bug_dir / _RESOLVED_SUBDIR, True)
    rows.sort(key=lambda r: r["id"], reverse=True)
    return rows


def read_bug(config, bug_id: str) -> str | None:
    """The full report text for ``bug_id`` (top-level or resolved/), or None if absent."""
    path = _report_path(config, bug_id)
    return path.read_text(encoding="utf-8", errors="replace") if path else None


def resolve_bug(config, bug_id: str) -> bool:
    """Move ``<id>.md`` into ``resolved/`` (idempotent-ish: already-resolved → True). Returns
    False if the id is unknown / malformed."""
    path = _report_path(config, bug_id)
    if path is None:
        return False
    if path.parent.name == _RESOLVED_SUBDIR:
        return True                                      # already resolved
    dest_dir = resolve_bug_dir(config) / _RESOLVED_SUBDIR
    dest_dir.mkdir(parents=True, exist_ok=True)
    _chmod_quiet(dest_dir, _BUG_DIR_MODE)        # resolved/ inherits the same explicit modes
    os.replace(path, dest_dir / path.name)
    log.info("scribe.bug.resolved", bug_id=bug_id)
    return True
