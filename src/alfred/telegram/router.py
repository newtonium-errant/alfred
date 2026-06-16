"""Opening-cue router — classify the first message of a new session.

When the bot gets a message and there's no active session, it calls
:func:`classify_opening_cue` with the message text and a compact summary of
recent closed sessions. The router runs one Sonnet API call with a pinned
instruction prompt and returns a :class:`RouterDecision`: session type,
model to use, and (optionally) the record path of a previous session to
continue.

Design notes:

- Pinned constant router model (:data:`alfred.telegram.session_types.ROUTER_MODEL`)
  per plan open question #7. Promote to config in wk3 if we need to swap
  without a code change.
- Prompt is inline here (plan open question #6) so the router is one file.
- JSON-only output — the model is instructed to emit nothing else. We
  parse with ``json.loads`` and fall back to ``note`` / default model /
  no continuation on any parse or network error. Graceful degradation is
  the whole point: a router failure should feel like wk1, not a crash.
- Article-continuation with no prior match stays on ``article`` / Opus
  (plan open question #8) — intent trumps absence of a prior. The
  continuation link is simply ``None``.
- Router returns no extra context, only a decision. The caller owns
  everything else (opening the session, pre-seeding transcript, logging).

The router prompt is deliberately short — the model doesn't need to know
anything about Alfred beyond "which of these five buckets does this
opening message fall into?".
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from ._anthropic_compat import messages_create_kwargs
from .session_types import (
    ROUTER_MODEL,
    SessionTypeDefaults,
    defaults_for,
    known_types,
)
from .utils import get_logger

log = get_logger(__name__)


# --- Data types -----------------------------------------------------------


@dataclass(frozen=True)
class RouterDecision:
    """The router's classification of one opening message.

    Attributes:
        session_type: Canonical type name (``note|task|journal|article|
            brainstorm|capture|peer_route``).
        model: Anthropic model id to start the session on. Usually the
            type's default, but the router may override (e.g. "quick article
            note" → Sonnet even for article type).
        continues_from: Record path of a previous session to resume, or
            ``None``. Populated when the opening cue implies continuation
            AND a matching record was found in recent state.
        reasoning: One-line rationale the model emitted — purely for logs.
            Safe to empty.
        target: Stage 3.5 — when ``session_type == "peer_route"``, which
            peer should receive the forwarded message. Canonical lowercase
            peer names (``kal-le``, ``stay-c``). ``None`` for every other
            session type.
        peer_route_hint: Optional — why the router thinks this routes to
            the target. Purely informational; Salem's routing uses
            ``target``, not the hint.
    """

    session_type: str
    model: str
    continues_from: str | None
    reasoning: str = ""
    target: str | None = None
    peer_route_hint: str = ""


# --- Prompt ---------------------------------------------------------------

# Kept short and explicit. The "only JSON" line is load-bearing — without it
# the model occasionally wraps the object in prose, which we'd then have to
# regex-extract. Failing closed to ``note`` is fine for one-off bad JSON,
# but regular JSON noise would mean the router never routes anything.
#
# Stage 3.5 hotfix c3: ``{self_name}`` / ``{self_display_name}`` are
# templated in per-call so each instance knows who it is. The classifier
# MUST NOT route to itself — if KAL-LE's classifier sees
# "KAL-LE, run pytest" and emits peer_route target=kal-le, that's
# self-referential and we fall back to note at parse time (see
# ``_decision_from_parsed``).
_ROUTER_PROMPT = """\
You classify the opening message of a Telegram voice/text session with \
Alfred (a personal assistant). Pick ONE session type and (optionally) \
flag continuation of a prior session.

You are running on instance "{self_name}". NEVER classify peer_route \
with target="{self_name}" — that would route to yourself. If the user \
addresses your own instance by name (e.g., "{self_display_name}, ..."), \
strip the address and classify the remaining content normally. Route \
to OTHER instances only.

Reply context: has_reply_context={has_reply_context}. When this is \
true, the user's message begins with a machine-generated \
`[You are replying to ...]` prefix quoting a specific earlier bot \
message — they long-pressed it in Telegram and hit Reply. This is a \
strong signal that they are continuing a prior line of thought, not \
opening a fresh session. Strongly prefer `continues_from` against a \
matching recent session, OR fall back to `note` (a lightweight \
follow-up). Do NOT open `capture`, `journal`, `article`, or \
`brainstorm` sessions on a reply unless the user's text after the \
prefix contains an explicit opening cue for that type (e.g., \
"capture: ..." or "let's brainstorm"). `peer_route` is still valid \
if the reply is asking KAL-LE to do coding work; the reply signal \
only overrides cue-driven type selection, not routing.

Types:
- note: quick capture, one-off reminders, short observations.
- task: "create a task", "remind me to", "add a task". Assistant will \
act, not converse.
- journal: introspective / reflective ("I want to think through X", \
"how am I feeling about Y").
- article: long-form drafting or editing ("let's continue the article", \
"work on the draft").
- brainstorm: divergent idea generation ("brainstorm names for X", \
"ideas for Y").
- capture: silent brainstorm-capture ("let me brainstorm", "thinking \
out loud", "I want to ramble", "just let me talk for a while"). The \
user wants to dump thoughts without interruption; the assistant stays \
silent mid-session and a structured summary is produced at /end.
- peer_route: coding, debugging, testing, or aftermath-lab curation \
work that belongs on KAL-LE (the coding instance). Cues (expanded): \
  * Direct addressing at start: "KAL-LE, X", "KAL-LE: X", "K.A.L.L.E., \
X" — strongly prefer peer_route target=kal-le (after verifying it's \
not self-address per the rule above).
  * Test running: "run pytest", "run the tests", "check the tests", \
"check the output of pytest", "check the output of X tests", \
"pytest tests/X", "npm test", "npm run lint".
  * Test debugging: "fix the broken test", "debug this test", "trace \
the failure", "why is X test failing".
  * Code work: "write a function", "refactor this", "add a test", \
"review the last three commits", "look at the diff on this branch".
  * Git inspection: "git status", "git diff", "what's on this branch", \
"show me the log".
  * Aftermath-lab curation: "promote this pattern to canonical", \
"ask kal-le about X", "why is the transport scheduler firing twice".

NEVER classify peer_route for morning-brief / digest status confirmations \
and acknowledgements — these are Salem-local bookkeeping, NOT a request for \
a peer to do work. This OVERRIDES every cue above, including direct \
addressing, git issues, and peer names. Specifically, classify as `note` \
(local), never peer_route, when the message is:
  * A tier-confirm command: "T1 confirm", "T2 confirm", "T3 confirm X", \
"T2 add ...", "T3 drop ...", or a bare leading status verb ("confirmed", \
"done", "closed", "keep", "drop").
  * An operator confirming or acknowledging an item from a brief, digest, \
or peer digest — phrasings of the shape "X confirmed closed", "X confirmed \
done", "X is done", "X confirmed in the digest/brief", "got it, X is \
closed", "ack X".
  * The SAME confirmation even when it names a git issue (gh#N), a ticket, \
OR another instance/peer by name. A message can mention "gh#7", "Vera", or \
"peer digest" and STILL be pure bookkeeping — mentioning a peer is not the \
same as asking that peer to do something.
The test: does the message ask a peer to DO new coding, debugging, testing, \
or curation work (→ peer_route), or does it merely RECORD / CONFIRM / \
ACKNOWLEDGE the status of something already done (→ note)? Status reports \
about closed/done items are always note, regardless of which issues or \
peers they name. peer_route is ONLY for dispatching new work to a peer.

Worked examples:
  * "Check Vera gh#7 confirmed closed in peer digest" → session_type=note, \
target=null. Confirming a closed digest item; names gh#7 + a peer but asks \
no one to DO anything.
  * "KAL-LE, run pytest on the new branch" → session_type=peer_route, \
target=kal-le. Dispatching new test work.

When you classify peer_route, set "target" to the peer name (``kal-le`` \
today; ``stay-c`` once that instance is live). Without a target, \
peer_route is malformed — fall back to note.

Continuation: if the user says "continue the last journal", "pick up the \
article we were writing", "same brainstorm as yesterday", etc., AND the \
recent sessions list below contains a matching session, set \
"continues_from" to that session's record_path. Otherwise null.

Recent sessions (most recent first):
{recent_summary}

Opening message:
{opening}

Respond with ONLY a JSON object, no prose, no markdown fences:
{{"session_type": "<one of: note, task, journal, article, brainstorm, capture, peer_route>",
  "continues_from": "<record_path or null>",
  "target": "<peer name or null — required when session_type is peer_route>",
  "peer_route_hint": "<one short sentence if peer_route, else null>",
  "reasoning": "<one short sentence>"}}
"""


# --- Deterministic prefix detection ---------------------------------------

# The ``capture:`` prefix forces capture-session dispatch without an LLM
# call. Matched case-insensitively against the leading token of the
# opening message. The prefix is load-bearing: if the user explicitly
# signals "this is a capture session" we should never round-trip to a
# classifier and risk a mis-route on a borderline phrasing.
_CAPTURE_PREFIX: str = "capture:"


def _detect_capture_prefix(message: str) -> bool:
    """Return True iff ``message`` starts with a ``capture:`` prefix.

    Case-insensitive, leading-whitespace-tolerant. Deliberately narrow —
    we check only the leading literal ``capture:`` token, not "let's
    capture" or "capturing thoughts now" — those borderline cases are
    the LLM classifier's job.
    """
    if not message:
        return False
    return message.lstrip().lower().startswith(_CAPTURE_PREFIX)


# --- Brief / status-confirm guard (peer-route bleed-stop §1a) --------------
#
# 2026-06-16 incident: "Check Vera gh#7 confirmed closed in peer digest" was
# mis-classified ``peer_route target=kal-le`` (the "gh#7" code-cue + literal
# "peer" tripped the LLM router), force-forwarded to KAL-LE, and never worked
# — the operator was confirming a morning-brief item, not asking KAL-LE to do
# work. This guard is the DETERMINISTIC, 100%-reliable half of the fix
# (operator directive: the T#-confirm / brief-confirm class must never depend
# on a probabilistic classifier). Mirrors the ``_detect_capture_prefix``
# discipline above: anchored at message start, case-insensitive,
# leading-whitespace-tolerant, deliberately NARROW.
#
# TIGHT by ratified decision: it matches ONLY the canonical anchored grammar
# (source-of-truth ``brief/tier_section.py``: "T1/T2/T3 confirm|add|...",
# plus bare leading status verbs). It deliberately does NOT try to match the
# fuzzy "X confirmed closed in [the] digest" shape — that is the prompt
# layer's job (the ``_ROUTER_PROMPT`` exclusion block, §1b). A greedier
# regex here would eat legit peer phrasings like "confirm the test passed,
# KAL-LE", which is exactly the false-positive we must avoid.
#
# Returns the matched-pattern LABEL (a short, log-safe string) on a hit, or
# ``None`` on no match. The label is the correction-signal substrate for the
# self-correcting-by-design standard (CLAUDE.md): callers emit it on the ILB
# log so accumulated mis-fires are greppable + tunable at morning cadence.

# Tier grammar: ``T1 confirm`` / ``T2 add`` / ``T3 drop`` etc. ``[123]`` and
# the verb set track the canonical brief reply patterns. ``\b`` after the
# verb means "T1 confirmation" (a longer word) does NOT match — only the
# bare verb or verb-plus-args ("T3 confirm walk Fergus").
_CONFIRM_GUARD_TIER_RE = re.compile(
    r"^\s*[Tt][123]\s+(confirm|add|drop|done|keep)\b",
    re.IGNORECASE,
)

# Bare leading status verbs: "confirmed", "done", "closed", etc. opening the
# message. Anchored so "I want KAL-LE to confirm X" (verb mid-sentence) does
# NOT match — only a message that LEADS with the confirmation verb.
_CONFIRM_GUARD_STATUS_RE = re.compile(
    r"^\s*(confirm|confirmed|done|closed|keep|drop)\b",
    re.IGNORECASE,
)


def is_brief_or_status_confirm(message: str) -> str | None:
    """Return a matched-pattern label iff ``message`` is a brief/status confirm.

    Deterministic peer-route exclusion guard (§1a). When this returns a
    non-``None`` label the message is a morning-brief / status confirmation
    (e.g. ``"T1 confirm"``, ``"T2 add eggs"``, ``"done"``, ``"confirmed"``)
    and MUST NOT peer-route — the caller forces local handling.

    The return value is the matched-pattern label, NOT a bare bool, so
    callers can log WHICH pattern fired (the correction-signal substrate for
    the self-correcting-design standard). Labels:

      * ``"tier_grammar"``  — anchored ``T[123] (confirm|add|drop|done|keep)``
      * ``"status_verb"``   — anchored bare leading status verb

    Returns ``None`` when the message is not a confirm (the common case —
    peer-route inference proceeds normally).

    Deliberately TIGHT (ratified): does not match fuzzy "X confirmed closed
    in digest" phrasings — those are the ``_ROUTER_PROMPT`` exclusion block's
    job (§1b). Tightness is the false-positive defense: "confirm the test
    passed, KAL-LE" leads with "confirm" so it WOULD match the status-verb
    rule — see the §1b prompt block + the test pin
    ``test_confirm_guard_does_not_eat_legit_peer_work`` for the boundary
    we accept here (such a phrasing is rare; the cost of a missed peer-route
    is the operator re-sending, vs. the cost of a swallowed confirm which is
    silent data-loss — so we err toward catching confirms).
    """
    if not message:
        return None
    if _CONFIRM_GUARD_TIER_RE.match(message):
        return "tier_grammar"
    if _CONFIRM_GUARD_STATUS_RE.match(message):
        return "status_verb"
    return None


# --- Helpers --------------------------------------------------------------


def _format_recent_summary(recent: list[dict[str, Any]]) -> str:
    """Render the recent-sessions list as a compact multi-line summary.

    Shape per entry (from ``state.closed_sessions``):
        {"record_path": str, "session_type": str|None,
         "started_at": str, "ended_at": str, ...}

    We only expose the three fields the router needs; anything else would
    just waste tokens. Limits to 10 lines to keep the prompt bounded.
    """
    if not recent:
        return "(none — first session)"
    lines: list[str] = []
    for entry in recent[:10]:
        path = entry.get("record_path", "")
        stype = entry.get("session_type") or "note"
        started = entry.get("started_at", "")
        lines.append(f"- {stype} @ {started[:10]} → {path}")
    return "\n".join(lines)


def _extract_text(response: Any) -> str:
    """Pull concatenated text from an Anthropic response's content list."""
    content = getattr(response, "content", None) or []
    parts: list[str] = []
    for block in content:
        if getattr(block, "type", None) == "text":
            text = getattr(block, "text", "")
            if text:
                parts.append(text)
    return "\n".join(parts).strip()


def _parse_decision(raw: str) -> dict[str, Any] | None:
    """Best-effort parse of the router's JSON-only response.

    Returns ``None`` on any parse failure — the caller then falls back to
    the default decision. We deliberately don't regex-extract a JSON
    substring: if the model ignored the "only JSON" instruction, we want
    the fallback to fire so the bug is visible in logs.
    """
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def _fallback_decision(reason: str) -> RouterDecision:
    """Return the safe-default decision used whenever the router errors."""
    defaults = defaults_for("note")
    return RouterDecision(
        session_type=defaults.session_type,
        model=defaults.model,
        continues_from=None,
        reasoning=reason,
    )


# Globally-known peer names — used as the fallback acceptance set when a
# caller doesn't pass an instance-specific ``valid_peer_targets``. The
# instance-specific set (sourced from ``transport.peers`` keys minus
# ``local`` / self) is what production callers SHOULD pass; this fallback
# preserves backwards compatibility for tests and any legacy in-process
# caller that hasn't been updated yet. See #62: a hardcoded global set
# silently accepted ``kal-le`` on Hypatia (whose ``transport.peers`` is
# ``[local, salem]``), causing the bot to attempt a route to a peer it
# wasn't configured to reach.
_VALID_PEER_TARGETS: set[str] = {"kal-le", "stay-c"}


def _decision_from_parsed(
    parsed: dict[str, Any],
    recent: list[dict[str, Any]],
    self_name: str = "",
    valid_peer_targets: set[str] | None = None,
) -> RouterDecision:
    """Build a :class:`RouterDecision` from a parsed JSON dict.

    Applies defaults from :mod:`session_types`, validates the session type
    (unknown → ``note``), and validates ``continues_from`` against the
    recent-sessions list (if the model hallucinated a record path, we
    refuse it rather than feed a phantom into the opener).

    Stage 3.5 addition: when ``session_type == "peer_route"``, require
    a valid ``target``. A missing or unknown target coerces back to
    ``note`` — we won't forward to a phantom peer.

    Stage 3.5 hotfix c3: ``self_name`` is the local instance's peer-key
    name (``salem``, ``kal-le``). Even though the prompt instructs the
    classifier NOT to emit ``peer_route target=<self>``, the model can
    still do it — so we guard at parse time too (degrade-to-note with a
    warning log). Empty string disables the check (tests and legacy
    callers).

    Issue #62 fix: ``valid_peer_targets`` is the per-instance set of
    peer-key names (sourced from this instance's ``transport.peers``
    config, minus ``local`` and self). When provided, the peer-target
    validation uses THIS set instead of the global
    :data:`_VALID_PEER_TARGETS`. Result: Hypatia (whose
    ``transport.peers`` is ``[local, salem]``) rejects
    ``target=kal-le`` even though kal-le is a globally-known peer
    name. ``None`` falls back to the hardcoded global set so existing
    callers/tests keep working.
    """
    session_type = parsed.get("session_type") or "note"
    if session_type not in known_types():
        log.info(
            "talker.router.unknown_type_coerced_to_note",
            session_type=session_type,
        )
        session_type = "note"

    defaults: SessionTypeDefaults = defaults_for(session_type)
    model = defaults.model

    # Peer-route target validation. A peer_route classification without
    # a known target degrades to ``note`` — we'd rather fall through to
    # Salem's normal handling than forward to nobody.
    #
    # Issue #62: prefer the per-instance ``valid_peer_targets`` (caller-
    # supplied, sourced from ``transport.peers`` minus ``local``/self)
    # over the hardcoded global set. The fallback exists so legacy/test
    # callers that don't pass a per-instance set still get the old
    # behaviour. Logs distinguish the two rejection modes so future
    # debugging can tell "unknown peer name globally" (old hardcoded
    # check) from "valid global name but not configured on THIS
    # instance" (new per-instance check).
    accepted_targets = (
        valid_peer_targets if valid_peer_targets is not None
        else _VALID_PEER_TARGETS
    )
    target: str | None = None
    peer_route_hint: str = ""
    if session_type == "peer_route":
        raw_target = parsed.get("target")
        if isinstance(raw_target, str) and raw_target.lower() in accepted_targets:
            candidate = raw_target.lower()
            # c3 parse-time guard: even with the "never self-target"
            # instruction in the prompt, the classifier can still emit
            # target=<self>. Drop to note with a warning so we can
            # track how often the instruction is ignored.
            if self_name and candidate == self_name:
                log.warning(
                    "talker.router.peer_route_self_target_coerced",
                    self_name=self_name,
                    raw_target=raw_target,
                    reason="classifier ignored self-target instruction",
                )
                session_type = "note"
                defaults = defaults_for("note")
                model = defaults.model
            else:
                target = candidate
                peer_route_hint = str(parsed.get("peer_route_hint") or "")[:200]
        elif (
            valid_peer_targets is not None
            and isinstance(raw_target, str)
            and raw_target.lower() in _VALID_PEER_TARGETS
        ):
            # NEW (#62): the target is a globally-known peer name but not
            # configured on THIS instance. Distinct log so the failure
            # mode is debuggable — operators can see at a glance that
            # the classifier emitted a plausible target the local
            # transport just doesn't know about.
            log.warning(
                "talker.router.peer_route_target_not_configured",
                raw_target=str(raw_target)[:80],
                valid_peers=sorted(accepted_targets),
            )
            session_type = "note"
            defaults = defaults_for("note")
            model = defaults.model
        else:
            log.info(
                "talker.router.peer_route_missing_target",
                raw_target=str(raw_target)[:80],
            )
            # Degrade to note — no phantom forwarding.
            session_type = "note"
            defaults = defaults_for("note")
            model = defaults.model

    # Continuation handling. Only trust ``continues_from`` if (a) the type
    # supports continuation and (b) the record path appears in our recent
    # state. This defends against model hallucination — the router can
    # invent a plausible-looking path, but state is the source of truth.
    raw_cont = parsed.get("continues_from")
    continues_from: str | None = None
    if (
        defaults.supports_continuation
        and isinstance(raw_cont, str)
        and raw_cont
        and raw_cont != "null"
    ):
        known_paths = {e.get("record_path") for e in recent}
        if raw_cont in known_paths:
            continues_from = raw_cont
        else:
            log.info(
                "talker.router.unknown_continuation_dropped",
                raw=raw_cont[:80],
            )

    reasoning = parsed.get("reasoning") or ""
    if not isinstance(reasoning, str):
        reasoning = str(reasoning)

    return RouterDecision(
        session_type=session_type,
        model=model,
        continues_from=continues_from,
        reasoning=reasoning[:200],  # trim for log friendliness
        target=target,
        peer_route_hint=peer_route_hint,
    )


# --- Public entry point ---------------------------------------------------


async def classify_opening_cue(
    client: Any,
    first_message: str,
    recent_sessions: list[dict[str, Any]],
    self_name: str = "",
    self_display_name: str = "",
    has_reply_context: bool = False,
    valid_peer_targets: set[str] | None = None,
) -> RouterDecision:
    """Classify one opening message; return a :class:`RouterDecision`.

    Args:
        client: An Anthropic ``AsyncAnthropic`` client (or any object with
            a ``messages.create`` async method).
        first_message: The text of the user's opening message.
        recent_sessions: Most-recent-first list of closed-session summaries
            from ``state.closed_sessions``.
        self_name: Stage 3.5 hotfix c3 — the local instance's peer-key
            name (``salem`` / ``kal-le``). Used to (a) tell the classifier
            NEVER to route to self via the prompt instruction, and (b)
            guard at parse time in case the classifier ignores the
            instruction. Defaults to ``""``; per
            ``feedback_hardcoding_and_alfred_naming.md`` a misconfigured
            caller produces a degraded prompt rather than silently
            routing as Salem. The parse-time self-target guard treats
            empty as "no check" (see ``_decision_from_parsed``).
        self_display_name: The human-facing form of the instance name
            (``Alfred`` / ``Salem`` / ``K.A.L.L.E.``) — appears in the
            prompt's self-address example. Defaults to ``""`` for the
            same loud-failure reason as ``self_name``.
        has_reply_context: Reply-context consumer hint. ``True`` when the
            incoming message is a Telegram reply to a prior bot message
            (``update.message.reply_to_message`` was populated and
            rendered into a ``[You are replying to ...]`` prefix in
            ``first_message``). Tells the classifier to tip its default
            toward continuation / note, away from fresh cue-driven
            types. Defaults to ``False`` for legacy callers and
            non-reply messages.
        valid_peer_targets: Issue #62 fix — the per-instance set of
            peer-key names this instance is configured to reach
            (sourced from ``transport.peers`` keys minus ``local`` and
            self). When provided, ``peer_route`` classifications with a
            target NOT in this set degrade to ``note`` even if the
            target is a globally-known peer name. ``None`` (default)
            falls back to the hardcoded global set for backwards
            compatibility with legacy/test callers; production
            ``handle_message`` flow always passes the per-instance
            set.

    Returns:
        A :class:`RouterDecision`. Any error (network, bad JSON, unknown
        type, hallucinated continuation, phantom self-target) degrades to
        a ``note`` / Sonnet / no-continuation decision. That keeps the
        user-visible behaviour identical to wk1 whenever the router is
        unreliable.
    """
    if not first_message:
        return _fallback_decision("empty_message")

    # Deterministic capture-prefix short-circuit. Runs BEFORE the LLM
    # call: an explicit ``capture:`` prefix is a user-asserted
    # classification and we must never round-trip it to the classifier.
    # Continuation is disabled for capture (``supports_continuation=False``
    # on the session-type defaults), so ``continues_from`` is always None
    # for this branch.
    if _detect_capture_prefix(first_message):
        capture_defaults = defaults_for("capture")
        log.info(
            "talker.router.capture_prefix",
            session_type="capture",
            model=capture_defaults.model,
        )
        return RouterDecision(
            session_type=capture_defaults.session_type,
            model=capture_defaults.model,
            continues_from=None,
            reasoning="capture: prefix (deterministic)",
        )

    prompt = _ROUTER_PROMPT.format(
        recent_summary=_format_recent_summary(recent_sessions),
        opening=first_message.strip(),
        self_name=self_name,
        self_display_name=self_display_name,
        has_reply_context=str(has_reply_context).lower(),
    )

    try:
        response = await client.messages.create(**messages_create_kwargs(
            model=ROUTER_MODEL,
            max_tokens=256,
            # Low temperature — classification, not creative writing. A
            # non-zero value lets the model escape obvious local-minima
            # ("always classify as note") without the noise of ``1.0``.
            temperature=0.2,
            messages=[{"role": "user", "content": prompt}],
        ))
    except Exception as exc:  # noqa: BLE001 — network / SDK failures mustn't crash the bot
        log.warning("talker.router.api_error", error=str(exc))
        return _fallback_decision("api_error")

    raw = _extract_text(response)
    parsed = _parse_decision(raw)
    if parsed is None:
        log.warning("talker.router.parse_failed", raw_head=raw[:200])
        return _fallback_decision("parse_failed")

    decision = _decision_from_parsed(
        parsed, recent_sessions, self_name,
        valid_peer_targets=valid_peer_targets,
    )
    log.info(
        "talker.router.decided",
        session_type=decision.session_type,
        model=decision.model,
        continues=decision.continues_from is not None,
    )
    return decision
