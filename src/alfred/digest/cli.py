"""Subcommand handlers for ``alfred digest``.

- ``alfred digest write   [--window-days N]`` — write the digest now.
- ``alfred digest preview [--window-days N]`` — print to stdout, no file.

Both subcommands resolve project paths from ``kalle.projects`` (with
hardcoded defaults). The scheduler invokes ``digest write`` weekly
when ``digest.enabled`` is true.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import load_from_unified
from .writer import resolve_repo_paths, write_digest, build_payload, render


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def cmd_write(raw: dict[str, Any], args: argparse.Namespace) -> int:
    config = load_from_unified(raw)
    project_paths: dict[str, Path] = resolve_repo_paths(raw)
    output_dir = Path(args.output_dir or config.output_dir)
    window_days = int(args.window_days or config.window_days)
    today = _now_utc()
    out_path, body, payload = write_digest(
        output_dir=output_dir,
        project_paths=project_paths,
        today=today,
        window_days=window_days,
        synthesis_vault=(
            Path(config.synthesis_vault) if config.synthesis_vault else None
        ),
        synthesis_top_n=config.synthesis_top_n,
        synthesis_weights=config.synthesis_weights or None,
    )
    print(json.dumps({
        "ok": True,
        "path": str(out_path),
        "filename": out_path.name,
        "window_days": window_days,
        "decisions_count": len(payload.decisions),
        "promotions_count": len(payload.promotions),
        "open_questions_count": len(payload.open_questions),
        "recurrences_count": len(payload.recurrences),
        "cross_arc_patterns_count": len(payload.cross_arc_patterns),
        "byte_count": len(body.encode("utf-8")),
    }, default=str))
    return 0


def cmd_preview(raw: dict[str, Any], args: argparse.Namespace) -> int:
    config = load_from_unified(raw)
    project_paths: dict[str, Path] = resolve_repo_paths(raw)
    window_days = int(args.window_days or config.window_days)
    today = _now_utc()
    payload = build_payload(
        project_paths=project_paths,
        today=today,
        window_days=window_days,
        synthesis_vault=(
            Path(config.synthesis_vault) if config.synthesis_vault else None
        ),
        synthesis_top_n=config.synthesis_top_n,
        synthesis_weights=config.synthesis_weights or None,
    )
    print(render(payload), end="")
    return 0


def build_subparser(subparsers: argparse._SubParsersAction) -> None:
    digest_p = subparsers.add_parser(
        "digest",
        help="KAL-LE cross-arc weekly synthesis",
    )
    sub = digest_p.add_subparsers(dest="digest_cmd")

    write_p = sub.add_parser("write", help="Write the digest now")
    write_p.add_argument(
        "--window-days", type=int, default=0,
        help="Override config window_days (0 = use config default)",
    )
    write_p.add_argument(
        "--output-dir", default=None,
        help="Override config output_dir (default: aftermath-lab/digests)",
    )

    preview_p = sub.add_parser(
        "preview",
        help="Print the digest to stdout without writing the file",
    )
    preview_p.add_argument(
        "--window-days", type=int, default=0,
        help="Override config window_days (0 = use config default)",
    )


def dispatch(raw: dict[str, Any], args: argparse.Namespace) -> int:
    sub = getattr(args, "digest_cmd", None)
    if sub == "write":
        return cmd_write(raw, args)
    if sub == "preview":
        return cmd_preview(raw, args)
    print("Usage: alfred digest {write|preview} [--window-days N]", file=sys.stderr)
    return 1
