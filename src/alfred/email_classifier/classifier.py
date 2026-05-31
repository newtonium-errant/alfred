"""Email classifier — LLM call + JSON parse + frontmatter mutation.

Entry point for the curator daemon's post-processor hook is
:func:`classify_records_for_inbox`. It decides whether the inbox file
was email-derived, picks the note records the curator just produced,
asks the LLM for a tier + ``action_hint`` per record, and writes both
fields back into the note's frontmatter via :func:`alfred.vault.ops.vault_edit`.

Failure modes are gentle: on parse failure or LLM error the record gets
``priority: <unclassified_sentinel>`` (default ``"unclassified"``) so a
calibration loop later can find and re-classify the gap. The pipeline
NEVER raises into the curator — classification is a non-blocking
post-processor.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import frontmatter
import structlog

from alfred.vault.mutation_log import log_mutation
from alfred.vault.ops import VaultError, vault_edit, vault_move

from .config import EmailClassifierConfig
from .vault_helpers import (
    NamedContact,
    get_named_contacts,
    render_contacts_for_prompt,
)

log = structlog.get_logger(__name__)


# --- Constants --------------------------------------------------------------

# Valid tier values the LLM is allowed to return. Anything else falls
# through to the unclassified sentinel.
_VALID_TIERS = ("high", "medium", "low", "spam")

# A few cheap regexes for ``is_email_inbox``. Mail-derived inbox files
# the curator ingests today (Outlook → n8n → webhook) include either a
# ``**From:**`` markdown header or an ``Account:`` line plus a ``Subject:``
# block. Both are markdown-safe so the regex works without parsing.
_EMAIL_FROM_RE = re.compile(r"^\s*\*?\*?From:\*?\*?\s*\S+@\S+", re.MULTILINE)
_EMAIL_SUBJECT_RE = re.compile(r"^\s*\*?\*?Subject:\*?\*?\s*\S+", re.MULTILINE)
_EMAIL_ACCOUNT_RE = re.compile(r"^\s*\*?\*?Account:\*?\*?\s*\S+", re.MULTILINE)

# Capture the FULL From-line for sender extraction (high-priority-sender
# override path, 2026-05-31). Two shapes seen in real Outlook → n8n
# captures:
#   * ``**From:** jamie@example.com`` (bare address)
#   * ``From: Chudnovsky, Paul (Halifax) <pchudnovsky@coxandpalmer.com>``
#     (display name + bracketed address — common from corporate mail
#     clients)
# The capture group is the everything-after-``From:`` payload; the
# parser :func:`_extract_sender` splits it into (email, display_name).
_EMAIL_FROM_CAPTURE_RE = re.compile(
    r"^\s*\*?\*?From:\*?\*?\s*(.+?)\s*$",
    re.MULTILINE,
)

# Operator-readable override-marker prefix in ``classifier_reason``.
# Grep-friendly so the calibration UI can filter for override-fired
# rows. Pinned in tests; rename here = update SKILL + calibration
# filter in lockstep.
HIGH_PRIORITY_SENDER_OVERRIDE_PREFIX = "OVERRIDE→high"


# --- Types ------------------------------------------------------------------


@dataclass
class ClassificationResult:
    """One classification outcome.

    ``priority`` is one of the four valid tiers OR the unclassified
    sentinel (when parse / LLM fails). ``action_hint`` is the model's
    free-text recommendation, or ``None`` when the model declined to
    suggest one. ``reasoning`` is a 1-sentence rationale stashed in
    frontmatter so calibration can show Andrew why the model picked
    what it picked.

    ``written_to`` is the vault-relative path of the note record the
    classifier mutated, or empty when the classifier short-circuited
    (disabled / non-email / no records).

    ``llm_priority`` (2026-05-31) is the LLM's PRE-OVERRIDE verdict —
    populated only when the high-priority-sender override fired (i.e.
    ``priority == "high"`` was forced AFTER the LLM said something
    else). ``None`` on the normal path so the field's presence is
    the audit signal that an override applied. Pinned in the
    structured log + in the frontmatter (``priority_llm_pre_override``)
    so calibration can review whether the override fired sensibly
    AND so an operator changing their mind about a contact's flag
    can see what the LLM would have picked on its own.

    ``override_applied`` (2026-05-31) is the boolean companion to
    ``llm_priority`` — explicit True when override fired, False
    otherwise. Convenience for downstream consumers (canary log,
    calibration dashboard); ``llm_priority is not None`` is the same
    signal in the data layer.

    ``quarantined_to`` (c6, 2026-05-31) is the vault-relative path the
    record was MOVED to when the spam-quarantine layer fired (i.e.
    ``priority == "spam"`` AND the daily_sync ``confidence.spam``
    flag is true). Empty string when quarantine did NOT fire (most
    common case — non-spam priority, or flag is false). When this
    field is populated, ``written_to`` is the PRE-quarantine
    location (the path where the classifier wrote the priority
    frontmatter); the record currently lives at ``quarantined_to``.
    """

    priority: str
    action_hint: str | None = None
    reasoning: str = ""
    written_to: str = ""
    llm_priority: str | None = None
    override_applied: bool = False
    quarantined_to: str = ""


# --- LLM call type -----------------------------------------------------------

# A pluggable callable so tests can swap in a fake without monkeypatching
# the Anthropic SDK module. The callable takes a ``(system, user, config)``
# triple and returns the raw assistant text. The default implementation
# calls Anthropic's Messages API in-process.
LLMCaller = Callable[[str, str, EmailClassifierConfig], str]


def _default_llm_caller(
    system: str,
    user: str,
    config: EmailClassifierConfig,
) -> str:
    """Default LLM caller — Anthropic SDK in-process.

    Returns the raw assistant text concatenating all ``text`` blocks from
    the response. On SDK error returns an empty string so the caller's
    sentinel-fallback path fires. We log the failure so it lands in the
    curator's structured log alongside other ``email_classifier.*`` events.
    """
    try:
        import anthropic
    except ImportError:
        log.warning("email_classifier.anthropic_not_installed")
        return ""

    api_key = config.anthropic.api_key
    if not api_key or api_key.startswith("${"):
        log.warning("email_classifier.no_api_key")
        return ""

    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=config.anthropic.model,
            max_tokens=config.anthropic.max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
    except Exception as exc:  # noqa: BLE001 — must not crash post-processor
        log.warning(
            "email_classifier.llm_call_failed",
            error=str(exc),
            model=config.anthropic.model,
        )
        return ""

    parts: list[str] = []
    for block in getattr(response, "content", []) or []:
        block_text = getattr(block, "text", None)
        if isinstance(block_text, str):
            parts.append(block_text)
    return "".join(parts)


# --- Email detection --------------------------------------------------------


def is_email_inbox(content: str) -> bool:
    """Return True when ``content`` looks like email-derived inbox content.

    Heuristic — matches a ``**From:** addr@host`` line, or both an
    ``Account:`` line and a ``Subject:`` line. Designed for the email
    pipeline's current shape (Outlook → n8n webhook). Voice-memo
    transcripts and Omi captures don't carry these headers, so the
    classifier short-circuits on them.
    """
    if not content:
        return False
    if _EMAIL_FROM_RE.search(content):
        return True
    if _EMAIL_ACCOUNT_RE.search(content) and _EMAIL_SUBJECT_RE.search(content):
        return True
    return False


# --- Sender extraction (for high-priority-sender override) -----------------


def _extract_sender(inbox_content: str) -> tuple[str, str]:
    """Parse the From-line and return ``(email, display_name)``.

    Handles the two shapes seen in real Outlook → n8n captures:
      * ``**From:** jamie@example.com`` → ``("jamie@example.com", "")``
      * ``From: Chudnovsky, Paul (Halifax) <pchudnovsky@coxandpalmer.com>``
        → ``("pchudnovsky@coxandpalmer.com", "Chudnovsky, Paul (Halifax)")``

    Returns ``("", "")`` when no From-line is found or the line carries
    no parseable email address. Defensive against malformed input —
    the override step's caller treats empty email as "no override
    possible" + falls through to the LLM verdict.

    Email is normalised by stripping ``mailto:`` prefix + angle
    brackets + surrounding whitespace + lowercased for case-insensitive
    match in the override step. Display name is preserved verbatim
    (operator-set aliases may use any case).
    """
    if not inbox_content:
        return "", ""
    match = _EMAIL_FROM_CAPTURE_RE.search(inbox_content)
    if match is None:
        return "", ""
    payload = match.group(1).strip()
    if not payload:
        return "", ""

    # Bracketed shape: ``Display Name <addr@host>``.
    bracket_match = re.match(r"^(.+?)\s*<([^<>]+@[^<>]+)>\s*$", payload)
    if bracket_match:
        display = bracket_match.group(1).strip().strip('"')
        email = bracket_match.group(2).strip()
    else:
        # Bare address (no display name). Strip mailto:/angle brackets
        # defensively in case the address still carries them.
        email = (
            payload.strip().strip("<>").removeprefix("mailto:").strip()
        )
        display = ""

    # Final cleanup + lowercased email for case-insensitive matching.
    email = email.removeprefix("mailto:").strip().lower()
    if "@" not in email:
        return "", display
    return email, display


def _apply_high_priority_sender_override(
    result: ClassificationResult,
    inbox_content: str,
    contacts: list[NamedContact],
) -> ClassificationResult:
    """Force ``priority=high`` when the inbox sender matches a contact
    flagged ``high_priority_sender: true``.

    Match semantics (per dispatch 2026-05-31):
      * Email match: case-insensitive equality between the sender's
        normalised email address and any address on the contact's
        ``emails`` list (also case-folded).
      * Alias match: case-insensitive substring match of any alias
        against the sender's display name (when present). Aliases
        commonly carry name strings (e.g. ``"Paul Chudnovsky"``);
        substring lets ``"Chudnovsky, Paul (Halifax)"`` match.
      * Domain match: OUT OF SCOPE (per dispatch: too permissive —
        ``accountant@gmail.com`` shouldn't match every gmail sender).

    On match, mutates ``result`` in place:
      * ``priority`` → ``"high"``
      * ``llm_priority`` → the pre-override value (for audit)
      * ``override_applied`` → True
      * ``reasoning`` gets the override marker prefixed (preserving
        the LLM's original reasoning afterward — the calibration
        review still gets the model's rationale).

    No-op when:
      * Sender can't be parsed (empty email AND empty display name).
      * No contact has ``high_priority_sender=True``.
      * No flagged contact matches the sender.
      * ``result.priority`` is already ``"high"`` (no-op override —
        avoid mutating ``llm_priority`` / ``override_applied`` when
        the LLM already agreed, so the audit field stays a true signal).
    """
    sender_email, sender_display = _extract_sender(inbox_content)
    if not sender_email and not sender_display:
        return result

    sender_email_lower = sender_email.lower()
    sender_display_lower = sender_display.lower()

    # Find a flagged contact that matches.
    matched: NamedContact | None = None
    for contact in contacts:
        if not contact.high_priority_sender:
            continue
        # Email match (case-insensitive on both sides).
        if sender_email_lower:
            for email in contact.emails:
                if email.strip().lower() == sender_email_lower:
                    matched = contact
                    break
        if matched is not None:
            break
        # Alias match (case-insensitive word-boundary against display name).
        #
        # NOTE-1 from code-reviewer on 6d85bc2 (2026-05-31): the prior
        # substring ``in`` check was too loose for short aliases. A
        # contact with alias ``"Pat"`` would match incoming display
        # names like ``"Patricia Smith"`` / ``"Pat O'Brien at SpamCo"``
        # / ``"Pattern Recognition Weekly"`` — false positives. Paul
        # Chudnovsky's multi-word alias was safe; foot-gun for the
        # next operator-flagged contact with a short alias.
        #
        # Fix: word-boundary regex (``\b<alias>\b``) so ``"Pat"`` matches
        # ``"Pat O'Brien"`` (boundary between space and ``P``) but NOT
        # ``"Patricia"`` (no boundary between ``Pat`` and ``r``). The
        # ``re.escape`` defends against aliases containing regex
        # metacharacters (apostrophes, parens, dots) — operator-set
        # aliases are freeform strings, never trust them as regex.
        if sender_display_lower:
            for alias in contact.aliases:
                alias_lower = alias.strip().lower()
                if not alias_lower:
                    continue
                pattern = r"\b" + re.escape(alias_lower) + r"\b"
                if re.search(pattern, sender_display_lower):
                    matched = contact
                    break
        if matched is not None:
            break

    if matched is None:
        return result

    # Already-high path: don't muddy the audit fields with a no-op
    # override (the LLM agreed; ``llm_priority`` should stay None so
    # downstream consumers can use it as a "did override fire?" signal).
    if result.priority == "high":
        return result

    # Override fires — capture pre-override state + mutate.
    result.llm_priority = result.priority
    result.override_applied = True
    result.priority = "high"

    override_marker = (
        f"{HIGH_PRIORITY_SENDER_OVERRIDE_PREFIX} — sender matches "
        f"[[person/{matched.name}]] flagged as high_priority_sender."
    )
    if result.reasoning:
        result.reasoning = f"{override_marker} (LLM said: {result.reasoning})"
    else:
        result.reasoning = override_marker
    return result


# --- Prompt construction ----------------------------------------------------


def _build_system_prompt(config: EmailClassifierConfig) -> str:
    """Compose the classifier system prompt from the cue groups.

    When ``config.calibration_corpus_path`` is set AND the file has at
    least one entry, the most-recent N corpus entries (deduplicated and
    diversified by tier) are appended as few-shot examples per the c2
    Phase 1 corpus → classifier feedback loop. Failures to read the
    corpus fall back to the cold cue lists alone — never crash the
    classifier on a corpus parsing error.
    """
    p = config.prompt

    def _bullets(items: list[str]) -> str:
        return "\n".join(f"  - {item}" for item in items)

    base = (
        "You are an email classifier for the Alfred operational instance. "
        "Read the email content and decide which priority tier it belongs to. "
        "Optionally suggest a free-text ``action_hint`` (e.g. \"calendar\", "
        "\"archive\", \"ignore\", \"file:newsletter/Tim Denning\") when there "
        "is a clear recommendation. Action hints are recommendations only — "
        "they will NEVER be auto-executed without operator confirmation.\n\n"
        "Tier cues:\n\n"
        f"high:\n{_bullets(p.high)}\n\n"
        f"medium:\n{_bullets(p.medium)}\n\n"
        f"low:\n{_bullets(p.low)}\n\n"
        f"spam:\n{_bullets(p.spam)}\n\n"
    )

    few_shot_block = _build_few_shot_block(config)
    if few_shot_block:
        base += few_shot_block + "\n\n"

    base += (
        "Return ONLY a JSON object with this exact shape:\n"
        "{\"priority\": \"high|medium|low|spam\", "
        "\"action_hint\": \"<string or null>\", "
        "\"reasoning\": \"<1 sentence rationale>\"}\n\n"
        "No prose, no code fences, no commentary — just the JSON object."
    )
    return base


def _build_few_shot_block(config: EmailClassifierConfig) -> str:
    """Render the calibration few-shot block from the corpus, or "".

    Reads the most recent N entries (per ``calibration_few_shot_count``)
    from ``calibration_corpus_path`` and renders them as labelled
    examples. Returns the empty string when the corpus is unset, empty,
    or unreadable — caller treats that as "no few-shot block".
    """
    if not config.calibration_corpus_path:
        return ""
    if config.calibration_few_shot_count <= 0:
        return ""
    try:
        # Local import to keep email_classifier independent of the
        # daily_sync module at import time — corpus is small and lives
        # under daily_sync because that's where it's written.
        from alfred.daily_sync.corpus import recent_corrections
    except ImportError:
        return ""
    try:
        entries = recent_corrections(
            config.calibration_corpus_path,
            limit=config.calibration_few_shot_count,
            diversify_by_tier=True,
        )
    except Exception:  # noqa: BLE001 — corpus issues never crash classification
        return ""
    if not entries:
        return ""

    lines = [
        "Recent calibration corrections from the operator (most-recent first):",
    ]
    for entry in reversed(entries):  # newest first for readability
        label = entry.andrew_priority or "?"
        was = entry.classifier_priority or "?"
        sender = entry.sender or "(unknown)"
        subject = entry.subject or "(no subject)"
        snippet = entry.snippet or ""
        reason = entry.andrew_reason or ""
        lines.append(
            f"  - {sender} — \"{subject}\" → operator says: {label}"
            f" (classifier said: {was})"
        )
        if snippet:
            lines.append(f"      snippet: {snippet}")
        if reason:
            lines.append(f"      operator reason: {reason}")
    lines.append(
        "Treat these as authoritative — when an incoming email matches one"
        " of these patterns, lean toward the operator's tier."
    )
    return "\n".join(lines)


def _build_user_prompt(
    note_subject: str,
    note_body: str,
    inbox_content: str,
    contacts: list[NamedContact],
) -> str:
    """Compose the per-record user prompt.

    The contact list is interpolated verbatim so the model can match
    senders against Andrew's address book. The "Named-contact handling"
    rules block above the list tightens the tiering — named contacts
    floor at ``medium`` (no ``low`` / ``spam`` for an actual contact
    match), and a contact + time-pressure / financial / legal / family
    marker is the principal ``high``-tier signal.
    """
    contact_block = render_contacts_for_prompt(contacts)
    rules = (
        "## Named-contact handling — minimums + ceilings\n"
        "The contacts list below is Andrew's address book (every "
        "``person/`` record in the vault). When the email's actual "
        "sender address matches a row in this list, apply these "
        "rules:\n"
        "  - **HIGH** when the email also carries time-pressure / "
        "reply-required / financial / legal / family-emergency "
        "markers (deadline language, direct question, payment / "
        "invoice content, medical or legal-process content, family "
        "urgency).\n"
        "  - **MEDIUM** is the minimum when the email is routine "
        "(\"thanks\", \"I'll get back to you next week\", general "
        "FYI, casual update). Named-contact + routine subject is "
        "still medium, not low — Andrew chose to keep this person "
        "on file.\n"
        "  - **LOW** only for obvious automated system notifications "
        "that happen to carry a contact's name in the display field "
        "(e.g. ``noreply@docusign.com`` sending \"Paul Chudnovsky "
        "wants you to sign\"). The actual sender address is the "
        "system, not the contact — match on the address.\n"
        "  - **SPAM** never applies to an email whose actual sender "
        "address matches a contact. Display-name spoofing where the "
        "address does NOT match a contact IS spam (phishing-shape) "
        "— weigh the address, not the display name.\n"
        "\n"
        "Note: an operator-set flag on the person record may post-"
        "process the final priority upward (e.g. always-high for a "
        "specific contact). That override happens in the code layer "
        "after your classification — classify normally per the rules "
        "above, and don't try to anticipate the override. Your "
        "``medium`` may surface to the operator as ``high`` if the "
        "flag is set; that's expected and not a sign your "
        "classification was wrong.\n\n"
    )
    return (
        f"{rules}"
        f"## Named contacts on file\n{contact_block}\n\n"
        f"## Note record subject\n{note_subject}\n\n"
        f"## Note record body\n{note_body}\n\n"
        f"## Original inbox content\n{inbox_content}\n\n"
        "Classify this email."
    )


# --- JSON parse -------------------------------------------------------------


def _parse_classification(raw: str) -> dict[str, Any] | None:
    """Extract the classifier JSON object from ``raw``.

    Tolerates surrounding whitespace and a single ```json fenced block
    (in case the model ignored the "no fences" instruction). Returns
    ``None`` if no parse succeeds.
    """
    if not raw or not raw.strip():
        return None

    text = raw.strip()
    # Strip a single fenced block if present
    fence_match = re.search(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL)
    if fence_match:
        text = fence_match.group(1).strip()

    # Direct parse
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass

    # Salvage: find the first {...} block
    brace_match = re.search(r"\{.*\}", text, re.DOTALL)
    if brace_match:
        try:
            data = json.loads(brace_match.group(0))
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            return None
    return None


def _coerce_result(
    parsed: dict[str, Any] | None,
    sentinel: str,
) -> ClassificationResult:
    """Validate parsed JSON shape and coerce into a ClassificationResult.

    Anything that doesn't fit the schema lands as the sentinel so c2's
    calibration can pick it up later. ``action_hint`` may be ``null``,
    a string, or absent.
    """
    if not isinstance(parsed, dict):
        return ClassificationResult(priority=sentinel)

    raw_priority = parsed.get("priority")
    priority = (
        str(raw_priority).strip().lower()
        if isinstance(raw_priority, str)
        else ""
    )
    if priority not in _VALID_TIERS:
        priority = sentinel

    raw_hint = parsed.get("action_hint")
    if raw_hint is None or raw_hint == "" or raw_hint == "null":
        action_hint: str | None = None
    elif isinstance(raw_hint, str):
        action_hint = raw_hint.strip() or None
    else:
        action_hint = None

    raw_reasoning = parsed.get("reasoning")
    reasoning = str(raw_reasoning).strip() if isinstance(raw_reasoning, str) else ""

    return ClassificationResult(
        priority=priority,
        action_hint=action_hint,
        reasoning=reasoning,
    )


# --- c6 spam quarantine ---------------------------------------------------


def _is_spam_quarantine_enabled(state_path: str) -> bool:
    """Read the daily_sync confidence.spam flag from the state file.

    c6 (2026-05-31). The quarantine layer only fires when the operator
    has explicitly ratified the spam tier via ``/calibration_ok spam``
    (which flips ``confidence.spam`` to ``true`` in the state file).
    Pre-flip — and through every prior calibration cycle — the
    classifier writes the spam priority into the frontmatter but
    leaves the record at its normal vault location.

    Failure-tolerant: missing state file, malformed JSON, missing
    ``confidence`` key, or unexpected types all return False (treat
    as flag-off). The justification matches the dispatch's edge-case
    spec: ``state file missing → treat as flag=false``. We never
    want a stat / parse error to silently quarantine records when
    the operator hasn't approved the surfacing.
    """
    try:
        text = Path(state_path).read_text(encoding="utf-8")
        state = json.loads(text)
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(state, dict):
        return False
    confidence = state.get("confidence")
    if not isinstance(confidence, dict):
        return False
    return bool(confidence.get("spam"))


def _quarantine_spam_record(
    vault_path: Path,
    note_rel_path: str,
    config: EmailClassifierConfig,
    *,
    session_path: str | None = None,
    now: datetime | None = None,
) -> str | None:
    """Move a spam-classified record to the quarantine directory.

    Returns the new vault-relative path on success, ``None`` when the
    move was skipped (e.g. the destination directory creation failed,
    or vault_move raised). Failure to quarantine is logged but does
    NOT crash the classifier — the record stays at its normal
    location and the operator can re-process via the calibration loop.

    Quarantine path convention (c6, 2026-05-31):
    ``<vault>/<config.quarantine_dir_name>/spam/<YYYY-MM>/<filename>``

    YYYY-MM bucketing matches the daily_sync calendar grouping +
    keeps each month's quarantine directory finite (operators can
    archive old months wholesale). The filename preserves the
    classifier's pre-quarantine name so an operator who finds a
    misclassification can grep the quarantine root by stem.

    ``now`` (default ``datetime.now()``) is injectable so tests can
    pin the YYYY-MM bucket without freezing the clock.
    """
    if now is None:
        now = datetime.now()
    month_bucket = now.strftime("%Y-%m")

    # Preserve the filename (just the basename) — the directory
    # changes from ``note/`` to ``<quarantine>/spam/<YYYY-MM>/``.
    filename = Path(note_rel_path).name
    dest_rel_path = (
        f"{config.quarantine_dir_name}/spam/{month_bucket}/{filename}"
    )

    # Pre-create the destination directory so vault_move's
    # filesystem-fallback path succeeds (it does mkdir(parents=True)
    # itself, but a pre-existing tree means the Obsidian-CLI path
    # also lands cleanly). Defensive — no harm if it already exists.
    dest_full = vault_path / dest_rel_path
    try:
        dest_full.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.warning(
            "email_classifier.quarantine_mkdir_failed",
            path=note_rel_path,
            dest=dest_rel_path,
            error=str(exc),
        )
        return None

    try:
        vault_move(vault_path, note_rel_path, dest_rel_path)
    except VaultError as exc:
        log.warning(
            "email_classifier.quarantine_move_failed",
            path=note_rel_path,
            dest=dest_rel_path,
            error=str(exc),
        )
        return None

    # Log to the mutation log so the audit trail captures the move
    # alongside the priority frontmatter edit. ``scope`` mirrors the
    # priority-edit mutation so an operator audit can correlate the
    # two by scope.
    if session_path is not None:
        try:
            log_mutation(
                session_path,
                "move",
                note_rel_path,
                scope="email_classifier",
                # Stash the dest in the mutation entry's extra data
                # so an operator reviewing the audit log can see the
                # quarantine destination without separately querying.
                dest=dest_rel_path,
            )
        except Exception as exc:  # noqa: BLE001 — audit log must not crash classifier
            log.warning(
                "email_classifier.quarantine_log_failed",
                path=note_rel_path,
                dest=dest_rel_path,
                error=str(exc),
            )

    log.info(
        "email_classifier.quarantined_spam",
        path=note_rel_path,
        dest=dest_rel_path,
        month_bucket=month_bucket,
    )
    return dest_rel_path


# --- Classification entry points -------------------------------------------


def classify_record(
    vault_path: Path,
    note_rel_path: str,
    inbox_content: str,
    config: EmailClassifierConfig,
    *,
    llm_caller: LLMCaller | None = None,
    session_path: str | None = None,
) -> ClassificationResult:
    """Classify a single note record and write the result to its frontmatter.

    Returns the :class:`ClassificationResult` even when the LLM fails
    (the sentinel-priority result is what gets written so calibration
    can find it). Caller-level failures (e.g. record not found) raise
    ``VaultError`` so the curator can log + skip without aborting the
    whole batch.
    """
    file_path = vault_path / note_rel_path
    if not file_path.exists():
        raise VaultError(f"Note record not found: {note_rel_path}")

    post = frontmatter.load(str(file_path))
    fm = post.metadata or {}
    body = post.content or ""
    subject = (
        str(fm.get("subject")
            or fm.get("name")
            or fm.get("description")
            or file_path.stem)
    )

    contacts = get_named_contacts(vault_path, config)
    system = _build_system_prompt(config)
    user = _build_user_prompt(
        note_subject=subject,
        note_body=body,
        inbox_content=inbox_content,
        contacts=contacts,
    )

    caller = llm_caller or _default_llm_caller
    raw = caller(system, user, config)
    parsed = _parse_classification(raw)
    result = _coerce_result(parsed, config.unclassified_sentinel)

    # High-priority-sender override (2026-05-31). Operator-declarative
    # override: when the inbox sender matches a ``person/*.md`` record
    # carrying ``high_priority_sender: true``, force priority=high
    # regardless of the LLM's verdict. The LLM-side heuristic ("lean
    # toward high for named contacts") is too soft for explicit
    # operator-marked senders. Implementation lives at
    # ``_apply_high_priority_sender_override``; see its docstring for
    # match semantics (email exact match + alias substring on display
    # name; domain match deliberately out of scope).
    result = _apply_high_priority_sender_override(
        result, inbox_content, contacts,
    )

    # Write priority + action_hint + (optional) reasoning into the
    # note's frontmatter via vault_edit so the mutation is logged
    # consistently with curator's other writes.
    set_fields: dict[str, Any] = {"priority": result.priority}
    # ``action_hint`` is always written so a downstream consumer can
    # rely on the field's presence; ``None`` becomes the YAML ``null``.
    set_fields["action_hint"] = result.action_hint
    if result.reasoning:
        set_fields["priority_reasoning"] = result.reasoning
    # Audit trail for the high-priority-sender override (2026-05-31).
    # Only persist when the override actually fired so normal-path
    # records don't grow a noisy nullable column. Calibration UI can
    # filter on ``priority_llm_pre_override`` presence to show only
    # override-affected rows for review.
    if result.override_applied and result.llm_priority is not None:
        set_fields["priority_llm_pre_override"] = result.llm_priority

    try:
        vault_edit(vault_path, note_rel_path, set_fields=set_fields)
        result.written_to = note_rel_path
        log_mutation(
            session_path,
            "edit",
            note_rel_path,
            scope="email_classifier",
        )
        log.info(
            "email_classifier.record_classified",
            path=note_rel_path,
            priority=result.priority,
            has_action_hint=result.action_hint is not None,
            # Audit signal for the high-priority-sender override
            # (2026-05-31). False on normal path; True when override
            # fired. ``llm_priority`` carries the pre-override verdict
            # so calibration grep can find override-affected rows
            # without re-reading the frontmatter.
            override_applied=result.override_applied,
            llm_priority=result.llm_priority,
        )
    except VaultError as exc:
        log.warning(
            "email_classifier.write_failed",
            path=note_rel_path,
            error=str(exc),
        )
        # Skip quarantine when the priority write failed — the record
        # didn't get the spam frontmatter persisted, so quarantining
        # it would lose the operator-recoverable signal. Operator
        # log review of email_classifier.write_failed surfaces the
        # broken case for retry.
        return result

    # c6 spam quarantine (2026-05-31). Runs AFTER the priority is
    # successfully persisted via vault_edit. Two gates: classifier
    # said "spam" AND the operator has ratified spam surfacing via
    # /calibration_ok spam (which flipped daily_sync confidence.spam
    # to true). Pre-ratification (operator still calibrating), spam
    # records stay in the normal location so /tier_inspect and
    # corpus review work normally.
    #
    # Quarantine failure (mkdir / move error) is logged but doesn't
    # propagate — the record stays at its normal location with the
    # spam priority frontmatter persisted. Operator-discoverable via
    # the email_classifier.quarantine_* warning logs.
    #
    # Per feedback_intentionally_left_blank.md: the no-op cases (not
    # spam OR flag not enabled) are silent-by-design — there's no
    # operator-actionable signal in "didn't quarantine the 5,000th
    # non-spam email today." Only the firing case logs, plus the
    # failure cases.
    if result.priority == "spam" and _is_spam_quarantine_enabled(
        config.quarantine_state_path
    ):
        quarantined_to = _quarantine_spam_record(
            vault_path,
            note_rel_path,
            config,
            session_path=session_path,
        )
        if quarantined_to is not None:
            result.quarantined_to = quarantined_to

    return result


def classify_records_for_inbox(
    vault_path: Path,
    inbox_content: str,
    note_paths: list[str],
    config: EmailClassifierConfig,
    *,
    llm_caller: LLMCaller | None = None,
    session_path: str | None = None,
) -> list[ClassificationResult]:
    """Post-processor entry point — called from the curator daemon.

    Behaviour:

    1. If ``config.enabled`` is False, return immediately (no LLM call).
    2. If ``inbox_content`` doesn't look like email, return immediately.
    3. Filter ``note_paths`` to ``note/*.md`` records (curator may also
       create person/org/task records — those are NOT classified).
    4. For each remaining note, call :func:`classify_record`.

    Returns the per-record results (empty list when short-circuited).
    Never raises — the curator daemon treats this as fire-and-forget.
    """
    if not config.enabled:
        log.debug("email_classifier.disabled")
        return []

    if not is_email_inbox(inbox_content):
        log.debug(
            "email_classifier.skip_non_email",
            note_count=len(note_paths),
        )
        return []

    note_only = [p for p in note_paths if p.startswith("note/") and p.endswith(".md")]
    if not note_only:
        log.debug("email_classifier.no_notes_to_classify")
        return []

    log.info(
        "email_classifier.batch_start",
        note_count=len(note_only),
    )

    results: list[ClassificationResult] = []
    for note_path in note_only:
        try:
            result = classify_record(
                vault_path=vault_path,
                note_rel_path=note_path,
                inbox_content=inbox_content,
                config=config,
                llm_caller=llm_caller,
                session_path=session_path,
            )
            results.append(result)
        except VaultError as exc:
            log.warning(
                "email_classifier.record_skipped",
                path=note_path,
                error=str(exc),
            )
        except Exception as exc:  # noqa: BLE001 — must not crash curator
            log.warning(
                "email_classifier.unexpected_error",
                path=note_path,
                error=str(exc),
            )

    log.info(
        "email_classifier.batch_complete",
        note_count=len(note_only),
        classified=len(results),
    )
    return results
