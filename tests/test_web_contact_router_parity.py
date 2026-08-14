"""Cross-language vocabulary pin for the C4 contact router.

The PWA sends a rule id and a surface name on every contact, and the transport
refuses anything outside its own vocabulary. That vocabulary therefore has TWO
spellings — a Python tuple and a TypeScript array — because TypeScript cannot
import a Python tuple. The second spelling is deliberate; this pin is what makes
it safe, and it is the same shape as
``tests/brief/test_narration_segment_order_parity.py`` (read the ``.ts`` as text,
extract the array, compare).

The failure this prevents is quiet and one-sided: add a surface in Python and the
PWA never routes to it; add one in TypeScript and every contact naming it is
400'd at the door, so the router silently stops learning from those opens.

ORDER MATTERS for the rules — it is the spec's PRIORITY order, and a client that
evaluated the rungs in a different sequence would open the wrong surface while
agreeing with the server about every string.
"""

from __future__ import annotations

import re
from pathlib import Path

from alfred.web.contact_state import RULE_ORDER, SURFACES

REPO_ROOT = Path(__file__).resolve().parents[1]
ROUTER_TS = REPO_ROOT / "web" / "lib" / "algernon" / "contactRouter.ts"


def _ts_array(name: str) -> list[str]:
    """Extract ``export const <name> = [ ... ] as const;`` from the TS source."""
    ts = ROUTER_TS.read_text(encoding="utf-8")
    match = re.search(
        rf"export const {name}\s*=\s*\[(.*?)\]\s*as const", ts, re.S
    )
    assert match is not None, f"{name} not found in {ROUTER_TS}"
    return re.findall(r"'([^']+)'", match.group(1))


def test_the_source_file_is_where_this_pin_thinks_it_is():
    """Positive control. Every comparison below passes vacuously against an
    empty file read from a path that silently moved."""
    assert ROUTER_TS.exists(), ROUTER_TS
    assert len(ROUTER_TS.read_text(encoding="utf-8")) > 500


def test_the_rule_order_matches_python_exactly_and_in_order():
    assert _ts_array("CONTACT_RULE_ORDER") == list(RULE_ORDER)


def test_the_surface_vocabulary_matches_python_exactly():
    assert _ts_array("CONTACT_SURFACES") == list(SURFACES)


def test_every_surface_has_a_route():
    """A surface the client cannot map to a path is a dead navigation. The TS
    type makes this exhaustive at compile time; this asserts it at the text
    level too, so a Python-side addition is caught by the Python suite rather
    than only by ``tsc``."""
    ts = ROUTER_TS.read_text(encoding="utf-8")
    match = re.search(
        r"SURFACE_PATHS: Record<ContactSurface, string> = \{(.*?)\}", ts, re.S
    )
    assert match is not None, "SURFACE_PATHS not found"
    mapped = set(re.findall(r"^\s*(\w+):", match.group(1), re.M))
    assert mapped == set(SURFACES)


def test_capture_is_absent_from_both_spellings():
    """Rule 1's surface. It is unarmed, and a vocabulary entry for a surface
    nothing can route to is the silent absence ARMED_RULES exists to prevent —
    so its absence here is deliberate and pinned, not an oversight waiting to
    be 'fixed'."""
    assert "capture" not in SURFACES
    assert "capture" not in _ts_array("CONTACT_SURFACES")
