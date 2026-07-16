"""STAY-C bug-report box watcher (task #4, box half — the SURFACING component).

STAY-C is in no Morning Brief and no BIT, and its clinical sandbox cannot egress, so without
this component a bug report is a SILENT SINK. This runs OUTSIDE the clinical unit, as the
operator, triggered by a systemd ``.path`` unit watching the bug dir, and forwards NEW reports
to Telegram (the existing alert-script pattern: ``TELEGRAM_BOT_TOKEN`` from the operator env).

TWO FORWARDING MODES — operator ruling 2026-07-16, BOTH built from day one:

  * ``locked`` — the FAIL-SAFE DEFAULT. A COUNT + FILENAME ping only, NEVER body content:
    PHI-safe by construction. The operator promotes a report to Forgejo/VERA by reading it
    ON-BOX (a human act), never automatically.
  * ``full`` — full-body forward (RRTS-style), for the all-synthetic era. The go-live gate
    flips the mode back to ``locked``.

The mode is read from ``STAYC_BUG_FORWARD_MODE``. ANY missing / unset / unparseable / unknown
value resolves to ``locked`` — a misconfiguration can NEVER silently escalate to full-body PHI
egress. That fail-safe is the load-bearing property of this component.

State: already-forwarded report ids live in a WATCHER-OWNED file (``$STAYC_BUG_STATE``, default
under the watcher's XDG state dir) — NOT the bug dir, which is group-r-x for the watcher (the
clinical daemon owns it, and it is deliberately not group-writable). Re-runs (the .path unit
fires on every change) never double-send. On a Telegram delivery failure the state is NOT
advanced, so the reports are retried rather than marked-and-lost.

Install is operator-gated (a systemd .path + .service, run as the operator) — see the bundled
``stayc-bug-watcher.{path,service}.template``. This module ships the ARTIFACT; the box install
is a deliberate operator step, like every STAY-C systemd change.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Callable, Iterable

import structlog

log = structlog.get_logger(__name__)

FORWARD_MODE_LOCKED = "locked"
FORWARD_MODE_FULL = "full"

ENV_MODE = "STAYC_BUG_FORWARD_MODE"
ENV_BUG_DIR = "STAYC_BUG_DIR"
ENV_TOKEN = "TELEGRAM_BOT_TOKEN"           # shared operator env (aftermath-sync-alert pattern)
ENV_CHAT_ID = "STAYC_BUG_TELEGRAM_CHAT_ID"
# The forwarded-state file lives in a WATCHER-OWNED location (NOT the bug dir — that dir is
# group-r-x for the watcher, not group-writable; the clinical daemon owns it). Default derives
# under the watcher's XDG state dir so the watcher can always write it. (R3/finding 7c.)
ENV_STATE = "STAYC_BUG_STATE"

_STATE_NAME = ".forwarded.json"
_TELEGRAM_MAX = 3800                        # under Telegram's 4096 hard cap, room for a header
# Per-run cap — a large backlog (e.g. after Telegram was down) must not fan out into hundreds of
# sequential chunks that trip Telegram's ~1 msg/s rate limit (which, post-R2, now HARD-FAILS the
# run). Forward at most this many reports per trigger; the rest drain on subsequent runs. (F6.)
MAX_REPORTS_PER_RUN = 20


class TelegramSendError(Exception):
    """A Telegram send FAILED (HTTP >= 400). Raised so ``run_once`` does NOT mark the reports
    forwarded — state is preserved for retry and the oneshot unit shows FAILED, instead of the
    silent-sink where a revoked token / wrong chat_id is treated as success. (R2/findings 4/6.)"""


def resolve_forward_mode(env: dict[str, str] | None = None) -> str:
    """``full`` ONLY for the exact (case/space-insensitive) string ``"full"``; EVERYTHING else
    — unset, empty, ``"locked"``, a typo, a nonsense value — resolves to ``locked``.

    This is the fail-safe: the DEFAULT direction is the PHI-safe one, so a missing or fat-
    fingered env var degrades to count+filename, never to full-body egress. (Mirror of the
    scribe ``mode`` synthetic-default legal line.)"""
    env = env if env is not None else os.environ
    raw = env.get(ENV_MODE)
    if isinstance(raw, str) and raw.strip().lower() == FORWARD_MODE_FULL:
        return FORWARD_MODE_FULL
    return FORWARD_MODE_LOCKED


def default_state_path(bug_dir: Path) -> Path:
    """The watcher's forwarded-state file when ``$STAYC_BUG_STATE`` is unset. NOT the bug dir
    (that is group-r-x, not group-writable) — the XDG state dir the watcher owns."""
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "stayc-bug-watcher" / _STATE_NAME


def load_forwarded(state_path: Path) -> set[str]:
    """The set of already-forwarded report ids (tolerant: a missing/corrupt state file → empty,
    never a crash — worst case is a re-send, never a lost report)."""
    if not state_path.is_file():
        return set()
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
        return set(data) if isinstance(data, list) else set()
    except (ValueError, OSError):
        return set()


def save_forwarded(state_path: Path, ids: Iterable[str]) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_path.with_name(state_path.name + ".tmp")
    tmp.write_text(json.dumps(sorted(set(ids))), encoding="utf-8")
    os.replace(tmp, state_path)


def scan_new_reports(bug_dir: Path, forwarded: set[str]) -> list[Path]:
    """Top-level ``*.md`` reports (id = stem) not yet forwarded, oldest first (ts-prefixed)."""
    if not bug_dir.is_dir():
        return []
    out = [p for p in bug_dir.iterdir()
           if p.is_file() and p.suffix == ".md" and p.stem not in forwarded]
    return sorted(out, key=lambda p: p.stem)


def build_alert(mode: str, reports: list[Path]) -> str:
    """Build the Telegram message for ``reports``.

    ``locked`` — COUNT + FILENAME(id) ONLY. The report BODY is never read, so no detail /
    context / diagnostic text can leak: PHI-safe by construction.
    ``full`` — the full report body per file (synthetic-era RRTS-style forward)."""
    n = len(reports)
    if mode == FORWARD_MODE_FULL:
        parts = [f"🐞 STAY-C: {n} new bug report(s) [FULL mode]"]
        for p in reports:
            try:
                body = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                body = "(unreadable)"
            parts.append(f"\n──────── {p.stem} ────────\n{body}")
        return "\n".join(parts)
    # locked (default): ids only — never open the file.
    lines = [f"🐞 STAY-C: {n} new bug report(s) — read on-box to triage.",
             "(locked mode: filenames only, no content)"]
    lines.extend(f"• {p.stem}" for p in reports)
    return "\n".join(lines)


def _chunk(text: str, size: int = _TELEGRAM_MAX) -> list[str]:
    """Split into Telegram-sized chunks (a full-mode body can exceed the 4096 cap)."""
    return [text[i:i + size] for i in range(0, len(text), size)] or [""]


def _httpx_sender(token: str, chat_id: str) -> Callable[[str], None]:
    """A real Telegram sender (httpx). Lazy so tests never need httpx or the network."""
    import httpx

    url = f"https://api.telegram.org/bot{token}/sendMessage"

    def _send(text: str) -> None:
        for chunk in _chunk(text):
            resp = httpx.post(url, json={"chat_id": chat_id, "text": chunk}, timeout=15.0)
            if resp.status_code >= 400:
                # RAISE — do NOT swallow. A revoked token (401) / wrong chat_id (400) / rate
                # limit (429) is a real DELIVERY FAILURE; swallowing it and returning would let
                # run_once mark the reports forwarded → permanent silent sink. (R2.)
                raise TelegramSendError(f"Telegram HTTP {resp.status_code}: {resp.text[-200:]}")

    return _send


def run_once(bug_dir: Path, *, mode: str, sender: Callable[[str], None],
             state_path: Path) -> dict:
    """Scan for NEW reports (capped at :data:`MAX_REPORTS_PER_RUN`), forward via ``sender``, and
    ONLY THEN mark them forwarded. If ``sender`` raises (Telegram HTTP error OR a transport
    error), the exception propagates and state is NOT saved — the reports stay unforwarded and
    are retried, and the caller surfaces the failure (R2). Returns a summary on success.

    ILB — emits an explicit signal on EVERY run, including the no-new-reports case (the .path
    unit fires on any dir change; "ran, nothing new" must be distinguishable from broken)."""
    forwarded = load_forwarded(state_path)
    new = scan_new_reports(bug_dir, forwarded)
    if not new:
        log.info("scribe.bug_watcher.no_new_reports", mode=mode)   # ran, nothing to forward
        return {"forwarded": 0, "mode": mode}
    capped = new[:MAX_REPORTS_PER_RUN]                              # bound the per-run fan-out (F6)
    overflow = len(new) - len(capped)
    text = build_alert(mode, capped)
    if overflow > 0:
        text += f"\n(+{overflow} more this run — will send on the next trigger.)"
    sender(text)                                                   # raises → NO save_forwarded below
    ids = {p.stem for p in capped}
    save_forwarded(state_path, forwarded | ids)
    # PHI-safe log — count + mode only, NEVER the report bodies.
    log.info("scribe.bug_watcher.forwarded", count=len(capped), mode=mode, overflow=overflow)
    return {"forwarded": len(capped), "mode": mode, "overflow": overflow, "ids": sorted(ids)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="STAY-C bug-report box watcher (surfacing).")
    parser.add_argument("--bug-dir", default=os.environ.get(ENV_BUG_DIR),
                        help=f"The bug-report dir (default: ${ENV_BUG_DIR}).")
    args = parser.parse_args(argv)

    if not args.bug_dir:
        print(f"error: no bug dir (set ${ENV_BUG_DIR} or pass --bug-dir).", file=sys.stderr)
        return 2
    bug_dir = Path(args.bug_dir).expanduser()
    state_path = (Path(os.environ[ENV_STATE]).expanduser() if os.environ.get(ENV_STATE)
                  else default_state_path(bug_dir))

    mode = resolve_forward_mode(os.environ)
    token = os.environ.get(ENV_TOKEN, "")
    chat_id = os.environ.get(ENV_CHAT_ID, "")
    if not token or not chat_id:
        # Fail LOUD — a watcher that cannot reach Telegram is a silent sink (the exact failure
        # this component exists to prevent). Non-zero so the operator sees the unit fail.
        print(f"error: ${ENV_TOKEN} and ${ENV_CHAT_ID} must both be set to forward.",
              file=sys.stderr)
        return 2

    try:
        summary = run_once(bug_dir, mode=mode, sender=_httpx_sender(token, chat_id),
                           state_path=state_path)
    except Exception as e:      # noqa: BLE001 — ANY failure (send, permission, transport) must
        # fail the unit LOUDLY with state preserved, never a silent success. (R2/R3.)
        print(f"stayc-bug-watcher: FAILED to forward — {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    print(f"stayc-bug-watcher: forwarded {summary['forwarded']} report(s) in {mode} mode.")
    return 0


if __name__ == "__main__":       # pragma: no cover — operator/systemd entrypoint
    raise SystemExit(main())
