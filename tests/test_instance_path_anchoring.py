"""#74 batch 1 — the four allowlisted debris leakers, anchored per-instance.

Each was a cwd-relative DEFAULT applied when a config omits the field: tests
set ``logging.dir`` to a tmp dir, the module ignored it and resolved against
the process cwd, and the suite wrote into the repo tree. On the box the same
defect is a cross-instance collision — one WorkingDirectory shared by Salem,
KAL-LE, Hypatia and VERA, so "./data/x" is ONE file for all four (#53's shape,
and the 2026-07-31 feed-store incident for real).

Every fix here has the same two obligations, pinned per leaker:

  * **byte-identity** — Salem's ``logging.dir`` IS ``./data``, so the derived
    default must reproduce the exact string the literal produced. That is what
    makes this a zero-migration change: no file moves on the box.
  * **distinctness** — two instances must resolve DIFFERENT paths, which is
    the property the literal did not have.

Tests run unconditionally per ``feedback_regression_pin_unconditional.md``.
"""
from __future__ import annotations

# A KAL-LE-shaped absolute data dir — a second, co-located instance.
_KALLE_DATA_DIR = "/home/andrew/.alfred/kalle/data"
# Salem's data dir, verified on the box 2026-08-11: logging.dir is the
# cwd-relative "./data" (WorkingDirectory makes it /data/algernon/alfred/data).
_SALEM_DATA_DIR = "./data"


# ===========================================================================
# Leaker 1 — scribe.input_dir  (data/scribe/scribe/{negation_candidates,
#            notegen_edit}.jsonl + their two lock files)
# ===========================================================================

def _scribe(raw: dict) -> object:
    from alfred.scribe.config import load_from_unified

    return load_from_unified(raw)


def test_scribe_input_dir_anchors_on_logging_dir() -> None:
    assert _scribe({"logging": {"dir": _KALLE_DATA_DIR}}).input_dir == (
        f"{_KALLE_DATA_DIR}/scribe/inbox"
    )


def test_scribe_salem_default_is_byte_identical() -> None:
    # The pre-#74 literal was exactly "./data/scribe/inbox".
    assert _scribe({"logging": {"dir": _SALEM_DATA_DIR}}).input_dir == "./data/scribe/inbox"


def test_scribe_default_unchanged_when_no_logging_block() -> None:
    # Minimal fixtures (and any config without a logging block) keep the old
    # value — the retrofit must not change behaviour where there is no anchor.
    assert _scribe({}).input_dir == "./data/scribe/inbox"
    assert _scribe({"scribe": {"mode": "synthetic"}}).input_dir == "./data/scribe/inbox"


def test_scribe_blank_logging_dir_does_not_root_anchor() -> None:
    # The hole the shared resolver closes: a blank dir must NOT produce the
    # root-anchored "/scribe/inbox".
    assert _scribe({"logging": {"dir": "   "}}).input_dir == "./data/scribe/inbox"


def test_scribe_explicit_input_dir_wins() -> None:
    cfg = _scribe({
        "logging": {"dir": _KALLE_DATA_DIR},
        "scribe": {"input_dir": "/x/inbox"},
    })
    assert cfg.input_dir == "/x/inbox"


def test_scribe_input_dir_is_instance_scoped_and_distinct() -> None:
    salem = _scribe({"logging": {"dir": _SALEM_DATA_DIR}}).input_dir
    kalle = _scribe({"logging": {"dir": _KALLE_DATA_DIR}}).input_dir
    # Mutation check — revert the default to the cwd-relative literal and both
    # collapse to "./data/scribe/inbox", reddening this.
    assert salem != kalle


def test_scribe_derived_default_carries_the_resolvers_with_it(tmp_path) -> None:
    """The write sinks — the files that actually leaked — follow the anchor.

    ``resolve_candidates_dir`` / ``resolve_notegen_feedback_dir`` derive from
    ``input_dir``, so anchoring the default is what moves the four leaked files
    out of the repo tree and into the instance's data dir.
    """
    from alfred.scribe.negation_suppression import resolve_candidates_dir
    from alfred.scribe.notegen_feedback import resolve_notegen_feedback_dir

    cfg = _scribe({"logging": {"dir": str(tmp_path)}})
    for resolved in (resolve_candidates_dir(cfg), resolve_notegen_feedback_dir(cfg)):
        # The load-bearing assertion: the sink is under the configured dir, so
        # nothing lands in the cwd.
        assert str(resolved).startswith(str(tmp_path))


def test_scribe_doubling_is_preserved_deliberately(tmp_path) -> None:
    """BATCH 1 DOES NOT FIX THE DOUBLING — this pin says so out loud.

    The resolvers compute ``<input_dir>.parent / "scribe"`` on the contract
    that ``input_dir`` is ``<DATA>/inbox``. The default's parent is already
    ``<DATA>/scribe``, so they land on ``<DATA>/scribe/scribe``. Anchoring the
    default (batch 1) moves the whole structure under the configured data dir
    without re-siting anything inside it, which is the point: the doubling is
    preserved EXACTLY, so this commit cannot move an operator's files.

    Batch 2 re-sites it and FLIPS this pin — that is the intended signal, not
    a regression. Until then the doubling is harmless: it doubles inside
    whatever data dir is configured, never in the cwd.
    """
    from alfred.scribe.negation_suppression import resolve_candidates_dir

    cfg = _scribe({"logging": {"dir": str(tmp_path)}})
    assert resolve_candidates_dir(cfg) == tmp_path / "scribe" / "scribe"


# ===========================================================================
# Leaker 2 — transport.canonical.{audit_log_path, proposals_path}
#            (data/canonical_audit.jsonl)
# ===========================================================================
#
# This one did NOT leak through the load path. The route-smoke tests build a
# bare ``TransportConfig(...)`` with no canonical block at all, so the leak
# came from the DATACLASS default — the #75 shape, where anchoring the loader
# alone would have left the writer aimed at the cwd and every pin still green.
# Hence two independent obligations, pinned separately below: the dataclass
# default is EMPTY (= disabled, the contract append_audit/append_proposal
# already had), and the LOADER derives a real per-instance path.

def _transport(raw: dict) -> object:
    from alfred.transport.config import load_from_unified

    return load_from_unified(raw)


def test_transport_canonical_dataclass_defaults_are_empty() -> None:
    """The half that actually stopped the leak.

    Mutation check — restore either literal and the route-smoke files write
    data/canonical_audit.jsonl into the repo tree again.
    """
    from alfred.transport.config import CanonicalConfig, TransportConfig

    assert CanonicalConfig().audit_log_path == ""
    assert CanonicalConfig().proposals_path == ""
    # And through the parent's default_factory — the exact shape the smoke
    # tests construct.
    assert TransportConfig().canonical.audit_log_path == ""
    assert TransportConfig().canonical.proposals_path == ""


def test_transport_empty_path_disables_rather_than_writing_to_cwd() -> None:
    # The empty default is only safe because both writers treat it as
    # disabled. Pin that contract — it is what makes "" the right default
    # rather than a silent cwd write.
    from alfred.transport.canonical_audit import append_audit
    from alfred.transport.canonical_proposals import append_proposal

    append_audit("", peer="p", record_type="person", name="x",
                 requested=[], granted=[], denied=[])
    append_proposal("", object())  # never dereferenced — returns on the path


def test_transport_audit_disabled_is_logged_not_silent() -> None:
    """Silence is ambiguous: an audit writing nothing must say so once.

    Per ``feedback_intentionally_left_blank.md`` — before this, an empty path
    was a bare ``return``, so "audit off" and "audit broken" looked identical
    in a log. Latched once per process, so the latch is reset here.
    """
    import structlog

    from alfred.transport import canonical_audit

    canonical_audit._audit_disabled_logged = False
    try:
        with structlog.testing.capture_logs() as captured:
            canonical_audit.append_audit(
                "", peer="p", record_type="person", name="x",
                requested=[], granted=[], denied=[])
            # Second call must NOT re-log — the latch is the anti-spam half.
            canonical_audit.append_audit(
                "", peer="p", record_type="person", name="y",
                requested=[], granted=[], denied=[])
    finally:
        canonical_audit._audit_disabled_logged = False

    matches = [c for c in captured
               if c.get("event") == "transport.canonical.audit_disabled"]
    assert len(matches) == 1
    assert matches[0]["reason"] == "empty_audit_log_path"


def test_transport_loader_derives_real_paths_when_block_absent() -> None:
    # A config with NO canonical block must still get working paths — the
    # pre-#74 behaviour. Building the block unconditionally is what preserves
    # it; leaving it to default_factory would silently disable the audit.
    cfg = _transport({"logging": {"dir": _KALLE_DATA_DIR}, "transport": {}})
    assert cfg.canonical.audit_log_path == f"{_KALLE_DATA_DIR}/canonical_audit.jsonl"
    assert cfg.canonical.proposals_path == f"{_KALLE_DATA_DIR}/canonical_proposals.jsonl"


def test_transport_salem_defaults_are_byte_identical() -> None:
    # Salem's config sets audit_log_path explicitly to this exact string, and
    # sets no proposals_path at all — so BOTH must resolve to the old literals.
    cfg = _transport({"logging": {"dir": _SALEM_DATA_DIR}, "transport": {}})
    assert cfg.canonical.audit_log_path == "./data/canonical_audit.jsonl"
    assert cfg.canonical.proposals_path == "./data/canonical_proposals.jsonl"


def test_transport_defaults_unchanged_when_no_logging_block() -> None:
    cfg = _transport({"transport": {}})
    assert cfg.canonical.audit_log_path == "./data/canonical_audit.jsonl"
    assert cfg.canonical.proposals_path == "./data/canonical_proposals.jsonl"


def test_transport_explicit_paths_win() -> None:
    cfg = _transport({
        "logging": {"dir": _KALLE_DATA_DIR},
        "transport": {"canonical": {
            "audit_log_path": "/x/audit.jsonl",
            "proposals_path": "/x/props.jsonl",
        }},
    })
    assert cfg.canonical.audit_log_path == "/x/audit.jsonl"
    assert cfg.canonical.proposals_path == "/x/props.jsonl"


def test_transport_canonical_paths_are_distinct_across_instances() -> None:
    salem = _transport({"logging": {"dir": _SALEM_DATA_DIR}, "transport": {}})
    kalle = _transport({"logging": {"dir": _KALLE_DATA_DIR}, "transport": {}})
    assert salem.canonical.audit_log_path != kalle.canonical.audit_log_path
    assert salem.canonical.proposals_path != kalle.canonical.proposals_path


def test_transport_cli_resolver_matches_the_loader() -> None:
    # ``resolve_audit_path`` is the CLI's separate read of the same value. If
    # it kept its own literal the CLI would tail a different file than the
    # daemon writes — pin the two agree, derived and explicit.
    from alfred.transport.canonical_audit import resolve_audit_path

    raw = {"logging": {"dir": _KALLE_DATA_DIR}, "transport": {}}
    assert resolve_audit_path(raw) == _transport(raw).canonical.audit_log_path

    raw_explicit = {
        "logging": {"dir": _KALLE_DATA_DIR},
        "transport": {"canonical": {"audit_log_path": "/x/audit.jsonl"}},
    }
    assert resolve_audit_path(raw_explicit) == "/x/audit.jsonl"
    # And the legacy fallback is unchanged for a config with no logging block.
    assert resolve_audit_path({}) == "./data/canonical_audit.jsonl"


# ===========================================================================
# Leaker 3 — feed store  (data/feed_items.jsonl + data/feed_items.lock)
# ===========================================================================
#
# The resolver was already per-instance-correct; what leaked was its LAST
# RUNG. When a config named no data dir at all it fell back to a cwd-relative
# "./data" — and the daily-sync fire path (daemon.py's feed block) loads this
# config from a raw dict that, in tests, carries no logging block. So the
# leak came through the LOADER here, unlike leaker 2. The bare-FeedConfig()
# path (BriefConfig's default_factory) is the same defect's other half; both
# close on one mechanism, FeedConfig.__post_init__.

def _feed(raw: dict) -> object:
    from alfred.feed.config import load_from_unified

    return load_from_unified(raw)


def test_feed_unanchored_config_writes_nowhere() -> None:
    """The fix: no data dir named => empty path AND the feed switches off.

    Mutation check — restore the "./data" last rung and
    tests/test_daily_sync/test_fire_once_ticket_notify.py writes
    data/feed_items.jsonl + .lock into the repo tree again.
    """
    cfg = _feed({})
    assert cfg.store_path == ""
    assert cfg.enabled is False


def test_feed_dataclass_default_is_also_off() -> None:
    # The other entry point: BriefConfig's default_factory never touches the
    # loader, so the dataclass must be safe on its own.
    from alfred.brief.config import BriefConfig
    from alfred.feed.config import FeedConfig

    assert FeedConfig().store_path == ""
    assert FeedConfig().enabled is False
    assert BriefConfig(vault_path="/x").feed.enabled is False


def test_feed_disabled_for_lack_of_path_is_logged_not_silent() -> None:
    """An enabled-looking feed that writes nothing must say why, once."""
    import structlog

    from alfred.feed import config as feed_config

    feed_config._unanchored_logged = False
    try:
        with structlog.testing.capture_logs() as captured:
            feed_config.FeedConfig()
            feed_config.FeedConfig()  # latched — must not re-log
    finally:
        feed_config._unanchored_logged = False

    matches = [c for c in captured
               if c.get("event") == "feed.config.disabled_no_store_path"]
    assert len(matches) == 1
    assert matches[0]["reason"] == "unanchored_store_path"


def test_feed_explicit_path_keeps_the_feed_on() -> None:
    # The coercion must fire ONLY on a missing path — an explicitly configured
    # feed stays on regardless of whether logging.dir is present.
    cfg = _feed({"feed": {"store_path": "/x/feed.jsonl"}})
    assert cfg.enabled is True
    assert cfg.store_path == "/x/feed.jsonl"


def test_feed_salem_default_is_byte_identical() -> None:
    cfg = _feed({"logging": {"dir": _SALEM_DATA_DIR}})
    assert cfg.store_path == "./data/feed_items.jsonl"
    assert cfg.enabled is True


def test_feed_paths_are_distinct_across_instances() -> None:
    salem = _feed({"logging": {"dir": _SALEM_DATA_DIR}}).store_path
    kalle = _feed({"logging": {"dir": _KALLE_DATA_DIR}}).store_path
    assert kalle == f"{_KALLE_DATA_DIR}/feed_items.jsonl"
    assert salem != kalle


# ===========================================================================
# Leaker 4 — voice-calibration telemetry  (data/voice_calibration/events.jsonl)
# ===========================================================================
#
# The third distinct source in this batch. Here the CONFIG value was only half
# the story: four hardcoded "./data/voice_calibration" fallbacks sat at the two
# mount sites in routes_voice.py — each site had it twice, as a getattr default
# AND as an or-fallback — so they would have won over any config value that was
# empty or absent. Anchoring web/config.py alone would have left all four
# standing and the file still appearing. Both halves are pinned.

def _web(raw: dict) -> object:
    from alfred.web.config import load_from_unified

    return load_from_unified(raw)


def _hold(raw: dict) -> object:
    return _web(raw).voice.stt.endpoint_hold


def test_voice_telemetry_dir_anchors_on_logging_dir() -> None:
    raw = {"logging": {"dir": _KALLE_DATA_DIR}, "web": {"voice": {"stt": {}}}}
    assert _hold(raw).telemetry_dir == f"{_KALLE_DATA_DIR}/voice_calibration"


def test_voice_telemetry_dir_salem_is_byte_identical() -> None:
    raw = {"logging": {"dir": _SALEM_DATA_DIR}, "web": {"voice": {"stt": {}}}}
    assert _hold(raw).telemetry_dir == "./data/voice_calibration"


def test_voice_telemetry_dir_derives_through_absent_nested_blocks() -> None:
    # The derivation must survive every level of the nesting being absent —
    # web -> voice -> stt -> endpoint_hold. Each builder has an isinstance
    # guard that returns a default-constructed config, and each had to be
    # taught to carry data_dir through; miss one and the anchor is silently
    # dropped for exactly the configs that omit the block.
    for raw in (
        {"logging": {"dir": _KALLE_DATA_DIR}},
        {"logging": {"dir": _KALLE_DATA_DIR}, "web": {}},
        {"logging": {"dir": _KALLE_DATA_DIR}, "web": {"voice": {}}},
        {"logging": {"dir": _KALLE_DATA_DIR}, "web": {"voice": {"stt": {}}}},
        {"logging": {"dir": _KALLE_DATA_DIR},
         "web": {"voice": {"stt": {"endpoint_hold": {}}}}},
    ):
        assert _hold(raw).telemetry_dir == f"{_KALLE_DATA_DIR}/voice_calibration", raw


def test_voice_telemetry_dataclass_default_is_empty() -> None:
    from alfred.web.config import WebVoiceEndpointHoldConfig

    assert WebVoiceEndpointHoldConfig().telemetry_dir == ""


def test_voice_telemetry_unanchored_is_off_not_cwd() -> None:
    # No logging.dir anywhere: the sink is OFF, not pointed at the cwd. Both
    # consumers in routes_voice skip telemetry on a falsy dir.
    assert _hold({"web": {"voice": {"stt": {}}}}).telemetry_dir == ""


def test_voice_explicit_telemetry_dir_wins() -> None:
    raw = {
        "logging": {"dir": _KALLE_DATA_DIR},
        "web": {"voice": {"stt": {"endpoint_hold": {"telemetry_dir": "/x/cal"}}}},
    }
    assert _hold(raw).telemetry_dir == "/x/cal"


def test_voice_telemetry_dirs_are_distinct_across_instances() -> None:
    salem = _hold({"logging": {"dir": _SALEM_DATA_DIR}}).telemetry_dir
    kalle = _hold({"logging": {"dir": _KALLE_DATA_DIR}}).telemetry_dir
    assert salem != kalle


def test_no_cwd_relative_voice_calibration_literal_survives_at_the_call_sites() -> None:
    """The four fallbacks are GONE from routes_voice.py — source-level pin.

    This is a text assertion on purpose. The four literals were unreachable
    from a normal config load (the config value shadowed them), so no
    behavioural pin distinguishes "removed" from "still there but not taken
    today" — the exact shape that let them sit unnoticed. A grep is the honest
    test for a dead-but-loaded fallback.
    """
    from pathlib import Path

    import alfred.web.routes_voice as rv

    source = Path(rv.__file__).read_text(encoding="utf-8")
    assert '"./data/voice_calibration"' not in source
    assert "'./data/voice_calibration'" not in source


# ===========================================================================
# Leaker 1, second writer — the scribe eval harness's default config
# ===========================================================================
#
# Anchoring scribe.input_dir (above) moved the notegen sink but NOT the
# negation-candidate sink, because a SECOND writer reaches it through a config
# that names no data dir: alfred.scribe.eval.harness._default_config. The
# full-suite run is what surfaced it — the per-file subset used to probe
# leaker 1 never touched the eval path. Fourth distinct source in one batch.

def test_scribe_eval_default_config_spools_outside_the_repo(tmp_path) -> None:
    """``alfred scribe eval`` must not write into the operator's real spool.

    This is a correctness pin, not just a debris pin. The candidate spool feeds
    a SELF-CORRECTING loop — morning-review approves rows out of it into a
    learned suppression glossary — so scoring synthetic eval fixtures through a
    config resolving to the live spool would seed that glossary with
    eval-fixture vocabulary. The fixture-seam CLI path (`alfred scribe eval`
    with no --real) reaches this default, so on the box it wrote into
    <data>/scribe/scribe/.
    """
    from pathlib import Path

    from alfred.scribe.eval.harness import _default_config
    from alfred.scribe.negation_suppression import resolve_candidates_dir

    cand_dir = resolve_candidates_dir(_default_config())
    repo_root = Path(__file__).resolve().parent.parent
    # Not under the repo (the debris half) …
    assert repo_root not in cand_dir.resolve().parents
    assert cand_dir.resolve() != repo_root
    # … and absolute, so it cannot follow the process cwd around (the half a
    # debris guard run from one directory would never notice).
    assert cand_dir.is_absolute()


def test_scribe_eval_scratch_dir_is_stable_within_a_process() -> None:
    # Two configs must share one scratch dir — a fresh mkdtemp per call would
    # litter the temp dir once per scored case.
    from alfred.scribe.eval.harness import _default_config

    assert _default_config().input_dir == _default_config().input_dir
