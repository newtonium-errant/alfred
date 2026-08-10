"""#72 item 4 — the section tap: one vocabulary, a gated statistic, a threaded payload.

Three things are pinned here, and they fail in three different ways if left
unpinned:

1. ONE VOCABULARY. The headings lived as inline literals in the summary
   renderer. Copying them into the stats would have let the two drift, and the
   drift is silent in the worst direction: the stats keep counting a section the
   card stopped rendering, and it reads as a healthy zero.

2. THE STATISTIC REFUSES UNTIL IT MEANS SOMETHING. Between the corpus half
   shipping and this tap shipping, every contest filed under ``unknown``. The
   breakdown was computed on every call and was structurally one bucket, so any
   surface rendering it would have shown a clean histogram carrying no
   information. The refusal is mechanical, not a note in a docstring.

3. THE PAYLOAD IS THREADED AT EVERY PRODUCTION CALL SITE. A default-None gate
   parameter that only tests pass is a live write side with a dead read side and
   every pin green. So the route is driven, not just the router.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from alfred.daily_sync.attribution_quality import attribution_quality_stats
from alfred.telegram.capture_sections import (
    BRIEF_RECAP_SECTIONS,
    RE_ENCOUNTERS_SECTION,
    SUMMARY_SECTIONS,
    is_known_section,
)

NOW = datetime(2026, 8, 10, 12, 0, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _contest_row(*, section: str = "", days_ago: float = 1) -> dict:
    return {
        "type": "attribution_contest",
        "marker_id": f"m-{section}-{days_ago}",
        "record_path": "session/A.md",
        "agent": "salem",
        "section_title": "Structured Summary",
        "marker_date": _iso(NOW - timedelta(days=days_ago + 1)),
        "andrew_action": "contest",
        "action_at": _iso(NOW - timedelta(days=days_ago)),
        "confirmed_via": "timeout_24h",
        "section": section,
    }


def _stats(tmp_path: Path, rows: list[dict]):
    path = tmp_path / "corpus.jsonl"
    path.write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8",
    )
    return attribution_quality_stats(path, now=NOW)


# --- 1. one vocabulary ------------------------------------------------------


def test_the_vocabulary_is_the_eight_rendered_headings():
    """The list a previous pass undercounted. Six go through the renderer's
    ``_section`` helper, ``Discarded Noise`` follows an intervening comment, and
    ``Re-encounters`` is appended directly — so a grep of ``_section(`` finds
    seven of eight. Pinned by count and by content."""
    assert len(SUMMARY_SECTIONS) == 8
    assert SUMMARY_SECTIONS == (
        "Topics", "Decisions", "Open Questions", "Action Items",
        "Key Insights", "Raw Contradictions", "Discarded Noise", "Re-encounters",
    )
    assert RE_ENCOUNTERS_SECTION in SUMMARY_SECTIONS


def test_the_brief_recap_is_a_subset_not_a_second_spelling():
    """The brief renderer shows fewer sections, not differently-spelled ones."""
    assert set(BRIEF_RECAP_SECTIONS) <= set(SUMMARY_SECTIONS)


def test_the_renderer_spells_its_headings_from_the_vocabulary():
    """The anti-drift pin, driven through the REAL renderer.

    Asserting the module constant against itself would prove nothing; this
    renders a summary and checks every heading in the vocabulary appears, in
    the vocabulary's order.
    """
    from alfred.telegram.capture_batch import StructuredSummary, render_summary_markdown

    out = render_summary_markdown(
        StructuredSummary(
            topics=["t"], decisions=["d"], open_questions=["q"],
            action_items=["a"], key_insights=["k"], raw_contradictions=["c"],
            discarded_noise=["n"],
        ),
        re_encounters_body="- re",
    )
    positions = [out.index(f"### {h}") for h in SUMMARY_SECTIONS]
    assert positions == sorted(positions), "headings render out of vocabulary order"


def test_every_non_prerendered_heading_has_a_backing_field():
    """Drift between the vocabulary and StructuredSummary must fail LOUD.

    The renderer uses ``getattr`` with no default precisely so a heading whose
    field vanished raises instead of rendering an empty section that reads as
    'nothing found'. This pins the mapping the renderer depends on.
    """
    from alfred.telegram.capture_batch import StructuredSummary

    summary = StructuredSummary()
    for heading in SUMMARY_SECTIONS:
        if heading == RE_ENCOUNTERS_SECTION:
            continue
        attr = heading.lower().replace(" ", "_")
        assert hasattr(summary, attr), f"{heading!r} has no backing field {attr!r}"


def test_is_known_section_is_exact():
    assert is_known_section("Topics")
    assert not is_known_section("topics"), "casefolded — should read as unknown"
    assert not is_known_section("Toppics")
    assert not is_known_section("")


# --- 2. the statistic refuses until the tap is live -------------------------


def test_per_section_data_is_refused_while_every_contest_is_unknown(tmp_path):
    """THE banked rider. Before the tap, the breakdown is 100% one bucket —
    structurally uninformative — so it must not be reachable."""
    s = _stats(tmp_path, [_contest_row(), _contest_row(days_ago=2)])

    assert s.contested == 2
    assert s.section_tap_live is False
    assert s.per_section_counts() is None, "exposed a breakdown that is all-unknown"


def test_the_refusal_is_none_not_an_empty_dict(tmp_path):
    """A caller treating falsy-as-empty would render 'no sections contested',
    which is a different and false claim from 'not measurable yet'."""
    s = _stats(tmp_path, [_contest_row()])
    assert s.per_section_counts() is None
    assert s.per_section_counts() != {}


def test_one_tapped_section_opens_the_breakdown(tmp_path):
    """And it opens for the WHOLE window, including the unknown rows — a
    card-level contest must stay in the denominator rather than vanishing."""
    s = _stats(tmp_path, [
        _contest_row(section="Topics"),
        _contest_row(days_ago=2),            # card-level, no section
    ])

    assert s.section_tap_live is True
    counts = s.per_section_counts()
    assert counts == {"Topics": 1, "unknown": 1}


def test_the_event_says_which_zero_it_is(tmp_path):
    """ILB: ``sections: null`` plus ``section_tap_live`` distinguishes 'nobody
    has tapped yet' from 'sections exist and none were contested'."""
    import structlog

    with structlog.testing.capture_logs() as cap:
        _stats(tmp_path, [_contest_row()])
    ev = [c for c in cap if c.get("event") == "daily_sync.attribution.quality"][0]
    assert ev["sections"] is None
    assert ev["section_tap_live"] is False


def test_nothing_outside_the_module_reads_the_raw_counter():
    """The structural half of the rider.

    ``per_section_counts()`` gating is only a guarantee if consumers actually
    go through it. This is the cross-module drift pin — the same shape the
    health layer uses for QUIET_HEALTH_STATUSES — so a future surface that
    reaches for the raw Counter fails here instead of quietly rendering an
    all-unknown histogram.
    """
    src = Path(__file__).resolve().parents[1] / "src" / "alfred"
    offenders = []
    for py in src.rglob("*.py"):
        if py.name == "attribution_quality.py":
            continue  # the owner
        if "contests_by_section" in py.read_text(encoding="utf-8"):
            offenders.append(str(py.relative_to(src)))
    assert offenders == [], (
        f"read the raw counter instead of per_section_counts(): {offenders}"
    )


# --- 3. the payload is threaded, end to end ---------------------------------


def test_an_unrecognised_tap_files_as_unknown_rather_than_minting_a_bucket():
    """A stale client must not be able to invent sections in the stats. The
    contest still lands — losing which section costs a dimension, losing the
    contest costs the correction."""
    from alfred.daily_sync.action_router import _normalise_contested_section

    assert _normalise_contested_section("Topics") == "Topics"
    assert _normalise_contested_section("  Topics  ") == "Topics"
    assert _normalise_contested_section("Made Up Heading") == ""
    assert _normalise_contested_section("topics") == ""
    assert _normalise_contested_section("") == ""
    assert _normalise_contested_section(None) == ""


def test_the_unknown_tap_is_logged_not_swallowed():
    import structlog

    from alfred.daily_sync.action_router import _normalise_contested_section

    with structlog.testing.capture_logs() as cap:
        _normalise_contested_section("Not A Section")
    matches = [
        c for c in cap
        if c.get("event") == "feed.act.attribution.section_unknown"
    ]
    assert len(matches) == 1
    assert matches[0]["section"] == "Not A Section"


def test_the_contest_dispatcher_writes_the_tap_onto_the_corpus_row(tmp_path, monkeypatch):
    """Drives the REAL contest dispatcher over a REAL vault and reads the row
    back, rather than inspecting the source of the function that writes it.

    This is the pin that proves the tap survives the whole write path: vault
    marker, corpus append, and the normalisation in between.
    """
    from typing import Any

    import yaml as _yaml

    from alfred.daily_sync import action_router
    from alfred.daily_sync import reply_dispatch as _rd
    from alfred.vault.attribution import AuditEntry, append_audit_entry

    vault = tmp_path / "vault"
    (vault / "note").mkdir(parents=True)
    fm: dict[str, Any] = {"type": "note", "name": "A"}
    append_audit_entry(fm, AuditEntry(
        marker_id="m1", agent="salem", date="2026-08-09T00:00:00+00:00",
        section_title="Structured Summary", reason="talker conversation turn",
    ))
    (vault / "note" / "A.md").write_text(
        "---\n" + _yaml.dump(fm, sort_keys=False) + "---\n\nbody\n", encoding="utf-8",
    )

    corpus = tmp_path / "attr_corpus.jsonl"
    monkeypatch.setattr(_rd, "_attribution_corpus_path", lambda *a, **kw: str(corpus))

    class _Store:
        def load(self):
            return {}

    result = action_router._dispatch_attribution_contest(
        "feed-1", "contest",
        {
            "marker_id": "m1", "record_path": "note/A.md",
            "agent": "salem", "section_title": "Structured Summary",
            "date": "2026-08-09T00:00:00+00:00",
        },
        feed_store=_Store(), config=object(), vault_path=vault,
        contested_section="Action Items",
    )

    assert result.ok is True
    rows = [
        json.loads(ln) for ln in corpus.read_text().splitlines() if ln.strip()
    ]
    assert len(rows) == 1
    assert rows[0]["type"] == "attribution_contest"
    assert rows[0]["section"] == "Action Items", "the tap never reached the corpus"


def test_an_invented_section_still_records_the_contest_as_unknown(
    tmp_path, monkeypatch,
):
    """The refusal direction: the bad value is dropped, the contest is NOT."""
    from typing import Any

    import yaml as _yaml

    from alfred.daily_sync import action_router
    from alfred.daily_sync import reply_dispatch as _rd
    from alfred.vault.attribution import AuditEntry, append_audit_entry

    vault = tmp_path / "vault"
    (vault / "note").mkdir(parents=True)
    fm: dict[str, Any] = {"type": "note", "name": "A"}
    append_audit_entry(fm, AuditEntry(
        marker_id="m1", agent="salem", date="2026-08-09T00:00:00+00:00",
        section_title="Structured Summary", reason="turn",
    ))
    (vault / "note" / "A.md").write_text(
        "---\n" + _yaml.dump(fm, sort_keys=False) + "---\n\nbody\n", encoding="utf-8",
    )
    corpus = tmp_path / "attr_corpus.jsonl"
    monkeypatch.setattr(_rd, "_attribution_corpus_path", lambda *a, **kw: str(corpus))

    class _Store:
        def load(self):
            return {}

    result = action_router._dispatch_attribution_contest(
        "feed-1", "contest",
        {
            "marker_id": "m1", "record_path": "note/A.md", "agent": "salem",
            "section_title": "S", "date": "2026-08-09T00:00:00+00:00",
        },
        feed_store=_Store(), config=object(), vault_path=vault,
        contested_section="Totally Invented",
    )

    assert result.ok is True, "the contest itself must still land"
    rows = [
        json.loads(ln) for ln in corpus.read_text().splitlines() if ln.strip()
    ]
    assert rows[0]["section"] == "", "an invented section minted a bucket"


# ---------------------------------------------------------------------------
# #72 item 4, the web half — CROSS-SURFACE DRIFT PIN.
#
# Same shape as the CONTEST_ACTION pin in test_daily_sync/test_attribution_tier
# and the snooze-ladder pin in tier/test_board_snooze: the PWA cannot import a
# Python tuple, so it keeps its own spelling of the vocabulary and the two can
# drift in silence. Parsed out of the TS source rather than restated, so this
# does not become the third copy the capture_sections docstring warns about.
# ---------------------------------------------------------------------------


def _web_contest_sections() -> list[str]:
    """The picker's vocabulary, read out of feedConstants.ts."""
    import re

    ts = (
        Path(__file__).resolve().parents[1]
        / "web" / "lib" / "algernon" / "feedConstants.ts"
    )
    assert ts.exists(), f"the web constants moved — update this pin: {ts}"
    block = re.search(
        r"export const CONTEST_SECTIONS = \[(.*?)\] as const;",
        ts.read_text(encoding="utf-8"),
        re.S,
    )
    assert block, "CONTEST_SECTIONS not found in feedConstants.ts"
    return re.findall(r"'([^']+)'", block.group(1))


def test_web_picker_offers_exactly_the_rendered_headings_in_order() -> None:
    """The tap's vocabulary and the renderer's must be the same list.

    Order counts as well as membership: the operator picks from this list on the
    card, and capture_sections' own comment makes the order part of the contract
    rather than incidental.

    Mutation: drop or rename one entry on either side alone → this fails and
    names the mismatch. It is the only thing that can: the web tests compare the
    array against itself, and the Python tests never look at the browser.
    """
    assert _web_contest_sections() == list(SUMMARY_SECTIONS), (
        "section vocabulary drift — the PWA picker and the summary renderer "
        "disagree about the headings"
    )


def test_every_heading_the_web_offers_survives_the_router_normaliser() -> None:
    """The end the drift actually costs.

    A picker value the router does not recognise is normalised to ``""`` and
    files under ``unknown``. That fails SAFE — the contest still lands — which
    is exactly why it is invisible: the operator gets his correction recorded,
    and the per-section statistic silently loses the dimension it exists for.
    So the pin asserts the round trip, not just the list equality above.
    """
    from alfred.daily_sync.action_router import _normalise_contested_section

    for section in _web_contest_sections():
        assert _normalise_contested_section(section) == section, (
            f"the PWA offers {section!r}, which the router files under unknown"
        )
