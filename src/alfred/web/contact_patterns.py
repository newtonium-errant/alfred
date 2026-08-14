"""Pattern-surfacing for the contact-surface router (C4).

The self-correcting half of the router, and the reason the contact log exists.
The spec's shape, unchanged: *"Algernon watches override patterns and surfaces
them explicitly to the operator as deck cards — never silent, never auto-applied.
During testing, all pattern detection is user-facing."*

So the loop is **capture → learn → propose → operator approves**, and every arrow
is visible:

* CAPTURE — every contact records the rule that fired, the surface opened, where
  the operator actually landed, and the triggering state
  (:mod:`alfred.web.contact_state`).
* LEARN — :func:`detect_patterns` reads the rolling window and finds a rule the
  operator keeps overriding to the same place.
* PROPOSE — :func:`emit_pattern_cards` deals a ``pattern_surfaced`` card onto the
  deck. Nothing else happens. The router's behaviour is byte-identical until a
  tap arrives.
* APPROVE — the operator's ``adopt`` tap (and ONLY that tap) writes the new
  default. ``ignore`` silences the pattern for the window instead.

WHAT IS DELIBERATELY NOT HERE. The spec's card offers four adjustments; two of
them ship:

* "Change default to <surface>"          → the ``adopt`` verb.
* "Ignore this pattern for N days"       → the ``ignore`` verb (N = window_days).
* "Dismiss"                              → collapsed INTO ``ignore``: with a
  suppression window behind both, "make it go away" and "ignore it for the
  window" are the same act, and two verbs that do one thing is a menu, not a
  choice.
* "Add inferred condition: <condition>"  → **NOT SHIPPED.** It edits the
  Hypatia-owned rule set, which is a redesign of the policy rather than a
  consumer of it. Declared here and in the card's own note, in the same
  degraded-but-honest posture the operator ratified for rule 1.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .contact_state import (
    RULE_DEFAULT,
    RULE_FIRST_CONTACT_AFTER_GAP,
    RULE_UNRESOLVED_NOTIFICATION,
    SURFACES,
    WebContactStore,
    parse_ts,
)
from .utils import get_logger

log = get_logger(__name__)

# The feed kind these cards ride. Declared here beside the producer; registered
# in ``alfred.feed.model.KINDS`` + ``KIND_DEFAULTS`` and given its verb ceiling
# in ``alfred.daily_sync.action_router.FEED_ACTIONS``.
PATTERN_KIND = "pattern_surfaced"

# The two verbs. Spelled here because the producer's card note names them and
# the dispatcher answers them — one spelling, two readers.
ACTION_ADOPT = "adopt"
ACTION_IGNORE = "ignore"

# Operator-facing rule names for the card copy. The rule IDS are wire
# vocabulary; these are the words the operator reads.
RULE_LABELS: dict[str, str] = {
    RULE_UNRESOLVED_NOTIFICATION: "Unresolved notification",
    RULE_FIRST_CONTACT_AFTER_GAP: "First contact after a gap",
    RULE_DEFAULT: "Default open",
}


def rule_label(rule: str) -> str:
    """Operator-facing name for a rule id, falling back to the id itself."""
    return RULE_LABELS.get(rule, rule)


@dataclass(frozen=True)
class SurfacedPattern:
    """One detected override pattern — the card's whole content."""

    rule: str
    surface: str
    overrides: int
    observations: int
    window_days: int

    @property
    def key(self) -> str:
        """Stable identity: one card per (rule, observed surface)."""
        return f"{self.rule}->{self.surface}"

    @property
    def ratio(self) -> float:
        return (
            self.overrides / self.observations if self.observations else 0.0
        )

    @property
    def title(self) -> str:
        """The spec's card sentence, as one line."""
        return (
            f"{rule_label(self.rule)} overridden {self.overrides} of the last "
            f"{self.observations} — you went to {self.surface} instead"
        )


def detect_patterns(
    contacts: list[dict[str, Any]],
    *,
    config: dict[str, Any],
    now: datetime | None = None,
) -> list[SurfacedPattern]:
    """Find override patterns in ``contacts`` worth surfacing.

    ``config`` is the resolved ``pattern_surfacing`` block (see
    :func:`alfred.web.day_state.pattern_config_from_args`).

    A pattern fires when, inside the rolling window, one rule's contacts were
    overridden to ONE surface at least ``min_observations`` times AND that
    surface accounts for at least ``threshold_ratio`` of that rule's contacts.
    Both bars, not either: the count alone would fire on 3 overrides out of 300,
    and the ratio alone would fire on 1 out of 1.

    ``same_context_required=False`` is NOT implemented and is treated as
    ``True`` with a warning. Pooling contexts would produce a pattern that names
    no rule, and ``adopt`` — the one verb that changes behaviour — has no
    defensible meaning without one. A guessed meaning would change what the
    router does on an operator tap, which is precisely what must never be
    guessed. The lever is honoured in the shape that has one.
    """
    at = now or datetime.now(timezone.utc)
    if not config.get("enabled", True):
        # ILB: the master switch being off is a configured state, not a quiet
        # detector.
        log.info(
            "web.contact_patterns.disabled",
            reason="pattern_surfacing.enabled is false in the preference record",
        )
        return []

    if not config.get("same_context_required", True):
        log.warning(
            "web.contact_patterns.same_context_forced",
            detail=(
                "pattern_surfacing.same_context_required=false is not "
                "implemented — a pooled pattern names no rule, so 'adopt' "
                "would have no defined target. Detecting per-rule instead."
            ),
        )

    window_days = int(config.get("window_days", 14))
    min_observations = int(config.get("min_observations", 3))
    threshold_ratio = float(config.get("threshold_ratio", 0.6))
    cutoff = at - timedelta(days=max(1, window_days))

    # Group the window's contacts by the rule that fired — the "same context".
    by_rule: dict[str, list[dict[str, Any]]] = {}
    undatable = 0
    for entry in contacts:
        ts = parse_ts(entry.get("ts"))
        if ts is None:
            # An undatable contact cannot be placed in or out of the window.
            # Skipped rather than assumed recent: counting it would let a
            # corrupt row push a pattern over its threshold.
            undatable += 1
            continue
        if ts < cutoff:
            continue
        rule = str(entry.get("rule") or "")
        if not rule:
            continue
        by_rule.setdefault(rule, []).append(entry)

    found: list[SurfacedPattern] = []
    for rule, entries in sorted(by_rule.items()):
        observations = len(entries)
        # Count overrides per DESTINATION — "you keep going to the feed" is a
        # pattern; "you keep going somewhere else" is not actionable.
        by_surface: dict[str, int] = {}
        for entry in entries:
            if not entry.get("overridden"):
                continue
            landed = str(entry.get("landed") or "")
            if landed not in SURFACES:
                continue
            by_surface[landed] = by_surface.get(landed, 0) + 1
        if not by_surface:
            continue
        # Highest count wins; ties break on surface name so the same input
        # always produces the same card rather than a coin-flip identity.
        surface, overrides = sorted(
            by_surface.items(), key=lambda kv: (-kv[1], kv[0])
        )[0]
        if overrides < min_observations:
            continue
        if observations <= 0 or (overrides / observations) < threshold_ratio:
            continue
        found.append(
            SurfacedPattern(
                rule=rule,
                surface=surface,
                overrides=overrides,
                observations=observations,
                window_days=window_days,
            )
        )

    # ILB: fires on EVERY detection run including the empty one. This runs on a
    # write path nobody watches, so a detector that only speaks when it finds
    # something is indistinguishable from one that stopped running.
    log.info(
        "web.contact_patterns.detected",
        found=len(found),
        rules_examined=len(by_rule),
        contacts_in_window=sum(len(v) for v in by_rule.values()),
        undatable_skipped=undatable,
        window_days=window_days,
        min_observations=min_observations,
        threshold_ratio=threshold_ratio,
        detail=(
            "ran, no pattern over threshold" if not found
            else "override pattern(s) over threshold"
        ),
    )
    return found


def build_pattern_item(
    pattern: SurfacedPattern,
    *,
    instance: str,
    user_key: str,
    now: datetime | None = None,
) -> Any:
    """Build the ``pattern_surfaced`` FeedItem for one detected pattern.

    Imported lazily by the caller's module so ``alfred.web`` keeps no import-time
    dependency on the feed package.

    ``user_key`` rides in ``source_ref``, NOT in ``evidence``: the act-path
    dispatcher must write the operator's decision back to the same contact-log
    key the card was minted from, and re-deriving "who the operator is" over
    there would bake the v1 single-user assumption into a second place. Evidence
    is rendered to the operator as key/value rows; ``source_ref`` is metadata and
    is rendered nowhere, which is the right home for a routing key.
    """
    from alfred.feed.model import FeedItem

    at = now or datetime.now(timezone.utc)
    return FeedItem.create(
        kind=PATTERN_KIND,
        # One card per (rule, surface). Re-detecting the same pattern folds onto
        # the SAME card with refreshed counts rather than dealing a second one.
        stable_key=pattern.key,
        instance=instance,
        title=pattern.title,
        evidence={
            "rule": pattern.rule,
            "rule_label": rule_label(pattern.rule),
            "observed_surface": pattern.surface,
            "overrides": pattern.overrides,
            "observations": pattern.observations,
            "ratio": round(pattern.ratio, 3),
            "window_days": pattern.window_days,
            # Named in the card so the operator does not have to infer what the
            # two buttons do from their labels alone.
            "adjust": (
                f"Adopt = open {pattern.surface} for this rule from now on. "
                f"Ignore = do not raise this pattern again for "
                f"{pattern.window_days} days."
            ),
            # The declared gap, on the card itself rather than only in a design
            # doc — the operator should learn what the system CANNOT offer from
            # the same place it offers the rest.
            "not_offered": (
                "Adding an inferred condition to the rule set is not available "
                "yet — that edit belongs to the preference record."
            ),
        },
        created_at=at.isoformat(),
        source_ref={"producer": "contact_router", "user_key": user_key},
    )


def emit_pattern_cards(
    *,
    contact_store: WebContactStore,
    user_key: str,
    feed_store: Any,
    instance: str,
    config: dict[str, Any],
    now: datetime | None = None,
) -> int:
    """Detect and deal pattern cards; returns the number emitted.

    NEVER RAISES INTO THE CALLER — the same belt discipline as
    :func:`alfred.feed.belt.try_feed_reconcile`. This runs inside the override
    write path, and an operator's override must be recorded even if the feed is
    unwritable. A pattern that goes un-surfaced is a missed suggestion; a lost
    override is lost evidence.
    """
    at = now or datetime.now(timezone.utc)
    try:
        patterns = detect_patterns(
            contact_store.contacts_for(user_key), config=config, now=at
        )
        emitted = 0
        suppressed = 0
        already = 0
        adopted = contact_store.adopted_for(user_key)
        for pattern in patterns:
            if contact_store.is_pattern_suppressed(user_key, pattern.key, now=at):
                suppressed += 1
                continue
            if adopted.get(pattern.rule) == pattern.surface:
                # The operator already said yes to exactly this. Re-proposing an
                # adopted default is the acked-card-revives failure wearing a
                # different hat.
                already += 1
                continue
            feed_store.upsert(
                build_pattern_item(
                    pattern, instance=instance, user_key=user_key, now=at
                )
            )
            emitted += 1
        log.info(
            "web.contact_patterns.emitted",
            user_key=user_key,
            emitted=emitted,
            suppressed=suppressed,
            already_adopted=already,
            detected=len(patterns),
            detail=(
                "ran, nothing to surface" if not emitted
                else "pattern card(s) dealt to the deck"
            ),
        )
        return emitted
    except Exception as exc:  # noqa: BLE001 — the belt: never break the override write
        log.warning(
            "web.contact_patterns.emit_failed",
            user_key=user_key,
            error=type(exc).__name__,
            detail=str(exc)[:200],
        )
        return 0
