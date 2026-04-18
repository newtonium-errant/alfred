"""Tests for :mod:`alfred.telegram.session_types`.

Covers the defaults table, the ``defaults_for`` lookup (including unknown
type fallback), and the ``ROUTER_MODEL`` constant shape.
"""

from __future__ import annotations

from alfred.telegram import session_types


def test_defaults_for_known_types_returns_expected_models() -> None:
    """Each of the 5 canonical types resolves to the doc-specified model."""
    assert session_types.defaults_for("note").model == "claude-sonnet-4-6"
    assert session_types.defaults_for("task").model == "claude-sonnet-4-6"
    assert session_types.defaults_for("journal").model == "claude-sonnet-4-6"
    assert session_types.defaults_for("article").model == "claude-opus-4-7"
    assert session_types.defaults_for("brainstorm").model == "claude-sonnet-4-6"


def test_defaults_for_unknown_type_falls_back_to_note() -> None:
    """Unknown / missing session types return the ``note`` defaults."""
    fallback = session_types.defaults_for("xyzzy")
    assert fallback.session_type == "note"
    assert fallback.model == "claude-sonnet-4-6"
    assert fallback.supports_continuation is False

    empty = session_types.defaults_for(None)
    assert empty.session_type == "note"

    blank = session_types.defaults_for("")
    assert blank.session_type == "note"


def test_router_model_and_continuation_flags() -> None:
    """Sanity-check the router-model constant and continuation flags."""
    assert session_types.ROUTER_MODEL.startswith("claude-sonnet-")

    # Only article / journal / brainstorm continue by design.
    continuable = {t for t in session_types.known_types()
                   if session_types.defaults_for(t).supports_continuation}
    assert continuable == {"journal", "article", "brainstorm"}
