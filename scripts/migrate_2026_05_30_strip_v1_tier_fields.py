#!/usr/bin/env python3
"""Backwards-compat shim for the V1 tier-field strip migration script.

ARCHIVED 2026-06-26 — DO NOT RUN CASUALLY. LIVE-FIELD HAZARD. The
underlying migration's original strip set included ``escalate_at_days``,
which is the LIVE V2 due-window knob — stripping it would sever task
auto-T1 surfacing. The active strip set has been narrowed to the two
genuinely-dead fields (``base_tier`` / ``escalate_to``), and a live
run now requires ``--i-understand-this-is-archived``. See the package
module ``alfred.scripts.migrate_2026_05_30_strip_v1_tier_fields``
docstring for the full archival note + live-field hazard. The record
strip is a deliberate operational step against the production vault,
not an automatic migration.

The real implementation lives at
``alfred.scripts.migrate_2026_05_30_strip_v1_tier_fields``. This shim
preserves the top-level invocation path:

    python scripts/migrate_2026_05_30_strip_v1_tier_fields.py [--dry-run] [--i-understand-this-is-archived] [--vault PATH]

Per the precedent set by ``migrate_2026-05-16_meditations_zettels.py``
and ``migrate_tier_phase1.py``, the implementation hoists into the
package so ``cls.__module__`` resolves cleanly against ``sys.modules``
(dataclass machinery requires this when the loader is
``importlib.util.spec_from_file_location``).

Recommended invocation (post-package-hoist; ARCHIVED — a live run
requires the acknowledgement flag, dry-run inspects freely):

    # Inspect — NO writes.
    python -m alfred.scripts.migrate_2026_05_30_strip_v1_tier_fields --dry-run

    # Execute (deliberate operational run only).
    python -m alfred.scripts.migrate_2026_05_30_strip_v1_tier_fields \
        --i-understand-this-is-archived
"""

from __future__ import annotations

import sys

from alfred.scripts.migrate_2026_05_30_strip_v1_tier_fields import main


if __name__ == "__main__":
    sys.exit(main())
