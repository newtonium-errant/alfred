"""Calibration block I/O — read the user profile's ALFRED:CALIBRATION block.

Wk3 commit 2. Calibration is Alfred's running model of the primary user,
stored as a marker-wrapped body block on the user's `person` record:

    <!-- ALFRED:CALIBRATION -->
    ## Communication Style
    - bulleted attribution _source: session/X_
    ## Workflow Preferences
    - ...
    <!-- END ALFRED:CALIBRATION -->

This module only handles the read path; commit 7 adds the write path
(`propose_updates` + `apply_proposals`). Keeping them in one module is
deliberate — both ends need the same regex, the same marker strings, and
the same error-surface contract (missing file → ``None``, malformed block
→ ``None``; never raise). Writers that live elsewhere would drift.

The distiller strips this block before extracting learnings (wk3 commit 4
adds the pattern to :mod:`alfred.distiller.parser`). That's the whole
reason this lives inside fenced markers: the distiller must never
re-learn Alfred's own self-notes back into vault learnings, or the
extraction pipeline would become a feedback loop.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

from .utils import get_logger

log = get_logger(__name__)


# Marker strings. Kept as module constants so commit 7's writer uses the
# exact same pair — a typo-level drift would silently produce a duplicate
# block on every session close.
CALIBRATION_MARKER_START: Final[str] = "<!-- ALFRED:CALIBRATION -->"
CALIBRATION_MARKER_END: Final[str] = "<!-- END ALFRED:CALIBRATION -->"


# DOTALL because the block routinely spans dozens of lines. Non-greedy so
# two adjacent blocks in the unlikely future don't merge into one match.
CALIBRATION_RE: Final[re.Pattern[str]] = re.compile(
    rf"{re.escape(CALIBRATION_MARKER_START)}(.*?){re.escape(CALIBRATION_MARKER_END)}",
    re.DOTALL,
)


def read_calibration(vault_path: Path, user_rel_path: str) -> str | None:
    """Read the calibration block from the user's person record.

    Args:
        vault_path: Vault root.
        user_rel_path: Vault-relative path to the user record (e.g.
            ``person/Andrew Newton``, with or without the ``.md`` suffix).

    Returns:
        The inner text of the calibration block (stripped), or ``None`` if:
            - the file doesn't exist,
            - the file has no calibration markers,
            - the block is present but empty.

    Never raises. Bot startup must not crash because a user's profile
    record is missing or malformed — the fallback is simply "no
    calibration context", which is what wk2 already shipped with.
    """
    if not user_rel_path:
        return None

    # Normalise: allow callers to pass either ``person/Andrew Newton`` or
    # ``person/Andrew Newton.md``. Keeps the call site simple regardless
    # of where the path came from (config stores stems, the router emits
    # wikilink-friendly paths).
    rel = user_rel_path.strip()
    if not rel.endswith(".md"):
        rel = f"{rel}.md"

    file_path = vault_path / rel
    if not file_path.exists():
        log.info(
            "talker.calibration.missing_user_record",
            user_rel_path=user_rel_path,
        )
        return None

    try:
        raw = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning(
            "talker.calibration.read_failed",
            user_rel_path=user_rel_path,
            error=str(exc),
        )
        return None

    match = CALIBRATION_RE.search(raw)
    if match is None:
        log.info(
            "talker.calibration.no_block",
            user_rel_path=user_rel_path,
        )
        return None

    inner = match.group(1).strip()
    if not inner:
        return None
    return inner
