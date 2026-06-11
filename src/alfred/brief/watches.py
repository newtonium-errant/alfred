"""Watch Items section — config-driven upstream checks run by the brief.

The morning brief performs live checks (the way the weather section
already does) against operator-configured watch items and renders one
line per item. Generic by design: the watch list is pure config
(``brief.watches``); nothing repo- or project-specific lives in code.

Two watch types (scope-first, deliberately small):

* ``github_pr`` — track one PR's state (open / merged / closed).
  FLIP = the state changed since the last brief (especially → merged).
* ``github_release_mention`` — watch a repo's releases for the first
  release strictly newer than ``baseline_tag`` (or the newest tag seen
  on a previous check) whose tag + name + body matches ``pattern``
  (regex, case-insensitive). FLIP = the first match appears.

Render contract (intentionally-left-blank throughout — a configured
watch ALWAYS produces a line):

* stable state    → quiet:  ``PR owner/repo#N — OPEN (unchanged)``
* first check     → quiet:  ``... — OPEN (baseline)``
* FLIP            → loud:   ``🚨 ... — MERGED — <on_flip_note>``
* terminal, post-flip → every subsequent brief renders the state plus
  ``✓ done — remove from config when acted on`` until the operator
  removes the item (operator-in-the-loop; never auto-removed). Once
  terminal, the API is no longer queried for that item.
* check failure   → ``watch unavailable (api error: ...)`` + a
  ``brief.watch_check_failed`` warning — NEVER kills the brief
  (per-item containment here; the daemon adds a section-boundary
  guard on top, same idiom as weather/874c751).

Transport: unauthenticated GitHub REST via httpx (60 req/hr unauth is
plenty at one brief per day; deliberately NOT shelling out to ``gh``
from the daemon). Timeouts short.

State: ``<data_dir>/brief_watches_state.json`` — last-seen per watch
id, atomic write, load-time schema-tolerance filter per the CLAUDE.md
state contract.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx

from .config import WatchItemConfig
from .utils import get_logger

log = get_logger(__name__)


GITHUB_API_BASE = "https://api.github.com"

# Short — the brief shouldn't stall on a slow GitHub day; a timeout is
# just a "watch unavailable" line.
_TIMEOUT_SECONDS = 10.0

# Unauthenticated GitHub REST requires a User-Agent.
_HEADERS = {
    "Accept": "application/vnd.github+json",
    "User-Agent": "alfred-brief-watches",
}

# How many releases one check scans (newest-first page). The boundary
# (baseline_tag / last-seen tag) advances every check, so a once-a-day
# cadence never needs more than the releases shipped since yesterday.
_RELEASES_PER_PAGE = 20

FLIP_MARKER = "🚨"
DONE_TAIL = "✓ done — remove from config when acted on"

# PR states that end the watch's useful life — once reached (and
# announced), the item renders the done-tail and stops querying.
_PR_TERMINAL_STATES = frozenset({"merged", "closed"})


# --- State -------------------------------------------------------------------


@dataclass
class WatchItemState:
    """Per-watch-id persisted state.

    ``last_state`` — github_pr: the last observed PR state
    ("open"/"merged"/"closed"); empty = never checked.
    ``last_seen_tag`` — github_release_mention: newest release tag
    already scanned (the "strictly newer than" boundary, advancing past
    ``baseline_tag`` after the first check).
    ``matched_tag`` — github_release_mention: the release that matched;
    non-empty = terminal.
    """

    last_state: str = ""
    last_seen_tag: str = ""
    matched_tag: str = ""
    last_checked: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "WatchItemState":
        # Load-time schema-tolerance contract (CLAUDE.md): unknown
        # fields from a newer/older writer are dropped, never crash.
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)


def load_watch_state(path: Path) -> dict[str, WatchItemState]:
    """Load the per-id state map. Missing/corrupt file → fresh (warned)."""
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        # ValueError covers BOTH json.JSONDecodeError (its subclass) AND
        # UnicodeDecodeError from read_text on an invalid-UTF-8 file
        # (review nit a3 — the old JSONDecodeError-only catch let a
        # binary-corrupted state file escalate to the daemon guard
        # instead of degrading to a fresh baseline here).
        log.warning(
            "brief.watches_state_load_failed",
            path=str(path),
            error=str(exc),
            error_type=exc.__class__.__name__,
        )
        return {}
    items_raw = raw.get("items", {}) if isinstance(raw, dict) else {}
    out: dict[str, WatchItemState] = {}
    if isinstance(items_raw, dict):
        for key, item_raw in items_raw.items():
            if isinstance(item_raw, dict):
                out[str(key)] = WatchItemState.from_dict(item_raw)
    return out


def save_watch_state(path: Path, items: dict[str, WatchItemState]) -> None:
    """Atomic write (.tmp → rename), repo state-persistence pattern."""
    payload = {"items": {key: asdict(st) for key, st in items.items()}}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- GitHub fetchers (module-level for test monkeypatching, the
# weather-module convention) --------------------------------------------------


async def _fetch_pr(client: httpx.AsyncClient, repo: str, number: int) -> dict:
    resp = await client.get(f"{GITHUB_API_BASE}/repos/{repo}/pulls/{number}")
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, dict) else {}


async def _fetch_releases(client: httpx.AsyncClient, repo: str) -> list[dict]:
    resp = await client.get(
        f"{GITHUB_API_BASE}/repos/{repo}/releases",
        params={"per_page": _RELEASES_PER_PAGE},
    )
    resp.raise_for_status()
    data = resp.json()
    return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []


# --- Per-type checks ----------------------------------------------------------


async def _check_pr(
    client: httpx.AsyncClient,
    item: WatchItemConfig,
    st: WatchItemState,
) -> str:
    """Check one PR watch. Mutates ``st``; returns the rendered line."""
    label = item.label or item.id
    ident = f"PR {item.repo}#{item.number}"

    prev = st.last_state
    if prev in _PR_TERMINAL_STATES:
        # Terminal latched on a previous brief — no API call; keep
        # nagging (quietly) until the operator removes the item.
        return f"- {label}: {ident} — {prev.upper()} {DONE_TAIL}"

    data = await _fetch_pr(client, item.repo, item.number)
    current = "merged" if data.get("merged_at") else str(data.get("state") or "unknown")
    st.last_state = current
    st.last_checked = _now_iso()

    if prev and current != prev:
        # THE FLIP — loud, once, with the operator's action note.
        note = f" — {item.on_flip_note}" if item.on_flip_note else ""
        return f"- {FLIP_MARKER} {label}: {ident} — {current.upper()}{note}"
    if not prev:
        # First-ever check establishes the baseline; no change was
        # OBSERVED, so no flip — even if the PR is already terminal
        # (an already-merged PR added to config latches straight to
        # the done-tail on the next brief).
        return f"- {label}: {ident} — {current.upper()} (baseline)"
    return f"- {label}: {ident} — {current.upper()} (unchanged)"


async def _check_release_mention(
    client: httpx.AsyncClient,
    item: WatchItemConfig,
    st: WatchItemState,
) -> str:
    """Check one release-mention watch. Mutates ``st``; returns the line."""
    label = item.label or item.id

    if st.matched_tag:
        # Terminal latched — no API call.
        return f"- {label}: release {st.matched_tag} matched {DONE_TAIL}"

    # Compile BEFORE any network I/O and label the failure as what it
    # is: a CONFIG error, not an API error (review nit a4 — ``re.error``
    # has the unhelpful class name ``error``, so the generic containment
    # rendered "api error: error: ..." for what is an operator typo in
    # the pattern). Mirrors the unknown-type config-error path.
    try:
        rx = re.compile(item.pattern, re.IGNORECASE)
    except re.error as exc:
        log.warning(
            "brief.watch_check_failed",
            id=item.id or label,
            error=f"invalid pattern regex: {exc}",
            error_type="config_error",
        )
        return (
            f"- {label}: watch unavailable "
            f"(config error: invalid pattern regex: {exc})"
        )
    releases = await _fetch_releases(client, item.repo)

    # Candidates = releases strictly newer than the boundary. GitHub
    # returns newest-first; walk until we hit the boundary tag. The
    # boundary advances to the newest tag every check, so each release
    # is scanned exactly once (a later body-edit on an already-scanned
    # release is deliberately not re-detected).
    boundary = st.last_seen_tag or item.baseline_tag
    candidates: list[dict] = []
    for rel in releases:
        tag = str(rel.get("tag_name") or "")
        if boundary and tag == boundary:
            break
        candidates.append(rel)

    matched: dict | None = None
    for rel in candidates:  # newest-first → first hit is the newest match
        haystack = " ".join((
            str(rel.get("tag_name") or ""),
            str(rel.get("name") or ""),
            str(rel.get("body") or ""),
        ))
        if rx.search(haystack):
            matched = rel
            break

    if releases:
        st.last_seen_tag = str(releases[0].get("tag_name") or st.last_seen_tag)
    st.last_checked = _now_iso()

    if matched is not None:
        tag = str(matched.get("tag_name") or "")
        st.matched_tag = tag
        note = f" — {item.on_flip_note}" if item.on_flip_note else ""
        return (
            f"- {FLIP_MARKER} {label}: release {tag} of {item.repo} "
            f"matches /{item.pattern}/{note}"
        )

    since = boundary or "the beginning"
    return (
        f"- {label}: no matching release in {item.repo} yet "
        f"(watching since {since})"
    )


async def _check_item(
    client: httpx.AsyncClient,
    item: WatchItemConfig,
    st: WatchItemState,
) -> str:
    if item.type == "github_pr":
        return await _check_pr(client, item, st)
    if item.type == "github_release_mention":
        return await _check_release_mention(client, item, st)
    # Unknown type — a config error, surfaced IN THE BRIEF (not just a
    # log line the operator never reads) per intentionally-left-blank.
    log.warning(
        "brief.watch_check_failed",
        id=item.id,
        error=f"unknown watch type '{item.type}'",
        error_type="config_error",
    )
    return (
        f"- {item.label or item.id}: watch unavailable "
        f"(config error: unknown watch type '{item.type}')"
    )


# --- Section entry point -------------------------------------------------------


async def check_and_format_watches(
    watches: list[WatchItemConfig],
    state_path: Path,
) -> str:
    """Run every configured watch and render the section body.

    Returns ``""`` when no watches are configured (the daemon then
    omits the section entirely — absence of the FEATURE is the one
    silence that's allowed; a CONFIGURED watch always yields a line).

    Per-item containment: one watch's failure (API error, bad regex,
    unexpected payload) renders that item's ``watch unavailable`` line
    + a ``brief.watch_check_failed`` warning and the remaining items
    still run. ``asyncio.CancelledError`` derives from BaseException
    (3.8+), so daemon-shutdown cancellation propagates through the
    ``except Exception`` untouched.

    State is saved even when items failed — successful items' progress
    (advanced boundaries, latched flips) survives a partial run; a
    failed item's entry is left exactly as loaded.
    """
    if not watches:
        return ""

    states = load_watch_state(state_path)
    lines: list[str] = []

    async with httpx.AsyncClient(
        timeout=_TIMEOUT_SECONDS,
        headers=_HEADERS,
        follow_redirects=True,
    ) as client:
        for item in watches:
            # Stable per-watch state key — explicit id, else the
            # TYPE-SPECIFIC fallback (review nit a1: the old inline
            # fallback omitted ``pattern``, colliding two id-less
            # release watches on one repo). The loader warns on empty
            # and duplicate resolved keys at config-load time.
            key = item.state_key()
            st = states.get(key) or WatchItemState()
            try:
                line = await _check_item(client, item, st)
            except Exception as exc:  # noqa: BLE001 — per-item containment
                log.warning(
                    "brief.watch_check_failed",
                    id=key,
                    error=str(exc),
                    error_type=exc.__class__.__name__,
                )
                line = (
                    f"- {item.label or key}: watch unavailable "
                    f"(api error: {exc.__class__.__name__}: {exc})"
                )
            states[key] = st
            lines.append(line)

    save_watch_state(state_path, states)
    return "\n".join(lines)
