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
from pathlib import Path
from typing import Any, Callable

import frontmatter
import structlog

from alfred.vault.mutation_log import log_mutation
from alfred.vault.ops import VaultError, vault_edit

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
    """

    priority: str
    action_hint: str | None = None
    reasoning: str = ""
    written_to: str = ""


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
    senders against Andrew's address book (the ``high`` tier "named
    person" cue).
    """
    contact_block = render_contacts_for_prompt(contacts)
    return (
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

    # Write priority + action_hint + (optional) reasoning into the
    # note's frontmatter via vault_edit so the mutation is logged
    # consistently with curator's other writes.
    set_fields: dict[str, Any] = {"priority": result.priority}
    # ``action_hint`` is always written so a downstream consumer can
    # rely on the field's presence; ``None`` becomes the YAML ``null``.
    set_fields["action_hint"] = result.action_hint
    if result.reasoning:
        set_fields["priority_reasoning"] = result.reasoning

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
        )
    except VaultError as exc:
        log.warning(
            "email_classifier.write_failed",
            path=note_rel_path,
            error=str(exc),
        )

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
