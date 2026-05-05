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


# ---------------------------------------------------------------------------
# .env auto-loader (P1 from QA 2026-05-05)
# ---------------------------------------------------------------------------


class TestParseDotenvLine:
    """Unit tests for the inline .env parser."""

    def test_simple_key_value(self):
        from alfred._env import _parse_dotenv_line
        assert _parse_dotenv_line("FOO=bar") == ("FOO", "bar")

    def test_double_quoted_value(self):
        from alfred._env import _parse_dotenv_line
        assert _parse_dotenv_line('FOO="bar baz"') == ("FOO", "bar baz")

    def test_single_quoted_value(self):
        from alfred._env import _parse_dotenv_line
        assert _parse_dotenv_line("FOO='bar baz'") == ("FOO", "bar baz")

    def test_export_prefix_stripped(self):
        """``set -a; source .env`` habit produces ``export KEY=value``
        lines. Parser strips the ``export `` prefix so they parse
        the same as bare ``KEY=value``."""
        from alfred._env import _parse_dotenv_line
        assert _parse_dotenv_line("export FOO=bar") == ("FOO", "bar")

    def test_comment_line_returns_none(self):
        from alfred._env import _parse_dotenv_line
        assert _parse_dotenv_line("# a comment") is None
        assert _parse_dotenv_line("   # indented comment") is None

    def test_blank_line_returns_none(self):
        from alfred._env import _parse_dotenv_line
        assert _parse_dotenv_line("") is None
        assert _parse_dotenv_line("   ") is None

    def test_missing_equals_returns_none(self):
        from alfred._env import _parse_dotenv_line
        assert _parse_dotenv_line("MALFORMED_NO_EQUALS") is None

    def test_empty_key_returns_none(self):
        """``=value`` with no key → malformed."""
        from alfred._env import _parse_dotenv_line
        assert _parse_dotenv_line("=value") is None

    def test_empty_value_allowed(self):
        """``KEY=`` is a valid empty-string assignment."""
        from alfred._env import _parse_dotenv_line
        assert _parse_dotenv_line("FOO=") == ("FOO", "")

    def test_value_with_equals_sign_preserved(self):
        """Values containing ``=`` use partition (only first ``=`` is
        the separator). Common shape for base64-encoded secrets."""
        from alfred._env import _parse_dotenv_line
        assert _parse_dotenv_line("FOO=a=b=c") == ("FOO", "a=b=c")

    def test_leading_trailing_whitespace_stripped_from_key(self):
        from alfred._env import _parse_dotenv_line
        assert _parse_dotenv_line("  FOO=bar  ") == ("FOO", "bar")

    def test_unmatched_quotes_not_stripped(self):
        """Only MATCHED surrounding quotes are stripped. ``"foo``
        (unmatched leading quote) keeps the quote character."""
        from alfred._env import _parse_dotenv_line
        assert _parse_dotenv_line('FOO="bar') == ("FOO", '"bar')


class TestLoadDotenvFile:
    """Pure read — does NOT touch os.environ."""

    def test_missing_file_returns_empty(self, tmp_path):
        from alfred._env import load_dotenv_file
        assert load_dotenv_file(tmp_path / "nonexistent.env") == {}

    def test_loads_canonical_shape(self, tmp_path):
        from alfred._env import load_dotenv_file
        env = tmp_path / ".env"
        env.write_text(
            "# comment\n"
            "FOO=bar\n"
            "BAZ=\"quoted value\"\n"
            "\n"
            "QUX='single-quoted'\n"
            "export EXPORTED=value\n",
            encoding="utf-8",
        )
        result = load_dotenv_file(env)
        assert result == {
            "FOO": "bar",
            "BAZ": "quoted value",
            "QUX": "single-quoted",
            "EXPORTED": "value",
        }

    def test_skips_malformed_lines(self, tmp_path):
        from alfred._env import load_dotenv_file
        env = tmp_path / ".env"
        env.write_text(
            "VALID=ok\n"
            "MALFORMED_NO_EQUALS\n"
            "=missing-key\n"
            "ALSO_VALID=ok2\n",
            encoding="utf-8",
        )
        assert load_dotenv_file(env) == {
            "VALID": "ok",
            "ALSO_VALID": "ok2",
        }

    def test_empty_file_returns_empty(self, tmp_path):
        from alfred._env import load_dotenv_file
        env = tmp_path / ".env"
        env.write_text("", encoding="utf-8")
        assert load_dotenv_file(env) == {}

    def test_all_comments_returns_empty(self, tmp_path):
        from alfred._env import load_dotenv_file
        env = tmp_path / ".env"
        env.write_text("# c1\n# c2\n", encoding="utf-8")
        assert load_dotenv_file(env) == {}

    def test_does_not_touch_os_environ(self, tmp_path, monkeypatch):
        """Pure read — caller decides whether to inject."""
        from alfred._env import load_dotenv_file
        monkeypatch.delenv("DOTENV_TEST_PURE_READ", raising=False)
        env = tmp_path / ".env"
        env.write_text("DOTENV_TEST_PURE_READ=value\n", encoding="utf-8")
        load_dotenv_file(env)
        # Pure-read contract: env untouched.
        assert "DOTENV_TEST_PURE_READ" not in __import__("os").environ


class TestAutoLoadDotenv:
    """``override=False`` semantics + missing-file no-op."""

    def test_loads_var_when_absent_from_env(self, tmp_path, monkeypatch):
        from alfred._env import auto_load_dotenv
        import os
        monkeypatch.delenv("AUTOLOAD_FRESH", raising=False)
        env = tmp_path / ".env"
        env.write_text("AUTOLOAD_FRESH=resolved\n", encoding="utf-8")
        loaded, skipped = auto_load_dotenv(env)
        assert loaded == 1
        assert skipped == 0
        assert os.environ.get("AUTOLOAD_FRESH") == "resolved"

    def test_existing_env_wins_with_override_false(
        self, tmp_path, monkeypatch,
    ):
        """The headline contract: explicit ``export FOO=...`` in the
        parent shell survives; .env only fills gaps."""
        from alfred._env import auto_load_dotenv
        import os
        monkeypatch.setenv("AUTOLOAD_PRESET", "from-parent-shell")
        env = tmp_path / ".env"
        env.write_text("AUTOLOAD_PRESET=from-dotenv\n", encoding="utf-8")
        loaded, skipped = auto_load_dotenv(env)
        assert loaded == 0
        assert skipped == 1
        # Parent-shell value wins.
        assert os.environ["AUTOLOAD_PRESET"] == "from-parent-shell"

    def test_override_true_replaces_existing(self, tmp_path, monkeypatch):
        """Reserved for test fixtures that want full env control."""
        from alfred._env import auto_load_dotenv
        import os
        monkeypatch.setenv("AUTOLOAD_FORCED", "from-parent")
        env = tmp_path / ".env"
        env.write_text("AUTOLOAD_FORCED=from-dotenv\n", encoding="utf-8")
        loaded, skipped = auto_load_dotenv(env, override=True)
        assert loaded == 1
        assert skipped == 0
        assert os.environ["AUTOLOAD_FORCED"] == "from-dotenv"

    def test_missing_file_is_noop(self, tmp_path):
        """Production deploys (systemd, k8s) set env directly; .env
        absence is the common case there. No-op, no raise."""
        from alfred._env import auto_load_dotenv
        loaded, skipped = auto_load_dotenv(tmp_path / "nonexistent.env")
        assert loaded == 0
        assert skipped == 0

    def test_partial_load_mixed_skip_and_load(
        self, tmp_path, monkeypatch,
    ):
        """Mixed case: one .env var is already in env (skipped), one
        isn't (loaded). Counts reported separately."""
        from alfred._env import auto_load_dotenv
        import os
        monkeypatch.setenv("AUTOLOAD_MIXED_PRESET", "preset")
        monkeypatch.delenv("AUTOLOAD_MIXED_FRESH", raising=False)
        env = tmp_path / ".env"
        env.write_text(
            "AUTOLOAD_MIXED_PRESET=ignored\n"
            "AUTOLOAD_MIXED_FRESH=injected\n",
            encoding="utf-8",
        )
        loaded, skipped = auto_load_dotenv(env)
        assert loaded == 1
        assert skipped == 1
        assert os.environ["AUTOLOAD_MIXED_PRESET"] == "preset"
        assert os.environ["AUTOLOAD_MIXED_FRESH"] == "injected"
