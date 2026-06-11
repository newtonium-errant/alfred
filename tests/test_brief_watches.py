"""Watch Items section — flip detection, ILB rendering, containment, state.

The brief runs operator-configured upstream checks live (weather-style)
and renders one line per item. Pins here lock the render contract
(quiet-unchanged / loud-flip / terminal done-tail), the per-item +
daemon-level containment (a watch can NEVER kill the brief), the state
schema-tolerance contract, and the feature-off silence.

All tests run unconditionally — GitHub fetchers are monkeypatched
module functions (the weather-module convention); no network, no
importorskip (per feedback_regression_pin_unconditional).
"""

from __future__ import annotations

import json
from pathlib import Path

import structlog

from alfred.brief import watches as watches_mod
from alfred.brief.config import WatchItemConfig
from alfred.brief.watches import (
    DONE_TAIL,
    FLIP_MARKER,
    WatchItemState,
    check_and_format_watches,
    load_watch_state,
    save_watch_state,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pr_item(**overrides) -> WatchItemConfig:  # type: ignore[no-untyped-def]
    kwargs = dict(
        id="llamacpp-pr",
        label="llama.cpp arch PR",
        type="github_pr",
        repo="example-org/example-repo",
        number=123,
        on_flip_note="rebuild and retry the spike",
    )
    kwargs.update(overrides)
    return WatchItemConfig(**kwargs)


def _release_item(**overrides) -> WatchItemConfig:  # type: ignore[no-untyped-def]
    kwargs = dict(
        id="vendor-release",
        label="vendor release mention",
        type="github_release_mention",
        repo="example-org/server",
        pattern="newarch|special.?model",
        baseline_tag="v1.0.0",
        on_flip_note="switch the spike to the vendored build",
    )
    kwargs.update(overrides)
    return WatchItemConfig(**kwargs)


def _patch_pr(monkeypatch, payload, *, calls: list | None = None):  # type: ignore[no-untyped-def]
    async def _fake(client, repo, number):  # type: ignore[no-untyped-def]
        if calls is not None:
            calls.append((repo, number))
        if isinstance(payload, Exception):
            raise payload
        return payload

    monkeypatch.setattr(watches_mod, "_fetch_pr", _fake)


def _patch_releases(monkeypatch, payload, *, calls: list | None = None):  # type: ignore[no-untyped-def]
    async def _fake(client, repo):  # type: ignore[no-untyped-def]
        if calls is not None:
            calls.append(repo)
        if isinstance(payload, Exception):
            raise payload
        return payload

    monkeypatch.setattr(watches_mod, "_fetch_releases", _fake)


def _seed_state(path: Path, items: dict[str, dict]) -> None:
    path.write_text(json.dumps({"items": items}), encoding="utf-8")


# ---------------------------------------------------------------------------
# Feature off — the one permitted silence
# ---------------------------------------------------------------------------


async def test_no_watches_returns_empty_string(tmp_path) -> None:
    out = await check_and_format_watches([], tmp_path / "state.json")
    assert out == ""
    assert not (tmp_path / "state.json").exists()  # no state churn either


# ---------------------------------------------------------------------------
# github_pr — baseline / unchanged / flip / terminal persistence
# ---------------------------------------------------------------------------


async def test_pr_first_check_is_quiet_baseline(tmp_path, monkeypatch) -> None:
    _patch_pr(monkeypatch, {"state": "open", "merged_at": None})
    out = await check_and_format_watches([_pr_item()], tmp_path / "s.json")
    assert "PR example-org/example-repo#123 — OPEN (baseline)" in out
    assert FLIP_MARKER not in out
    # State established for the next brief.
    states = load_watch_state(tmp_path / "s.json")
    assert states["llamacpp-pr"].last_state == "open"


async def test_pr_unchanged_renders_quiet(tmp_path, monkeypatch) -> None:
    _seed_state(tmp_path / "s.json", {"llamacpp-pr": {"last_state": "open"}})
    _patch_pr(monkeypatch, {"state": "open", "merged_at": None})
    out = await check_and_format_watches([_pr_item()], tmp_path / "s.json")
    assert "OPEN (unchanged)" in out
    assert FLIP_MARKER not in out
    assert DONE_TAIL not in out


async def test_pr_flip_to_merged_renders_loud_with_note(tmp_path, monkeypatch) -> None:
    """● The flip brief: state change since last brief → 🚨 + on_flip_note."""
    _seed_state(tmp_path / "s.json", {"llamacpp-pr": {"last_state": "open"}})
    _patch_pr(monkeypatch, {"state": "closed", "merged_at": "2026-06-11T01:00:00Z"})
    out = await check_and_format_watches([_pr_item()], tmp_path / "s.json")
    assert FLIP_MARKER in out
    assert "MERGED" in out
    assert "rebuild and retry the spike" in out
    assert DONE_TAIL not in out  # the flip brief is the loud one, not the nag
    states = load_watch_state(tmp_path / "s.json")
    assert states["llamacpp-pr"].last_state == "merged"


async def test_pr_terminal_persists_without_api_call(tmp_path, monkeypatch) -> None:
    """● Post-flip terminal: every brief renders the done-tail until the
    operator removes the item, and the API is no longer queried."""
    _seed_state(tmp_path / "s.json", {"llamacpp-pr": {"last_state": "merged"}})
    calls: list = []
    _patch_pr(monkeypatch, {"state": "open", "merged_at": None}, calls=calls)
    # Two consecutive briefs — same nag both times, zero fetches.
    for _ in range(2):
        out = await check_and_format_watches([_pr_item()], tmp_path / "s.json")
        assert "MERGED" in out
        assert DONE_TAIL in out
        assert FLIP_MARKER not in out
    assert calls == []


async def test_pr_reopen_flip_is_loud_then_quiet(tmp_path, monkeypatch) -> None:
    # A non-terminal flip (closed isn't observed here: open → open is
    # quiet; open → closed would latch). Reopen case: a PR watched from
    # "closed" can't happen (closed latches terminal) — so pin the
    # OTHER non-terminal transition: baseline open, API now says open,
    # quiet; then API says merged → loud. Sequencing sanity across two
    # briefs with state carried in the file.
    state_path = tmp_path / "s.json"
    _patch_pr(monkeypatch, {"state": "open", "merged_at": None})
    out1 = await check_and_format_watches([_pr_item()], state_path)
    assert "(baseline)" in out1
    _patch_pr(monkeypatch, {"state": "closed", "merged_at": "2026-06-11T02:00:00Z"})
    out2 = await check_and_format_watches([_pr_item()], state_path)
    assert FLIP_MARKER in out2 and "MERGED" in out2
    out3 = await check_and_format_watches([_pr_item()], state_path)
    assert DONE_TAIL in out3 and FLIP_MARKER not in out3


# ---------------------------------------------------------------------------
# github_release_mention — no-match / flip / boundary advance / terminal
# ---------------------------------------------------------------------------


_RELEASES_NO_MATCH = [
    {"tag_name": "v1.0.2", "name": "v1.0.2", "body": "routine fixes"},
    {"tag_name": "v1.0.1", "name": "v1.0.1", "body": "routine fixes"},
    {"tag_name": "v1.0.0", "name": "v1.0.0", "body": "baseline"},
]

_RELEASES_WITH_MATCH = [
    {"tag_name": "v1.1.0", "name": "v1.1.0",
     "body": "adds NewArch support for special-model GGUFs"},
    {"tag_name": "v1.0.2", "name": "v1.0.2", "body": "routine fixes"},
    {"tag_name": "v1.0.0", "name": "v1.0.0", "body": "baseline"},
]


async def test_release_no_match_quiet_and_boundary_advances(tmp_path, monkeypatch) -> None:
    state_path = tmp_path / "s.json"
    _patch_releases(monkeypatch, _RELEASES_NO_MATCH)
    out = await check_and_format_watches([_release_item()], state_path)
    assert "no matching release" in out
    assert FLIP_MARKER not in out
    states = load_watch_state(state_path)
    # Boundary advanced to the newest scanned tag — each release is
    # scanned exactly once across briefs.
    assert states["vendor-release"].last_seen_tag == "v1.0.2"
    assert states["vendor-release"].matched_tag == ""


async def test_release_releases_older_than_baseline_never_match(tmp_path, monkeypatch) -> None:
    # The baseline release itself mentions the pattern — but it isn't
    # STRICTLY NEWER than baseline_tag, so it must not fire.
    _patch_releases(monkeypatch, [
        {"tag_name": "v1.0.0", "name": "v1.0.0", "body": "newarch groundwork"},
    ])
    out = await check_and_format_watches([_release_item()], tmp_path / "s.json")
    assert FLIP_MARKER not in out
    assert "no matching release" in out


async def test_release_first_match_renders_loud_and_latches(tmp_path, monkeypatch) -> None:
    """● The release flip: first matching release newer than the
    baseline → 🚨 + note; subsequent briefs nag without fetching."""
    state_path = tmp_path / "s.json"
    calls: list = []
    _patch_releases(monkeypatch, _RELEASES_WITH_MATCH, calls=calls)
    out = await check_and_format_watches([_release_item()], state_path)
    assert FLIP_MARKER in out
    assert "v1.1.0" in out
    assert "switch the spike to the vendored build" in out
    assert len(calls) == 1
    states = load_watch_state(state_path)
    assert states["vendor-release"].matched_tag == "v1.1.0"

    # Next brief: terminal latched — done-tail, no fetch.
    out2 = await check_and_format_watches([_release_item()], state_path)
    assert DONE_TAIL in out2
    assert "v1.1.0" in out2
    assert FLIP_MARKER not in out2
    assert len(calls) == 1  # unchanged — no second API call


async def test_release_match_pattern_is_case_insensitive(tmp_path, monkeypatch) -> None:
    _patch_releases(monkeypatch, [
        {"tag_name": "v1.1.0", "name": "SPECIAL MODEL day", "body": ""},
        {"tag_name": "v1.0.0", "name": "v1.0.0", "body": ""},
    ])
    out = await check_and_format_watches([_release_item()], tmp_path / "s.json")
    assert FLIP_MARKER in out


# ---------------------------------------------------------------------------
# Containment — per-item + ILB lines + daemon level
# ---------------------------------------------------------------------------


async def test_one_failing_watch_contained_others_run(tmp_path, monkeypatch) -> None:
    """● Per-item containment: a failing check renders its ILB
    'watch unavailable' line + warning; the healthy item still runs."""
    import httpx

    _patch_pr(monkeypatch, httpx.ConnectError("connection refused"))
    _patch_releases(monkeypatch, _RELEASES_NO_MATCH)
    items = [_pr_item(), _release_item()]
    with structlog.testing.capture_logs() as captured:
        out = await check_and_format_watches(items, tmp_path / "s.json")
    lines = out.splitlines()
    assert len(lines) == 2  # EVERY configured watch yields a line (ILB)
    assert "watch unavailable (api error: ConnectError" in lines[0]
    assert "no matching release" in lines[1]
    warns = [c for c in captured if c.get("event") == "brief.watch_check_failed"]
    assert len(warns) == 1
    assert warns[0]["id"] == "llamacpp-pr"
    assert warns[0]["error_type"] == "ConnectError"
    # Healthy item's state progress survived the partial run.
    states = load_watch_state(tmp_path / "s.json")
    assert states["vendor-release"].last_seen_tag == "v1.0.2"


async def test_invalid_regex_renders_config_error_label(tmp_path, monkeypatch) -> None:
    """Review nit a4: an operator typo in the pattern is a CONFIG error
    and must say so — ``re.error``'s class name is literally ``error``,
    so the generic containment used to render the unhelpful
    'api error: error: ...'. No network call happens either (compile
    precedes fetch)."""
    calls: list = []
    _patch_releases(monkeypatch, _RELEASES_NO_MATCH, calls=calls)
    item = _release_item(pattern="(unclosed")
    with structlog.testing.capture_logs() as captured:
        out = await check_and_format_watches([item], tmp_path / "s.json")
    assert "watch unavailable (config error: invalid pattern regex:" in out
    assert "api error" not in out
    assert calls == []  # compile failure short-circuits before the fetch
    warns = [c for c in captured if c.get("event") == "brief.watch_check_failed"]
    assert len(warns) == 1
    assert warns[0]["error_type"] == "config_error"


async def test_unknown_type_renders_config_error_line(tmp_path) -> None:
    item = WatchItemConfig(id="bogus", label="bogus watch", type="rss_feed")
    with structlog.testing.capture_logs() as captured:
        out = await check_and_format_watches([item], tmp_path / "s.json")
    assert "watch unavailable (config error: unknown watch type 'rss_feed')" in out
    warns = [c for c in captured if c.get("event") == "brief.watch_check_failed"]
    assert len(warns) == 1
    assert warns[0]["error_type"] == "config_error"


async def test_daemon_level_watches_crash_never_kills_brief(tmp_path, monkeypatch) -> None:
    """● Daemon no-kill (weather idiom, 874c751): a structural watches
    bug yields a brief with the explicit unavailable line + warning."""
    from alfred.brief import daemon as daemon_mod
    from alfred.brief.config import BriefConfig, StateConfig
    from alfred.brief.state import StateManager

    vault = tmp_path / "vault"
    vault.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    async def _structural_bug(watches, state_path):  # type: ignore[no-untyped-def]
        raise RuntimeError("watches module exploded")

    monkeypatch.setattr(daemon_mod, "check_and_format_watches", _structural_bug)
    # Weather must not hit the network in this test either.

    async def _no_weather(config):  # type: ignore[no-untyped-def]
        return "*Weather data unavailable.*"

    monkeypatch.setattr(daemon_mod, "fetch_and_format", _no_weather)

    config = BriefConfig(
        vault_path=str(vault),
        state=StateConfig(path=str(data_dir / "brief_state.json")),
        watches=[_pr_item()],
    )
    state_mgr = StateManager(config.state.path)

    with structlog.testing.capture_logs() as captured:
        rel_path = await daemon_mod.generate_brief(config, state_mgr)

    assert rel_path is not None  # the run SURVIVED
    content = (vault / rel_path).read_text(encoding="utf-8")
    assert "Watch Items" in content
    assert "*Watch checks unavailable.*" in content
    events = [c for c in captured
              if c.get("event") == "brief.watches_section_failed"]
    assert len(events) == 1
    assert events[0]["error_type"] == "RuntimeError"


async def test_daemon_omits_section_when_unconfigured(tmp_path, monkeypatch) -> None:
    """Feature off = section absent — at the BRIEF level, not just the
    module level."""
    from alfred.brief import daemon as daemon_mod
    from alfred.brief.config import BriefConfig, StateConfig
    from alfred.brief.state import StateManager

    vault = tmp_path / "vault"
    vault.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    async def _no_weather(config):  # type: ignore[no-untyped-def]
        return "*Weather data unavailable.*"

    monkeypatch.setattr(daemon_mod, "fetch_and_format", _no_weather)

    called: list = []

    async def _should_not_run(watches, state_path):  # type: ignore[no-untyped-def]
        called.append(True)
        return "- should never appear"

    monkeypatch.setattr(daemon_mod, "check_and_format_watches", _should_not_run)

    config = BriefConfig(
        vault_path=str(vault),
        state=StateConfig(path=str(data_dir / "brief_state.json")),
        # watches default: empty list — feature off.
    )
    state_mgr = StateManager(config.state.path)
    rel_path = await daemon_mod.generate_brief(config, state_mgr)

    assert rel_path is not None
    content = (vault / rel_path).read_text(encoding="utf-8")
    assert "Watch Items" not in content
    assert called == []  # the check never even ran


# ---------------------------------------------------------------------------
# State — schema tolerance, corruption, atomicity round-trip
# ---------------------------------------------------------------------------


def test_state_from_dict_applies_schema_tolerance_filter() -> None:
    """● Load-time schema-tolerance contract (CLAUDE.md): unknown fields
    from a newer/older writer are dropped, known fields survive."""
    st = WatchItemState.from_dict({
        "last_state": "merged",
        "last_seen_tag": "v1.0.2",
        "matched_tag": "",
        "last_checked": "2026-06-11T00:00:00+00:00",
        "future_field_from_v2": {"nested": True},
        "another_unknown": 42,
    })
    assert st.last_state == "merged"
    assert st.last_seen_tag == "v1.0.2"
    assert not hasattr(st, "future_field_from_v2")


def test_load_state_tolerates_unknown_keys_and_shapes(tmp_path) -> None:
    path = tmp_path / "s.json"
    path.write_text(json.dumps({
        "items": {
            "good": {"last_state": "open", "junk_key": 1},
            "not-a-dict": "garbage",
        },
        "unknown_top_level": [1, 2, 3],
    }), encoding="utf-8")
    states = load_watch_state(path)
    assert states["good"].last_state == "open"
    assert "not-a-dict" not in states  # non-dict entry dropped, no crash


def test_load_state_corrupt_json_yields_fresh_with_warning(tmp_path) -> None:
    path = tmp_path / "s.json"
    path.write_text("{not json", encoding="utf-8")
    with structlog.testing.capture_logs() as captured:
        states = load_watch_state(path)
    assert states == {}
    warns = [c for c in captured
             if c.get("event") == "brief.watches_state_load_failed"]
    assert len(warns) == 1


def test_load_state_invalid_utf8_yields_fresh_with_warning(tmp_path) -> None:
    """Review nit a3: UnicodeDecodeError (a ValueError, not OSError or
    JSONDecodeError) from a binary-corrupted state file must degrade to
    fresh-baseline + warning here — not escalate to the daemon guard."""
    path = tmp_path / "s.json"
    path.write_bytes(b"\xff\xfe\x00garbage\x80")
    with structlog.testing.capture_logs() as captured:
        states = load_watch_state(path)
    assert states == {}
    warns = [c for c in captured
             if c.get("event") == "brief.watches_state_load_failed"]
    assert len(warns) == 1
    assert warns[0]["error_type"] == "UnicodeDecodeError"


def test_state_round_trip(tmp_path) -> None:
    path = tmp_path / "nested" / "s.json"  # parent auto-created
    save_watch_state(path, {
        "a": WatchItemState(last_state="open"),
        "b": WatchItemState(last_seen_tag="v2", matched_tag="v2"),
    })
    states = load_watch_state(path)
    assert states["a"].last_state == "open"
    assert states["b"].matched_tag == "v2"
    assert not path.with_suffix(".json.tmp").exists()  # atomic rename completed


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------


def test_load_from_unified_parses_watches() -> None:
    from alfred.brief.config import load_from_unified

    cfg = load_from_unified({
        "brief": {
            "watches": [
                {
                    "id": "pr-watch", "label": "a PR", "type": "github_pr",
                    "repo": "o/r", "number": 7,
                    "on_flip_note": "act on it",
                },
                {
                    "id": "rel-watch", "type": "github_release_mention",
                    "repo": "o/r", "pattern": "x", "baseline_tag": "v1",
                },
                "not-a-dict-entry",
            ],
        },
    })
    assert len(cfg.watches) == 2
    assert cfg.watches[0].number == 7
    assert cfg.watches[0].on_flip_note == "act on it"
    assert cfg.watches[1].baseline_tag == "v1"


def test_load_from_unified_defaults_to_no_watches() -> None:
    from alfred.brief.config import load_from_unified

    cfg = load_from_unified({"brief": {}})
    assert cfg.watches == []


def test_loader_warns_on_non_dict_entry_and_missing_id() -> None:
    """Review nits a1/a2: a YAML typo entry and an id-less watch both
    surface as load-time warnings instead of silent tolerance."""
    from alfred.brief.config import load_from_unified

    with structlog.testing.capture_logs() as captured:
        cfg = load_from_unified({
            "brief": {
                "watches": [
                    "oops-a-string",
                    {"label": "no id set", "type": "github_pr",
                     "repo": "o/r", "number": 7},
                ],
            },
        })
    assert len(cfg.watches) == 1
    invalid = [c for c in captured
               if c.get("event") == "brief.watch_entry_invalid"]
    assert len(invalid) == 1
    assert invalid[0]["reason"] == "entry_type_str"
    missing = [c for c in captured
               if c.get("event") == "brief.watch_missing_id"]
    assert len(missing) == 1
    assert missing[0]["fallback_key"] == "github_pr:o/r:7"


def test_loader_warns_on_duplicate_state_key() -> None:
    from alfred.brief.config import load_from_unified

    with structlog.testing.capture_logs() as captured:
        cfg = load_from_unified({
            "brief": {
                "watches": [
                    {"id": "same-id", "type": "github_pr",
                     "repo": "o/r", "number": 1},
                    {"id": "same-id", "type": "github_pr",
                     "repo": "o/r", "number": 2},
                ],
            },
        })
    assert len(cfg.watches) == 2  # both kept — warning, not a drop
    dupes = [c for c in captured
             if c.get("event") == "brief.watch_duplicate_state_key"]
    assert len(dupes) == 1
    assert dupes[0]["key"] == "same-id"


# ---------------------------------------------------------------------------
# State-key fallback (review nit a1 — the collision bug-of-record)
# ---------------------------------------------------------------------------


def test_state_key_explicit_id_wins() -> None:
    assert _release_item(id="my-id").state_key() == "my-id"


def test_state_key_release_fallback_includes_pattern_hash() -> None:
    """● The bug-of-record: two id-less release watches on ONE repo used
    to share the key ``type:repo:0`` — the first match latched BOTH and
    the second pattern could never fire. Distinct patterns must resolve
    to distinct keys."""
    a = _release_item(id="", pattern="newarch")
    b = _release_item(id="", pattern="totally-different")
    assert a.state_key() != b.state_key()
    assert a.repo == b.repo  # same repo — the collision scenario
    # Same pattern still resolves stably (idempotent key).
    assert a.state_key() == _release_item(id="", pattern="newarch").state_key()


def test_state_key_pr_fallback_unchanged() -> None:
    # PR fallbacks keep the legacy shape — ``number`` already
    # disambiguates them, and changing the shape would orphan any
    # existing id-less PR watch state.
    assert _pr_item(id="").state_key() == "github_pr:example-org/example-repo:123"


async def test_two_idless_release_watches_track_independently(tmp_path, monkeypatch) -> None:
    """● End-to-end pin for the collision: watch A matches and latches;
    watch B (different pattern, same repo, also id-less) must NOT
    inherit A's latch — it keeps checking and reports no-match."""
    state_path = tmp_path / "s.json"
    calls: list = []
    _patch_releases(monkeypatch, _RELEASES_WITH_MATCH, calls=calls)
    watch_a = _release_item(id="", pattern="newarch")          # matches v1.1.0
    watch_b = _release_item(id="", pattern="never-mentioned")  # matches nothing

    out1 = await check_and_format_watches([watch_a, watch_b], state_path)
    lines1 = out1.splitlines()
    assert FLIP_MARKER in lines1[0]          # A flipped
    assert "no matching release" in lines1[1]  # B did not inherit the latch

    out2 = await check_and_format_watches([watch_a, watch_b], state_path)
    lines2 = out2.splitlines()
    assert DONE_TAIL in lines2[0]            # A latched terminal (no fetch)
    assert "no matching release" in lines2[1]  # B still live + still fetching
    # Fetch count: run1 = A + B, run2 = B only (A latched) → 3 total.
    assert len(calls) == 3
