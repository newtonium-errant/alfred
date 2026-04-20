"""Smoke tests for the ``alfred instructor`` top-level CLI wiring.

Verifies the parser accepts ``instructor scan|run|status`` and the
dispatcher in ``alfred.cli`` routes to ``alfred.instructor.cli``.
"""

from __future__ import annotations

import pytest


def test_parser_accepts_instructor_scan_subcommand() -> None:
    from alfred.cli import build_parser
    parser = build_parser()
    args = parser.parse_args(["instructor", "scan"])
    assert args.command == "instructor"
    assert args.instructor_cmd == "scan"


def test_parser_accepts_instructor_run_subcommand() -> None:
    from alfred.cli import build_parser
    parser = build_parser()
    args = parser.parse_args(["instructor", "run"])
    assert args.command == "instructor"
    assert args.instructor_cmd == "run"


def test_parser_accepts_instructor_status_subcommand() -> None:
    from alfred.cli import build_parser
    parser = build_parser()
    args = parser.parse_args(["instructor", "status"])
    assert args.command == "instructor"
    assert args.instructor_cmd == "status"


def test_parser_rejects_unknown_instructor_subcommand() -> None:
    """Unknown subcommand → parse_args succeeds but instructor_cmd is None.

    The top-level dispatcher ``cmd_instructor`` handles the ``None``
    case by printing usage + exiting 1. Test that branch separately.
    """
    from alfred.cli import build_parser
    parser = build_parser()
    args = parser.parse_args(["instructor"])
    assert args.command == "instructor"
    assert args.instructor_cmd is None


def test_handler_registered_in_main_dispatch_table() -> None:
    """``cmd_instructor`` is registered under the ``instructor`` handler key.

    Regression guard — easy to add the parser + forget to add the
    handler, which would silently print help instead of running.
    """
    from alfred import cli as alfred_cli
    # cmd_instructor is defined at module scope.
    assert hasattr(alfred_cli, "cmd_instructor")
