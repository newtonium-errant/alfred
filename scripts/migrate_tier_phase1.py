#!/usr/bin/env python3
"""Backwards-compat shim for the tier Phase 1 migration script.

ARCHIVED 2026-06-25 — COMPLETED ONE-TIME MIGRATION, DO NOT RUN. The V1
tier fields this script populated (``base_tier`` / ``escalate_to``) were
removed from the schema surface 2026-06-25 (routine-systems
consolidation Step 1). See the package module
``alfred.scripts.migrate_tier_phase1`` docstring for the full archival
note. The shim is kept only so the documented invocation path doesn't
404; it is not part of any live migration.

The real implementation lives at ``alfred.scripts.migrate_tier_phase1``.
This shim preserves the top-level invocation path:

    python scripts/migrate_tier_phase1.py [--dry-run] [--vault PATH]

Per the precedent set by ``migrate_2026-05-16_meditations_zettels.py``,
the implementation hoists into the package so ``cls.__module__``
resolves cleanly against ``sys.modules`` (dataclass machinery requires
this when the loader is ``importlib.util.spec_from_file_location``).

Recommended invocation (post-package-hoist):

    python -m alfred.scripts.migrate_tier_phase1 [--dry-run]
"""

from __future__ import annotations

import sys

from alfred.scripts.migrate_tier_phase1 import main


if __name__ == "__main__":
    sys.exit(main())
