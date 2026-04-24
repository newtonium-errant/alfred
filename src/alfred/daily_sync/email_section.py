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

import re
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
    # headers in the note body sometimes; we walk the first ~30 lines
    # looking for ``From:`` / ``Subject:`` markdown headers. c4
    # introduces the display-name/domain fallback chain so records with
    # only an email address (``info@email.borrowell.com``) render as
    # their root domain (``borrowell.com``) rather than ``(unknown)``.
    raw_sender, subject = _extract_email_headers(post.content or "")
    if not subject:
        subject = str(fm.get("subject") or fm.get("name") or file_path.stem)
    # Fallback chain per c4:
    #   1. Resolved person record name (frontmatter ``from`` / ``sender``)
    #   2. ``From:`` header display name
    #   3. Email domain
    #   4. Literal ``(unknown)``
    fm_sender = str(fm.get("from") or fm.get("sender") or "").strip()
    # Treat the literal sentinel ``(unknown)`` as absence — a prior
    # version of the curator wrote this placeholder into frontmatter and
    # we don't want to propagate it through the fallback chain.
    if fm_sender.lower() in {"(unknown)", "unknown"}:
        fm_sender = ""
    display_sender = _display_sender_from_raw(fm_sender) if fm_sender else ""
    if not display_sender:
        display_sender = _display_sender_from_raw(raw_sender)
    sender = display_sender or "(unknown)"

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
    """Pull ``From:`` and ``Subject:`` lines from the first ~30 lines.

    Tolerates the markdown-bulleted style the curator emits for prose
    summaries (``- **From:** info@example.com``) in addition to the
    plain ``From: ...`` and starred ``*From*: ...`` variants. The
    leading bullet prefix (``-``, ``*``, ``•`` + a run of ``*`` used by
    bold markdown) is stripped before matching.

    Returns raw values — normalization of the sender (extracting a
    display name, stripping angle brackets, deriving a domain) is
    handled by :func:`_display_sender_from_raw` at the caller so the
    extracted From header survives round-tripping into the corpus.
    """
    sender = ""
    subject = ""
    for line in body.splitlines()[:30]:
        stripped = _strip_line_marker(line)
        lower = stripped.lower()
        if not sender and lower.startswith("from:"):
            sender = stripped.split(":", 1)[1].strip().strip("*").strip()
        elif not subject and lower.startswith("subject:"):
            subject = stripped.split(":", 1)[1].strip().strip("*").strip()
        if sender and subject:
            break
    return sender, subject


def _strip_line_marker(line: str) -> str:
    """Remove leading bullet / dash / asterisk markers from a body line.

    Handles the curator's markdown-bulleted header style
    (``- **From:** ...``) which the previous implementation missed
    because ``.lstrip("*")`` doesn't see past the leading ``-``.
    """
    stripped = line.strip()
    # Strip leading bullet prefix: ``- ``, ``* ``, ``• ``, optionally
    # repeated (though nested bullets for a From header would be weird).
    while stripped and stripped[0] in "-*•":
        stripped = stripped[1:].lstrip()
    # Strip any leading bold markers (``**From:**`` → ``From:``).
    while stripped.startswith("**"):
        stripped = stripped[2:].lstrip()
    # Also strip a single leading ``*`` (italic marker) — preserved
    # behavior from the previous ``lstrip("*")`` path.
    stripped = stripped.lstrip("*").strip()
    return stripped


# Display-name and domain parsers for sender fallback chain (c4).

# ``Borrowell <noreply@borrowell.com>`` → display name ``Borrowell``.
# Anchored with lookahead so a bare ``noreply@borrowell.com`` returns
# no match (not "noreply" — we don't want the local-part as a name).
_DISPLAY_NAME_RE = re.compile(r"""^\s*(?P<name>[^<>@]+?)\s*<[^>]+>\s*$""", re.VERBOSE)

# ``info@email.borrowell.com`` → domain ``borrowell.com`` (registrable
# domain approximation: last two dot-separated labels). Good enough for
# display — we're not doing PSL parsing for ``.co.uk`` edge cases.
_EMAIL_ADDR_RE = re.compile(r"""[A-Za-z0-9._%+\-]+@(?P<domain>[A-Za-z0-9.\-]+)""")


def _display_sender_from_raw(raw_sender: str) -> str:
    """Collapse a raw ``From:`` value into a human-readable sender string.

    Fallback chain (applied in order, first non-empty wins):

      1. Display name from ``Display Name <addr@domain>`` form.
      2. The raw value if it's already non-empty and not an email
         address (e.g. ``Borrowell`` on its own).
      3. The email domain, with a leading ``www.`` stripped and
         any leading subdomain (``email.``, ``mail.``, ``mx.``) stripped
         so ``info@email.borrowell.com`` renders as ``borrowell.com``
         rather than ``email.borrowell.com``.
      4. Empty string — caller substitutes ``(unknown)``.
    """
    value = (raw_sender or "").strip()
    if not value:
        return ""

    # 1. ``Display Name <addr@domain>`` — preferred when present.
    m = _DISPLAY_NAME_RE.match(value)
    if m:
        name = m.group("name").strip().strip('"').strip("'")
        if name:
            return name

    # 2. Bare non-email string (e.g. ``Borrowell``): keep as-is.
    if "@" not in value:
        return value

    # 3. Derive domain. Strip any leading ``www.`` and one common
    # mail-prefix subdomain so the displayed root reads naturally.
    m = _EMAIL_ADDR_RE.search(value)
    if m:
        domain = m.group("domain").lower().strip()
        if domain.startswith("www."):
            domain = domain[len("www."):]
        for prefix in ("email.", "mail.", "mx.", "smtp.", "em."):
            if domain.startswith(prefix):
                domain = domain[len(prefix):]
                break
        if domain:
            return domain

    # 4. Nothing usable — let the caller substitute ``(unknown)``.
    return ""


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


def peek_last_batch_count() -> int:
    """Return the count of items in the most-recently-built batch.

    Non-destructive — used by the assembler's ``item_count_after`` hook
    so the next section provider's items are numbered continuously.
    """
    return len(_LAST_BATCH_HOLDER.get("items", []))


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
        item_count_after=peek_last_batch_count,
    )
