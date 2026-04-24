#!/usr/bin/env python3
"""Static contract check: distiller.contracts <-> vault/schema.py alignment.

Validates that ``distiller.contracts.LearningCandidate`` stays aligned
with the learn-record schema in ``vault/schema.py``. Specifically:

  1. The Pydantic ``type`` field's ``Literal[...]`` values equal
     ``LEARN_TYPES`` as a set.
  2. For every learn type, the Pydantic ``status`` model-validator
     accepts exactly the values in ``STATUS_BY_TYPE[type]``.
  3. Every learn type in ``LEARN_TYPES`` resolves to a known directory
     in ``TYPE_DIRECTORY``. This is what the deterministic writer
     relies on to decide where to land the file.

Exits 0 on parity, non-zero with a clear message on drift.

No pytest — see ``feedback_pytest_wsl_hang.md``. Run:

    python scripts/smoke_contract_parity.py

Rationale: if a future schema change (new learn type, new status,
directory rename) lands without a matching change here, the distiller
rebuild will silently produce malformed output or refuse legitimate
values. This script catches the drift before the daemon restarts.
"""

from __future__ import annotations

import sys
import typing
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from pydantic import ValidationError  # noqa: E402

from alfred.distiller.contracts import LearningCandidate  # noqa: E402
from alfred.vault.schema import (  # noqa: E402
    LEARN_TYPES,
    STATUS_BY_TYPE,
    TYPE_DIRECTORY,
)


def _fail(msg: str) -> None:
    """Print a clear failure message and exit non-zero."""
    print(f"[FAIL] {msg}", file=sys.stderr)
    sys.exit(1)


def check_type_literal_matches_learn_types() -> None:
    """Assert LearningCandidate.type Literal values == LEARN_TYPES."""
    annotation = LearningCandidate.model_fields["type"].annotation
    literal_args = set(typing.get_args(annotation))
    if literal_args != LEARN_TYPES:
        _fail(
            "LearningCandidate.type Literal values drift from LEARN_TYPES.\n"
            f"  Literal: {sorted(literal_args)}\n"
            f"  schema : {sorted(LEARN_TYPES)}\n"
            f"  missing in contract: {sorted(LEARN_TYPES - literal_args)}\n"
            f"  missing in schema  : {sorted(literal_args - LEARN_TYPES)}"
        )
    print(f"[OK] type Literal matches LEARN_TYPES ({len(LEARN_TYPES)} types)")


def _build_spec(type_: str, status: str) -> LearningCandidate:
    """Helper: build a minimal-valid candidate for the given type+status."""
    return LearningCandidate(
        type=type_,  # type: ignore[arg-type]
        title="Parity check title",
        confidence="low",
        status=status,
        claim="Parity check claim text, at least twenty characters long.",
    )


def check_status_validator_matches_schema() -> None:
    """Assert the status validator accepts exactly STATUS_BY_TYPE[type]."""
    drift: list[str] = []

    for learn_type in sorted(LEARN_TYPES):
        allowed = STATUS_BY_TYPE.get(learn_type)
        if not allowed:
            drift.append(
                f"{learn_type}: STATUS_BY_TYPE is empty; "
                f"contracts.py will reject every value"
            )
            continue

        # Every allowed status must construct successfully.
        for status in sorted(allowed):
            try:
                _build_spec(learn_type, status)
            except ValidationError as exc:
                drift.append(
                    f"{learn_type}/{status}: allowed by schema but "
                    f"rejected by contract ({exc})"
                )

        # Any plausibly-wrong value must be rejected.
        sentinel = f"___not_a_real_status_{learn_type}"
        try:
            _build_spec(learn_type, sentinel)
        except ValidationError:
            pass
        else:
            drift.append(
                f"{learn_type}/{sentinel}: contract accepted an out-of-schema "
                f"status — validator is not enforcing STATUS_BY_TYPE"
            )

    if drift:
        _fail("status-validator drift:\n  " + "\n  ".join(drift))

    print(
        f"[OK] status validator matches STATUS_BY_TYPE "
        f"({sum(len(STATUS_BY_TYPE[t]) for t in LEARN_TYPES)} "
        f"(type, status) pairs)"
    )


def check_directory_resolution() -> None:
    """Assert every learn type maps to an entry in TYPE_DIRECTORY.

    The writer's directory choice is ``TYPE_DIRECTORY.get(spec.type, spec.type)``.
    If a learn type is missing from the map, the fallback silently uses
    the raw type string — which happens to work today (the directory
    name matches the type name for all current learn types), but a future
    rename could break it silently. Assert explicit presence.
    """
    missing = sorted(LEARN_TYPES - set(TYPE_DIRECTORY))
    if missing:
        _fail(
            "TYPE_DIRECTORY is missing entries for learn types:\n  "
            + ", ".join(missing)
            + "\nwriter.write_learn_record would fall back to the raw type "
            "name — add explicit entries to vault/schema.py."
        )
    print(
        f"[OK] TYPE_DIRECTORY covers all learn types "
        f"({len(LEARN_TYPES)}/{len(LEARN_TYPES)})"
    )


def main() -> int:
    print("Distiller contracts <-> vault/schema parity check\n")
    check_type_literal_matches_learn_types()
    check_status_validator_matches_schema()
    check_directory_resolution()
    print("\n[PASS] All parity checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
