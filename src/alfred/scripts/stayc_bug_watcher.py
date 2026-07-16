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

State: already-forwarded report ids live in ``<bug_dir>/.forwarded.json`` (a dotfile, so
``scribe.bug.list_bugs`` — which only reads ``*.md`` — never treats it as a report). Re-runs
(the .path unit fires on every change) never double-send.

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

_STATE_NAME = ".forwarded.json"
_TELEGRAM_MAX = 3800                        # under Telegram's 4096 hard cap, room for a header


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


def _state_path(bug_dir: Path) -> Path:
    return bug_dir / _STATE_NAME


def load_forwarded(bug_dir: Path) -> set[str]:
    """The set of already-forwarded report ids (tolerant: a missing/corrupt state file → empty,
    never a crash — worst case is a re-send, never a lost report)."""
    p = _state_path(bug_dir)
    if not p.is_file():
        return set()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return set(data) if isinstance(data, list) else set()
    except (ValueError, OSError):
        return set()


def save_forwarded(bug_dir: Path, ids: Iterable[str]) -> None:
    tmp = _state_path(bug_dir).with_name(_STATE_NAME + ".tmp")
    tmp.write_text(json.dumps(sorted(set(ids))), encoding="utf-8")
    os.replace(tmp, _state_path(bug_dir))


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
                log.warning("scribe.bug_watcher.telegram_error",
                            status=resp.status_code, body_tail=resp.text[-200:])

    return _send


def run_once(bug_dir: Path, *, mode: str, sender: Callable[[str], None]) -> dict:
    """Scan for NEW reports, forward via ``sender``, mark them forwarded. Returns a summary.

    ILB — emits an explicit signal on EVERY run, including the no-new-reports case (the .path
    unit fires on any dir change; "ran, nothing new" must be distinguishable from broken)."""
    forwarded = load_forwarded(bug_dir)
    new = scan_new_reports(bug_dir, forwarded)
    if not new:
        log.info("scribe.bug_watcher.no_new_reports", mode=mode)   # ran, nothing to forward
        return {"forwarded": 0, "mode": mode}
    text = build_alert(mode, new)
    sender(text)
    ids = {p.stem for p in new}
    save_forwarded(bug_dir, forwarded | ids)
    # PHI-safe log — count + mode only, NEVER the report bodies.
    log.info("scribe.bug_watcher.forwarded", count=len(new), mode=mode)
    return {"forwarded": len(new), "mode": mode, "ids": sorted(ids)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="STAY-C bug-report box watcher (surfacing).")
    parser.add_argument("--bug-dir", default=os.environ.get(ENV_BUG_DIR),
                        help=f"The bug-report dir (default: ${ENV_BUG_DIR}).")
    args = parser.parse_args(argv)

    if not args.bug_dir:
        print(f"error: no bug dir (set ${ENV_BUG_DIR} or pass --bug-dir).", file=sys.stderr)
        return 2
    bug_dir = Path(args.bug_dir).expanduser()

    mode = resolve_forward_mode(os.environ)
    token = os.environ.get(ENV_TOKEN, "")
    chat_id = os.environ.get(ENV_CHAT_ID, "")
    if not token or not chat_id:
        # Fail LOUD — a watcher that cannot reach Telegram is a silent sink (the exact failure
        # this component exists to prevent). Non-zero so the operator sees the unit fail.
        print(f"error: ${ENV_TOKEN} and ${ENV_CHAT_ID} must both be set to forward.",
              file=sys.stderr)
        return 2

    summary = run_once(bug_dir, mode=mode, sender=_httpx_sender(token, chat_id))
    print(f"stayc-bug-watcher: forwarded {summary['forwarded']} report(s) in {mode} mode.")
    return 0


if __name__ == "__main__":       # pragma: no cover — operator/systemd entrypoint
    raise SystemExit(main())
