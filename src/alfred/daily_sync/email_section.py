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
from dataclasses import dataclass, field
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

    c5 — when ``cluster_record_paths`` is non-empty, this item
    represents a cluster of N near-identical records (e.g. four
    weekly Borrowell credit-score notifications). ``record_path``
    is the most-recent member (used for display and fallback);
    ``cluster_record_paths`` lists EVERY member path so the
    dispatcher fan-outs one correction to all N underlying records.
    A singleton item has ``cluster_record_paths == []`` —
    equivalent to the pre-c5 behavior.
    """

    item_number: int  # 1-indexed, matches what Andrew sees
    record_path: str  # vault-relative
    classifier_priority: str
    classifier_action_hint: str | None
    classifier_reason: str
    sender: str
    subject: str
    snippet: str
    # c5 — cluster members (excluding ``record_path`` itself is fine
    # but we include it so downstream consumers don't have to special-
    # case the "primary" vs "member" distinction). Empty list means
    # singleton — the historical behavior. Most-recent member's
    # ISO date is passed through ``cluster_most_recent_label`` for
    # display (e.g. ``"2026-04-11"`` → ``"(4 similar, most recent
    # 2026-04-11)"``).
    cluster_record_paths: list[str] = field(default_factory=list)
    cluster_most_recent_label: str = ""

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
            "cluster_record_paths": list(self.cluster_record_paths),
            "cluster_most_recent_label": self.cluster_most_recent_label,
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


# ---------------------------------------------------------------------------
# c5 — cluster candidates by (sender, subject pattern)
# ---------------------------------------------------------------------------
#
# Andrew's 2026-04-24 Daily Sync had four near-identical Borrowell
# credit-score-update weekly pings. Presenting them as four separate
# calibration asks forces Andrew to repeat the same correction four
# times. Cluster them into one ask, apply the correction to all four
# underlying records.

# Strip trailing date-shaped / version-shaped suffixes so
# "Credit Score Update 2026-04-11" matches "Credit Score Update
# 2026-04-04". Patterns are tried in order; anything that matches is
# stripped and the loop continues (so a title ending in "April 2026 v2"
# collapses to just the stem).
_SUBJECT_TRAILING_PATTERNS = [
    # ISO dates: 2026-04-11, 2026/04/11
    re.compile(r"\s+\d{4}[-/.]\d{1,2}[-/.]\d{1,2}\s*$"),
    # Month + year: "April 2026", "Apr 2026", "April, 2026"
    re.compile(
        r"\s+(?:jan|january|feb|february|mar|march|apr|april|may|jun|june|"
        r"jul|july|aug|august|sep|september|sept|oct|october|nov|november|"
        r"dec|december)\s*,?\s*\d{4}\s*$",
        re.IGNORECASE,
    ),
    # Year alone at the very end (after an ISO or month strip): "2026"
    re.compile(r"\s+\d{4}\s*$"),
    # Version markers: "v2", "v1.3", "V2"
    re.compile(r"\s+[vV]\d+(?:\.\d+)*\s*$"),
    # Trailing #N / (N): "Weekly Digest #42", "Dispatch (3)"
    re.compile(r"\s+#\d+\s*$"),
    re.compile(r"\s+\(\d+\)\s*$"),
    # Bare trailing integer: "Weekly Digest 42"
    re.compile(r"\s+\d+\s*$"),
    # Trailing punctuation left over from a strip
    re.compile(r"[\s\-–—:|·]+$"),
]


def _normalize_subject_pattern(subject: str) -> str:
    """Normalize a subject line into a cluster key.

    Strips trailing dates, months+year, bare years, version markers,
    ``#N``, ``(N)``, and bare trailing integers so recurring series
    with an identifier that varies per occurrence collapse to the same
    key. Case-folded + whitespace-collapsed. Returns an empty string
    when the subject was empty or stripped entirely (caller treats
    empty-key as an own cluster — no grouping).
    """
    if not subject:
        return ""
    s = subject.strip()
    # Iterate a few times so "Credit Score Update 2026-04-11" strips
    # the date, then again the trailing space/dash left behind. Cap at
    # 4 rounds to avoid a regex quadratic.
    for _ in range(4):
        before = s
        for pat in _SUBJECT_TRAILING_PATTERNS:
            s = pat.sub("", s).rstrip()
        if s == before:
            break
    s = " ".join(s.split()).casefold()
    return s


def _cluster_key_for(candidate: _CandidateRecord) -> str:
    """Return the (sender_norm, subject_pattern) cluster key for a candidate.

    The key combines the normalized sender (casefolded, whitespace-
    collapsed) with the normalized subject pattern. A candidate whose
    subject normalizes to empty AND has no sender returns a unique
    per-path key so it isn't accidentally clustered with other
    metadata-missing records.
    """
    sender_norm = " ".join((candidate.sender or "").split()).casefold()
    subject_norm = _normalize_subject_pattern(candidate.subject or "")
    if not sender_norm and not subject_norm:
        # Fall back to a per-path key so empty-metadata records don't
        # all clump into a single giant cluster.
        return f"__singleton__::{candidate.rel_path}"
    return f"{sender_norm}::{subject_norm}"


@dataclass
class _Cluster:
    """Internal grouping of candidates sharing a cluster key.

    ``members`` is ordered newest-first (by mtime). ``primary`` is
    always ``members[0]`` — the most-recent member, used as the
    display record for the collapsed ask.
    """

    key: str
    members: list[_CandidateRecord] = field(default_factory=list)

    @property
    def primary(self) -> _CandidateRecord:
        return self.members[0]

    @property
    def size(self) -> int:
        return len(self.members)

    def any_uncalibrated(self, seen: set[str]) -> bool:
        """True when at least one member of the cluster is not yet in
        the corpus — the cluster is "fresh" and worth showing."""
        return any(m.rel_path not in seen for m in self.members)

    def most_recent_date_label(self) -> str:
        """Return a display label for the cluster's most-recent member.

        We prefer an ISO date parsed out of the primary's subject (the
        curator's titles reliably end in ``YYYY-MM-DD`` for recurring
        emails). Falls back to an empty string when nothing date-like
        is present — the renderer handles that gracefully.
        """
        m = re.search(r"\d{4}[-/.]\d{1,2}[-/.]\d{1,2}", self.primary.subject or "")
        if m:
            return m.group(0)
        return ""


def _group_into_clusters(
    candidates: list[_CandidateRecord],
) -> list[_Cluster]:
    """Collapse a flat candidate list into clusters by cluster key.

    Preserves newest-first ordering at the cluster level — the first
    cluster in the output is the one whose primary (most-recent
    member) has the newest mtime, which matters for the sampler's
    "fresh first" preference.
    """
    by_key: dict[str, _Cluster] = {}
    order: list[str] = []
    # Candidates arrive newest-first; preserve that for cluster ordering.
    for c in candidates:
        key = _cluster_key_for(c)
        cluster = by_key.get(key)
        if cluster is None:
            cluster = _Cluster(key=key, members=[c])
            by_key[key] = cluster
            order.append(key)
        else:
            cluster.members.append(c)
    # Members arrive newest-first already; no resort needed.
    return [by_key[k] for k in order]


def _sample_batch(
    vault_path: Path,
    corpus_path: str | Path,
    batch_size: int,
    *,
    note_dir: str = "note",
    now_ts: float | None = None,
) -> list[_Cluster]:
    """Return up to ``batch_size`` CLUSTERS for the next calibration batch.

    c5 — each cluster groups near-identical recurring emails (e.g. the
    four weekly Borrowell credit-score pings collapse to one cluster
    of size 4). :func:`build_batch` turns each cluster into one
    :class:`BatchItem` that the dispatcher fan-outs across all
    underlying records when Andrew corrects it.

    Order of preference (applied at the cluster level):
      1. Clusters with at least one fresh member (not yet in corpus),
         newest-first by primary mtime.
      2. Fallback (only if step 1 didn't fill the batch): include
         already-fully-calibrated clusters, stratified across tiers.
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

    # c5 — collapse candidates into clusters BEFORE the batch_size
    # slice so we don't pick five clusters' worth of individual
    # candidates and then collapse to two asks.
    clusters = _group_into_clusters(candidates)

    fresh = [cl for cl in clusters if cl.any_uncalibrated(seen)]
    if len(fresh) >= batch_size:
        return fresh[:batch_size]

    # Fallback — stratified across tiers from the full cluster pool
    # (already-calibrated clusters allowed). Walk tier-by-tier round-
    # robin so a single tier doesn't dominate.
    chosen: list[_Cluster] = list(fresh)
    chosen_keys = {cl.key for cl in chosen}
    by_tier: dict[str, list[_Cluster]] = {t: [] for t in _REAL_TIERS}
    for cl in clusters:
        if cl.key in chosen_keys:
            continue
        # Cluster's tier is its primary member's tier.
        tier = cl.primary.priority
        by_tier.setdefault(tier, []).append(cl)

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

    c5 — items can now represent clusters of N near-identical records.
    When a cluster has more than one member, ``cluster_record_paths``
    is populated with every member path (including the primary); the
    dispatcher fan-outs Andrew's correction to all of them. Singleton
    clusters produce a BatchItem with ``cluster_record_paths == []``
    — unchanged from the pre-c5 shape.
    """
    clusters = _sample_batch(
        vault_path=vault_path,
        corpus_path=config.corpus.path,
        batch_size=config.batch_size,
    )
    items: list[BatchItem] = []
    for i, cluster in enumerate(clusters):
        primary = cluster.primary
        cluster_paths: list[str] = []
        most_recent_label = ""
        if cluster.size > 1:
            cluster_paths = [m.rel_path for m in cluster.members]
            most_recent_label = cluster.most_recent_date_label()
        items.append(
            BatchItem(
                item_number=i + 1,
                record_path=primary.rel_path,
                classifier_priority=primary.priority,
                classifier_action_hint=primary.action_hint,
                classifier_reason=primary.reasoning,
                sender=primary.sender,
                subject=primary.subject,
                snippet=primary.snippet,
                cluster_record_paths=cluster_paths,
                cluster_most_recent_label=most_recent_label,
            )
        )
    return items


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
        header = f'{item.item_number}. [{tier_label}] {sender} — "{subject}"'
        # c5 — cluster indicator: "(4 similar, most recent 2026-04-11)"
        cluster_size = len(item.cluster_record_paths)
        if cluster_size > 1:
            if item.cluster_most_recent_label:
                header += (
                    f" ({cluster_size} similar, most recent "
                    f"{item.cluster_most_recent_label})"
                )
            else:
                header += f" ({cluster_size} similar)"
        lines.append(header)
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
