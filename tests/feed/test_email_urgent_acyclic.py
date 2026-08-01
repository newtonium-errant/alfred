"""#27 slice 1 — the curator/email_classifier → ``alfred.feed`` edge is acyclic.

The classifier now imports ``alfred.feed`` (FeedItem/FeedStore/FeedEmitHandle) to
emit ``email_urgent`` items at classify time. That edge is only safe if
``alfred.feed`` stays a LEAF — it must never import curator, email_classifier,
daily_sync, brief, mail, transport, or telegram back, or the dependency graph
gains a cycle (and the leaf-store separability the feed relies on breaks).

This AST-scans every module in ``alfred.feed`` so the property can't silently
regress — a stray ``from alfred.<non-leaf> import ...`` (e.g. someone reaching
into daily_sync for the sender sentinel instead of keeping feed pure) reddens
here rather than at import time in production.
"""

from __future__ import annotations

import ast
from pathlib import Path

# This test lives at tests/feed/ (two levels under the repo root).
_PKG = Path(__file__).resolve().parents[2] / "src" / "alfred" / "feed"

# The non-leaf platform packages feed must never import (the back-edge set).
_FORBIDDEN = {
    "curator", "email_classifier", "daily_sync", "brief", "mail",
    "transport", "telegram",
}


def _modules() -> list[Path]:
    return sorted(_PKG.glob("*.py"))


def _forbidden_import(mod: str) -> bool:
    parts = mod.split(".")
    return len(parts) >= 2 and parts[0] == "alfred" and parts[1] in _FORBIDDEN


def test_feed_package_has_expected_modules() -> None:
    names = {p.name for p in _modules()}
    assert {"__init__.py", "model.py", "store.py", "config.py", "belt.py", "emit.py"} <= names


def test_feed_never_imports_a_non_leaf_platform_package() -> None:
    offenders: list[str] = []
    for path in _modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if _forbidden_import(alias.name):
                        offenders.append(f"{path.name}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                # level>0 is a relative import inside the package — fine.
                if node.level == 0 and _forbidden_import(node.module or ""):
                    offenders.append(f"{path.name}: from {node.module} import ...")
    assert offenders == [], f"alfred.feed must stay a leaf — back-edges: {offenders}"
