"""STAY-C bug-report box watcher (task #4, box half — the SURFACING component).

STAY-C is in no Morning Brief and no BIT, and its clinical sandbox cannot egress, so without
this component a bug report is a SILENT SINK. This runs OUTSIDE the clinical unit, as the
operator, triggered by a systemd ``.path`` unit watching the bug dir, and SURFACES reports by
writing a **Salem brief-relay spool file** (STAY-C uses NO Telegram — standing operator rule
2026-07-16). Salem's Morning Brief reads that file; the watcher's contract ends at the file.

TWO MODES — operator ruling 2026-07-16, BOTH built from day one:

  * ``locked`` — the FAIL-SAFE DEFAULT. The spool carries a COUNT + OPAQUE report IDS only,
    NEVER body content: PHI-safe by construction (the ids are opaque — ``ts-<hex>``, no summary
    text). The operator promotes a report by reading it ON-BOX (a human act), never automatically.
  * ``full`` — count + ids + summary lines + full bodies (the all-synthetic era). The go-live
    gate flips the mode back to ``locked``.

The mode is read from ``STAYC_BUG_FORWARD_MODE``. ANY missing / unset / unparseable / unknown
value resolves to ``locked`` — a misconfiguration can NEVER silently escalate to full-body
egress. That fail-safe is the load-bearing property of this component.

THE SPOOL FILE (a CROSS-COMPONENT CONTRACT — Salem reads it):
  * A WHOLE-FILE snapshot of the currently-UNRESOLVED reports, REGENERATED in full each run
    (idempotent — same unresolved set + mode → same content bar the timestamp; no append drift).
  * Written ATOMICALLY (tmp → os.replace) so Salem never reads a half-written file.
  * A ``generated_at`` timestamp + ``unresolved`` count + ``new_since_last`` count header.
  * Written OUTSIDE the STAY-C trust zone at ``$STAYC_BUG_RELAY_PATH`` (watcher-writable,
    Salem-readable — operator sets it at install).

FAIL-LOUD (R2): a spool WRITE failure (unwritable path / permission) raises and exits non-zero
WITHOUT advancing state, so the unit visibly FAILS and the next trigger retries — never the
silent sink where an undeliverable relay is treated as success.

State: the ids that were in the LAST written snapshot live in a WATCHER-OWNED file
(``$STAYC_BUG_STATE``, default under the watcher's XDG state dir) — NOT the bug dir (that is
group-r-x for the watcher; the clinical daemon owns it, deliberately not group-writable). It
is used only to compute the ``new_since_last`` signal; it is advanced ONLY after a successful
write.

Install is operator-gated (a systemd .path + .service, run as the operator) — see the bundled
``stayc-bug-watcher.{path,service}.template``. This module ships the ARTIFACT.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Callable, Iterable

import structlog

log = structlog.get_logger(__name__)

FORWARD_MODE_LOCKED = "locked"
FORWARD_MODE_FULL = "full"

ENV_MODE = "STAYC_BUG_FORWARD_MODE"
ENV_BUG_DIR = "STAYC_BUG_DIR"
# The Salem-readable relay spool — OUTSIDE the STAY-C trust zone (watcher writes, Salem reads).
ENV_RELAY_PATH = "STAYC_BUG_RELAY_PATH"
# The watcher's OWN last-snapshot-ids file (NOT the group-r-x bug dir). Default: XDG state dir.
ENV_STATE = "STAYC_BUG_STATE"

_STATE_NAME = ".snapshot_ids.json"
_SUMMARY_RE = re.compile(r"^summary:\s*(.*)$", re.MULTILINE)   # reads the frontmatter summary


class RelayWriteError(Exception):
    """The relay spool WRITE FAILED (unwritable path / permission). Raised so ``run_once`` does
    NOT advance state — the reports are re-surfaced next run and the oneshot unit shows FAILED,
    instead of the silent-sink where an undeliverable relay is treated as success. (R2.)"""


def resolve_forward_mode(env: dict[str, str] | None = None) -> str:
    """``full`` ONLY for the exact (case/space-insensitive) string ``"full"``; EVERYTHING else
    — unset, empty, ``"locked"``, a typo, a nonsense value — resolves to ``locked``.

    This is the fail-safe: the DEFAULT direction is the PHI-safe one, so a missing or fat-
    fingered env var degrades to count+ids, never to full-body egress. (Mirror of the scribe
    ``mode`` synthetic-default legal line.)"""
    env = env if env is not None else os.environ
    raw = env.get(ENV_MODE)
    if isinstance(raw, str) and raw.strip().lower() == FORWARD_MODE_FULL:
        return FORWARD_MODE_FULL
    return FORWARD_MODE_LOCKED


def default_state_path(bug_dir: Path) -> Path:
    """The watcher's last-snapshot-ids file when ``$STAYC_BUG_STATE`` is unset. NOT the bug dir
    (that is group-r-x, not group-writable) — the XDG state dir the watcher owns."""
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "stayc-bug-watcher" / _STATE_NAME


def load_forwarded(state_path: Path) -> set[str]:
    """The ids that were in the LAST written snapshot (tolerant: missing/corrupt → empty, never
    a crash — worst case is a report re-counted as 'new' once, never lost)."""
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


def list_unresolved_reports(bug_dir: Path) -> list[Path]:
    """The currently-UNRESOLVED reports — top-level ``*.md`` (resolved/ is a subdir, excluded),
    oldest first (ts-prefixed id)."""
    if not bug_dir.is_dir():
        return []
    out = [p for p in bug_dir.iterdir() if p.is_file() and p.suffix == ".md"]
    return sorted(out, key=lambda p: p.stem)


def _summary_of(path: Path) -> str:
    """The report's frontmatter ``summary:`` line (best-effort; '' on any read error). Only
    called in FULL mode — locked mode NEVER opens a report file."""
    try:
        m = _SUMMARY_RE.search(path.read_text(encoding="utf-8", errors="replace"))
        return m.group(1).strip() if m else ""
    except OSError:
        return ""


def build_snapshot(mode: str, reports: list[Path], *, generated_at: str, new_count: int) -> str:
    """The WHOLE-FILE spool snapshot for the CURRENT unresolved ``reports``.

    ``locked`` — count + OPAQUE ids only; the report bodies are NEVER opened, so no
    summary/detail/context can leak: PHI-safe by construction.
    ``full`` — count + ids + a summary line + the full body per report (synthetic-era)."""
    header = (
        "# STAY-C bug reports — relay snapshot\n"
        f"generated_at: {generated_at}\n"
        f"mode: {mode}\n"
        f"unresolved: {len(reports)}\n"
        f"new_since_last: {new_count}\n"
    )
    if not reports:
        return header + "\n_(no unresolved bug reports)_\n"     # ILB — an explicit empty snapshot

    if mode == FORWARD_MODE_FULL:
        parts = [header, "\n## Reports (full)\n"]
        for p in reports:
            summary = _summary_of(p)
            try:
                body = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                body = "(unreadable)"
            parts.append(f"\n──────── {p.stem} — {summary} ────────\n{body}")
        return "".join(parts)
    # locked (default) — ids ONLY, files never opened.
    lines = [header, "\n## Reports (locked — ids only, read on-box to triage)\n"]
    lines.extend(f"- {p.stem}\n" for p in reports)
    return "".join(lines)


def _relay_writer(relay_path: Path) -> Callable[[str], None]:
    """An ATOMIC spool writer (tmp → os.replace) so Salem never reads a half-written file. A
    write failure (unwritable path / permission) raises :class:`RelayWriteError` — fail-loud."""
    def _write(text: str) -> None:
        try:
            relay_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = relay_path.with_name(relay_path.name + ".tmp")
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, relay_path)
        except OSError as e:
            raise RelayWriteError(f"cannot write relay spool {relay_path}: {e}") from e

    return _write


def run_once(bug_dir: Path, *, mode: str, writer: Callable[[str], None],
             state_path: Path) -> dict:
    """Regenerate the WHOLE spool from the CURRENT unresolved reports and write it via
    ``writer``, THEN advance the last-snapshot-ids state. If ``writer`` raises the exception
    propagates and state is NOT advanced — the reports are re-surfaced next run and the caller
    surfaces the failure (R2). Idempotent: same unresolved set + mode → same content (bar ts).

    ILB — writes a snapshot (and logs) on EVERY run, including the zero-unresolved case (the
    .path unit fires on any dir change; "0 open bugs" must be distinguishable from a stale file)."""
    prev = load_forwarded(state_path)
    reports = list_unresolved_reports(bug_dir)
    ids = {p.stem for p in reports}
    new = sorted(ids - prev)
    text = build_snapshot(mode, reports, generated_at=_now_iso(), new_count=len(new))
    writer(text)                                                   # raises → NO state advance below
    save_forwarded(state_path, ids)                                # forwarded = ids in THIS snapshot
    # PHI-safe log — counts + mode only, NEVER report bodies.
    log.info("scribe.bug_watcher.relayed", unresolved=len(reports), new=len(new), mode=mode)
    return {"unresolved": len(reports), "new": len(new), "mode": mode}


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="STAY-C bug-report box watcher (surfacing).")
    parser.add_argument("--bug-dir", default=os.environ.get(ENV_BUG_DIR),
                        help=f"The bug-report dir (default: ${ENV_BUG_DIR}).")
    args = parser.parse_args(argv)

    if not args.bug_dir:
        print(f"error: no bug dir (set ${ENV_BUG_DIR} or pass --bug-dir).", file=sys.stderr)
        return 2
    bug_dir = Path(args.bug_dir).expanduser()

    relay = os.environ.get(ENV_RELAY_PATH, "")
    if not relay:
        # Fail LOUD — a watcher with nowhere to write the relay is a silent sink (the exact
        # failure this component exists to prevent). Non-zero so the operator sees the unit fail.
        print(f"error: ${ENV_RELAY_PATH} must be set (the Salem-readable relay spool path).",
              file=sys.stderr)
        return 2
    relay_path = Path(relay).expanduser()
    state_path = (Path(os.environ[ENV_STATE]).expanduser() if os.environ.get(ENV_STATE)
                  else default_state_path(bug_dir))
    mode = resolve_forward_mode(os.environ)

    try:
        summary = run_once(bug_dir, mode=mode, writer=_relay_writer(relay_path),
                           state_path=state_path)
    except Exception as e:      # noqa: BLE001 — ANY failure (write, permission) must fail the
        # unit LOUDLY with state preserved, never a silent success. (R2/R3.)
        print(f"stayc-bug-watcher: FAILED to write relay — {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    print(f"stayc-bug-watcher: relayed {summary['unresolved']} unresolved "
          f"({summary['new']} new) in {mode} mode.")
    return 0


if __name__ == "__main__":       # pragma: no cover — operator/systemd entrypoint
    raise SystemExit(main())
