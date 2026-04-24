"""``alfred audit ...`` subcommand handlers.

The c3 ``infer-marker`` sweep promotes pre-existing ``_source:`` soft
attributions on calibration entries into the structured BEGIN_INFERRED
+ ``attribution_audit`` contract. Defaults to dry-run; use ``--apply``
to write changes.

The CLI prints a per-candidate plan in dry-run mode so Andrew can see
exactly what the sweep would touch before authorising a write.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

from .sweep import sweep_paths


def _vault_path_from_config(config_path: str | Path = "config.yaml") -> Path:
    """Read the configured vault path from ``config.yaml``.

    Falls back to ``./vault`` when the config can't be loaded — the
    same fallback ``alfred vault`` uses elsewhere. Tests pass an
    explicit ``--vault-path`` so this never fires in CI.
    """
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return Path("./vault")
    vault = raw.get("vault", {}) or {}
    p = vault.get("path") or "./vault"
    return Path(p)


def _gather_default_paths(vault_path: Path) -> list[str]:
    """Default scope: every ``person/*.md`` under the vault.

    The c3 v1 scope is intentionally tight — calibration blocks live
    on person records, and that's where the soft ``_source:``
    annotations carry the most weight.
    """
    person_dir = vault_path / "person"
    if not person_dir.is_dir():
        return []
    return sorted(
        f"person/{p.name}" for p in person_dir.glob("*.md")
    )


def cmd_infer_marker(args: argparse.Namespace) -> int:
    """Handler for ``alfred audit infer-marker``.

    Returns the process exit code (0 on success, 1 when any errors
    were recorded).
    """
    vault_path = (
        Path(args.vault_path) if args.vault_path else _vault_path_from_config()
    )
    if not vault_path.exists():
        print(f"vault path not found: {vault_path}", file=sys.stderr)
        return 1

    if args.paths:
        rel_paths = list(args.paths)
    else:
        rel_paths = _gather_default_paths(vault_path)

    if not rel_paths:
        print("No records to scan (default scope: vault/person/*.md).")
        return 0

    apply = bool(args.apply)
    print(
        f"{'APPLY' if apply else 'DRY-RUN'}: scanning {len(rel_paths)} record(s) "
        f"under {vault_path}"
    )
    result = sweep_paths(vault_path, rel_paths, apply=apply)

    # Group candidates by record for a readable plan.
    by_record: dict[str, list] = {}
    for c in result.candidates:
        by_record.setdefault(c.record_path, []).append(c)

    for rec, cands in by_record.items():
        print(f"\n  {rec} — {len(cands)} candidate(s):")
        for c in cands:
            preview = c.bullet_text
            if len(preview) > 80:
                preview = preview[:77] + "..."
            print(
                f"    L{c.line_number} [{c.section_title}] "
                f"agent={c.agent} src={c.source}"
            )
            print(f"        \"{preview}\"")

    if result.errors:
        print("\nErrors:")
        for path, msg in result.errors:
            print(f"  {path}: {msg}")

    print(f"\n{result.summary_line()}")
    if not apply:
        print("Re-run with --apply to write the markers.")
    return 1 if result.errors else 0


def build_parser(parent_subparsers: argparse._SubParsersAction) -> None:
    """Register ``alfred audit ...`` with the top-level CLI parser.

    Called from :mod:`alfred.cli` during ``build_parser``.
    """
    audit_p = parent_subparsers.add_parser(
        "audit",
        help="Vault audit / attribution-marker sweeps",
    )
    audit_sub = audit_p.add_subparsers(dest="audit_cmd")

    im = audit_sub.add_parser(
        "infer-marker",
        help=(
            "Promote pre-existing _source: annotations on calibration "
            "entries into BEGIN_INFERRED + attribution_audit markers. "
            "Defaults to dry-run."
        ),
    )
    im.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Write the markers (default is dry-run).",
    )
    im.add_argument(
        "--paths",
        nargs="+",
        default=None,
        help=(
            "Vault-relative paths to scan. Default: every "
            "person/*.md in the vault."
        ),
    )
    im.add_argument(
        "--vault-path",
        default=None,
        help="Override the vault root (default: read from config.yaml).",
    )


def dispatch(args: argparse.Namespace) -> int:
    """Dispatch the right ``audit`` subcommand. Returns exit code."""
    sub = getattr(args, "audit_cmd", None)
    if sub == "infer-marker":
        return cmd_infer_marker(args)
    print("usage: alfred audit infer-marker [--apply] [--paths ...]")
    return 1


__all__ = ["build_parser", "dispatch", "cmd_infer_marker"]
