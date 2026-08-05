"""Email-tier card evidence enrichment pins (#26).

The email_tier card must carry its claim: a bounded body PREVIEW (600-cap +
truncated flag) + a Gmail deep-link, so the operator can judge the classification
and open the real email. Gates:

  * bounded body — short → verbatim, not truncated; long → 600-cap + "…" +
    truncated=True; header-skipped (reuses the snippet cleaning).
  * evidence population — a record with a body + ``email_message_id`` yields
    body / message_id / gmail_url on the item (+ its to_dict = the feed evidence).
  * Telegram BYTE-PARITY — ``render_batch`` output is byte-identical with vs
    without the new fields (the sync render never reads them).
  * id-stability — enriching an item does NOT shift its feed id (the digest
    precedent; ``_email_key`` keys on record_path/cluster only).
  * gmail deep-link — strips RFC822 angle brackets, URL-encodes, empty → "".

Tests run unconditionally per ``feedback_regression_pin_unconditional.md``.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import frontmatter
import pytest

from alfred.daily_sync.email_section import (
    _EMAIL_BODY_CAP,
    BatchItem,
    _bounded_email_body,
    _read_candidate,
    bounded_email_body,
    render_batch,
)
from alfred.daily_sync.feed_producer import build_feed_items
from alfred.mail.gmail_filing import gmail_rfc822_search_url


# --- bounded body -----------------------------------------------------------


def test_bounded_email_body_public_name_and_alias() -> None:
    """#27 promoted ``_bounded_email_body`` → public ``bounded_email_body`` (the
    classify-time email_urgent emitter is the second consumer). The private
    alias stays pointed at the public function so existing callers/tests hold."""
    assert bounded_email_body is _bounded_email_body
    assert bounded_email_body("short") == ("short", False)


def test_bounded_body_short_not_truncated() -> None:
    body, truncated = _bounded_email_body("Hey, can we move the meeting to 3pm?")
    assert body == "Hey, can we move the meeting to 3pm?"
    assert truncated is False


def test_bounded_body_long_truncated() -> None:
    content = "word " * 400  # ~2000 chars, well over the 600 cap
    body, truncated = _bounded_email_body(content)
    assert truncated is True
    assert len(body) <= _EMAIL_BODY_CAP + 1  # +1 for the "…" marker
    assert body.endswith("…")


def test_bounded_body_skips_headers() -> None:
    """Reuses the snippet cleaning — email headers never reach the body."""
    content = dedent("""\
        **From:** alice@example.com
        **Subject:** Re: the thing

        The actual body prose starts here.
    """)
    body, _ = _bounded_email_body(content)
    assert "alice@example.com" not in body
    assert "Subject:" not in body
    assert "The actual body prose starts here." in body


# --- gmail deep-link --------------------------------------------------------


def test_gmail_url_strips_brackets_and_encodes() -> None:
    url = gmail_rfc822_search_url("<CADnj4=abc123@mail.gmail.com>")
    assert url.startswith("https://mail.google.com/mail/u/0/#search/rfc822msgid:")
    assert "<" not in url and ">" not in url
    assert "%40" in url  # the @ is URL-encoded
    assert "CADnj4" in url


def test_gmail_url_empty_is_blank() -> None:
    assert gmail_rfc822_search_url("") == ""
    assert gmail_rfc822_search_url("   ") == ""
    assert gmail_rfc822_search_url("<>") == ""


# --- evidence population (record → item) ------------------------------------


def _seed(vault: Path, name: str, *, message_id: str, body: str) -> str:
    (vault / "note").mkdir(parents=True, exist_ok=True)
    fm = {
        "type": "note", "name": name, "priority": "high",
        # #40 — genuine email note ⇒ carries the provenance marker.
        "email_derived": True,
        "priority_reasoning": "reply-required", "action_hint": "calendar",
        "email_message_id": message_id,
    }
    post = frontmatter.Post(
        f"**From:** alice@example.com\n**Subject:** Hi\n\n{body}\n", **fm,
    )
    (vault / "note" / f"{name}.md").write_text(
        frontmatter.dumps(post) + "\n", encoding="utf-8",
    )
    return f"note/{name}.md"


def test_read_candidate_populates_evidence(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    rel = _seed(vault, "Email1", message_id="<msg-1@example.com>", body="Real email body content here.")
    rec = _read_candidate(vault, rel)
    assert rec is not None
    assert rec.body == "Real email body content here."
    assert rec.truncated is False
    assert rec.message_id == "<msg-1@example.com>"
    assert rec.gmail_url == gmail_rfc822_search_url("<msg-1@example.com>")
    assert "rfc822msgid:" in rec.gmail_url


def test_read_candidate_no_message_id_blank_url(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    rel = _seed(vault, "Email2", message_id="", body="body")
    rec = _read_candidate(vault, rel)
    assert rec is not None
    assert rec.message_id == "" and rec.gmail_url == ""  # no id → no link


def test_batch_item_to_dict_carries_evidence_fields() -> None:
    item = BatchItem(
        item_number=1, record_path="note/A.md", classifier_priority="high",
        classifier_action_hint=None, classifier_reason="r", sender="a@b.com",
        subject="s", snippet="snip", body="the body", truncated=True,
        message_id="<m@x>", gmail_url="https://mail.google.com/x",
    )
    d = item.to_dict()
    assert d["body"] == "the body" and d["truncated"] is True
    assert d["message_id"] == "<m@x>" and d["gmail_url"] == "https://mail.google.com/x"


# --- Telegram byte-parity (the new fields never touch the sync render) -------


def test_render_batch_byte_identical_with_and_without_new_fields() -> None:
    base = dict(
        item_number=1, record_path="note/A.md", classifier_priority="high",
        classifier_action_hint="calendar", classifier_reason="reply-required",
        sender="jamie@example.com", subject="Friday meeting", snippet="move to 3pm?",
    )
    plain = BatchItem(**base)
    enriched = BatchItem(
        **base, body="a much longer body preview " * 20, truncated=True,
        message_id="<m@x>", gmail_url="https://mail.google.com/mail/u/0/#search/rfc822msgid:m",
    )
    assert render_batch([plain]) == render_batch([enriched])  # byte-identical


# --- id-stability (enrichment must not shift the feed id) -------------------


def test_feed_item_id_stable_when_enriched() -> None:
    """The digest-precedent invariant: adding body/message_id/gmail_url must NOT
    shift the email_tier feed id (``_email_key`` keys on record_path/cluster)."""
    base = dict(
        item_number=1, record_path="note/A.md", classifier_priority="high",
        classifier_action_hint=None, classifier_reason="r", sender="a@b.com",
        subject="s", snippet="snip",
    )
    plain = BatchItem(**base)
    enriched = BatchItem(
        **base, body="body preview", truncated=False,
        message_id="<m@x>", gmail_url="https://mail.google.com/x",
    )
    id_plain = build_feed_items("email_tier", [plain.to_dict()], "salem")[0].id
    id_enriched = build_feed_items("email_tier", [enriched.to_dict()], "salem")[0].id
    assert id_plain == id_enriched  # id unchanged by enrichment
    # And the enriched feed item's evidence carries the body (the card's source).
    ev = build_feed_items("email_tier", [enriched.to_dict()], "salem")[0].evidence
    assert ev["body"] == "body preview" and ev["gmail_url"] == "https://mail.google.com/x"


# ---------------------------------------------------------------------------
# #34 rider (e) — the structural corridor: no marker, no candidate, ever
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "extra",
    [
        pytest.param({}, id="bare"),
        # Each of these is a proxy someone might be tempted to fall back on.
        # #40 happened because a consumer treated the FIRST one as provenance.
        pytest.param({"priority": "high"}, id="real-tier"),
        pytest.param({"priority": "high", "action_hint": "calendar"}, id="tier+hint"),
        pytest.param(
            {"email_message_id": "<m@example.com>"}, id="message-id",
        ),
        pytest.param(
            {"priority": "high", "priority_reasoning": "reply-required",
             "action_hint": "calendar", "email_message_id": "<m@example.com>",
             "subject": "Invoice", "sender": "billing@example.com"},
            id="every-proxy-at-once",
        ),
        # Marker present but FALSEY in each spelling PyYAML produces.
        pytest.param({"email_derived": False, "priority": "high"}, id="marker-false"),
        pytest.param({"email_derived": "", "priority": "high"}, id="marker-empty"),
        pytest.param({"email_derived": None, "priority": "high"}, id="marker-null"),
    ],
)
def test_no_marker_means_no_candidate_whatever_else_the_record_says(
    tmp_path: Path, extra: dict,
) -> None:
    """The corridor pin: ``_read_candidate`` returns None when the provenance
    marker is absent or falsey, REGARDLESS of any other frontmatter.

    This closes the invented-field fallback class. #40 put an EMAIL TIER card
    on a screenshot note because a consumer treated ``priority`` as proof of
    mail origin — a stamp the backfill's permissive heuristic applies to any
    note merely MENTIONING email. Every field below is a proxy that reads as
    plausible evidence and is not; the pin forbids the next one being wired in
    as a fallback.

    Fail-closed is the correct direction: a missed calibration item costs one
    batch slot; a false one costs a corpus row asserting an email judgment
    about something that was never email.
    """
    vault = tmp_path / "vault"
    (vault / "note").mkdir(parents=True, exist_ok=True)
    fm = {"type": "note", "name": "Suspect", **extra}
    post = frontmatter.Post(
        "**From:** alice@example.com\n**Subject:** Hi\n\nBody text.\n", **fm,
    )
    (vault / "note" / "Suspect.md").write_text(
        frontmatter.dumps(post) + "\n", encoding="utf-8",
    )

    assert _read_candidate(vault, "note/Suspect.md") is None


def test_the_marker_alone_is_not_enough_either(tmp_path: Path) -> None:
    """PAIRED with the corridor pin above, in the opposite direction.

    The marker is NECESSARY, not sufficient — a genuinely email-derived note
    with no real tier is still skipped, because there is nothing to calibrate.
    Without this, a build that returned a candidate for anything marker-bearing
    would pass every case above.
    """
    vault = tmp_path / "vault"
    (vault / "note").mkdir(parents=True, exist_ok=True)
    post = frontmatter.Post(
        "**From:** a@b.com\n**Subject:** s\n\nBody.\n",
        **{"type": "note", "name": "Marked", "email_derived": True},
    )
    (vault / "note" / "Marked.md").write_text(
        frontmatter.dumps(post) + "\n", encoding="utf-8",
    )

    assert _read_candidate(vault, "note/Marked.md") is None


def test_marker_plus_tier_IS_a_candidate(tmp_path: Path) -> None:
    """And the positive case, so the two refusals above cannot be satisfied by
    a build that simply never returns a candidate."""
    vault = tmp_path / "vault"
    rel = _seed(vault, "Genuine", message_id="<m@example.com>", body="Body.")
    assert _read_candidate(vault, rel) is not None
