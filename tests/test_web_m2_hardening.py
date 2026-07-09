"""Web M2 hardening pins — pure-function + config-gate coverage.

Three merge-inert / default-safe bits:
  * (b) /auth/login rate-limit — ``_LoginRateLimiter`` sliding-window.
  * (c) magic-link deep-link — ``safe_next_path`` + ``_build_magic_link``
        (the load-bearing open-redirect allowlist).
  * (d) web-only daemon mode — ``_missing_config_reasons(web_only=...)`` +
        ``web.web_only`` config load + preserved instance.name fail-loud.

Integration coverage (real transport app / handler wiring) for (b) + (c)
lives alongside the existing suite in ``tests/test_web_routes_auth.py``.
"""

from __future__ import annotations

import pytest

from alfred.telegram.config import (
    AnthropicConfig,
    InstanceConfig,
    STTConfig,
    TalkerConfig,
    VaultConfig,
    load_from_unified as load_talker,
)
from alfred.telegram.daemon import _missing_config_reasons
from alfred.web.config import load_from_unified as load_web
from alfred.web.routes_auth import (
    _LoginRateLimiter,
    _build_magic_link,
    safe_next_path,
)


# ---------------------------------------------------------------------------
# Fake monotonic clock (injected into the limiter — no real sleeping).
# ---------------------------------------------------------------------------
class _FakeClock:
    def __init__(self, t: float = 1_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


# ===========================================================================
# BIT (c) — safe_next_path (open-redirect allowlist). LOAD-BEARING.
# Mirrors web/tests/safeNextPath.test.ts so the Python (server) and TS
# (callback) sanitizers stay byte-compatible.
# ===========================================================================


@pytest.mark.parametrize(
    "raw",
    ["/", "/chat", "/a/b?c=d", "/chat?instance=hypatia", "/ingest"],
)
def test_safe_next_path_allows_relative_same_origin(raw: str) -> None:
    assert safe_next_path(raw) == raw


@pytest.mark.parametrize("raw", ["", None, 42, [], {}])
def test_safe_next_path_empty_or_nonstring_defaults(raw) -> None:
    assert safe_next_path(raw) == "/"


@pytest.mark.parametrize(
    "raw",
    [
        "chat",                 # not rooted at '/'
        "https://evil.com",     # absolute URL / scheme
        "http://evil.com",
        "//evil.com",           # protocol-relative
        "/\\evil.com",          # backslash trick (browser may normalise)
        "/foo\\bar",            # backslash anywhere
        "javascript:alert(1)",  # scheme, no leading '/'
        "/foo\tbar",            # control/whitespace
        "/foo\nbar",
        "/ leading-space",
        "\\\\evil.com",         # leading backslashes
    ],
)
def test_safe_next_path_rejects_open_redirect_vectors(raw: str) -> None:
    # EVERY open-redirect attempt falls back to the default path.
    assert safe_next_path(raw) == "/"


# ===========================================================================
# BIT (c) — _build_magic_link embeds ONLY a sanitized relative next.
# ===========================================================================


def test_build_magic_link_no_next_is_byte_identical() -> None:
    # Default next → link is byte-for-byte the pre-deep-link form (merge-inert).
    assert (
        _build_magic_link("https://s.example.com", "TOK")
        == "https://s.example.com/auth/callback?token=TOK"
    )
    assert (
        _build_magic_link("https://s.example.com/", "TOK", "/")
        == "https://s.example.com/auth/callback?token=TOK"
    )


def test_build_magic_link_appends_encoded_valid_next() -> None:
    link = _build_magic_link("https://s.example.com", "TOK", "/chat?instance=hypatia")
    # ? and = in the next value are URL-encoded so they don't break the outer
    # query string; the callback decodes + re-sanitizes back to the path.
    assert link == (
        "https://s.example.com/auth/callback?token=TOK"
        "&next=%2Fchat%3Finstance%3Dhypatia"
    )


@pytest.mark.parametrize(
    "evil",
    ["//evil.com", "https://evil.com", "/\\evil.com", "javascript:alert(1)", "chat"],
)
def test_build_magic_link_omits_open_redirect_next(evil: str) -> None:
    # An open-redirect next collapses to '/' and is NOT appended — the emailed
    # link never carries an attacker-controlled redirect.
    link = _build_magic_link("https://s.example.com", "TOK", evil)
    assert link == "https://s.example.com/auth/callback?token=TOK"
    assert "next=" not in link


# ===========================================================================
# BIT (b) — _LoginRateLimiter sliding window.
# ===========================================================================


def test_rate_limiter_n_plus_one_rejected_then_window_recovers() -> None:
    clock = _FakeClock()
    rl = _LoginRateLimiter(
        max_per_email=3, window_s=900, max_global=999, clock=clock
    )
    key = ("1.2.3.4", "andrew@example.com")
    # N allowed.
    assert [rl.allow(key) for _ in range(3)] == [True, True, True]
    # N+1th within the window → rejected.
    assert rl.allow(key) is False
    # A send after the window elapses (injected clock) → allowed again.
    clock.advance(901)
    assert rl.allow(key) is True


def test_rate_limiter_rejected_send_consumes_no_budget() -> None:
    clock = _FakeClock()
    rl = _LoginRateLimiter(max_per_email=2, window_s=900, max_global=999, clock=clock)
    key = ("ip", "a@b.com")
    assert rl.allow(key) is True
    assert rl.allow(key) is True
    # Hammer past the cap — every extra attempt rejected, none recorded.
    for _ in range(10):
        assert rl.allow(key) is False
    # After the window, full capacity (max_per_email) is restored — the
    # rejected hammering did not push the recovery window forward.
    clock.advance(901)
    assert [rl.allow(key) for _ in range(2)] == [True, True]
    assert rl.allow(key) is False


def test_rate_limiter_per_key_isolation() -> None:
    clock = _FakeClock()
    rl = _LoginRateLimiter(max_per_email=1, window_s=900, max_global=999, clock=clock)
    a = ("ip", "a@b.com")
    b = ("ip", "b@b.com")
    assert rl.allow(a) is True
    assert rl.allow(a) is False   # a is capped
    assert rl.allow(b) is True    # b is independent


def test_rate_limiter_global_ceiling_across_distinct_keys() -> None:
    clock = _FakeClock()
    # Per-email cap is generous; the GLOBAL ceiling (email-rotation defense)
    # trips first when an attacker rotates addresses.
    rl = _LoginRateLimiter(
        max_per_email=99, window_s=900, max_global=3, clock=clock
    )
    results = [rl.allow(("ip", f"user{i}@b.com")) for i in range(4)]
    assert results == [True, True, True, False]
    # After the window the global budget restores.
    clock.advance(901)
    assert rl.allow(("ip", "user5@b.com")) is True


def test_rate_limiter_clear_resets_both_gates() -> None:
    clock = _FakeClock()
    rl = _LoginRateLimiter(max_per_email=1, window_s=900, max_global=1, clock=clock)
    assert rl.allow(("ip", "a@b.com")) is True
    assert rl.allow(("ip", "a@b.com")) is False
    rl.clear()
    assert rl.allow(("ip", "a@b.com")) is True


# ===========================================================================
# BIT (d) — web-only daemon config gate.
# ===========================================================================


def _talker(
    *,
    bot_token: str = "",
    allowed_users=None,
    anthropic_key: str = "",
    stt_key: str = "",
    vault_path: str = "",
    instance_name: str = "Salem",
) -> TalkerConfig:
    return TalkerConfig(
        bot_token=bot_token,
        allowed_users=allowed_users if allowed_users is not None else [],
        anthropic=AnthropicConfig(api_key=anthropic_key, model="m"),
        stt=STTConfig(api_key=stt_key, model="w"),
        vault=VaultConfig(path=vault_path),
        instance=InstanceConfig(name=instance_name),
    )


def test_missing_config_flag_unset_is_byte_for_byte() -> None:
    # Default (flag UNSET) → today's five reasons, EXACT text + order.
    reasons = _missing_config_reasons(_talker())
    assert reasons == [
        "telegram.bot_token is empty",
        "telegram.allowed_users is empty",
        "telegram.anthropic.api_key is empty",
        "telegram.stt.api_key is empty",
        "vault.path is empty",
    ]


def test_missing_config_web_only_relaxes_telegram_prereqs() -> None:
    # web_only + agent prereqs present, Telegram bits empty → nothing blocks.
    cfg = _talker(anthropic_key="k", vault_path="/v")
    assert _missing_config_reasons(cfg, web_only=True) == []


def test_missing_config_web_only_still_requires_anthropic() -> None:
    cfg = _talker(vault_path="/v")  # anthropic empty
    assert _missing_config_reasons(cfg, web_only=True) == [
        "telegram.anthropic.api_key is empty"
    ]


def test_missing_config_web_only_still_requires_vault() -> None:
    cfg = _talker(anthropic_key="k")  # vault empty
    assert _missing_config_reasons(cfg, web_only=True) == ["vault.path is empty"]


def test_missing_config_web_only_bot_and_stt_optional() -> None:
    # The exact PIN shape: web-only + no bot_token + no stt.api_key + no
    # allowed_users, but agent prereqs present → daemon does NOT early-exit.
    cfg = _talker(anthropic_key="k", vault_path="/v")
    assert cfg.bot_token == ""
    assert cfg.stt.api_key == ""
    assert cfg.allowed_users == []
    assert _missing_config_reasons(cfg, web_only=True) == []


# ---- web.web_only config load --------------------------------------------


def test_web_only_config_defaults_false() -> None:
    assert load_web({"web": {"enabled": True}}).web_only is False
    assert load_web({}).web_only is False


def test_web_only_config_opt_in_true() -> None:
    wc = load_web({"web": {"enabled": True, "web_only": True}})
    assert wc.enabled is True
    assert wc.web_only is True


# ---- instance.name fail-loud preserved (NON-NEGOTIABLE) -------------------


def test_web_only_does_not_weaken_instance_name_fail_loud() -> None:
    # instance.name fail-loud lives in talker config LOAD, upstream of the
    # web_only gate — so it fires regardless of web_only (the daemon can't
    # even reach _missing_config_reasons). A YAML without instance.name must
    # STILL crash loud (feedback_hardcoding_and_alfred_naming.md).
    with pytest.raises(TypeError, match="name"):
        load_talker(
            {
                "telegram": {"bot_token": "", "anthropic": {"api_key": "k"}},
                "vault": {"path": "/v"},
                "web": {"enabled": True, "web_only": True},
            }
        )
    # Sanity: WITH instance.name, load succeeds (the guard is name-specific).
    cfg = load_talker(
        {
            "telegram": {"instance": {"name": "Salem"}},
            "vault": {"path": "/v"},
        }
    )
    assert cfg.instance.name == "Salem"
