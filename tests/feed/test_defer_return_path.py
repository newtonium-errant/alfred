"""Every kind carrying the generic defer verbs can bring a deferred card back.

THE FAILURE THIS CLOSES. #102 1b-ii folded the defer verbs into every decide
kind that was not explicitly excluded — a widening whose eligibility rule has
one author, which is the good property. What it could not check is the other
half: a defer is a PROMISE (the item is not judged, only moved, so it MUST
return), and the ONLY mechanism that returns one is a ``reconcile`` pass over
the kind. ``_revival_suppressed`` is the sole consumer of ``defer_window_open``
and it is reached from ``reconcile`` alone; ``/feed/items`` filters state by
exact string, so a ``deferred`` card is off every open query until something
reconciles it back.

``email_urgent`` had the verbs and no reconciler — the curator upserts it
per-item and nothing ever reconciles the kind — so a deferred urgent card was
TERMINAL. The model's own words: "a defer that never returns is a silent drop
wearing a politer name."

THE PIN IS THE PARTITION, not a list of today's answers. Every kind carrying
the verbs must be either declared in ``DEFER_RETURN_PATH`` (with its claim
VERIFIED against the named source below) or sit in ``DEFER_EXCLUDED_KINDS`` —
never both, never neither. A kind added to ``FEED_ACTIONS`` gains the verbs by
the auto-fold and fails here until its author answers the question, which is the
only form of this check that a future producer cannot walk past.

WHY TO TRUST THIS RULE RATHER THAN MERELY OBEY IT. The partition was not
shaped around tonight's answer, and the evidence is a kind nobody had this
rule in mind for: ``pattern_surfaced`` was excluded long before this pin
existed, for a reason of its own — its ``ignore`` verb is a windowed
set-aside written to the contact router's suppression map, and a second,
unconsulted set-aside beside it would be a promise broken. It ALSO happens
to have no reconciler, so it satisfies "reconciler OR excluded" through the
second branch without anyone arranging it. A rule the existing membership
already satisfies, with no carve-out written for it, is a rule that was
discovered rather than invented; ``email_urgent`` turning out to be the ONLY
residue when the whole ceiling was swept is the other half of that evidence.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from alfred.daily_sync import feed_producer
from alfred.daily_sync.action_router import (
    DEFER_ACTIONS,
    DEFER_EXCLUDED_KINDS,
    DEFER_RETURN_PATH,
    FEED_ACTIONS,
    STATUS_INVALID_ACTION,
    act,
)
from alfred.daily_sync.config import DailySyncConfig
from alfred.feed import FeedItem, FeedStore
from alfred.feed.model import STATE_OPEN
from alfred.transport import returns_feed


def kinds_carrying_defer(feed_actions: dict[str, dict] = FEED_ACTIONS) -> set[str]:
    """The kinds whose ceiling admits at least one generic defer verb.

    Derived from the live ceiling rather than listed, so the set this test
    reasons about is the set the router will actually accept.
    """
    return {
        kind for kind, verbs in feed_actions.items()
        if any(verb in verbs for verb in DEFER_ACTIONS)
    }


def unanswered_kinds(
    feed_actions: dict[str, dict],
    declared: dict[str, str],
    excluded: frozenset[str] | set[str],
) -> set[str]:
    """Kinds that carry the defer verbs and answer neither question.

    A PARAMETERISED helper rather than an inline expression, so the same
    predicate that guards production can be run against a hypothetical ceiling —
    which is how the "a future producer fails a test" claim gets demonstrated
    rather than asserted (see :meth:`TestThePartition.test_a_new_defer_carrying_kind_fails_until_answered`).
    """
    return kinds_carrying_defer(feed_actions) - set(declared) - set(excluded)


# --- the verifiers: each declaration is checked against its named source -----


def _reconciled_kinds_by_ast(module: ModuleType) -> tuple[set[str], int]:
    """Kinds this module passes to ``try_feed_reconcile``, plus the count it
    could not resolve statically.

    Resolves a literal argument directly and a NAME through the module's own
    namespace (``KIND_REMINDER_RETURNED`` is imported, so ``getattr`` is what
    reads it). A loop variable or an attribute is UNRESOLVED and reported as
    such rather than guessed at — an unresolved call is why the feed_producer
    verifier reads a registry instead.

    WHAT THIS PROVES, AND WHAT IT DOES NOT. Presence in the SOURCE, not
    execution. The walk finds the call, so a declaration naming the wrong kind
    IS caught — but a call sitting under ``if False:``, or behind any branch
    that never runs, reads as present and this returns it. Reachability at
    runtime is the producer lane's own tests to establish, not this one's:
    ``reminder_returned``'s reconcile is exercised by the returns-ring sweep
    tests (a card whose task closes really does go ``acted``), which is where
    that question belongs and where it is already answered. This file's
    standard for the other half is that a verifier nobody wrote is a claim
    nobody checked; its sibling is that a verifier which cannot see dead code
    has to say so.
    """
    source = Path(inspect.getfile(module)).read_text(encoding="utf-8")
    found: set[str] = set()
    unresolved = 0
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        if name != "try_feed_reconcile" or len(node.args) < 2:
            continue
        arg = node.args[1]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            found.add(arg.value)
        elif isinstance(arg, ast.Name):
            value = getattr(module, arg.id, None)
            if isinstance(value, str):
                found.add(value)
            else:
                unresolved += 1
        else:
            unresolved += 1
    return found, unresolved


def _brief_section_kinds() -> set[str]:
    """The ``feed_kind="..."`` literals in the brief daemon.

    The brief reconciles ``section.feed_kind`` — an attribute, so no static
    resolution of the call site is possible; the literals at the SectionResult
    construction sites are where the kinds actually are.
    """
    from alfred.brief import daemon as brief_daemon

    source = Path(inspect.getfile(brief_daemon)).read_text(encoding="utf-8")
    kinds: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.keyword) and node.arg == "feed_kind":
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                kinds.add(node.value.value)
    return kinds


def verify_declaration(kind: str, module_path: str) -> str | None:
    """``None`` when the declared return path really reconciles ``kind``,
    else the reason it does not.

    An UNKNOWN module is a failure, deliberately: adding a reconciler in a new
    module means adding five lines of verifier here, and that is the proof the
    declaration is a fact rather than an assertion. A verifier nobody wrote is
    a claim nobody checked.
    """
    if module_path == "alfred.daily_sync.feed_producer":
        # The strongest available check: the live registry the producer loops.
        if kind not in feed_producer._FAMILIES:
            return f"{kind!r} is not in feed_producer._FAMILIES"
        return None
    if module_path == "alfred.transport.returns_feed":
        found, _unresolved = _reconciled_kinds_by_ast(returns_feed)
        if kind not in found:
            return f"{kind!r} is not reconciled in {module_path} (found {sorted(found)})"
        return None
    if module_path == "alfred.brief.daemon":
        if kind not in _brief_section_kinds():
            return f"{kind!r} is not a brief section feed_kind"
        return None
    return (
        f"no verifier for {module_path!r} — write one so the declaration for "
        f"{kind!r} is checked rather than trusted"
    )


# --- the partition -----------------------------------------------------------


class TestThePartition:
    def test_every_defer_carrying_kind_declares_a_return_path_or_is_excluded(
        self,
    ) -> None:
        unanswered = unanswered_kinds(
            FEED_ACTIONS, DEFER_RETURN_PATH, DEFER_EXCLUDED_KINDS,
        )
        assert unanswered == set(), (
            f"these kinds carry the defer verbs and answer neither question: "
            f"{sorted(unanswered)}. A defer that cannot come back is a silent "
            f"drop — declare the reconciler in DEFER_RETURN_PATH, or put the "
            f"kind in DEFER_EXCLUDED_KINDS."
        )

    def test_an_excluded_kind_never_carries_the_verbs(self) -> None:
        """The auto-fold's own invariant, asserted from the outside: exclusion
        must actually REMOVE the capability, not merely annotate it."""
        leaked = DEFER_EXCLUDED_KINDS & kinds_carrying_defer()
        assert leaked == set(), sorted(leaked)

    def test_a_new_defer_carrying_kind_fails_until_answered(self) -> None:
        """THE CLAIM THIS PIN EXISTS TO MAKE, demonstrated on a hypothetical
        ceiling rather than promised in a comment.

        A producer adding a kind to FEED_ACTIONS gains the defer verbs by the
        auto-fold below it and inherits the promise that comes with them. Here
        that future is simulated: the same predicate that guards production
        flags the new kind, and stops flagging it the moment either question is
        answered — which is exactly the two doors the real rule offers.
        """
        hypothetical = {k: dict(v) for k, v in FEED_ACTIONS.items()}
        hypothetical["some_future_kind"] = {"ack": {}}
        for verb in DEFER_ACTIONS:  # what the auto-fold would do
            hypothetical["some_future_kind"][verb] = {}

        flagged = unanswered_kinds(
            hypothetical, DEFER_RETURN_PATH, DEFER_EXCLUDED_KINDS,
        )
        assert flagged == {"some_future_kind"}

        # Door 1 — declare a return path.
        declared = dict(DEFER_RETURN_PATH)
        declared["some_future_kind"] = "alfred.daily_sync.feed_producer"
        assert unanswered_kinds(hypothetical, declared, DEFER_EXCLUDED_KINDS) == set()

        # Door 2 — exclude it.
        excluded = set(DEFER_EXCLUDED_KINDS) | {"some_future_kind"}
        assert unanswered_kinds(hypothetical, DEFER_RETURN_PATH, excluded) == set()

        # And door 1 is not a rubber stamp: a declaration still has to survive
        # verification, which this false one does not.
        assert verify_declaration("some_future_kind", "alfred.daily_sync.feed_producer")

    def test_no_kind_is_both_excluded_and_declared(self) -> None:
        both = DEFER_EXCLUDED_KINDS & set(DEFER_RETURN_PATH)
        assert both == set(), sorted(both)

    def test_the_declaration_carries_no_kind_that_lost_the_verbs(self) -> None:
        """The other direction: a stale declaration for a kind that is now
        excluded (or gone) would read as coverage the system does not have."""
        stale = set(DEFER_RETURN_PATH) - kinds_carrying_defer()
        assert stale == set(), sorted(stale)

    def test_every_excluded_kind_is_a_real_ceiling_kind(self) -> None:
        """A phantom exclusion silently protects nothing."""
        phantom = DEFER_EXCLUDED_KINDS - set(FEED_ACTIONS)
        assert phantom == set(), sorted(phantom)

    def test_the_partition_is_not_vacuous(self) -> None:
        """Positive control. Every assertion above holds trivially if no kind
        carries the verbs at all — which is exactly what a broken auto-fold
        would look like."""
        carrying = kinds_carrying_defer()
        assert len(carrying) >= 5, sorted(carrying)
        assert "email_tier" in carrying
        assert "reminder_returned" in carrying


class TestTheDeclarationsAreVerifiedNotTrusted:
    @pytest.mark.parametrize("kind", sorted(DEFER_RETURN_PATH))
    def test_each_declared_return_path_really_reconciles_its_kind(
        self, kind: str,
    ) -> None:
        problem = verify_declaration(kind, DEFER_RETURN_PATH[kind])
        assert problem is None, problem

    def test_the_verifier_rejects_a_false_claim(self) -> None:
        """THE ANTI-VACUITY CONTROL, and the one that matters most here: a
        verifier that answers ``None`` for everything would make the whole class
        above green while checking nothing. Each branch is shown refusing a
        claim that is not true."""
        assert verify_declaration("weather", "alfred.daily_sync.feed_producer")
        assert verify_declaration("email_tier", "alfred.transport.returns_feed")
        assert verify_declaration("reminder_returned", "alfred.brief.daemon")
        # An unwritten verifier is a failure, not a pass.
        assert verify_declaration("email_tier", "alfred.some.new.producer")

    def test_the_ast_verifier_finds_the_real_call(self) -> None:
        """The reader itself, controlled: it must resolve the kind through the
        module namespace (the argument is an imported NAME, not a literal), and
        it must report what it could not resolve rather than silently dropping
        it."""
        found, unresolved = _reconciled_kinds_by_ast(returns_feed)
        assert found == {"reminder_returned"}, found
        assert unresolved == 0
        # And on a module whose call site passes a loop variable, it resolves
        # NOTHING and says so — which is why that kind's verifier reads the
        # registry instead of the source.
        producer_found, producer_unresolved = _reconciled_kinds_by_ast(feed_producer)
        assert producer_found == set()
        assert producer_unresolved >= 1


# --- email_urgent: the kind this lane closed ---------------------------------


def _act(store: FeedStore, tmp_path: Path, feed_id: str, verb: str) -> Any:
    cfg = DailySyncConfig(enabled=True, batch_size=5)
    cfg.corpus.path = str(tmp_path / "corpus.jsonl")
    cfg.state.path = str(tmp_path / "ds_state.json")
    return act(
        feed_id, verb, feed_store=store, config=cfg, vault_path=None,
        instance_name="salem", instance_scope="talker",
    )


class TestEmailUrgentIsClosed:
    def test_it_is_excluded_and_carries_no_defer_verb(self) -> None:
        assert "email_urgent" in DEFER_EXCLUDED_KINDS
        assert not any(v in FEED_ACTIONS["email_urgent"] for v in DEFER_ACTIONS)
        assert set(FEED_ACTIONS["email_urgent"]) == {"ack"}

    def test_a_defer_is_refused_and_leaves_the_card_open(self, tmp_path: Path) -> None:
        """The behavioural close, through the real act path. BEFORE this lane
        the same call returned ok=True with the item ``acted`` and
        ``acted_action='ack'`` — the operator said "later" and the system
        recorded a decision they never made. An honest refusal is the fix
        available tonight; the reconciler that lets the verb work is boarded."""
        store = FeedStore(str(tmp_path / "feed.jsonl"))
        item = FeedItem.create(
            kind="email_urgent", stable_key="note/Urgent.md", instance="salem",
            title="Urgent email: sender@example.com — subject",
            evidence={"record_path": "note/Urgent.md", "high_source": "llm"},
        )
        store.upsert(item)

        for verb in DEFER_ACTIONS:
            result = _act(store, tmp_path, item.id, verb)
            assert result.ok is False, verb
            assert result.status == STATUS_INVALID_ACTION, verb
            stored = store.load()[item.id]
            assert stored.state == STATE_OPEN, verb
            assert stored.acted_action is None, verb

        # Positive control in the same test: the verb it DOES have still works,
        # so the refusals above are the gate and not a dead act path.
        ok = _act(store, tmp_path, item.id, "ack")
        assert ok.ok is True
        assert store.load()[item.id].acted_action == "ack"


class TestTheInterceptRefusesIndependently:
    """The verb-gate on the ``URGENT_KIND`` intercept — defence in depth.

    Exclusion alone makes a defer refused because the ceiling no longer holds
    the verb, and the intercept's own membership test is what rejects it. That
    is CONTINGENT: the boarded reconciler lane exists precisely to put those
    verbs back, and on that day a kind-only intercept would resume converting
    every defer into an ack, because it sits above the defer dispatcher and
    answers first.

    So the intercept is gated on the VERB. With the ceiling temporarily widened
    back — the exact state the follow-up lane will create — a defer must reach
    its own dispatcher and land ``deferred``, not ``acted``.
    """

    def test_a_defer_reaches_its_dispatcher_when_the_ceiling_admits_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from alfred.daily_sync import action_router as arouter
        from alfred.feed.model import STATE_DEFERRED

        widened = {k: dict(v) for k, v in FEED_ACTIONS.items()}
        for verb in DEFER_ACTIONS:
            widened["email_urgent"][verb] = {}
        monkeypatch.setattr(arouter, "FEED_ACTIONS", widened)

        store = FeedStore(str(tmp_path / "feed.jsonl"))
        item = FeedItem.create(
            kind="email_urgent", stable_key="note/U2.md", instance="salem",
            title="Urgent email", evidence={"record_path": "note/U2.md"},
        )
        store.upsert(item)

        result = _act(store, tmp_path, item.id, "defer_3d")

        assert result.ok is True
        stored = store.load()[item.id]
        assert stored.state == STATE_DEFERRED, (
            "a defer on a ceiling that admits it must land deferred — a "
            "kind-only intercept would answer 'acted' here"
        )
        assert stored.deferred_until
