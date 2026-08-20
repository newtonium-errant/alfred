"""#13 reject-with-correction — a NO on a routine match that teaches the answer.

The matcher's verbs used to be binary: confirm promoted the proposed pair,
reject suppressed it, and the operator had no way to say what the completion
ACTUALLY meant. These pins cover the third and fourth verdicts and, more
importantly, everything the resolver must REFUSE:

  * ``correct`` — reject the proposal AND alias the phrase to the operator's
    chosen item. Two corpus rows, one claim each.
  * ``one_off`` — reject the proposal AND record that the phrase means no
    routine item at all. Suppresses the PHRASE, not just the pair.
  * refusals — every one asserts the LOGGED REASON, not just that the call
    failed. A refusal for an unrelated cause (missing metadata, wrong verb)
    returns the same ``(error, False)`` shape as the guard firing, so a pin that
    only checked the shape would stay green against a build with no guard.
  * corpus-poisoning guard — a target that isn't a live routine item is refused
    and writes NOTHING: not the alias, not even the reject. A half-landed
    correction is worse than a refused one (the card leaves the deck as
    "handled" while the answer is dropped), so the whole verdict is atomic.
  * the deck path (``action_router.act``) as well as the resolver, because a
    feature threaded only in tests is a feature production never runs.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
import structlog
import yaml

from alfred.daily_sync import action_router as arouter
from alfred.daily_sync import reply_dispatch as rd
from alfred.daily_sync.action_router import (
    STATUS_ACTED,
    STATUS_INVALID_ACTION,
    act,
)
from alfred.daily_sync.assembler import ReplyCorrection, parse_reply
from alfred.daily_sync.config import DailySyncConfig
from alfred.daily_sync.confidence import save_state
from alfred.daily_sync.feed_producer import build_feed_items
from alfred.feed import FeedStore
from alfred.feed.model import STATE_ACTED
from alfred.routine import match_calibration as mc

REFUSED_EVENT = "daily_sync.routine_match.correction_refused"
VERDICT_EVENT = "daily_sync.routine_match.verdict_recorded"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _write_routine(vault: Path, name: str, items: list[dict]) -> None:
    routine_dir = vault / "routine"
    routine_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "type": "routine", "name": name, "status": "active",
        "cadence": {"type": "daily"}, "items": items,
    }
    fm = yaml.dump(payload, default_flow_style=False, sort_keys=False)
    (routine_dir / f"{name}.md").write_text(
        f"---\n{fm}---\n\n# {name}\n", encoding="utf-8",
    )


def _vault(tmp_path: Path) -> Path:
    """A two-record vault. 'Clean hammer' is the operator's real 2026-08-03
    example: it does NOT mean 'Clean house', it means 'Tidy the workshop'."""
    vault = tmp_path / "vault"
    _write_routine(vault, "Weekly", [
        {"text": "Clean house"},
        {"text": "Tidy the workshop"},
    ])
    _write_routine(vault, "Daily", [{"text": "Walk the dog"}])
    return vault


def _item(
    *,
    query: str = "clean hammer",
    matched_to: str = "Clean house",
    record: str = "Weekly",
    confidence: float = 0.4,
    kind: str = mc.KIND_LOW_CONF,
) -> dict[str, Any]:
    return {
        "item_number": 1,
        "query": query,
        "matched_to": matched_to,
        "record": record,
        "confidence": confidence,
        "completion_date": "2026-08-03",
        "captured_at": "2026-08-03T09:00:00+00:00",
        "kind": kind,
    }


def _rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [
        json.loads(line) for line in path.read_text().splitlines() if line.strip()
    ]


def _reasons(captured: list[dict]) -> list[str]:
    return [
        c.get("reason") for c in captured if c.get("event") == REFUSED_EVENT
    ]


def _resolve(
    correction: ReplyCorrection,
    item: dict[str, Any],
    corpus: Path,
    vault: Path | None,
) -> tuple[str | None, bool, list[dict]]:
    with structlog.testing.capture_logs() as captured:
        err, did_write = rd._resolve_routine_match_correction(
            correction, item, str(corpus), vault_path=vault,
        )
    return err, did_write, captured


# ---------------------------------------------------------------------------
# correct — the reject that teaches
# ---------------------------------------------------------------------------


def test_correct_writes_reject_and_alias_pair(tmp_path: Path) -> None:
    """The headline contract: a validated pick writes BOTH rows — a reject of
    what we proposed and an alias to what the operator meant."""
    vault = _vault(tmp_path)
    corpus = tmp_path / "corpus.jsonl"

    err, did_write, captured = _resolve(
        ReplyCorrection(
            item_number=1, reject=True, correction_target="Tidy the workshop",
        ),
        _item(), corpus, vault,
    )

    assert err is None and did_write is True
    rows = _rows(corpus)
    assert [r["type"] for r in rows] == [mc.CORPUS_REJECT, mc.CORPUS_ALIAS]
    qkey = mc.query_key("clean hammer")
    assert all(r["query_key"] == qkey for r in rows)
    # The reject names the WRONG item; the alias names the RIGHT one.
    assert rows[0]["item_text"] == "Clean house"
    assert rows[1]["item_text"] == "Tidy the workshop"
    assert rows[1]["record"] == "Weekly"

    verdicts = [c for c in captured if c.get("event") == VERDICT_EVENT]
    assert len(verdicts) == 1
    assert verdicts[0]["verdict"] == "corrected"
    assert verdicts[0]["corrected_to"] == "Tidy the workshop"
    assert verdicts[0]["rows_written"] == 2


def test_corrected_phrase_is_consultable_as_an_alias(tmp_path: Path) -> None:
    """Round-trip: after the correction, the glossary the matcher loads resolves
    the phrase to the operator's item — the taught answer is live, not just
    logged."""
    vault = _vault(tmp_path)
    corpus = tmp_path / "corpus.jsonl"

    rd._resolve_routine_match_correction(
        ReplyCorrection(
            item_number=1, reject=True, correction_target="Tidy the workshop",
        ),
        _item(), str(corpus), vault_path=vault,
    )

    glossary = mc.load_glossary(corpus)
    qkey = mc.query_key("clean hammer")
    assert glossary.alias_for(qkey) == "Tidy the workshop"
    # And the wrong pair stays rejected — teaching the right answer must not
    # quietly un-reject the one the operator said no to.
    assert glossary.verdict(qkey, "Clean house") == "reject"


def test_correct_accepts_a_target_across_records(tmp_path: Path) -> None:
    """The pick list spans the whole vault, not just the proposed record — the
    right answer is often on a different routine."""
    vault = _vault(tmp_path)
    corpus = tmp_path / "corpus.jsonl"

    err, did_write, _ = _resolve(
        ReplyCorrection(
            item_number=1, reject=True, correction_target="Walk the dog",
        ),
        _item(), corpus, vault,
    )

    assert err is None and did_write is True
    rows = _rows(corpus)
    assert rows[1]["item_text"] == "Walk the dog"
    # Provenance follows the item to ITS record, not the proposal's.
    assert rows[1]["record"] == "Daily"


def test_correct_normalises_the_pick_but_stores_canonical_text(
    tmp_path: Path,
) -> None:
    """A pick whose case/spacing drifted from the vault still resolves, and the
    row carries the VAULT's spelling — glossary pairs key on ``item_text``
    verbatim, so writing the operator's variant would create a pair the matcher
    never looks up."""
    vault = _vault(tmp_path)
    corpus = tmp_path / "corpus.jsonl"

    err, did_write, _ = _resolve(
        ReplyCorrection(
            item_number=1, reject=True,
            correction_target="  tidy   THE workshop ",
        ),
        _item(), corpus, vault,
    )

    assert err is None and did_write is True
    assert _rows(corpus)[1]["item_text"] == "Tidy the workshop"
    assert mc.load_glossary(corpus).alias_for(
        mc.query_key("clean hammer"),
    ) == "Tidy the workshop"


# ---------------------------------------------------------------------------
# Refusals — each asserts WHY, and that nothing was written
# ---------------------------------------------------------------------------


def test_free_text_target_is_refused_and_writes_nothing(tmp_path: Path) -> None:
    """The corpus-poisoning guard. An invented target never becomes a glossary
    row — and the reject doesn't land either, so the operator's answer isn't
    half-taken. The logged reason is what distinguishes this from a refusal for
    any other cause."""
    vault = _vault(tmp_path)
    # Deliberately under a directory that does not exist, so the debris check
    # covers the parent mkdir append_corpus would have done.
    corpus = tmp_path / "nested" / "corpus.jsonl"

    err, did_write, captured = _resolve(
        ReplyCorrection(
            item_number=1, reject=True,
            correction_target="Polish the DeLorean",
        ),
        _item(), corpus, vault,
    )

    assert did_write is False
    assert err is not None and "Polish the DeLorean" in err
    assert _reasons(captured) == ["target_not_a_routine_item"]
    # Nothing touched out there — not the file, not its parent.
    assert not corpus.exists()
    assert not corpus.parent.exists()


def test_target_equal_to_the_proposal_is_refused(tmp_path: Path) -> None:
    """"No — and it means the thing I just said no to" is contradictory. Refuse
    rather than write a reject and an alias for the same pair, which would
    resolve to whichever row loaded last."""
    vault = _vault(tmp_path)
    corpus = tmp_path / "corpus.jsonl"

    err, did_write, captured = _resolve(
        ReplyCorrection(
            item_number=1, reject=True, correction_target="clean HOUSE",
        ),
        _item(), corpus, vault,
    )

    assert did_write is False and err is not None
    assert _reasons(captured) == ["target_is_the_proposal"]
    assert not corpus.exists()


def test_correction_without_reject_is_refused(tmp_path: Path) -> None:
    """A correction enriches a NO. Arriving with a confirm means the caller
    built a contradictory verdict; guessing which half to honour would silently
    invent an operator intent."""
    vault = _vault(tmp_path)
    corpus = tmp_path / "corpus.jsonl"

    err, did_write, captured = _resolve(
        ReplyCorrection(
            item_number=1, ok=True, correction_target="Tidy the workshop",
        ),
        _item(), corpus, vault,
    )

    assert did_write is False and err is not None
    assert _reasons(captured) == ["correction_without_reject"]
    assert not corpus.exists()


def test_one_off_without_reject_is_refused(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    corpus = tmp_path / "corpus.jsonl"

    err, did_write, captured = _resolve(
        ReplyCorrection(item_number=1, ok=True, one_off=True),
        _item(), corpus, vault,
    )

    assert did_write is False and err is not None
    assert _reasons(captured) == ["correction_without_reject"]
    assert not corpus.exists()


def test_target_and_one_off_together_are_refused(tmp_path: Path) -> None:
    """The two enriched verdicts are mutually exclusive — "it means X" and "it
    means nothing" can't both be true."""
    vault = _vault(tmp_path)
    corpus = tmp_path / "corpus.jsonl"

    err, did_write, captured = _resolve(
        ReplyCorrection(
            item_number=1, reject=True,
            correction_target="Tidy the workshop", one_off=True,
        ),
        _item(), corpus, vault,
    )

    assert did_write is False and err is not None
    assert _reasons(captured) == ["target_and_one_off"]
    assert not corpus.exists()


def test_target_without_a_vault_is_refused_not_written_unvalidated(
    tmp_path: Path,
) -> None:
    """No vault means no way to check the pick. The fail-closed direction is
    mandatory: accepting the string unvalidated is exactly the poisoning this
    feature has to prevent."""
    corpus = tmp_path / "corpus.jsonl"

    err, did_write, captured = _resolve(
        ReplyCorrection(
            item_number=1, reject=True, correction_target="Tidy the workshop",
        ),
        _item(), corpus, None,
    )

    assert did_write is False and err is not None
    assert _reasons(captured) == ["no_vault_path"]
    assert not corpus.exists()


def test_plain_reject_still_needs_no_vault(tmp_path: Path) -> None:
    """Contrast pin: the vault requirement is scoped to a CORRECTION. A bare
    reject on an instance without a vault path keeps working exactly as before —
    the new gate must not narrow the old verb."""
    corpus = tmp_path / "corpus.jsonl"

    err, did_write, _ = _resolve(
        ReplyCorrection(item_number=1, reject=True), _item(), corpus, None,
    )

    assert err is None and did_write is True
    rows = _rows(corpus)
    assert [r["type"] for r in rows] == [mc.CORPUS_REJECT]


# ---------------------------------------------------------------------------
# one_off — the honest third verdict
# ---------------------------------------------------------------------------


def test_one_off_writes_reject_plus_oneoff(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    corpus = tmp_path / "corpus.jsonl"

    err, did_write, captured = _resolve(
        ReplyCorrection(item_number=1, reject=True, one_off=True),
        _item(), corpus, vault,
    )

    assert err is None and did_write is True
    rows = _rows(corpus)
    assert [r["type"] for r in rows] == [mc.CORPUS_REJECT, mc.CORPUS_ONEOFF]
    # The one-off row carries the proposal as PROVENANCE, keyed on the phrase.
    assert rows[1]["query_key"] == mc.query_key("clean hammer")
    assert rows[1]["item_text"] == "Clean house"

    verdicts = [c for c in captured if c.get("event") == VERDICT_EVENT]
    assert len(verdicts) == 1
    assert verdicts[0]["verdict"] == "one_off"
    assert verdicts[0]["rows_written"] == 2


def test_one_off_needs_no_vault(tmp_path: Path) -> None:
    """Nothing to validate — the operator named no item. The vault gate applies
    to a target, not to the one-off door."""
    corpus = tmp_path / "corpus.jsonl"

    err, did_write, _ = _resolve(
        ReplyCorrection(item_number=1, reject=True, one_off=True),
        _item(), corpus, None,
    )

    assert err is None and did_write is True
    assert [r["type"] for r in _rows(corpus)] == [
        mc.CORPUS_REJECT, mc.CORPUS_ONEOFF,
    ]


# ---------------------------------------------------------------------------
# Glossary semantics — what the stored verdicts MEAN
# ---------------------------------------------------------------------------


def _pending(query: str, matched_to: str, *, record: str = "Weekly") -> mc.PendingMatch:
    # DERIVED FROM WALL CLOCK, never a literal — the age-out drop in
    # `filter_pending_for_review` is judged against REAL time when no `today=`
    # is threaded (`today = today or date.today()`, match_calibration.py), and
    # the callers in this file thread none. The first draft froze
    # captured_at at 2026-08-04, a dated suite regression set to red on
    # 2026-08-26 when the 21-day `DEFAULT_PENDING_MAX_AGE_DAYS` window
    # crossed it: test_plain_reject_does_not_suppress_a_different_candidate
    # would lose its row to `aged_out` and fail over CORRECT production
    # behavior (its one-off sibling survives only because that drop is
    # checked before the age check). Derived at one day back, the row stays
    # "recently captured, well inside the window" forever. Windows in
    # wall-clock-predicate tests are derived, never literal (gate rule,
    # 2026-08-19).
    captured = datetime.now(timezone.utc) - timedelta(days=1)
    return mc.PendingMatch(
        query=query, matched_to=matched_to, record=record, confidence=0.4,
        completion_date=captured.date().isoformat(),
        captured_at=captured.isoformat(),
    )


def test_one_off_suppresses_a_different_candidate(tmp_path: Path) -> None:
    """The whole reason one-off is not just another reject: it suppresses the
    PHRASE. A later capture pairing the same phrase with a DIFFERENT candidate
    must not come back — the operator already said the phrase means nothing."""
    corpus = tmp_path / "corpus.jsonl"
    rd._resolve_routine_match_correction(
        ReplyCorrection(item_number=1, reject=True, one_off=True),
        _item(), str(corpus), vault_path=None,
    )
    glossary = mc.load_glossary(corpus)

    surfaced, stats = mc.filter_pending_for_review(
        [_pending("clean hammer", "Tidy the workshop")], glossary,
    )

    assert surfaced == []
    assert stats.one_off == 1
    # Attributed to the one-off, NOT folded into the pair verdict the same
    # action also wrote.
    assert stats.resolved == 0
    assert stats.suppressed() == 1


def test_plain_reject_does_not_suppress_a_different_candidate(
    tmp_path: Path,
) -> None:
    """Contrast pin — this is the behaviour one-off exists to differ from. A
    reject speaks only about the pair, so a new suggestion for the same phrase
    is still worth asking about. If this ever goes green as suppressed, one-off
    has stopped being a distinct verdict."""
    corpus = tmp_path / "corpus.jsonl"
    rd._resolve_routine_match_correction(
        ReplyCorrection(item_number=1, reject=True),
        _item(), str(corpus), vault_path=None,
    )
    glossary = mc.load_glossary(corpus)

    surfaced, stats = mc.filter_pending_for_review(
        [_pending("clean hammer", "Tidy the workshop")], glossary,
    )

    assert len(surfaced) == 1
    assert stats.one_off == 0 and stats.suppressed() == 0


def test_correction_suppresses_the_phrase_via_its_alias(tmp_path: Path) -> None:
    """A taught answer also stops the re-asking — R1's alias drop already covers
    it, so the correction path composes with that rather than adding a second
    mechanism."""
    vault = _vault(tmp_path)
    corpus = tmp_path / "corpus.jsonl"
    rd._resolve_routine_match_correction(
        ReplyCorrection(
            item_number=1, reject=True, correction_target="Tidy the workshop",
        ),
        _item(), str(corpus), vault_path=vault,
    )
    glossary = mc.load_glossary(corpus)

    surfaced, stats = mc.filter_pending_for_review(
        [_pending("clean hammer", "Walk the dog")], glossary,
    )

    assert surfaced == []
    assert stats.resolved == 1 and stats.one_off == 0


def test_reject_after_a_one_off_does_not_clear_it(tmp_path: Path) -> None:
    """Order-independence pin. The one-off verdict writes a reject of its own,
    so if a reject cleared ``one_offs`` the two rows of a single operator action
    could cancel each other depending on replay order."""
    corpus = tmp_path / "corpus.jsonl"
    qkey = mc.query_key("clean hammer")
    for entry in (
        mc.MatchCorpusEntry(
            type=mc.CORPUS_ONEOFF, query_key=qkey, item_text="Clean house",
        ),
        mc.MatchCorpusEntry(
            type=mc.CORPUS_REJECT, query_key=qkey, item_text="Clean house",
        ),
    ):
        mc.append_corpus(corpus, entry)

    assert mc.load_glossary(corpus).is_one_off(qkey) is True


def test_a_later_alias_clears_a_one_off(tmp_path: Path) -> None:
    """Later-verdict-wins: the operator has now named what the phrase means, so
    it is no longer a one-off."""
    corpus = tmp_path / "corpus.jsonl"
    qkey = mc.query_key("clean hammer")
    mc.append_corpus(corpus, mc.MatchCorpusEntry(
        type=mc.CORPUS_ONEOFF, query_key=qkey, item_text="Clean house",
    ))
    mc.append_corpus(corpus, mc.MatchCorpusEntry(
        type=mc.CORPUS_ALIAS, query_key=qkey, item_text="Tidy the workshop",
    ))

    glossary = mc.load_glossary(corpus)
    assert glossary.is_one_off(qkey) is False
    assert glossary.alias_for(qkey) == "Tidy the workshop"


def test_a_later_one_off_retracts_an_alias(tmp_path: Path) -> None:
    """The mirror. Declaring the phrase meaningless withdraws a standing "it
    means X" claim — and the confirmed pair that alias implied goes with it,
    or the matcher would keep fast-pathing a claim the operator retracted."""
    corpus = tmp_path / "corpus.jsonl"
    qkey = mc.query_key("clean hammer")
    mc.append_corpus(corpus, mc.MatchCorpusEntry(
        type=mc.CORPUS_ALIAS, query_key=qkey, item_text="Tidy the workshop",
    ))
    mc.append_corpus(corpus, mc.MatchCorpusEntry(
        type=mc.CORPUS_ONEOFF, query_key=qkey, item_text="Clean house",
    ))

    glossary = mc.load_glossary(corpus)
    assert glossary.is_one_off(qkey) is True
    assert glossary.alias_for(qkey) is None
    assert glossary.verdict(qkey, "Tidy the workshop") is None


def test_an_older_reader_still_honours_the_reject(tmp_path: Path) -> None:
    """Forward-compat pin for the two-row split. Rows carrying an unknown type
    are skipped by ``load_glossary``, so a build that predates ``match_oneoff``
    still sees the suppression the same operator action wrote — it degrades to
    "pair rejected" instead of ignoring the operator."""
    corpus = tmp_path / "corpus.jsonl"
    rd._resolve_routine_match_correction(
        ReplyCorrection(item_number=1, reject=True, one_off=True),
        _item(), str(corpus), vault_path=None,
    )
    lines = corpus.read_text().splitlines()
    unknown_type = [
        json.loads(ln) for ln in lines if json.loads(ln)["type"] != mc.CORPUS_REJECT
    ]
    assert len(unknown_type) == 1, "the enriched verdict must ride on its own row"

    # Replay with the newer row's type mangled — an old reader's view.
    legacy = tmp_path / "legacy.jsonl"
    legacy.write_text("\n".join(
        ln for ln in lines if json.loads(ln)["type"] == mc.CORPUS_REJECT
    ) + "\n")
    assert mc.load_glossary(legacy).verdict(
        mc.query_key("clean hammer"), "Clean house",
    ) == "reject"


# ---------------------------------------------------------------------------
# The deck path — act() is what production actually calls
# ---------------------------------------------------------------------------


def _ds_config(tmp_path: Path) -> DailySyncConfig:
    cfg = DailySyncConfig(enabled=True, batch_size=5)
    cfg.corpus.path = str(tmp_path / "email_corpus.jsonl")
    cfg.state.path = str(tmp_path / "state.json")
    return cfg


def _publish(store: FeedStore, item: dict[str, Any]) -> str:
    fis = build_feed_items("routine_match", [item], "salem")
    assert len(fis) == 1
    store.upsert(fis[0])
    return fis[0].id


def _act(
    store: FeedStore,
    cfg: DailySyncConfig,
    feed_id: str,
    action_id: str,
    *,
    vault: Path | None = None,
    correction_target: str | None = None,
):
    return act(
        feed_id, action_id,
        feed_store=store, config=cfg, vault_path=vault,
        instance_name="salem", instance_scope="talker",
        correction_target=correction_target,
    )


@pytest.fixture
def deck(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A published routine_match card wired to a tmp corpus + real vault."""
    cfg = _ds_config(tmp_path)
    store = FeedStore(str(tmp_path / "feed.jsonl"))
    corpus = tmp_path / "corpus.jsonl"
    monkeypatch.setattr(
        rd, "_routine_match_corpus_path", lambda *a, **kw: str(corpus),
    )
    item = _item()
    feed_id = _publish(store, item)
    save_state(cfg.state.path, {"last_batch": {
        "date": "2026-08-03", "message_ids": [100],
        "routine_match_items": [item],
    }})
    return {
        "cfg": cfg, "store": store, "corpus": corpus,
        "id": feed_id, "vault": _vault(tmp_path),
    }


def test_deck_correct_writes_the_pair_and_acts(deck) -> None:
    """The e2e that per-layer unit pins cannot give: the target survives the
    whole act path (router ceiling → ReplyCorrection → resolver) and the card
    leaves the deck."""
    result = _act(
        deck["store"], deck["cfg"], deck["id"], "correct",
        vault=deck["vault"], correction_target="Tidy the workshop",
    )

    assert result.ok and result.status == STATUS_ACTED
    assert "Tidy the workshop" in result.detail
    rows = _rows(deck["corpus"])
    assert [r["type"] for r in rows] == [mc.CORPUS_REJECT, mc.CORPUS_ALIAS]
    assert rows[1]["item_text"] == "Tidy the workshop"
    assert deck["store"].load()[deck["id"]].state == STATE_ACTED


def test_deck_one_off_writes_reject_plus_oneoff_and_acts(deck) -> None:
    result = _act(
        deck["store"], deck["cfg"], deck["id"], "one_off", vault=deck["vault"],
    )

    assert result.ok and result.status == STATUS_ACTED
    assert "one-off" in result.detail
    assert [r["type"] for r in _rows(deck["corpus"])] == [
        mc.CORPUS_REJECT, mc.CORPUS_ONEOFF,
    ]
    assert deck["store"].load()[deck["id"]].state == STATE_ACTED


def test_deck_correct_without_a_target_is_refused(deck) -> None:
    """Refused, never degraded into a plain reject. Silently downgrading would
    tell the operator their answer landed while the deck learned nothing."""
    with structlog.testing.capture_logs() as captured:
        result = _act(
            deck["store"], deck["cfg"], deck["id"], "correct",
            vault=deck["vault"],
        )

    assert not result.ok and result.status == STATUS_INVALID_ACTION
    reasons = [
        c.get("reason") for c in captured
        if c.get("event") == "feed.act.invalid_action"
    ]
    assert reasons == ["correct_without_target"]
    assert _rows(deck["corpus"]) == []
    # The card stays open so the operator can answer properly.
    assert deck["store"].load()[deck["id"]].state != STATE_ACTED


def test_deck_correct_with_a_bogus_target_is_refused(deck) -> None:
    """The poisoning guard, reached through the production entry point."""
    result = _act(
        deck["store"], deck["cfg"], deck["id"], "correct",
        vault=deck["vault"], correction_target="Polish the DeLorean",
    )

    assert not result.ok
    assert _rows(deck["corpus"]) == []
    assert deck["store"].load()[deck["id"]].state != STATE_ACTED


def test_a_target_cannot_be_smuggled_onto_confirm(deck) -> None:
    """Ceiling pin: ``correction_target`` is consumed by exactly one
    (kind, action) pair. Sent with any other action it is dropped — a confirm
    stays a confirm, no alias appears."""
    result = _act(
        deck["store"], deck["cfg"], deck["id"], "confirm",
        vault=deck["vault"], correction_target="Tidy the workshop",
    )

    assert result.ok and result.status == STATUS_ACTED
    rows = _rows(deck["corpus"])
    assert [r["type"] for r in rows] == [mc.CORPUS_CONFIRM]
    assert rows[0]["item_text"] == "Clean house"


def test_a_target_cannot_be_smuggled_onto_reject(deck) -> None:
    """Same ceiling, the adjacent verb — a bare ``reject`` must not silently
    become a correction just because a target rode along."""
    result = _act(
        deck["store"], deck["cfg"], deck["id"], "reject",
        vault=deck["vault"], correction_target="Tidy the workshop",
    )

    assert result.ok and result.status == STATUS_ACTED
    assert [r["type"] for r in _rows(deck["corpus"])] == [mc.CORPUS_REJECT]


def test_correct_is_not_reachable_on_another_kind(tmp_path: Path) -> None:
    """The new actions live under routine_match only — they must not widen any
    other family's ceiling."""
    for kind, actions in arouter.FEED_ACTIONS.items():
        if kind == arouter.ROUTINE_MATCH_KIND:
            continue
        assert arouter.CORRECT_ACTION not in actions
        assert arouter.ONE_OFF_ACTION not in actions


# ---------------------------------------------------------------------------
# #34 — pending-queue verbs must not confirm a non-pending item
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("token", ["noted", "show"])
def test_a_pending_verb_on_a_routine_match_errors_and_teaches_nothing(
    tmp_path: Path, token: str,
) -> None:
    """THE PIN. ``3 noted`` aimed at a routine match used to CONFIRM it.

    ``parse_reply`` sees only text, so every OK-verb collapses to ``ok=True``
    before any item kind is known — which made ``noted`` indistinguishable from
    ``confirm`` at the resolver. It therefore wrote a corpus row teaching the
    matcher a verdict the operator never gave, on an input most likely to be a
    typo or a mis-numbered line. Erroring is the honest outcome: the operator
    retypes, and nothing is learned from a slip.
    """
    vault = _vault(tmp_path)
    corpus = tmp_path / "corpus.jsonl"
    correction = ReplyCorrection(item_number=1, ok=True, consumed_token=token)

    err, did_write, captured = _resolve(correction, _item(), corpus, vault)

    assert err and token in err, "the refusal names the verb the operator typed"
    assert did_write is False
    assert _rows(corpus) == [], "no corpus row may be written from a slip"
    assert "pending_only_verb" in _reasons(captured), (
        "the refusal must be logged with its REASON — a refusal for the right "
        "cause and one for an unrelated cause are otherwise indistinguishable"
    )


def test_a_real_confirm_still_confirms(tmp_path: Path) -> None:
    """PRESERVED BEHAVIOUR, paired with the pin above. A build that "fixed" the
    leak by refusing every ``ok=True`` passes that test and fails this one."""
    vault = _vault(tmp_path)
    corpus = tmp_path / "corpus.jsonl"
    correction = ReplyCorrection(item_number=1, ok=True, consumed_token="confirm")

    err, did_write, _ = _resolve(correction, _item(), corpus, vault)

    assert err is None
    assert did_write is True
    assert _rows(corpus), "a genuine confirm still teaches the matcher"


def test_chaining_inherits_the_token_so_a_chained_pending_verb_refuses(
    tmp_path: Path,
) -> None:
    """Chaining is driven through the REAL parser, not hand-built (#34 gate).

    An earlier version of this pin justified token-keying by claiming a chained
    ``same``/``ditto`` copies ``ok`` WITHOUT the token. Measured: it copies the
    token too — ``parse_reply("1 noted\n2 same")`` yields item 2 with
    ``consumed_token="noted"``. So ``ok=True``-with-empty-token is unreachable
    through the parser, and the old justification was false.

    The behaviour that inheritance produces is the CORRECT one, which is what
    this pins: chaining off a pending verb carries the pending verb, so the
    chained item refuses on a routine match exactly as the head item does. A
    chain cannot launder ``noted`` into a confirm.
    """
    parsed = parse_reply("1 noted\n2 same")
    assert [c.consumed_token for c in parsed.corrections] == ["noted", "noted"], (
        "the parser's chain behaviour changed — this pin's premise is measured, "
        "not assumed"
    )

    vault = _vault(tmp_path)
    corpus = tmp_path / "corpus.jsonl"
    for correction in parsed.corrections:
        correction.item_number = 1          # aim both at the routine-match item
        err, did_write, captured = _resolve(correction, _item(), corpus, vault)
        assert err and "noted" in err
        assert did_write is False
        assert "pending_only_verb" in _reasons(captured)
    assert _rows(corpus) == []


def test_chaining_off_a_real_confirm_still_confirms(tmp_path: Path) -> None:
    """The other direction, also parser-driven: a chain from ``confirm``
    inherits ``confirm`` and is applied. Without this, a build that refused
    every chained correction would pass the pin above."""
    parsed = parse_reply("1 confirm\n2 ditto")
    assert [c.consumed_token for c in parsed.corrections] == ["confirm", "confirm"]

    vault = _vault(tmp_path)
    corpus = tmp_path / "corpus.jsonl"
    correction = parsed.corrections[1]
    correction.item_number = 1

    err, did_write, _ = _resolve(correction, _item(), corpus, vault)

    assert err is None
    assert did_write is True


def test_an_ok_with_no_consumed_token_is_accepted_defensively(
    tmp_path: Path,
) -> None:
    """DEFENSIVE pin on a HAND-BUILT shape the parser does not currently emit.

    Labelled as such deliberately. Every parser path that sets ``ok`` also sets
    a token (measured above), so this correction shape is unreachable today —
    it is pinned because the guard's keying is defined over the token, and a
    future parser change that produced a token-less ``ok`` must degrade to
    ACCEPT rather than to a silent refusal of ordinary confirms.

    The real justification for keying on the token rather than on ``ok`` is
    OVER-BREADTH, not chaining: ``ok``-keying would refuse every
    ``confirm``/``keep``/``yes``, which the mutation on this suite demonstrates
    (5 red, including two of #13's own shape guards).
    """
    vault = _vault(tmp_path)
    corpus = tmp_path / "corpus.jsonl"
    correction = ReplyCorrection(item_number=1, ok=True)

    err, did_write, _ = _resolve(correction, _item(), corpus, vault)

    assert err is None
    assert did_write is True


def test_pending_only_tokens_are_exactly_the_pending_queue_verbs() -> None:
    """Contract pin. These are the verbs `_applicable_calibration_verbs`
    advertises for ``has_pending`` and that the capability ceiling lists under
    the ``pending`` kind — three places that must agree about what "pending
    verb" means."""
    from alfred.daily_sync.action_router import DEFER_ACTIONS, FEED_ACTIONS
    from alfred.daily_sync.assembler import PENDING_ONLY_OK_TOKENS

    assert PENDING_ONLY_OK_TOKENS == frozenset({"noted", "show"})
    # The ceiling also carries the GESTURE-only defer verbs (#102 1b-ii), which
    # have no text grammar and must never gain one: F5 freezes the reply verb
    # set, and a defer is a swipe, not a word the operator types. So the sets
    # agree on the RESOLVER-BACKED verbs — which is what this pin has always
    # been about — with the gesture family excluded by name rather than by a
    # loosened comparison that would stop noticing a real addition.
    resolver_backed = set(FEED_ACTIONS["pending"]) - set(DEFER_ACTIONS)
    assert resolver_backed == PENDING_ONLY_OK_TOKENS, (
        "the capability ceiling's pending actions and the parser-side "
        "pending-only verbs must name the same set"
    )
    # And the defer family really is present — otherwise the subtraction above
    # would be silently vacuous and this pin would pass on a build that lost it.
    assert set(DEFER_ACTIONS) <= set(FEED_ACTIONS["pending"])
