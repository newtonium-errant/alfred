"""Topical filing classifier — deterministic rules → LLM fallback → ``email_category`` frontmatter.

#7 7c-i. Curator post-pass entry point: :func:`classify_filing_for_inbox`. It runs BESIDE the priority
``email_classifier`` as an orthogonal axis — the category is about the EMAIL (sender + subject), constant
across the notes the curator produced, and is written as an additive ``email_category`` frontmatter field
that NEVER touches the priority fields (``priority`` / ``action_hint`` / ``priority_reasoning``).

Decision order:
  1. The deterministic rule table (ported verbatim from n8n) — first-match-wins.
  2. On no-rule-match AND ``fallback_enabled``: the Sonnet LLM fallback (the long tail n8n dropped to
     "skip"), constrained to the four seed category labels or "none", improved over time by the
     category-correction few-shot (the self-correcting loop).
  3. On still-no-category: an explicit ILB ``email_filing.no_category`` log, no write.

INERT re: Gmail — 7c-i writes vault frontmatter ONLY. The Gmail-side label re-application is 7c-ii,
hard-gated on the ratified ``confidence.filing`` flag (built here, consumed there). The pass NEVER raises
into the curator (fire-and-forget), mirroring the priority classifier.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import structlog

from alfred.vault.mutation_log import log_mutation
from alfred.vault.ops import VaultError, vault_edit

from .config import EmailFilingConfig
from .rules import (
    SEED_CATEGORY_LABELS,
    extract_sender_and_subject,
    load_rules,
    match_category,
)

log = structlog.get_logger(__name__)


# --- Email-shape gate (mirrors email_classifier.is_email_inbox) ------------

_EMAIL_FROM_RE = re.compile(r"^\s*\*?\*?From:\*?\*?\s*\S+@\S+", re.MULTILINE)
_EMAIL_SUBJECT_RE = re.compile(r"^\s*\*?\*?Subject:\*?\*?\s*\S+", re.MULTILINE)
_EMAIL_ACCOUNT_RE = re.compile(r"^\s*\*?\*?Account:\*?\*?\s*\S+", re.MULTILINE)


def is_email_inbox(content: str) -> bool:
    """Return True when ``content`` looks email-derived (a ``From:`` address line, or Account+Subject).

    Mirrors ``email_classifier.is_email_inbox`` — kept local for orthogonality. Voice/Omi captures don't
    carry these headers, so the filing pass short-circuits on them."""
    if not content:
        return False
    if _EMAIL_FROM_RE.search(content):
        return True
    if _EMAIL_ACCOUNT_RE.search(content) and _EMAIL_SUBJECT_RE.search(content):
        return True
    return False


# --- Result ----------------------------------------------------------------


@dataclass
class FilingResult:
    """Outcome of one email's filing classification.

    ``category`` is the ``Parent/Child`` label or ``None`` (no rule + no fallback). ``source`` is
    ``"rule"`` / ``"llm"`` / ``"none"``. ``written`` lists the note paths that got the ``email_category``
    frontmatter (empty when no category or all writes failed)."""

    category: str | None = None
    source: str = "none"
    written: list[str] = field(default_factory=list)


# --- LLM fallback ----------------------------------------------------------

# Pluggable so tests can swap a fake without monkeypatching the SDK.
LLMCaller = Callable[[str, str, EmailFilingConfig], str]


def _default_llm_caller(system: str, user: str, config: EmailFilingConfig) -> str:
    """Default LLM caller — Anthropic SDK in-process. Returns raw assistant text, or "" on any error
    (the caller treats "" as 'no category'). Mirrors email_classifier's caller; kept local (orthogonality)."""
    try:
        import anthropic
    except ImportError:
        log.warning("email_filing.anthropic_not_installed")
        return ""
    api_key = config.anthropic.api_key
    if not api_key or api_key.startswith("${"):
        log.warning("email_filing.no_api_key")
        return ""
    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=config.anthropic.model,
            max_tokens=config.anthropic.max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
    except Exception as exc:  # noqa: BLE001 — must not crash the post-pass
        log.warning("email_filing.llm_call_failed", error=str(exc), model=config.anthropic.model)
        return ""
    parts: list[str] = []
    for block in getattr(response, "content", []) or []:
        block_text = getattr(block, "text", None)
        if isinstance(block_text, str):
            parts.append(block_text)
    return "".join(parts)


def _build_fallback_system(config: EmailFilingConfig) -> str:
    """System prompt for the LLM fallback: the closed category set + the category-correction few-shot."""
    labels = "\n".join(f"  - {label}" for label in sorted(SEED_CATEGORY_LABELS))
    base = (
        "You are a topical email FILING classifier for a personal mailbox. Given an email, choose the ONE "
        "category it should be filed under, or \"none\" if it fits no category. Categories are exactly:\n\n"
        f"{labels}\n\n"
        "\"none\" is correct for personal correspondence, newsletters, and anything that isn't a receipt, "
        "invoice, tax document, or financial/personal-purchase record.\n\n"
    )
    few_shot = _build_category_few_shot(config)
    if few_shot:
        base += few_shot + "\n\n"
    base += (
        "Return ONLY a JSON object: {\"category\": \"Parent/Child\" or \"none\"}. "
        "No prose, no code fences."
    )
    return base


def _build_category_few_shot(config: EmailFilingConfig) -> str:
    """Render recent operator category-corrections as few-shot examples, or "". This is the FEED-BACK half
    of the self-correcting loop — the fallback gets better as the operator corrects it."""
    if not config.calibration_corpus_path or config.calibration_few_shot_count <= 0:
        return ""
    try:
        from alfred.daily_sync.corpus import recent_category_corrections
    except ImportError:
        return ""
    try:
        entries = recent_category_corrections(
            config.calibration_corpus_path, limit=config.calibration_few_shot_count,
        )
    except Exception:  # noqa: BLE001 — corpus issues never crash classification
        return ""
    if not entries:
        return ""
    lines = ["Recent operator filing corrections (authoritative — lean toward these patterns):"]
    for e in entries:
        sender = e.sender or "(unknown)"
        subject = e.subject or "(no subject)"
        lines.append(f"  - {sender} — \"{subject}\" → {e.andrew_category or 'none'}")
    return "\n".join(lines)


def _build_fallback_user(from_addr: str, subject: str, inbox_content: str) -> str:
    return (
        f"## Sender\n{from_addr or '(unknown)'}\n\n"
        f"## Subject\n{subject or '(no subject)'}\n\n"
        f"## Email content\n{inbox_content}\n\n"
        "Classify this email's filing category."
    )


def _parse_category(raw: str) -> str | None:
    """Extract a validated ``Parent/Child`` label from the LLM response, or None.

    Tolerates a fenced block; validates against the closed seed set (an out-of-set or "none" label → None
    → no write). The LLM can NEVER invent a category outside the ported taxonomy."""
    if not raw or not raw.strip():
        return None
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    label: str | None = None
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            raw_label = data.get("category")
            if isinstance(raw_label, str):
                label = raw_label.strip()
    except json.JSONDecodeError:
        brace = re.search(r"\{.*\}", text, re.DOTALL)
        if brace:
            try:
                data = json.loads(brace.group(0))
                if isinstance(data, dict) and isinstance(data.get("category"), str):
                    label = data["category"].strip()
            except json.JSONDecodeError:
                label = None
    if label is None:
        return None
    return label if label in SEED_CATEGORY_LABELS else None


def _llm_fallback(
    from_addr: str,
    subject: str,
    inbox_content: str,
    config: EmailFilingConfig,
    llm_caller: LLMCaller | None,
) -> tuple[str, str] | None:
    """Run the LLM fallback. Returns ``(parent, child)`` from the closed set, or None (skip)."""
    caller = llm_caller or _default_llm_caller
    system = _build_fallback_system(config)
    user = _build_fallback_user(from_addr, subject, inbox_content)
    label = _parse_category(caller(system, user, config))
    if label is None:
        return None
    parent, child = label.split("/", 1)
    return (parent, child)


# --- Entry point -----------------------------------------------------------


def classify_filing_for_inbox(
    vault_path: Path,
    inbox_content: str,
    note_paths: list[str],
    config: EmailFilingConfig,
    *,
    llm_caller: LLMCaller | None = None,
    session_path: str | None = None,
) -> FilingResult:
    """Curator post-pass — classify the email's filing category once and write it to each note record.

    The category is a property of the EMAIL (sender + subject), so it's computed once and written to every
    ``note/*.md`` the curator produced from this inbox file. Never raises — the curator treats this as
    fire-and-forget (a filing fault cannot corrupt the priority classification or the curation)."""
    if not config.enabled:
        log.debug("email_filing.disabled")
        return FilingResult()
    if not is_email_inbox(inbox_content):
        log.debug("email_filing.skip_non_email", note_count=len(note_paths))
        return FilingResult()
    note_only = [p for p in note_paths if p.startswith("note/") and p.endswith(".md")]
    if not note_only:
        log.debug("email_filing.no_notes_to_file")
        return FilingResult()

    from_addr, subject = extract_sender_and_subject(inbox_content)
    rules = load_rules(config.rules_additions_path or None)
    matched = match_category(from_addr, subject, rules)
    source = "rule"
    if matched is None and config.fallback_enabled:
        matched = _llm_fallback(from_addr, subject, inbox_content, config, llm_caller)
        source = "llm" if matched is not None else "none"

    if matched is None:
        # ILB: an explicit "ran, no category" signal so 'nothing matched' is distinguishable from a broken
        # filer. This is the n8n "skip" branch — the common case for personal mail / newsletters.
        log.info(
            "email_filing.no_category",
            from_addr=from_addr,
            subject=subject[:80],
            fallback_enabled=config.fallback_enabled,
        )
        return FilingResult(category=None, source="none", written=[])

    category = f"{matched[0]}/{matched[1]}"
    written: list[str] = []
    for note_path in note_only:
        try:
            # Additive, isolated write: ONLY email_category. Never touches priority/action_hint —
            # the orthogonality guarantee is that this set_fields carries exactly one key.
            vault_edit(vault_path, note_path, set_fields={"email_category": category})
            log_mutation(session_path, "edit", note_path, scope="email_filing")
            written.append(note_path)
        except VaultError as exc:
            log.warning("email_filing.write_failed", path=note_path, error=str(exc))
        except Exception as exc:  # noqa: BLE001 — must never crash the curator
            log.warning("email_filing.unexpected_write_error", path=note_path, error=str(exc))

    log.info(
        "email_filing.categorized",
        category=category,
        source=source,
        from_addr=from_addr,
        notes=len(written),
    )
    return FilingResult(category=category, source=source, written=written)
