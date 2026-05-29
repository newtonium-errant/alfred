#!/usr/bin/env python3
"""Backwards-compat shim for the Routine Phase 2A migration script.

The real implementation lives at
``alfred.scripts.migrate_routine_recurring_bills``. This shim
preserves the top-level invocation path:

    python scripts/migrate_routine_recurring_bills.py [--dry-run] [--vault PATH]

Per the precedent set by ``scripts/migrate_tier_phase1.py``, the
implementation hoists into the package so ``cls.__module__``
resolves cleanly against ``sys.modules`` (dataclass machinery
requires this when the loader is
``importlib.util.spec_from_file_location``).

Recommended invocation (post-package-hoist):

    python -m alfred.scripts.migrate_routine_recurring_bills [--dry-run]
"""

from __future__ import annotations

import sys

from alfred.scripts.migrate_routine_recurring_bills import main


if __name__ == "__main__":
    sys.exit(main())
