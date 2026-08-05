"""#54 — the learned-speech-vocabulary review surface + its operator CLI.

Covers the three joins the feature actually rests on: the config DERIVE (this
section must read the same files the capture side writes), the section RENDER
(including both intentionally-left-blank shapes), and the CLI decision
round-trip (approve → biasing, reject → retired, neither → re-proposed forever).

Drives the real parser + handler for the CLI half, not the helpers underneath —
a decision surface that is green in unit tests and unreachable from the command
line is the failure this whole task exists to stop repeating.
"""

from __future__ import annotations

import argparse
import json
from datetime import date

import structlog
import yaml

from alfred.cli import _cmd_stt_vocab, build_parser
from alfred.daily_sync.config import load_from_unified
from alfred.daily_sync.stt_vocab_section import (
    peek_last_batch_count,
    register,
    stt_vocab_section,
)
from alfred.daily_sync import assembler
from alfred.telegram.config import (
    DEFAULT_STT_VOCAB_CORPUS_PATH,
    DEFAULT_STT_VOCAB_DECIDED_PATH,
    DEFAULT_STT_VOCAB_TERMS,
)

TODAY = date(2026, 8, 5)

# Two corrections of the same term — the operator repeating himself, which is
# exactly what MIN_CORRECTION_COUNT=2 is tuned to notice.
TRACTOR_PAIRS = [
    {"transcript": "clean the chicken tracker", "sent": "clean the chicken tractor"},
    {"transcript": "the chicken tracker again", "sent": "the chicken tractor again"},
]


def _write_corpus(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps({**r, "instance": "salem", "at": "2026-08-05T10:00:00+00:00"}) + "\n"
                for r in rows),
        encoding="utf-8",
    )


def _raw(tmp_path, *, enabled=True, terms=("Algernon", "KAL-LE"), extra=None):
    """A unified config whose talker.stt points at tmp_path stores."""
    raw = {
        "talker": {
            "stt": {
                "vocab_terms": list(terms),
                "vocab_learning_enabled": True,
                "vocab_corpus_path": str(tmp_path / "corrections.jsonl"),
                "vocab_decided_path": str(tmp_path / "decided.jsonl"),
            },
        },
        "daily_sync": {"enabled": True, "stt_vocab": {"enabled": enabled}},
    }
    if extra:
        raw["daily_sync"]["stt_vocab"].update(extra)
    return raw


def _cfg(tmp_path, **kw):
    return load_from_unified(_raw(tmp_path, **kw))


# ---------------------------------------------------------------------------
# The config derive — one source for a path with a writer and a reader
# ---------------------------------------------------------------------------


def test_derives_paths_and_terms_from_the_talker_config(tmp_path):
    cfg = _cfg(tmp_path).stt_vocab
    assert cfg.vocab_corpus_path == str(tmp_path / "corrections.jsonl")
    assert cfg.vocab_decided_path == str(tmp_path / "decided.jsonl")
    assert cfg.vocab_terms == ["Algernon", "KAL-LE"]


def test_absent_talker_block_falls_back_to_the_shared_constants(tmp_path):
    # The same constants the talker itself defaults to, so the no-override case
    # still agrees rather than silently reading a different file.
    cfg = load_from_unified({"daily_sync": {"enabled": True, "stt_vocab": {"enabled": True}}})
    assert cfg.stt_vocab.vocab_corpus_path == DEFAULT_STT_VOCAB_CORPUS_PATH
    assert cfg.stt_vocab.vocab_decided_path == DEFAULT_STT_VOCAB_DECIDED_PATH
    assert cfg.stt_vocab.vocab_terms == list(DEFAULT_STT_VOCAB_TERMS)


def test_explicit_daily_sync_override_wins_over_the_derive(tmp_path):
    cfg = _cfg(tmp_path, extra={"vocab_corpus_path": str(tmp_path / "split.jsonl")}).stt_vocab
    assert cfg.vocab_corpus_path == str(tmp_path / "split.jsonl")
    # The un-overridden fields still track the talker (an intentional split is
    # per-field, not all-or-nothing).
    assert cfg.vocab_decided_path == str(tmp_path / "decided.jsonl")


def test_derive_survives_a_config_with_no_instance_block(tmp_path):
    # REGRESSION. The first cut built a whole TalkerConfig to read three fields;
    # `telegram.config.load_from_unified` requires `instance.name`, so a config
    # carrying talker.stt but no instance block raised — and the fallback then
    # pointed this section at the DEFAULT paths while capture wrote the
    # overridden ones. A read side and a write side disagreeing in silence.
    raw = _raw(tmp_path)
    assert "instance" not in raw
    cfg = load_from_unified(raw).stt_vocab
    assert cfg.vocab_corpus_path == str(tmp_path / "corrections.jsonl")


def test_env_substitution_still_applies_to_a_derived_path(tmp_path, monkeypatch):
    # The derive reads `raw` AFTER _substitute_env, so a ${VAR} path resolves.
    monkeypatch.setenv("STT_TEST_DIR", str(tmp_path))
    raw = _raw(tmp_path)
    raw["talker"]["stt"]["vocab_corpus_path"] = "${STT_TEST_DIR}/from-env.jsonl"
    cfg = load_from_unified(raw).stt_vocab
    assert cfg.vocab_corpus_path == f"{tmp_path}/from-env.jsonl"


# ---------------------------------------------------------------------------
# The section render
# ---------------------------------------------------------------------------


def test_disabled_omits_the_section_entirely(tmp_path):
    assert stt_vocab_section(_cfg(tmp_path, enabled=False), TODAY) is None


def test_ilb_when_nothing_recorded_names_the_capture_flag(tmp_path):
    # Enabled but empty must be explicable. The overwhelmingly likely cause is
    # that capture was never switched on, so the card says where to look rather
    # than leaving a blank that reads as breakage.
    out = stt_vocab_section(_cfg(tmp_path), TODAY)
    assert "## Speech vocabulary review" in out
    assert "No speech corrections recorded yet" in out
    assert "vocab_learning_enabled" in out
    # The biasing list rides even the empty state — the operator can still see
    # what is in force.
    assert "Currently biasing 2 terms (2 shipped + 0 learned" in out


def test_ilb_with_pairs_but_no_proposals_names_all_three_reasons(tmp_path):
    # Below threshold: one correction is a typo, not a pattern.
    _write_corpus(tmp_path / "corrections.jsonl",
                  [{"transcript": "a wrong word", "sent": "a run word"}])
    out = stt_vocab_section(_cfg(tmp_path), TODAY)
    assert "1 correction pair recorded" in out
    # Deliberately does NOT assert one reason: "not repeated yet" would be a lie
    # about a term the operator approved this morning.
    assert "already approved" in out and "already rejected" in out
    assert "not yet corrected 2×" in out


def test_a_recurring_correction_surfaces_with_its_count_and_evidence(tmp_path):
    _write_corpus(tmp_path / "corrections.jsonl", TRACTOR_PAIRS)
    out = stt_vocab_section(_cfg(tmp_path), TODAY, start_index=7)
    assert "## Speech vocabulary review (1 proposal)" in out
    # Numbered from the assembler's start_index so Daily Sync numbering stays
    # continuous across sections.
    assert "7. “tractor” — corrected 2×" in out
    # The evidence is what makes the term recognisable at a glance.
    assert "heard “tracker”" in out
    assert 'alfred stt-vocab approve "<term>"' in out


def test_the_batch_count_hook_reports_the_surfaced_items(tmp_path):
    _write_corpus(tmp_path / "corrections.jsonl", TRACTOR_PAIRS)
    stt_vocab_section(_cfg(tmp_path), TODAY)
    assert peek_last_batch_count() == 1


def test_a_term_already_shipped_is_never_proposed(tmp_path):
    # It is already biasing; a correction on it means the bias is not ENOUGH,
    # which is a different problem and must not read as this one.
    _write_corpus(tmp_path / "corrections.jsonl", TRACTOR_PAIRS)
    out = stt_vocab_section(_cfg(tmp_path, terms=("Algernon", "tractor")), TODAY)
    assert "“tractor” — corrected 2×" not in out


def test_a_term_the_operator_already_ruled_on_never_re_surfaces(tmp_path):
    # The groundhog bug `routine.match_calibration.filter_pending_for_review`
    # had to fix once: an append-only sink only grows, so a declined suggestion
    # came back every morning forever. The section must consult the decided
    # store — for BOTH verdicts, since an approved term is already biasing and a
    # rejected one was answered.
    from alfred.telegram.stt_vocab_learning import (
        DECISION_APPROVE, DECISION_REJECT, VocabDecision, append_decision,
    )
    _write_corpus(tmp_path / "corrections.jsonl", TRACTOR_PAIRS)
    decided = tmp_path / "decided.jsonl"

    append_decision(decided, VocabDecision(type=DECISION_REJECT, term="tractor"))
    assert "“tractor” — corrected 2×" not in stt_vocab_section(_cfg(tmp_path), TODAY)

    append_decision(decided, VocabDecision(type=DECISION_APPROVE, term="tractor"))
    out = stt_vocab_section(_cfg(tmp_path), TODAY)
    assert "“tractor” — corrected 2×" not in out
    # ...and now it is biasing, which the card states rather than leaving the
    # operator to infer from the proposal's absence.
    assert "2 shipped + 1 learned" in out


def test_disabled_clears_a_previously_surfaced_batch(tmp_path):
    # Otherwise the assembler's numbering hook would keep counting items from a
    # section that is no longer rendering.
    _write_corpus(tmp_path / "corrections.jsonl", TRACTOR_PAIRS)
    stt_vocab_section(_cfg(tmp_path), TODAY)
    assert peek_last_batch_count() == 1
    stt_vocab_section(_cfg(tmp_path, enabled=False), TODAY)
    assert peek_last_batch_count() == 0


def test_register_is_idempotent_across_daemon_refires(tmp_path):
    # ``register_provider`` RAISES on a duplicate name and the daemon
    # re-registers on every fire, so the guard is load-bearing, not tidiness.
    # Clears first and after: the registry is module-global, so a test that
    # leaves it dirty silently arms the next one.
    assembler.clear_providers()
    try:
        register()
        register()
        assert "stt_vocab" in assembler.registered_providers()
    finally:
        assembler.clear_providers()


# ---------------------------------------------------------------------------
# Log emission — the operator's grep workflow is part of the contract
# ---------------------------------------------------------------------------


def test_surfaced_emits_a_pinned_log_line(tmp_path):
    _write_corpus(tmp_path / "corrections.jsonl", TRACTOR_PAIRS)
    with structlog.testing.capture_logs() as captured:
        stt_vocab_section(_cfg(tmp_path), TODAY)
    hits = [c for c in captured if c.get("event") == "stt_vocab.surfaced"]
    assert len(hits) == 1
    assert hits[0]["count"] == 1
    assert hits[0]["pairs"] == 2


def test_the_quiet_case_emits_ran_nothing_to_propose(tmp_path):
    with structlog.testing.capture_logs() as captured:
        stt_vocab_section(_cfg(tmp_path), TODAY)
    hits = [c for c in captured if c.get("event") == "stt_vocab.no_proposals"]
    assert len(hits) == 1
    assert hits[0]["detail"] == "ran, nothing to propose"
    assert hits[0]["pairs"] == 0


# ---------------------------------------------------------------------------
# The operator CLI — driven through the real parser + handler
# ---------------------------------------------------------------------------


def _config_file(tmp_path, **kw):
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(_raw(tmp_path, **kw)), encoding="utf-8")
    return str(p)


def _run(tmp_path, argv, **kw):
    """Parse a real argv through build_parser and dispatch the real handler."""
    args = build_parser().parse_args(["--config", _config_file(tmp_path, **kw), *argv])
    _cmd_stt_vocab(args)
    return args


def test_cli_parser_wires_the_subcommand(tmp_path):
    args = build_parser().parse_args(["stt-vocab", "approve", "tractor", "--operator", "andrew"])
    assert args.command == "stt-vocab"
    assert args.stt_vocab_cmd == "approve"
    assert args.term == "tractor"
    assert args.operator == "andrew"


def test_cli_list_says_nothing_pending_rather_than_printing_nothing(tmp_path, capsys):
    _run(tmp_path, ["stt-vocab", "list"])
    out = capsys.readouterr().out
    assert "No speech corrections recorded yet" in out
    assert "Currently biasing 2 terms" in out


def test_cli_list_shows_a_proposal_and_the_full_current_list(tmp_path, capsys):
    _write_corpus(tmp_path / "corrections.jsonl", TRACTOR_PAIRS)
    _run(tmp_path, ["stt-vocab", "list"])
    out = capsys.readouterr().out
    assert "“tractor” — corrected 2×" in out
    # BOTH halves shown on every render: the operator is being asked to grow a
    # list, and a growth decision made without seeing the list is made blind.
    assert "shipped: Algernon, KAL-LE" in out
    assert "learned: (none yet)" in out


def test_cli_approve_makes_the_term_bias_and_shows_the_grown_list(tmp_path, capsys):
    _write_corpus(tmp_path / "corrections.jsonl", TRACTOR_PAIRS)
    _run(tmp_path, ["stt-vocab", "approve", "tractor", "--operator", "andrew"])
    out = capsys.readouterr().out
    payload = json.loads(out[out.index("{"):out.index("}") + 1])
    assert payload["status"] == "approved"
    # Not merely recorded — READ BACK through the seam that actually feeds
    # transcription. "Written to a file" is not the same claim as "is biasing".
    assert payload["biasing_now"] is True
    assert "learned: tractor" in out


def test_cli_approve_then_the_proposal_stops_coming_back(tmp_path, capsys):
    # The groundhog bug routine.match_calibration already had to fix once.
    _write_corpus(tmp_path / "corrections.jsonl", TRACTOR_PAIRS)
    _run(tmp_path, ["stt-vocab", "approve", "tractor", "--operator", "andrew"])
    capsys.readouterr()
    _run(tmp_path, ["stt-vocab", "list"])
    assert "“tractor” — corrected 2×" not in capsys.readouterr().out


def test_cli_reject_suppresses_the_proposal_without_biasing_it(tmp_path, capsys):
    _write_corpus(tmp_path / "corrections.jsonl", TRACTOR_PAIRS)
    _run(tmp_path, ["stt-vocab", "reject", "tractor", "--operator", "andrew"])
    out = capsys.readouterr().out
    assert '"status": "rejected"' in out
    assert "learned: (none yet)" in out
    _run(tmp_path, ["stt-vocab", "list"])
    assert "“tractor” — corrected 2×" not in capsys.readouterr().out


def test_cli_reject_retires_a_term_the_operator_once_approved(tmp_path, capsys):
    # LATER-WINS: he can change his mind without editing a file.
    _write_corpus(tmp_path / "corrections.jsonl", TRACTOR_PAIRS)
    _run(tmp_path, ["stt-vocab", "approve", "tractor", "--operator", "andrew"])
    capsys.readouterr()
    _run(tmp_path, ["stt-vocab", "reject", "tractor", "--operator", "andrew"])
    out = capsys.readouterr().out
    assert '"status": "rejected"' in out
    assert "learned: (none yet)" in out


def test_cli_repeat_approve_records_no_duplicate_decision(tmp_path, capsys):
    _write_corpus(tmp_path / "corrections.jsonl", TRACTOR_PAIRS)
    _run(tmp_path, ["stt-vocab", "approve", "tractor", "--operator", "andrew"])
    _run(tmp_path, ["stt-vocab", "approve", "tractor", "--operator", "andrew"])
    out = capsys.readouterr().out
    assert '"status": "unchanged"' in out
    # One row, not two — the store is the decision HISTORY, so a duplicate would
    # misreport what the operator actually did.
    rows = (tmp_path / "decided.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(rows) == 1


def test_cli_refuses_to_approve_an_already_shipped_term(tmp_path, capsys):
    # It biases already; recording an approval would leave a row that later
    # reads as though the operator taught it.
    try:
        _run(tmp_path, ["stt-vocab", "approve", "Algernon", "--operator", "andrew"])
    except SystemExit as e:
        assert e.code == 1
    else:  # pragma: no cover - the handler must exit non-zero
        raise AssertionError("approving a shipped term should exit 1")
    assert "already shipped" in capsys.readouterr().out
    assert not (tmp_path / "decided.jsonl").exists()


def test_cli_requires_an_operator_identity(tmp_path, capsys):
    args = argparse.Namespace(
        config=_config_file(tmp_path), stt_vocab_cmd="approve", term="tractor", operator="",
    )
    try:
        _cmd_stt_vocab(args)
    except SystemExit as e:
        assert e.code == 1
    else:  # pragma: no cover
        raise AssertionError("a missing --operator should exit 1")
    assert "--operator" in capsys.readouterr().out


def test_cli_json_mode_carries_the_decision_state(tmp_path, capsys):
    _write_corpus(tmp_path / "corrections.jsonl", TRACTOR_PAIRS)
    _run(tmp_path, ["stt-vocab", "approve", "tractor", "--operator", "andrew"])
    capsys.readouterr()
    _run(tmp_path, ["stt-vocab", "list", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["approved"] == ["tractor"]
    assert payload["proposals"] == []
    assert payload["pairs_recorded"] == 2
    assert "tractor" in payload["biasing"]


# ---------------------------------------------------------------------------
# The daemon join — a section nobody registers renders for nobody
# ---------------------------------------------------------------------------


async def test_fire_once_actually_renders_the_card(tmp_path, monkeypatch):
    """The registration call site in ``daemon.py``, driven for real.

    Every pin above calls ``stt_vocab_section`` directly, so all of them stay
    green if ``daemon.py`` never registers the provider — the section would be
    correct, complete, and invisible in the operator's actual morning message.
    That is the same shape as an optional parameter threaded in tests and
    nowhere in production, so it gets a pin through the production entry point.
    """
    from alfred.daily_sync.config import DailySyncConfig, SttVocabConfig
    from alfred.daily_sync.daemon import fire_once

    _write_corpus(tmp_path / "corrections.jsonl", TRACTOR_PAIRS)
    cfg = DailySyncConfig(enabled=True, batch_size=5)
    cfg.corpus.path = str(tmp_path / "corpus.jsonl")
    cfg.state.path = str(tmp_path / "state.json")
    cfg.stt_vocab = SttVocabConfig(
        enabled=True,
        vocab_corpus_path=str(tmp_path / "corrections.jsonl"),
        vocab_decided_path=str(tmp_path / "decided.jsonl"),
        vocab_terms=["Algernon", "KAL-LE"],
    )

    sent: list[str] = []

    async def _fake_send_batch(user_id, chunks, *, dedupe_key=None, client_name=None):
        sent.extend(chunks)
        return {"telegram_message_ids": [9001]}

    import alfred.transport.client as client_mod
    monkeypatch.setattr(client_mod, "send_outbound_batch", _fake_send_batch)

    # THE WHOLE POINT: empty the module-global registry so the ONLY thing that
    # can put this provider back is daemon.py's own register() call. Without
    # this the pin is a no-op — an earlier test in this file registers the
    # provider, it survives in global state, and deleting the daemon's call site
    # reddens nothing. Measured: that mutation survived until this line existed.
    assembler.clear_providers()
    await fire_once(cfg, tmp_path, user_id=42, today=TODAY)

    body = "\n".join(sent)
    assert "Speech vocabulary review" in body
    assert "“tractor” — corrected 2×" in body
