"""Tests for ``alfred.curator.context`` helpers.

Covers the deterministic, pure pieces of the curator's vault-context builder:
the sender-email regex parser that the email pipeline depends on. The
filesystem-walking ``build_vault_context`` and the bigger
``gather_sender_context`` have their own dedicated tests elsewhere — this
module keeps to the narrow regex contract for ``extract_sender_email``.
"""

from __future__ import annotations

import pytest

from alfred.curator.context import extract_sender_email


class TestExtractSenderEmail:
    """Contract for ``extract_sender_email``: pull one email out of the
    ``**From:**`` line of an inbox markdown payload, or return ``None``.

    This feeds the sender-context lookup in the curator pipeline, so a
    silent regex regression would quietly disable email-aware enrichment
    on every inbox item.
    """

    def test_extracts_bare_email(self) -> None:
        # Simplest case: "**From:** foo@bar.com" with no angle brackets.
        content = "**From:** andrew@example.com\n\nSome body text."
        assert extract_sender_email(content) == "andrew@example.com"

    def test_returns_none_when_no_from_line(self) -> None:
        # Missing sentinel → None, never raises.
        content = "No sender marker here.\n\nJust body."
        assert extract_sender_email(content) is None

    def test_strips_angle_brackets_and_mailto(self) -> None:
        # Email clients commonly emit "<mailto:foo@bar>" forms; both wrappers
        # must be stripped to match the frontmatter-stored plain form.
        content = "**From:** <mailto:hello@example.org>\nBody."
        assert extract_sender_email(content) == "hello@example.org"

    def test_rejects_non_email_token(self) -> None:
        # The regex demands an "@" in the captured group; ensure a malformed
        # from-line doesn't pass through as a bogus "email" with no @.
        content = "**From:** not-an-email\nBody."
        assert extract_sender_email(content) is None
