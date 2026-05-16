#!/usr/bin/env python3
"""Backwards-compat shim for the Phase 1 Meditations migration script.

The real implementation lives at
``alfred.scripts.migrate_2026_05_16_meditations_zettels`` (note Python
module names require underscores, not dashes). This shim preserves the
documented dash-form invocation path:

    python scripts/migrate_2026-05-16_meditations_zettels.py [--apply] [--vault PATH]

Module-location rationale: the migration logic carries a
``MigrationPlan`` dataclass whose introspection at decorator-time
walks ``sys.modules[cls.__module__]``. When the script lived at the
top-level ``scripts/`` tree and tests loaded it via
``importlib.util.spec_from_file_location``, ``cls.__module__`` resolved
to a name not in ``sys.modules``, crashing the dataclass machinery
with ``AttributeError: 'NoneType' object has no attribute '__dict__'``.
Hoisting the real implementation into the package fixes that —
tests now ``import alfred.scripts.migrate_2026_05_16_meditations_zettels``
normally and the dataclass resolves cleanly.

Recommended invocation (post-package-hoist):
    python -m alfred.scripts.migrate_2026_05_16_meditations_zettels [--apply]
"""

from __future__ import annotations

import sys

from alfred.scripts.migrate_2026_05_16_meditations_zettels import main


if __name__ == "__main__":
    sys.exit(main())
