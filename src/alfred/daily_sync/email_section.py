"""Email-calibration section provider — c2's first concrete provider.

Samples N recently-classified email-derived note records and renders
them as a numbered batch Andrew can reply to with terse corrections.

Sampling strategy:
  1. Walk ``vault/note/*.md`` newest-first by mtime.
  2. Keep records whose frontmatter has ``priority`` set to a real tier
     (i.e. the classifier has run and produced a confident output —
     the unclassified sentinel is excluded so calibration only sees
     real classifier decisions).
  3. Filter to records whose path is NOT already in the corpus
     (calibration corpus is append-only; we don't show Andrew the same
     note twice).
  4. If we collected ``batch_size`` items: return them.
  5. If not, fall back to a stratified sample across whatever tiers we
     do have so Andrew sees a balanced mix even on a quiet day.

The provider returns ``None`` (omit the section) when the vault has
zero classified items at all — the empty-Daily-Sync header already
covers that case.

Side effect: when we successfully sample a batch, we stash the item ↔
record mapping in :func:`prepare_batch_state` so the daemon can persist
it to the Daily Sync state file. The reply parser reads from that
state file to map "item 2" back to a record path.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable

import frontmatter

from .config import DailySyncConfig
from .corpus import iter_corrections


_REAL_TIERS = ("high", "medium", "low", "spam")


@dataclass
class BatchItem:
    """One item in a Daily Sync email-calibration batch.

    All fields are display-only; the bot writes them into the state
    file so the reply parser can resolve "item 2" without re-reading
    the underlying record.
    """

    item_number: int  # 1-indexed, matches what Andrew sees
    record_path: str  # vault-relative
    classifier_priority: str
    classifier_action_hint: str | None
    classifier_reason: str
    sender: str
    subject: str
    snippet: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_number": self.item_number,
            "record_path": self.record_path,
            "classifier_priority": self.classifier_priority,
            "classifier_action_hint": self.classifier_action_hint,
            "classifier_reason": self.classifier_reason,
            "sender": self.sender,
            "subject": self.subject,
            "snippet": self.snippet,
        }


@dataclass
class _CandidateRecord:
    rel_path: str
    priority: str
    action_hint: str | None
    reasoning: str
    sender: str
    subject: str
    snippet: str
    mtime: float


def _read_candidate(
    vault_path: Path, rel_path: str,
) -> _CandidateRecord | None:
    """Load one note record and pull the calibration-relevant fields.

    Returns ``None`` when the record can't be read or has no real
    classifier tier (so the caller can skip it cheaply).
    """
    file_path = vault_path / rel_path
    if not file_path.exists():
        return None
    try:
        post = frontmatter.load(str(file_path))
        mtime = file_path.stat().st_mtime
    except Exception:
        return None

    fm = post.metadata or {}
    priority = str(fm.get("priority") or "").strip().lower()
    if priority not in _REAL_TIERS:
        return None

    raw_hint = fm.get("action_hint")
    if raw_hint is None or raw_hint == "" or raw_hint == "null":
        action_hint: str | None = None
    elif isinstance(raw_hint, str):
        action_hint = raw_hint.strip() or None
    else:
        action_hint = str(raw_hint)

    reasoning = str(fm.get("priority_reasoning") or "").strip()

    # Sender/subject/snippet best-effort. The curator stores raw email
    # headers in the note body sometimes; we walk the first ~20 lines
    # looking for ``From:`` / ``Subject:`` markdown headers.
    sender, subject = _extract_email_headers(post.content or "")
    if not subject:
        subject = str(fm.get("subject") or fm.get("name") or file_path.stem)
    if not sender:
        sender = str(fm.get("from") or fm.get("sender") or "(unknown)")

    snippet = _extract_snippet(post.content or "", limit=120)

    return _CandidateRecord(
        rel_path=rel_path,
        priority=priority,
        action_hint=action_hint,
        reasoning=reasoning,
        sender=sender,
        subject=subject,
        snippet=snippet,
        mtime=mtime,
    )


def _extract_email_headers(body: str) -> tuple[str, str]:
    """Pull `From:` and `Subject:` lines from the first ~20 lines."""
    sender = ""
    subject = ""
    for line in body.splitlines()[:30]:
        stripped = line.strip().lstrip("*").strip()
        lower = stripped.lower()
        if not sender and lower.startswith("from:"):
            sender = stripped.split(":", 1)[1].strip().strip("*").strip()
        elif not subject and lower.startswith("subject:"):
            subject = stripped.split(":", 1)[1].strip().strip("*").strip()
        if sender and subject:
            break
    return sender, subject


def _extract_snippet(body: str, *, limit: int = 120) -> str:
    """Return the first ~``limit`` chars of body content (excluding headers).

    Skips any leading lines that look like email headers (``From:``,
    ``To:``, ``Subject:``, ``Date:``, ``Account:``) so the snippet is
    actual prose, not duplicated metadata.
    """
    header_prefixes = ("from:", "to:", "subject:", "date:", "account:", "cc:", "bcc:")
    lines: list[str] = []
    for line in body.splitlines():
        stripped = line.strip().lstrip("*").strip()
        if not stripped:
            if lines:
                # Blank line after some content — keep it as a separator
                # then take everything that follows verbatim until limit.
                lines.append("")
                continue
            continue
        lower = stripped.lower()
        if any(lower.startswith(p) for p in header_prefixes):
            continue
        if stripped.startswith("#"):
            # Markdown heading; usually the title — skip if it's literally
            # the subject line. Always keep otherwise.
            continue
        lines.append(stripped)

    text = " ".join(line for line in lines if line)
    text = " ".join(text.split())  # collapse whitespace
    if len(text) > limit:
        text = text[: limit - 3].rstrip() + "..."
    return text


def _already_calibrated(corpus_path: str | Path) -> set[str]:
    """Return the set of record paths already present in the corpus."""
    seen: set[str] = set()
    for entry in iter_corrections(corpus_path):
        if entry.record_path:
            seen.add(entry.record_path)
    return seen


def _sample_batch(
    vault_path: Path,
    corpus_path: str | Path,
    batch_size: int,
    *,
    note_dir: str = "note",
    now_ts: float | None = None,
) -> list[_CandidateRecord]:
    """Return up to ``batch_size`` candidates for the next calibration batch.

    Order of preference:
      1. Recent (mtime newest-first), classifier-tagged, NOT in corpus.
      2. Fallback (only if step 1 didn't fill the batch): include any
         already-calibrated recent items, stratified across tiers so
         Andrew sees a balanced view even on a slow week.
    """
    note_root = vault_path / note_dir
    if not note_root.is_dir():
        return []

    seen = _already_calibrated(corpus_path)

    # Walk note/*.md and read every candidate. Cap the read count so a
    # huge vault doesn't blow up the assembly step — newest-first via
    # mtime cap guarantees we never miss the latest items.
    candidates: list[_CandidateRecord] = []
    files = sorted(
        note_root.glob("*.md"),
        key=lambda p: p.stat().st_mtime if p.exists() else 0.0,
        reverse=True,
    )
    # Hard cap on file reads per assembly. 200 is a generous upper bound
    # for a daily classifier — nobody emails Andrew that much in a day.
    for md_file in files[:200]:
        rel = f"{note_dir}/{md_file.name}"
        candidate = _read_candidate(vault_path, rel)
        if candidate is None:
            continue
        candidates.append(candidate)

    if not candidates:
        return []

    fresh = [c for c in candidates if c.rel_path not in seen]
    if len(fresh) >= batch_size:
        return fresh[:batch_size]

    # Fallback — stratified across tiers from the full candidate pool
    # (already-calibrated rows allowed). Walk tier-by-tier round-robin
    # so a single tier doesn't dominate.
    chosen: list[_CandidateRecord] = list(fresh)
    chosen_set = {c.rel_path for c in chosen}
    by_tier: dict[str, list[_CandidateRecord]] = {t: [] for t in _REAL_TIERS}
    for c in candidates:
        if c.rel_path in chosen_set:
            continue
        by_tier.setdefault(c.priority, []).append(c)

    while len(chosen) < batch_size:
        added_in_round = False
        for tier in _REAL_TIERS:
            if not by_tier.get(tier):
                continue
            chosen.append(by_tier[tier].pop(0))
            added_in_round = True
            if len(chosen) >= batch_size:
                break
        if not added_in_round:
            break

    return chosen[:batch_size]


def build_batch(
    vault_path: Path,
    config: DailySyncConfig,
) -> list[BatchItem]:
    """Sample a batch and return it as :class:`BatchItem` rows.

    Public surface for the daemon and the ``/calibrate`` slash command.
    Returns ``[]`` when the vault has nothing classifiable.
    """
    candidates = _sample_batch(
        vault_path=vault_path,
        corpus_path=config.corpus.path,
        batch_size=config.batch_size,
    )
    return [
        BatchItem(
            item_number=i + 1,
            record_path=c.rel_path,
            classifier_priority=c.priority,
            classifier_action_hint=c.action_hint,
            classifier_reason=c.reasoning,
            sender=c.sender,
            subject=c.subject,
            snippet=c.snippet,
        )
        for i, c in enumerate(candidates)
    ]


def render_batch(items: list[BatchItem]) -> str:
    """Render the batch as the email-calibration section's body.

    Format::

        ## Email calibration (5 items)

        1. [HIGH] jamie@example.com — "Re: Friday meeting"
           snippet: Hey, can we move it to 3pm?
           action: calendar
           reason: Reply-required + named contact

        2. [LOW] notifications@example.com — "Weekly digest #42"
           snippet: ...

    The leading "##" header tells Andrew which section he's reading;
    the numbered items are what he references in his reply ("2 down").
    """
    if not items:
        return ""
    lines = [f"## Email calibration ({len(items)} item{'s' if len(items) != 1 else ''})", ""]
    for item in items:
        tier_label = item.classifier_priority.upper()
        sender = item.sender or "(unknown sender)"
        subject = item.subject or "(no subject)"
        lines.append(f'{item.item_number}. [{tier_label}] {sender} — "{subject}"')
        if item.snippet:
            lines.append(f"   snippet: {item.snippet}")
        if item.classifier_action_hint:
            lines.append(f"   action: {item.classifier_action_hint}")
        if item.classifier_reason:
            lines.append(f"   reason: {item.classifier_reason}")
        lines.append("")
    lines.append(
        "Reply with terse corrections — e.g. `✅` for all-confirmed, "
        "`2 down, 4 spam`, `2: actually high — Jamie was waiting`."
    )
    return "\n".join(lines).rstrip()


# ---------------------------------------------------------------------------
# Section provider entry point + registration
# ---------------------------------------------------------------------------


# Module-level vault-path holder. The daemon sets this at startup so
# the section-provider callable (signature ``(config, today)``) doesn't
# need a vault arg threaded through. Module-level state is a small
# concession to the registry contract; a per-call closure would also
# work but would require the daemon to register a fresh provider on
# every fire.
_VAULT_PATH_HOLDER: dict[str, Path] = {}


def set_vault_path(vault_path: Path) -> None:
    """Configure the module-level vault path used by the section provider.

    Daemon calls this once at startup; tests may call it before invoking
    :func:`email_calibration_section` directly. Idempotent.
    """
    _VAULT_PATH_HOLDER["path"] = vault_path


def get_vault_path() -> Path | None:
    """Return the currently-configured vault path (None if unset)."""
    return _VAULT_PATH_HOLDER.get("path")


# Module-level batch holder so the daemon can read the batch back after
# the assembler runs (the assembler signature doesn't return per-section
# metadata, only the rendered string). Cleared on each new fire.
_LAST_BATCH_HOLDER: dict[str, list[BatchItem]] = {}


def consume_last_batch() -> list[BatchItem]:
    """Return and clear the most recently-built batch.

    Called by the daemon after :func:`assemble_message` so it can
    persist the item ↔ record mapping into the Daily Sync state file.
    """
    items = _LAST_BATCH_HOLDER.get("items", [])
    _LAST_BATCH_HOLDER["items"] = []
    return items


def email_calibration_section(
    config: DailySyncConfig,
    today: date,
) -> str | None:
    """Section provider — builds and renders the email calibration batch.

    Registered with priority 10 (highest tier per memo's ordering).
    Returns ``None`` when the vault has no calibratable items.
    """
    vault_path = get_vault_path()
    if vault_path is None or not vault_path.is_dir():
        return None
    items = build_batch(vault_path, config)
    if not items:
        return None
    _LAST_BATCH_HOLDER["items"] = items
    return render_batch(items)


# Register the provider at import time. The daemon imports this module
# explicitly so the registration is deterministic and not dependent on
# import-order luck.
def register() -> None:
    """Idempotent provider registration. Safe to call multiple times."""
    from . import assembler
    if "email_calibration" in assembler.registered_providers():
        return
    assembler.register_provider(
        "email_calibration",
        priority=10,
        provider=email_calibration_section,
    )
