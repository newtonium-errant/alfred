"""The narration slide order is duplicated across two languages — pin it.

``SEGMENT_ORDER`` exists twice: in ``alfred.brief.narration`` (the producer) and
in ``web/lib/algernon/player.ts`` (the deck that orders the slides). Neither can
import the other, so the duplication is by necessity — and the failure mode is
QUIET, which is what makes a pin worth its weight here rather than a comment.

``player.ts``'s ``slideIndex`` maps an unrecognised section id to
``SEGMENT_ORDER.length`` deliberately, so that "a new/unmapped id still renders
rather than vanishing". A segment added on the server and forgotten in the TS
list therefore does not disappear and does not throw — it plays LAST. The
``waiting`` segment added in Phase C would have played AFTER the sign-off, which
is exactly the kind of wrong that ships.

Same shape and same reason as the ``SLOT_LABELS`` parity pin in
``tests/tier/test_day_plan.py``.

Tests run unconditionally per ``feedback_regression_pin_unconditional.md``.
"""

from __future__ import annotations

import re
from pathlib import Path

from alfred.brief.narration import SEGMENT_ORDER

REPO_ROOT = Path(__file__).resolve().parents[2]
PLAYER_TS = REPO_ROOT / "web" / "lib" / "algernon" / "player.ts"


def _frontend_segment_order() -> list[str]:
    ts = PLAYER_TS.read_text(encoding="utf-8")
    match = re.search(
        r"export const SEGMENT_ORDER\s*=\s*\[(.*?)\]\s*as const", ts, re.S,
    )
    assert match is not None, f"SEGMENT_ORDER not found in {PLAYER_TS}"
    return re.findall(r"'([^']+)'", match.group(1))


def test_segment_order_matches_the_frontend_element_for_element() -> None:
    """Order matters, not just membership — the deck SORTS by this list."""
    assert _frontend_segment_order() == list(SEGMENT_ORDER)


def test_waiting_is_ordered_before_the_sign_off_on_both_sides() -> None:
    """The waiting segment hands the operator off to the deck, so it is the
    last thing said BEFORE "go" — not after it. This is the specific ordering
    the quiet-append failure mode would have broken."""
    fe = _frontend_segment_order()
    for order in (list(SEGMENT_ORDER), fe):
        assert order.index("waiting") < order.index("sign_off")
        assert order[-1] == "sign_off"
