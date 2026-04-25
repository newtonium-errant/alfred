"""Subcommand handlers for ``alfred reviews``.

JSON-by-default output to match ``alfred vault`` — the bash_exec gate
allows KAL-LE to invoke this CLI directly, and a structured contract
keeps the shell-tooling integration robust.

Subcommands:
- ``alfred reviews write --project <name> --topic <topic> --body <body|->``
- ``alfred reviews list  --project <name> [--status open|addressed|all]``
- ``alfred reviews read  --project <name> --file <filename>``
- ``alfred reviews mark-addressed --project <name> --file <filename>``
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .config import resolve_project_path
from .store import (
    ReviewsError,
    list_reviews,
    mark_addressed,
    read_review,
    write_review,
)


def _output(data: dict[str, Any]) -> None:
    print(json.dumps(data, default=str))


def _error(msg: str, code: int = 1) -> int:
    print(json.dumps({"error": msg}, default=str))
    return code


def _read_body(body_arg: str) -> str:
    if body_arg == "-":
        return sys.stdin.read()
    return body_arg


def cmd_write(raw: dict[str, Any], args: argparse.Namespace) -> int:
    try:
        vault = resolve_project_path(raw, args.project)
    except KeyError as exc:
        return _error(str(exc), code=2)
    body = _read_body(args.body)
    if not args.topic.strip():
        return _error("--topic must be non-empty", code=2)
    try:
        record = write_review(
            vault, project=args.project, topic=args.topic, body=body,
        )
    except ReviewsError as exc:
        return _error(str(exc))
    _output({
        "ok": True,
        "project": args.project,
        "filename": record.filename,
        "path": str(record.abs_path),
        "frontmatter": record.frontmatter,
    })
    return 0


def cmd_list(raw: dict[str, Any], args: argparse.Namespace) -> int:
    try:
        vault = resolve_project_path(raw, args.project)
    except KeyError as exc:
        return _error(str(exc), code=2)
    try:
        records = list_reviews(vault, status=args.status)
    except ReviewsError as exc:
        return _error(str(exc), code=2)
    _output({
        "project": args.project,
        "status_filter": args.status,
        "count": len(records),
        "reviews": [r.to_dict() for r in records],
    })
    return 0


def cmd_read(raw: dict[str, Any], args: argparse.Namespace) -> int:
    try:
        vault = resolve_project_path(raw, args.project)
    except KeyError as exc:
        return _error(str(exc), code=2)
    try:
        record = read_review(vault, filename=args.file)
    except ReviewsError as exc:
        return _error(str(exc))
    _output(record.to_dict())
    return 0


def cmd_mark_addressed(raw: dict[str, Any], args: argparse.Namespace) -> int:
    try:
        vault = resolve_project_path(raw, args.project)
    except KeyError as exc:
        return _error(str(exc), code=2)
    try:
        record = mark_addressed(vault, filename=args.file)
    except ReviewsError as exc:
        return _error(str(exc))
    _output({
        "ok": True,
        "project": args.project,
        "filename": record.filename,
        "frontmatter": record.frontmatter,
    })
    return 0


def build_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register ``alfred reviews ...`` subcommand tree."""
    reviews_p = subparsers.add_parser(
        "reviews",
        help="Per-project KAL-LE review files",
    )
    sub = reviews_p.add_subparsers(dest="reviews_cmd")

    write_p = sub.add_parser("write", help="Create a new open review")
    write_p.add_argument("--project", required=True, help="Project name")
    write_p.add_argument("--topic", required=True, help="One-line topic")
    write_p.add_argument(
        "--body", required=True,
        help="Review body markdown (use '-' to read from stdin)",
    )

    list_p = sub.add_parser("list", help="List KAL-LE-authored reviews")
    list_p.add_argument("--project", required=True, help="Project name")
    list_p.add_argument(
        "--status", default="open",
        choices=["open", "addressed", "all"],
        help="Status filter (default: open)",
    )

    read_p = sub.add_parser("read", help="Read one KAL-LE-authored review")
    read_p.add_argument("--project", required=True, help="Project name")
    read_p.add_argument("--file", required=True, help="Filename in reviews dir")

    mark_p = sub.add_parser(
        "mark-addressed",
        help="Flip a KAL-LE-authored review's status to addressed",
    )
    mark_p.add_argument("--project", required=True, help="Project name")
    mark_p.add_argument("--file", required=True, help="Filename in reviews dir")


def dispatch(raw: dict[str, Any], args: argparse.Namespace) -> int:
    sub = getattr(args, "reviews_cmd", None)
    if sub == "write":
        return cmd_write(raw, args)
    if sub == "list":
        return cmd_list(raw, args)
    if sub == "read":
        return cmd_read(raw, args)
    if sub == "mark-addressed":
        return cmd_mark_addressed(raw, args)
    print(
        "Usage: alfred reviews {write|list|read|mark-addressed} --project <name>",
        file=sys.stderr,
    )
    return 1
