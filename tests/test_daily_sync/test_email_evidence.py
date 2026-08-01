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

from alfred.daily_sync.email_section import (
    _EMAIL_BODY_CAP,
    BatchItem,
    _bounded_email_body,
    _read_candidate,
    render_batch,
)
from alfred.daily_sync.feed_producer import build_feed_items
from alfred.mail.gmail_filing import gmail_rfc822_search_url


# --- bounded body -----------------------------------------------------------


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
