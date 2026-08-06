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
    """A production-shaped unified config whose telegram.stt points at tmp_path."""
    raw = {
        # The SCHEMA's key is ``telegram`` (telegram/config.py reads
        # raw.get("telegram"), config.yaml.example ships ``telegram:``). The
        # first version of this fixture said ``talker`` — matching the shape the
        # code READ rather than the shape the schema DEFINES — so the whole
        # family agreed with the bug and every assertion below passed against a
        # derive that never fired on a real config. Carries the fields
        # telegram.config itself requires (bot_token / allowed_users /
        # instance.name) so ONE dict can drive BOTH loaders.
        "vault": {"path": "./vault"},
        "telegram": {
            "bot_token": "DUMMY_TELEGRAM_TEST_TOKEN",
            "allowed_users": [1],
            "instance": {"name": "test-instance"},
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


def test_BOTH_loaders_agree_on_one_production_shaped_config(tmp_path):
    """The pin that would have caught the wrong-key bug, and the only one that could.

    Every other derive test here hands in a dict THIS FILE wrote, so a fixture
    keyed to the shape the code happened to read made the whole family agree
    with the bug: the derive "worked" on `talker`, and `telegram.config` — the
    real write side — silently saw nothing. Twenty-eight green tests said the
    read and write sides agreed while on any real config they disagreed about
    every field.

    The fix is to stop letting one side define the schema. This drives BOTH REAL
    LOADERS over ONE raw dict shaped like config.yaml.example and asserts the
    three fields match. It cannot go green while the two loaders read different
    top-level keys, whatever any fixture claims — the write side is no longer
    something this file can assert into existence.
    """
    from alfred.telegram.config import load_from_unified as load_talker

    corpus = str(tmp_path / "CUSTOM_corrections.jsonl")
    decided = str(tmp_path / "CUSTOM_decided.jsonl")
    terms = ["chicken tractor", "front run"]
    raw = _raw(tmp_path)
    raw["telegram"]["stt"].update({
        "vocab_corpus_path": corpus,
        "vocab_decided_path": decided,
        "vocab_terms": terms,
    })

    # WRITE side: what the web capture route and the STT chain actually use.
    write = load_talker(raw).stt
    # READ side: what the review card and the operator CLI actually use.
    read = load_from_unified(raw).stt_vocab

    assert write.vocab_corpus_path == read.vocab_corpus_path == corpus
    assert write.vocab_decided_path == read.vocab_decided_path == decided
    assert list(write.vocab_terms) == list(read.vocab_terms) == terms


def test_both_loaders_agree_on_the_DEFAULTS_too(tmp_path):
    """The same agreement where nothing is overridden. A shared fallback constant
    can hide a key mismatch — both sides land on the default and look identical —
    so the override case above is the load-bearing one and this is the floor:
    they must not diverge even when neither side was configured."""
    from alfred.telegram.config import load_from_unified as load_talker

    raw = _raw(tmp_path)
    for k in ("vocab_corpus_path", "vocab_decided_path", "vocab_terms"):
        raw["telegram"]["stt"].pop(k, None)

    write = load_talker(raw).stt
    read = load_from_unified(raw).stt_vocab
    assert write.vocab_corpus_path == read.vocab_corpus_path
    assert write.vocab_decided_path == read.vocab_decided_path
    assert list(write.vocab_terms) == list(read.vocab_terms)


def test_absent_telegram_block_falls_back_to_the_shared_constants(tmp_path):
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
    # carrying telegram.stt but no instance block raised — and the fallback then
    # pointed this section at the DEFAULT paths while capture wrote the
    # overridden ones. A read side and a write side disagreeing in silence.
    raw = _raw(tmp_path)
    del raw["telegram"]["instance"]
    cfg = load_from_unified(raw).stt_vocab
    assert cfg.vocab_corpus_path == str(tmp_path / "corrections.jsonl")


def test_env_substitution_still_applies_to_a_derived_path(tmp_path, monkeypatch):
    # The derive reads `raw` AFTER _substitute_env, so a ${VAR} path resolves.
    monkeypatch.setenv("STT_TEST_DIR", str(tmp_path))
    raw = _raw(tmp_path)
    raw["telegram"]["stt"]["vocab_corpus_path"] = "${STT_TEST_DIR}/from-env.jsonl"
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


# ---------------------------------------------------------------------------
# NOTE-3 — the cap offered is the REMAINING budget, not the total
# ---------------------------------------------------------------------------


def _approve_many(decided_path, n, prefix="learned"):
    """Fill the learned store with n approved terms."""
    from alfred.telegram.stt_vocab_learning import (
        DECISION_APPROVE, VocabDecision, append_decision,
    )
    for i in range(n):
        append_decision(decided_path, VocabDecision(
            type=DECISION_APPROVE, term=f"{prefix}{i:03d}",
        ))


def test_at_cap_the_card_says_the_budget_is_full_not_nothing_to_propose(tmp_path):
    """The worst of the three quiet cases to get wrong. At cap the operator would
    otherwise read "no patterns found" while real recurring corrections sit
    unproposed behind a budget he could free with one command."""
    from alfred.telegram.stt_vocab_learning import MAX_LEARNED_TERMS

    _write_corpus(tmp_path / "corrections.jsonl", TRACTOR_PAIRS)
    _approve_many(tmp_path / "decided.jsonl", MAX_LEARNED_TERMS)

    out = stt_vocab_section(_cfg(tmp_path), TODAY)
    assert "budget is full" in out
    assert f"{MAX_LEARNED_TERMS}/{MAX_LEARNED_TERMS}" in out
    # It names the way OUT, not just the wall.
    assert "reject" in out.lower()
    # And it must NOT claim there was nothing worth proposing.
    assert "nothing new to propose" not in out


def test_below_cap_the_offer_is_trimmed_to_what_can_be_honoured(tmp_path):
    """With one slot left, one proposal may surface — not the twenty the total cap
    would allow, nineteen of which `apply_approved_terms` would drop on arrival."""
    from alfred.telegram.stt_vocab_learning import MAX_LEARNED_TERMS

    pairs = []
    for i in range(5):
        pairs += [{"transcript": f"word heard{i:03d} here", "sent": f"word meant{i:03d} here"}] * 2
    _write_corpus(tmp_path / "corrections.jsonl", pairs)
    _approve_many(tmp_path / "decided.jsonl", MAX_LEARNED_TERMS - 1)

    out = stt_vocab_section(_cfg(tmp_path), TODAY)
    assert "(1 proposal)" in out


def test_the_cli_list_also_reports_at_cap_rather_than_going_quiet(tmp_path, capsys):
    """Both surfaces, one message — the card and the CLI must not disagree about
    why the list is empty."""
    from alfred.telegram.stt_vocab_learning import MAX_LEARNED_TERMS

    _write_corpus(tmp_path / "corrections.jsonl", TRACTOR_PAIRS)
    _approve_many(tmp_path / "decided.jsonl", MAX_LEARNED_TERMS)
    _run(tmp_path, ["stt-vocab", "list"])
    out = capsys.readouterr().out
    assert "budget is full" in out
    assert "reject" in out.lower()


def test_the_cli_json_reports_the_remaining_budget(tmp_path, capsys):
    from alfred.telegram.stt_vocab_learning import MAX_LEARNED_TERMS

    _approve_many(tmp_path / "decided.jsonl", 3)
    _run(tmp_path, ["stt-vocab", "list", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["max_learned"] == MAX_LEARNED_TERMS
    assert payload["learned_remaining"] == MAX_LEARNED_TERMS - 3


def test_the_cli_writes_NOTHING_at_the_default_store_paths(tmp_path, monkeypatch):
    """Debris pin. The wrong-key bug had a second symptom nobody was looking for:
    with the derive reading a key that did not exist, every CLI test fell back to
    the SHIPPED default paths and appended real decisions into the repo's own
    `data/stt_vocab_decided.jsonl`. That file is gitignored, so the tree looked
    clean — and it silently poisoned an unrelated suite
    (`test_web_stt_shadow_config_gate`, which reads the default store through
    `effective_vocab_terms` and suddenly saw a learned "tractor").

    "Did it write the right file" is the wrong question; "did it touch anything
    out there" is the right one. Runs from a CWD of its own so a relative default
    path would land somewhere observable, and asserts it did not.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir(exist_ok=True)
    _write_corpus(tmp_path / "corrections.jsonl", TRACTOR_PAIRS)

    _run(tmp_path, ["stt-vocab", "approve", "tractor", "--operator", "andrew"])

    # The decision landed in the CONFIGURED store...
    assert (tmp_path / "decided.jsonl").exists()
    # ...and nothing appeared at either shipped default, relative to this CWD.
    assert not (tmp_path / DEFAULT_STT_VOCAB_DECIDED_PATH).exists()
    assert not (tmp_path / DEFAULT_STT_VOCAB_CORPUS_PATH).exists()
    assert list((tmp_path / "data").iterdir()) == []
