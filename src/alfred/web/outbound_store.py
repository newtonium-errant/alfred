"""Latest-outbound spool — daemon-rendered artifacts the web surface reads.

The morning brief and the Daily Sync are PUSH artifacts (vault write +
Telegram). Parity item #30 gives the PWA a READ-ON-OPEN surface for the
same content: each producer daemon best-effort spools its latest rendered
markdown here, and the web route ``GET /web/outbound/{kind}/latest``
(:mod:`alfred.web.routes_brief`) serves it back.

Layout, under the instance's data dir::

    <data_dir>/web_outbound/<kind>.md      — the rendered markdown, byte-exact
    <data_dir>/web_outbound/<kind>.json    — {"date": "<YYYY-MM-DD>"} sidecar

Design constraints (deliberate):

* **Tiny + dependency-free** — stdlib only, importable from the brief and
  daily_sync daemons without dragging web/aiohttp deps into them (the
  daemons import lazily inside their best-effort try blocks anyway).
* **Atomic writes** — ``.tmp`` then :func:`os.replace`, so a concurrent
  reader never observes a torn file. The markdown is written FIRST and
  the sidecar LAST, so a crash between the two leaves the previous
  coherent pair's sidecar semantics intact at worst for one read.
* **Schema-tolerant reads that never raise** — a missing spool, a
  missing/corrupt sidecar, or an unreadable payload all read as ``None``
  ("nothing spooled yet"), which the route surfaces as the
  intentionally-left-blank empty 200 — never a 404, never a 500.

:func:`write_latest` DOES raise on I/O failure (e.g. unwritable dir) —
the producer daemons wrap it in their own swallow-and-log best-effort
blocks (mirroring their Telegram-push swallow), so the contract stays
"a spool failure must never break the brief / the fire" at the caller.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

# Subdirectory of the data dir the spool lives in.
OUTBOUND_SUBDIR = "web_outbound"


def _kind_paths(data_dir: str | Path, kind: str) -> tuple[Path, Path]:
    base = Path(data_dir) / OUTBOUND_SUBDIR
    return base / f"{kind}.md", base / f"{kind}.json"


def _atomic_write(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` via a same-directory tmp + os.replace."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def write_latest(
    data_dir: str | Path, kind: str, date_iso: str, markdown: str,
) -> None:
    """Atomically spool ``markdown`` + a ``{"date": date_iso}`` sidecar.

    Raises ``OSError`` on I/O failure — callers are the daemons'
    best-effort blocks, which swallow + log (see module docstring).
    """
    md_path, sidecar_path = _kind_paths(data_dir, kind)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(md_path, markdown)
    _atomic_write(
        sidecar_path, json.dumps({"date": date_iso}, separators=(",", ":"))
    )


def read_latest(data_dir: str | Path | None, kind: str) -> dict[str, Any] | None:
    """The latest spooled artifact as ``{"date", "markdown"}``, or ``None``.

    Never raises: absent spool, missing/corrupt sidecar, or an unreadable
    payload all → ``None`` (treated as "nothing spooled yet" — the
    route's intentionally-left-blank empty state).
    """
    if data_dir is None:
        return None
    md_path, sidecar_path = _kind_paths(data_dir, kind)
    try:
        markdown = md_path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        raw = json.loads(sidecar_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — corrupt/missing sidecar → absent
        return None
    date = raw.get("date") if isinstance(raw, dict) else None
    if not isinstance(date, str) or not date:
        return None
    return {"date": date, "markdown": markdown}
