"""Tests for ``alfred._env`` — the canonical home for ``${VARNAME}``
substitution.

Consolidated from previously-divergent helpers in
``alfred/orchestrator.py`` and ``alfred/transport/config.py`` per
review-block C from 2026-05-05. New callers should import from this
module directly; ``transport/config.py``'s ``ENV_RE`` and
``_substitute_env`` are kept as backward-compat aliases.

Coverage:
  * ``ENV_PLACEHOLDER_RE`` matches the canonical shape
  * ``resolve_env_placeholders`` (single-string variant) — each
    edge case the orchestrator's injector relies on, including the
    empty-string coalesce semantics that close review-block A.
  * ``substitute_env_in_value`` (recursive variant) — dict / list /
    scalar walking, mirrors every config loader's local helper.
  * Backward-compat: importing the aliases from
    ``alfred.transport.config`` returns the canonical objects.
"""

from __future__ import annotations

import pytest

from alfred._env import (
    ENV_PLACEHOLDER_RE,
    resolve_env_placeholders,
    substitute_env_in_value,
)


# ---------------------------------------------------------------------------
# ENV_PLACEHOLDER_RE shape
# ---------------------------------------------------------------------------


class TestEnvPlaceholderRe:
    def test_matches_canonical_shape(self):
        assert ENV_PLACEHOLDER_RE.findall("${VARNAME}") == ["VARNAME"]
        assert ENV_PLACEHOLDER_RE.findall(
            "Bearer ${MY_TOKEN}",
        ) == ["MY_TOKEN"]

    def test_matches_multiple_placeholders(self):
        assert ENV_PLACEHOLDER_RE.findall(
            "${A}/${B}/${C_123}",
        ) == ["A", "B", "C_123"]

    def test_does_not_match_unwrapped_dollar(self):
        """``$VAR`` (no braces) is NOT matched — this regex is
        ``${VAR}``-only by design."""
        assert ENV_PLACEHOLDER_RE.findall("$BARE_VAR") == []

    def test_does_not_match_empty_braces(self):
        """``${}`` requires at least one word char."""
        assert ENV_PLACEHOLDER_RE.findall("${}") == []


# ---------------------------------------------------------------------------
# resolve_env_placeholders — single-string variant
# ---------------------------------------------------------------------------


class TestResolveEnvPlaceholders:
    def test_no_placeholders_returns_input_unchanged(self):
        assert resolve_env_placeholders("plain-value") == "plain-value"
        assert resolve_env_placeholders("") == ""

    def test_resolves_single_placeholder(self, monkeypatch):
        monkeypatch.setenv("MY_TEST_VAR", "resolved-value")
        assert (
            resolve_env_placeholders("${MY_TEST_VAR}")
            == "resolved-value"
        )

    def test_resolves_placeholder_inside_string(self, monkeypatch):
        monkeypatch.setenv("MY_TOKEN", "abc123")
        assert (
            resolve_env_placeholders("Bearer ${MY_TOKEN}")
            == "Bearer abc123"
        )

    def test_unresolved_placeholder_stays_literal(self, monkeypatch):
        """Defensive: env var missing → ``${VARNAME}`` literal stays
        so callers can detect "still unresolved" via startswith."""
        monkeypatch.delenv("DEFINITELY_NOT_SET_VAR", raising=False)
        assert (
            resolve_env_placeholders("${DEFINITELY_NOT_SET_VAR}")
            == "${DEFINITELY_NOT_SET_VAR}"
        )

    def test_partial_resolution_keeps_unresolved_literal(self, monkeypatch):
        monkeypatch.setenv("RESOLVED_VAR", "ok")
        monkeypatch.delenv("MISSING_VAR", raising=False)
        result = resolve_env_placeholders(
            "${RESOLVED_VAR}/${MISSING_VAR}",
        )
        assert result == "ok/${MISSING_VAR}"

    def test_empty_string_env_coalesces_to_literal(self, monkeypatch):
        """Review-block A from 2026-05-05: an env var EXPLICITLY set
        to empty string (``ALFRED_KALLE_TRANSPORT_TOKEN=""``) must be
        treated the same as an unset var. Pre-fix used
        ``os.environ.get(name, fallback)`` which returns ``""`` for
        the empty case and ``""`` would propagate to subprocesses as
        ``Bearer `` (empty header) → 401 with a different shape than
        "placeholder unresolved."

        Canonical helper uses ``os.environ.get(name) or fallback``
        which coalesces both ``None`` AND ``""`` to the fallback —
        single defensive shape."""
        monkeypatch.setenv("EXPLICITLY_EMPTY_VAR", "")
        # Coalesces to literal — same shape as the missing-var case.
        assert (
            resolve_env_placeholders("${EXPLICITLY_EMPTY_VAR}")
            == "${EXPLICITLY_EMPTY_VAR}"
        )

    def test_whitespace_only_env_does_NOT_coalesce(self, monkeypatch):
        """Boundary check: only the EMPTY string coalesces. A var set
        to whitespace (``" "``) is treated as a real value — the
        ``or`` operator's truthiness check considers ``" "`` truthy.
        Documented behaviour, not a bug — the operator who sets a
        var to a single space chose that explicitly."""
        monkeypatch.setenv("WHITESPACE_VAR", " ")
        assert resolve_env_placeholders("${WHITESPACE_VAR}") == " "


# ---------------------------------------------------------------------------
# substitute_env_in_value — recursive variant
# ---------------------------------------------------------------------------


class TestSubstituteEnvInValue:
    def test_string_substitution(self, monkeypatch):
        monkeypatch.setenv("MY_VAR", "hello")
        assert substitute_env_in_value("${MY_VAR}") == "hello"

    def test_dict_substitution_recurses(self, monkeypatch):
        monkeypatch.setenv("TOKEN", "abc")
        result = substitute_env_in_value({
            "outer": {"token": "${TOKEN}", "name": "literal"},
        })
        assert result == {
            "outer": {"token": "abc", "name": "literal"},
        }

    def test_list_substitution_recurses(self, monkeypatch):
        monkeypatch.setenv("ITEM", "resolved")
        assert substitute_env_in_value(
            ["${ITEM}", "literal", "${MISSING}"],
        ) == ["resolved", "literal", "${MISSING}"]

    def test_non_string_scalars_pass_through(self):
        """Numbers, booleans, None pass through unchanged."""
        assert substitute_env_in_value(42) == 42
        assert substitute_env_in_value(True) is True
        assert substitute_env_in_value(None) is None
        assert substitute_env_in_value(3.14) == 3.14

    def test_nested_mixed_structure(self, monkeypatch):
        monkeypatch.setenv("HOST", "127.0.0.1")
        monkeypatch.setenv("TOKEN", "secret")
        raw = {
            "transport": {
                "server": {"host": "${HOST}", "port": 8892},
                "auth": {
                    "tokens": {
                        "local": {
                            "token": "${TOKEN}",
                            "allowed_clients": ["a", "${TOKEN}", "b"],
                        },
                    },
                },
            },
        }
        result = substitute_env_in_value(raw)
        assert result["transport"]["server"]["host"] == "127.0.0.1"
        assert result["transport"]["server"]["port"] == 8892
        assert (
            result["transport"]["auth"]["tokens"]["local"]["token"]
            == "secret"
        )
        # List substitution.
        assert (
            result["transport"]["auth"]["tokens"]["local"]["allowed_clients"]
            == ["a", "secret", "b"]
        )

    def test_does_not_mutate_input(self, monkeypatch):
        """Returns NEW collections — caller's input is unchanged."""
        monkeypatch.setenv("V", "x")
        original = {"key": "${V}"}
        result = substitute_env_in_value(original)
        assert result == {"key": "x"}
        # Original dict still has the placeholder.
        assert original == {"key": "${V}"}


# ---------------------------------------------------------------------------
# Backward-compat — transport/config.py aliases
# ---------------------------------------------------------------------------


class TestTransportConfigAliases:
    def test_transport_config_env_re_is_canonical(self):
        """``transport.config.ENV_RE`` must BE the same object as
        ``alfred._env.ENV_PLACEHOLDER_RE`` — not a separate compiled
        regex with the same pattern. Identity check guards against
        accidental drift if anyone re-introduces a local ``re.compile``
        in transport/config.py."""
        from alfred.transport.config import ENV_RE
        assert ENV_RE is ENV_PLACEHOLDER_RE

    def test_transport_config_substitute_env_is_canonical(self):
        """``transport.config._substitute_env`` must BE the canonical
        ``substitute_env_in_value``."""
        from alfred.transport.config import _substitute_env
        assert _substitute_env is substitute_env_in_value
