"""Section-provider registry + message assembler + reply parser.

A section provider is a callable ``(config, today) → str | None``.
Returning a string adds the section to the assembled message; returning
``None`` omits the section entirely (the assembler does NOT render an
empty header).

The framework is provider-agnostic. c2 ships exactly one provider
(:mod:`daily_sync.email_section`); friction-queue and open-questions
providers are deferred per the memo. To register a provider in code:

    from alfred.daily_sync import register_provider
    register_provider("email_calibration", priority=10, provider=my_callable)

The integer ``priority`` is the sort key (lower = renders first). The
"Calibration batches" section is the highest-priority slot per
``project_daily_sync_ooda.md``, so c2's email provider claims
``priority=10``. Future friction-queue would be ~20, open-questions ~30.
"""

from __future__ import annotations

import inspect
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, Optional

from .config import DailySyncConfig

# Type alias for the section provider callable contract. ``today`` is
# tz-aware in the daemon but ``date.today()`` (naive) in tests; either
# works because providers only use it for header rendering, never math.
#
# Providers that produce numbered items (email calibration, attribution
# audit) accept an optional ``start_index: int = 1`` keyword so the
# global numbering across sections stays continuous (item 1 in email →
# item 6 in attribution if email rendered 5 items). Providers that don't
# need it omit the kwarg; the assembler inspects the signature and
# only forwards it when present.
SectionProvider = Callable[..., Optional[str]]


# Header rendered when every provider returned None. We still send a
# message rather than skip silently because the operator-visibility
# stance from brief Phase 1 applies here too: a missing morning ping
# should be rare enough to be alarming, not normal.
EMPTY_SYNC_BODY = (
    "Daily Sync — {date}\n\n"
    "No items today. Reply if you want to surface anything for me."
)


# Banner prepended to every Daily Sync message so the reply parser can
# distinguish a Daily Sync push from any other Salem message in
# the conversation. Match is by Telegram message_id (state-file
# round-trip), but the banner also helps Andrew at a glance.
SYNC_BANNER = "Daily Sync — {date}"


@dataclass
class _ProviderEntry:
    name: str
    priority: int
    provider: SectionProvider
    # Optional callable returning the count of items the provider
    # rendered on its most recent call. Used by ``assemble_message`` to
    # advance the global ``start_index`` across sections so item
    # numbering is continuous (email items 1..5, attribution items
    # 6..10). Providers that don't produce numbered items omit it.
    item_count_after: Callable[[], int] | None = None


# Module-level registry. Providers register themselves at import time
# (the bot module imports ``daily_sync.email_section`` which calls
# ``register_provider``). For tests, ``clear_providers`` resets the
# registry between cases.
_REGISTRY: list[_ProviderEntry] = []


def register_provider(
    name: str,
    *,
    priority: int,
    provider: SectionProvider,
    item_count_after: Callable[[], int] | None = None,
) -> None:
    """Add a section provider to the registry.

    Raises :class:`ValueError` when the same ``name`` is registered
    twice — keeps the test surface deterministic. Tests can call
    :func:`clear_providers` to reset.

    ``item_count_after`` is an optional callable invoked AFTER the
    provider returns; its int result is added to the running global
    ``start_index`` so the next provider's items are numbered
    continuously. Providers that don't produce numbered items omit it.
    """
    if any(entry.name == name for entry in _REGISTRY):
        raise ValueError(f"section provider {name!r} already registered")
    _REGISTRY.append(_ProviderEntry(
        name=name,
        priority=priority,
        provider=provider,
        item_count_after=item_count_after,
    ))
    _REGISTRY.sort(key=lambda e: (e.priority, e.name))


def registered_providers() -> list[str]:
    """Return the names of currently-registered providers, in render order."""
    return [entry.name for entry in _REGISTRY]


def clear_providers() -> None:
    """Reset the registry. Test helper — never called in production."""
    _REGISTRY.clear()


def _provider_accepts_start_index(provider: SectionProvider) -> bool:
    """Return True when ``provider`` declares a ``start_index`` parameter.

    Providers that produce numbered items take ``start_index`` so the
    assembler can keep item numbers continuous across sections; older
    providers without the parameter render unaffected.
    """
    try:
        sig = inspect.signature(provider)
    except (TypeError, ValueError):
        return False
    return "start_index" in sig.parameters


def assemble_message(
    config: DailySyncConfig,
    today: date,
) -> str:
    """Run every registered provider and join the non-empty outputs.

    Empty case (every provider returned ``None``) → returns
    :data:`EMPTY_SYNC_BODY` rendered with today's date so the operator
    still gets a daily ping. Per the memo: visibility beats silence.

    Item numbering is global across sections: the assembler tracks a
    running ``start_index`` (1-based) and forwards it to providers that
    accept the kwarg. After each provider returns, the assembler calls
    ``item_count_after`` (when registered) and bumps ``start_index`` by
    that many — so email's 5 items stay 1..5 and attribution's items
    pick up at 6.
    """
    sections: list[str] = []
    next_index = 1
    for entry in _REGISTRY:
        try:
            if _provider_accepts_start_index(entry.provider):
                result = entry.provider(config, today, start_index=next_index)
            else:
                result = entry.provider(config, today)
        except Exception as exc:  # noqa: BLE001 — provider failure never crashes the daily sync
            sections.append(
                f"[{entry.name}] section provider failed: {exc.__class__.__name__}: {exc}"
            )
            continue
        if result is None:
            continue
        text = result.strip()
        if text:
            sections.append(text)
        # Bump the running index regardless of whether the section
        # rendered text — a provider that produced 0 items contributes
        # 0 to the count, which is the right outcome.
        if entry.item_count_after is not None:
            try:
                count = int(entry.item_count_after())
            except (TypeError, ValueError):
                count = 0
            if count > 0:
                next_index += count

    if not sections:
        return EMPTY_SYNC_BODY.format(date=today.isoformat())

    banner = SYNC_BANNER.format(date=today.isoformat())
    body = "\n\n".join(sections)
    return f"{banner}\n\n{body}"


# ---------------------------------------------------------------------------
# Reply parser — terse Telegram replies → structured corrections
# ---------------------------------------------------------------------------
#
# Andrew replies in informal forms. The parser must tolerate:
#
#   "✅"             → all confirmed (no item-level changes)
#   "ok"             → all confirmed
#   "all good"       → all confirmed
#   "2 down, 4 spam" → item 2 → low (down = next-tier-down), item 4 → spam
#   "2: actually high — Jamie was waiting" → item 2 → high, with note
#   "1 ok, 3 down"   → item 1 confirmed, item 3 → next-tier-down
#
# The parser does NOT have access to the original batch's per-item
# classifier tier — that's the daemon/dispatcher's job. So "down" and
# "up" produce :class:`ReplyCorrection` instances with ``new_tier=None``
# and ``modifier="down"``/``"up"``; the caller resolves them by looking
# at the batch.
#
# Unparseable fragments are recorded in :attr:`ReplyParseResult.unparsed`
# so the bot can produce a "I couldn't parse: ..." reply.

# Modifiers in priority order. ``ok`` MUST be checked BEFORE numeric
# tier names so "1 ok" doesn't accidentally match a tier name.
_TIER_TOKENS = {
    "high": "high",
    "medium": "medium",
    "med": "medium",
    "low": "low",
    "spam": "spam",
}
_RELATIVE_TOKENS = {
    "up": "up",
    "down": "down",
}
_OK_TOKENS = {
    "ok",
    "okay",
    "good",
    "yes",
    "y",
    "confirmed",
    # Attribution-audit verbs that semantically equal "confirm this
    # item as-is". For email items these still mean "andrew_priority
    # equals classifier_priority"; for attribution items the dispatcher
    # interprets ``ok=True`` as "flip the marker to confirmed".
    "confirm",
    "keep",
    "✅",
    # Pending Items Queue verbs (Phase 1). ``noted`` resolves a
    # pending item without action; ``show`` is the leading verb of
    # the multi-word ``"show me"`` form (the parser only consumes
    # the first token, so ``"3 show me"`` matches ``show`` here +
    # ``me`` lands in the note). The dispatcher distinguishes
    # ``noted`` vs ``show me`` via the ``correction.note`` content
    # AND the resolution_option labels on the item.
    "noted",
    "show",
}
# Reject verbs — only meaningful for attribution items (email items
# don't have a "delete the section" action). The dispatcher routes a
# reject correction onto ``reject_marker`` for attribution items and
# onto the unparsed bucket for email items.
_REJECT_TOKENS = {"reject", "delete", "remove", "no"}

# "Same" / "Same as above" / "Ditto" / "^" carrot — chain intent from a
# prior item. Andrew uses these when several items share the same
# correction (e.g. a run of routine Borrowell pings that all become
# "spam"). The parser captures the chain reference and
# :func:`parse_reply` resolves it by copying the source item's
# tier/modifier/ok/reject onto this item.
#
# Supported forms (case-insensitive; a leading bullet/carrot is
# stripped via :func:`_strip_bullet` before this regex runs):
#
#   "same"                     → prior item
#   "same."                    → prior item
#   "Same as above"            → prior item
#   "ditto"                    → prior item
#   "same as 1" / "same as #1" → explicit reference to item 1
#
# Trailing content after a dash/colon/em-dash/comma is preserved as
# note content (``"Same - also, why was this ranked lower?"`` keeps
# the question for downstream consumers even though the tier inherits).
_SAME_CHAIN_RE = re.compile(
    r"""
    ^\s*
    (?:                                       # chain token (either form)
        same(?:\s+as(?:\s+\#?(?P<explicit>\d+)|\s+above)?)?
      | ditto
    )
    \s*
    (?:                                       # optional trailing note, any separator
        [.!]?\s*(?:[—–\-:]+|,)\s*(?P<note>.*)
      | [.!]?\s*$
    )
    """,
    re.IGNORECASE | re.VERBOSE | re.DOTALL,
)


# Whole-message ack tokens. Any of these alone (after stripping
# whitespace and the leading bullet) means "all items confirmed as
# classified, no changes". Emoji + word forms.
_ALL_OK_PATTERNS = re.compile(
    r"^(?:✅|✔|👍|ok|okay|all good|all ok|looks good|approved)\s*[.!]?\s*$",
    re.IGNORECASE,
)


@dataclass
class ReplyCorrection:
    """One per-item correction extracted from Andrew's reply.

    ``item_number`` is 1-indexed (matches the rendered batch). ``new_tier``
    is the explicit tier name when Andrew supplied one, else ``None`` and
    ``modifier`` carries the relative direction ("down"/"up"). ``ok`` is
    True when Andrew explicitly confirmed an item without changing
    anything (e.g. "1 ok" or "6 confirm"). ``reject`` is True for the
    attribution-audit reject verbs (``reject``/``delete``/``remove``/
    ``no``) which only make sense on attribution items — the dispatcher
    routes a ``reject`` correction onto ``reject_marker`` for an
    attribution item and onto the unparsed bucket for an email item.
    ``note`` carries any free-text reasoning.

    ``consumed_token`` (Phase 1 Pending Items) records the literal
    token the parser matched for this correction (``"noted"``,
    ``"show"``, ``"ok"``, ``"high"``, etc.). The pending-items
    dispatcher routes on this so ``"3 noted"`` and ``"3 show me"``
    both produce ``ok=True`` corrections but resolve to different
    resolution_option ids on the queue item. Empty string when the
    correction came via a tier name / modifier / synthetic all-ok.

    ``same_as_item`` is set when the fragment used "Same" / "Ditto" /
    "Same as #N" chaining. It is a transient marker consumed by
    :func:`parse_reply`, which copies the referenced item's
    tier/modifier/ok/reject fields onto this correction before handing
    it to the dispatcher. By the time the dispatcher sees the
    correction, ``same_as_item`` is always ``None``.
    """

    item_number: int
    new_tier: str | None = None
    modifier: str | None = None
    ok: bool = False
    reject: bool = False
    note: str = ""
    same_as_item: int | None = None
    consumed_token: str = ""


@dataclass
class ReplyParseResult:
    """Structured outcome of parsing a Daily Sync reply.

    ``all_ok`` short-circuits everything else — when True, the caller
    should mark every item in the batch as confirmed without changes
    and ignore the (empty) ``corrections`` list. ``unparsed`` carries
    the raw fragments that didn't match any rule so the bot can echo
    them back to Andrew.
    """

    all_ok: bool = False
    corrections: list[ReplyCorrection] = field(default_factory=list)
    unparsed: list[str] = field(default_factory=list)


# Match an item-level fragment. Either:
#   "2 down" / "4: high — note" / "1 ok" / "1. down"
# Or, for cross-item delimiters, splits on commas and semicolons and
# the literal word " and ". The fragment regex below extracts the
# leading item number, the modifier/tier token(s), and any trailing
# free-text note (separated by ``:``, ``--``, or ``—``).
#
# The separator class includes ``.`` so numbered-list shapes like
# ``1. Down`` parse natively — that's the form autocorrect, dictation,
# and voice transcripts produce, alongside the bare ``1 down`` and
# explicit ``1: down`` / ``1 - down`` Andrew already uses.
_FRAGMENT_RE = re.compile(
    r"""
    ^\s*
    (?P<item>\d+)                     # item number
    \s*[:.\-]?\s*                     # optional separator (incl. "." for "1. Down")
    (?P<rest>.*?)                     # tokens + optional note
    \s*$
    """,
    re.VERBOSE,
)

# Splits a token string into the leading recognised tokens and the
# remaining free-text note. Tokens are space-separated; once we hit a
# non-token word, the rest becomes the note.
_NOTE_SEPARATOR_RE = re.compile(r"\s*(?:[—–\-]+|:|because|since|—)\s+", re.IGNORECASE)


# Numbered-list item boundary at the start of a line. Matches any of:
#   ``1.`` ``1)`` ``1:`` ``1 -`` at the beginning of any line (bullet
#   form — preferred), OR
#   ``1 <word>`` where ``<word>`` is plausibly a token/modifier/tier
#   verb — the bare form Andrew also uses when replying by voice
#   (dictation drops the period on short items like "2 ok").
#
# The second form avoids false-positives on prose like "1 hour later
# 2 questions" by requiring the post-number token to look like a word
# character (``\w``) AND by only running this boundary detection when
# the reply has already been flagged as list-shaped via
# :func:`_has_numbered_list_shape`. A reply that's pure prose stays
# in the historical comma/semicolon/and splitter.
_LIST_ITEM_BOUNDARY_RE = re.compile(
    r"(?:^|\n)\s*(?=\d+\s*(?:[.\):\-]\s|\s+\w))"
)

# Strict-shape detector — a more conservative pattern used ONLY to
# decide whether the reply is list-shaped at all. Requires at least one
# line that starts with ``\d+[.):\-]`` (bullet form). Without the bullet
# gate we'd false-positive on "2 questions came up yesterday" and treat
# prose as a list. Once the bullet gate has confirmed list shape, the
# boundary regex above (which also matches the bare ``N <word>`` form)
# safely carves every line — bullet-or-not.
_LIST_SHAPE_GATE_RE = re.compile(r"(?:^|\n)\s*\d+\s*[.\):\-]\s")


def _strip_bullet(s: str) -> str:
    """Remove a leading bullet, dash, or numeric marker from a line.

    Tolerates ``- 1 down`` and ``* 1 down`` and bare ``1 down``.
    """
    return re.sub(r"^[-*•]\s+", "", s.strip())


def _has_numbered_list_shape(text: str) -> bool:
    """Return True when the reply contains at least one line that starts
    with a numbered-list bullet marker (``1.``, ``2)``, ``3:``, ``4 -``).

    Drives :func:`_split_fragments`: when the reply has list shape we
    carve it on the list boundaries (so prose periods and the word
    "and" inside an item's note don't fragment the item). Otherwise we
    fall back to the single-line shorthand splitter (``,`` ``;``
    ``and``) — Andrew still types ``"2 down, 4 spam"`` as one line.

    The gate requires the bullet form (``\\d+[.\\):\\-]``) — bare
    ``\\d+ <word>`` is too common in prose ("2 questions came up") to
    use as a list marker on its own. Once the gate confirms list
    shape, :data:`_LIST_ITEM_BOUNDARY_RE` tolerates the bare form for
    subsequent lines (so ``"1. spam\\n2 ok"`` splits correctly —
    Andrew drops the period on short voice-dictated items).
    """
    if not text:
        return False
    return bool(_LIST_SHAPE_GATE_RE.search(text))


def _split_fragments(text: str) -> list[str]:
    """Split a multi-clause reply into per-item fragments.

    Two split modes:

      * **Numbered-list mode** — when the reply has at least one line
        starting with ``\\d+[.):\\- ]``, we split on those boundaries
        ONLY. Item bodies keep their internal periods, "and"s, and
        commas intact (critical for multi-sentence notes like
        ``"1. Spam - routine. Warnings only."``). This is the shape
        produced by autocorrect, dictation, and voice transcripts.

      * **Single-line shorthand mode** — when no list boundary is
        present, fall back to the historical ``,`` ``;`` ``\\n`` ``and``
        splitter so Andrew's terse ``"2 down, 4 spam"`` still works.

    Rationale: a period inside an item body is NOT an item boundary;
    only a newline followed by a number-marker is. See 2026-04-24 live
    session where ``"1. Spam - routine. Warnings only."`` split on the
    period and ``"warnings only."`` landed as an orphan fragment.
    """
    if not text:
        return []
    if _has_numbered_list_shape(text):
        # List-boundary split: each fragment is a number-marker followed
        # by its body (possibly multi-line, possibly containing periods
        # or "and"s). Leading content before the first number is treated
        # as a preamble and dropped — Andrew's replies don't include
        # one, but we don't want to crash on ``"ok: 1. down, 2. up"``.
        parts = _LIST_ITEM_BOUNDARY_RE.split(text)
        return [p.strip() for p in parts if p.strip()]
    # Convert " and " to a comma so the split below is uniform. Avoid
    # touching " and " inside notes (e.g. "Jamie and the customer") by
    # only splitting on " and " between item-prefixed tokens — but a
    # full grammar is overkill, so we accept some over-splitting and
    # rely on the fragment-level regex to fail unparseable shards into
    # the ``unparsed`` bucket.
    text = re.sub(r"\s+and\s+", ",", text)
    parts = re.split(r"[,;\n]+", text)
    return [p.strip() for p in parts if p.strip()]


def parse_reply(reply_text: str) -> ReplyParseResult:
    """Parse Andrew's terse reply into a :class:`ReplyParseResult`.

    Always returns a :class:`ReplyParseResult` — even an empty / fully
    unparseable reply just produces ``unparsed=[reply_text]``.

    "Same" / "Ditto" / "Same as #N" chaining (c2) is resolved here: a
    chaining fragment is parsed into a :class:`ReplyCorrection` with
    ``same_as_item`` set, then this function copies the source
    correction's tier/modifier/ok/reject onto it and clears
    ``same_as_item``. Chain resolution walks left-to-right: each chain
    fragment resolves against the accumulated ``corrections`` list as
    it stands when that fragment is parsed.
    """
    result = ReplyParseResult()
    if not reply_text:
        return result

    cleaned = _strip_bullet(reply_text.strip())
    if not cleaned:
        return result

    if _ALL_OK_PATTERNS.match(cleaned):
        result.all_ok = True
        return result

    for fragment in _split_fragments(cleaned):
        correction = _parse_fragment(fragment)
        if correction is None:
            result.unparsed.append(fragment)
            continue
        if correction.same_as_item is not None or _is_same_chain_placeholder(correction):
            resolved, err = _resolve_same_chain(correction, result.corrections)
            if err is not None:
                result.unparsed.append(f"item {correction.item_number}: {err}")
                continue
            result.corrections.append(resolved)
            continue
        result.corrections.append(correction)

    return result


def _is_same_chain_placeholder(correction: ReplyCorrection) -> bool:
    """Return True when the correction was parsed as a chain reference
    (``same_as_item`` set by :func:`_parse_fragment`).

    Kept as a helper so the intent of the check in :func:`parse_reply`
    is explicit and survives future extensions (e.g. a "no-op" correction
    type that doesn't chain).
    """
    return correction.same_as_item is not None


def _resolve_same_chain(
    correction: ReplyCorrection,
    prior_corrections: list[ReplyCorrection],
) -> tuple[ReplyCorrection, str | None]:
    """Resolve a chain reference by copying tier/modifier/ok/reject from
    the source correction.

    Source selection:

      * ``same_as_item=N`` (explicit) — find the correction whose
        ``item_number == N`` in ``prior_corrections``. If absent,
        return an error.
      * ``same_as_item=-1`` (implicit "same"/"ditto"/"same as above") —
        take the LAST entry in ``prior_corrections``. If the list is
        empty, return an error.

    The resolved correction preserves this fragment's own ``note`` (the
    chaining text after a dash/colon) so trailing content like
    ``"Same - also, why was this ranked lower?"`` is retained. The
    source's note is NOT inherited — only the classification intent.
    """
    if correction.same_as_item is None:
        # Defensive: caller is supposed to have checked. Return the
        # correction unchanged with no error.
        return correction, None

    explicit = correction.same_as_item
    source: ReplyCorrection | None = None
    if explicit == -1:
        if not prior_corrections:
            return correction, (
                "no prior item to chain from — 'Same' must follow another item"
            )
        source = prior_corrections[-1]
    else:
        source = next(
            (c for c in prior_corrections if c.item_number == explicit),
            None,
        )
        if source is None:
            return correction, (
                f"no item {explicit} to chain from"
            )

    # Copy classification fields. Preserve THIS correction's own note
    # so trailing content survives. ``same_as_item`` is cleared so the
    # dispatcher sees a normal, fully-resolved correction.
    resolved = ReplyCorrection(
        item_number=correction.item_number,
        new_tier=source.new_tier,
        modifier=source.modifier,
        ok=source.ok,
        reject=source.reject,
        note=correction.note,
        same_as_item=None,
        consumed_token=source.consumed_token,
    )
    return resolved, None


def _parse_fragment(fragment: str) -> ReplyCorrection | None:
    """Parse one ``"2 down"`` / ``"4: high — note"`` shard.

    Returns ``None`` when the fragment doesn't carry an item number
    plus at least one recognised token. The caller buckets ``None``
    returns into :attr:`ReplyParseResult.unparsed`.

    "Same" / "Ditto" / "Same as #N" (c2) produces a placeholder
    :class:`ReplyCorrection` with ``same_as_item`` set (``-1`` for
    implicit chain-to-prior, ``N`` for explicit reference); the
    caller (:func:`parse_reply`) resolves the chain against the
    accumulated corrections list.
    """
    fragment = _strip_bullet(fragment)
    match = _FRAGMENT_RE.match(fragment)
    if not match:
        return None
    try:
        item_num = int(match.group("item"))
    except ValueError:
        return None
    rest = (match.group("rest") or "").strip()

    if not rest:
        # Bare "2" with no token is ambiguous — treat as unparseable.
        return None

    # "Same" / "Ditto" / "Same as #N" chaining — detect BEFORE the
    # token walker so the chain placeholder isn't mis-parsed as an
    # unparseable fragment (``same`` isn't in any of the token sets).
    same_match = _SAME_CHAIN_RE.match(rest)
    if same_match:
        explicit = same_match.group("explicit")
        note = (same_match.group("note") or "").strip()
        return ReplyCorrection(
            item_number=item_num,
            same_as_item=int(explicit) if explicit else -1,
            note=note,
        )

    # Split the rest into "tokens" (one or two words) and the trailing
    # free-text note. Note separator is ``:``, em/en dash, or the words
    # "because"/"since".
    note = ""
    sep = _NOTE_SEPARATOR_RE.search(rest)
    if sep:
        token_part = rest[: sep.start()].strip()
        note = rest[sep.end():].strip()
    else:
        # No explicit separator — take the FIRST recognised token and
        # treat everything after as the note. This handles both
        # "3 down because spam" and "3 down" (latter has no note).
        words = rest.split(maxsplit=1)
        token_part = words[0]
        note = words[1] if len(words) > 1 else ""

    token_words = token_part.lower().split()
    if not token_words:
        return None

    correction = ReplyCorrection(item_number=item_num, note=note)
    # Walk the leading words once to extract the first tier/modifier/
    # ok/reject token; ignore "actually" and similar filler words.
    consumed = False
    leftover_note_bits: list[str] = []
    for word in token_words:
        normalized = word.strip(".,!?")
        if normalized in {"actually", "really", "instead"}:
            continue
        if not consumed:
            if normalized in _TIER_TOKENS:
                correction.new_tier = _TIER_TOKENS[normalized]
                correction.consumed_token = normalized
                consumed = True
                continue
            if normalized in _RELATIVE_TOKENS:
                correction.modifier = _RELATIVE_TOKENS[normalized]
                correction.consumed_token = normalized
                consumed = True
                continue
            if normalized in _OK_TOKENS:
                correction.ok = True
                correction.consumed_token = normalized
                consumed = True
                continue
            if normalized in _REJECT_TOKENS:
                correction.reject = True
                correction.consumed_token = normalized
                consumed = True
                continue
        # Once we've consumed the first token, the rest belongs to the
        # note (in case the user wrote "3 high urgent followup" with
        # no separator).
        leftover_note_bits.append(word)

    if not consumed:
        return None

    # If we collected leftover words AND there was no explicit-separator
    # note, glue the leftover back onto the note so we don't lose them.
    if leftover_note_bits and not note:
        correction.note = " ".join(leftover_note_bits)
    elif leftover_note_bits and note:
        correction.note = " ".join(leftover_note_bits) + " " + note

    correction.note = correction.note.strip()
    return correction


# ---------------------------------------------------------------------------
# Tier-arithmetic helpers — for resolving "down"/"up" against a batch
# ---------------------------------------------------------------------------

# Tier order: low ↔ medium ↔ high ↔ (spam stays its own bucket; "down"
# from spam stays at spam, "up" from high stays at high — saturating).
# spam isn't in the up/down ladder because moving a message from
# urgency to spam is a categorical reclassification, not a step.
_TIER_LADDER = ["low", "medium", "high"]


def apply_modifier(current_tier: str, modifier: str) -> str:
    """Return the tier ``current_tier`` shifts to under ``modifier``.

    ``"down"`` moves toward less urgent (high → medium → low → low).
    ``"up"`` moves toward more urgent (low → medium → high → high).
    Spam saturates: any modifier on a spam item leaves it at spam.
    Unknown current tiers (sentinel/unclassified) saturate at low for
    "down" and high for "up" — the conservative choices.
    """
    current = (current_tier or "").lower().strip()
    if current == "spam":
        return "spam"
    if current not in _TIER_LADDER:
        return "low" if modifier == "down" else "high"
    idx = _TIER_LADDER.index(current)
    if modifier == "down":
        return _TIER_LADDER[max(0, idx - 1)]
    if modifier == "up":
        return _TIER_LADDER[min(len(_TIER_LADDER) - 1, idx + 1)]
    return current
