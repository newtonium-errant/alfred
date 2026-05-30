#!/usr/bin/env python3
"""Backwards-compat shim for the V1 tier-field strip migration script.

The real implementation lives at
``alfred.scripts.migrate_2026_05_30_strip_v1_tier_fields``. This shim
preserves the top-level invocation path:

    python scripts/migrate_2026_05_30_strip_v1_tier_fields.py [--dry-run] [--vault PATH]

Per the precedent set by ``migrate_2026-05-16_meditations_zettels.py``
and ``migrate_tier_phase1.py``, the implementation hoists into the
package so ``cls.__module__`` resolves cleanly against ``sys.modules``
(dataclass machinery requires this when the loader is
``importlib.util.spec_from_file_location``).

Recommended invocation (post-package-hoist):

    python -m alfred.scripts.migrate_2026_05_30_strip_v1_tier_fields [--dry-run]
"""

from __future__ import annotations

import sys

from alfred.scripts.migrate_2026_05_30_strip_v1_tier_fields import main


if __name__ == "__main__":
    sys.exit(main())
