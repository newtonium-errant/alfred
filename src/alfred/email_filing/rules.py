"""Topical filing rules — the deterministic category table, ported VERBATIM from the n8n workflow.

#7 7c-i. The n8n "Categorize Email - Code" node (workflow Pb3Jh54bjYDoJgpi) classified each email into
one of four hierarchical categories by sender-domain + subject-substring rules. This module ports those
rules byte-equivalent on the CATEGORY DECISION so the native pipeline files exactly as n8n does today.

Fidelity contract (the load-bearing artifact — pinned by fixtures ported straight from the n8n cases):
  * FIRST-MATCH-WINS in the n8n order: Receipts → Invoices → Tax → Personal. A digitalocean "receipt"
    hits Receipts; a bare digitalocean invoice hits Invoices — order-sensitive, pinned.
  * EACH rule preserves its n8n predicate form as-written — three distinct shapes:
      - ``domain == X``          exact-domain-equality (e.g. digitalocean.com)
      - ``X in from_addr``       substring-on-the-bare-from (e.g. apple.com, doordash)
      - ``X in domain``          substring-on-the-domain (e.g. amazon.com / amazon.ca)
    A predicate-form swap must red a pin.
  * Inputs mirror n8n exactly: ``from_addr`` = the parsed BARE address, lowercased (n8n's
    ``parseAddr(header('From')).toLowerCase()``); ``subject`` = lowercased; ``domain`` =
    ``from_addr.split('@')[1]``.

Operator-approved additions (7c-i-b writes them) append AFTER the seeds, so seeds ALWAYS win — n8n
parity is never overridden by an addition. The additions read path ships here (loader reads seeds + an
optional additions file); the WRITE side (the approval CLI + proposal generation) is 7c-i-b.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import structlog

log = structlog.get_logger(__name__)

# A predicate over (from_addr_bare_lower, subject_lower, domain) → bool.
FilingPredicate = Callable[[str, str, str], bool]


@dataclass(frozen=True)
class FilingRule:
    """One category rule. ``match`` returns True when the email belongs to ``parent/child``."""

    parent: str
    child: str
    match: FilingPredicate

    @property
    def label(self) -> str:
        """The hierarchical category label, e.g. ``Business/Receipts``."""
        return f"{self.parent}/{self.child}"


# --- The four seed rules, ported VERBATIM from the n8n Categorize node -------
#
# Each lambda transcribes its n8n rule's predicate exactly, preserving the
# domain==X / X-in-from / X-in-domain distinction per clause. Do NOT "simplify"
# these into a domain→category map — the mixed predicate forms are load-bearing.

_SEED_RULES: tuple[FilingRule, ...] = (
    FilingRule(
        "Business", "Receipts",
        lambda frm, subj, dom: (
            ("receipt" in subj and dom in {"digitalocean.com", "railway.app", "cloudflare.com", "github.com"})
            or ("payment confirmation" in subj and dom in {"digitalocean.com", "railway.app", "cloudflare.com"})
            or "software license" in subj
        ),
    ),
    FilingRule(
        "Business", "Invoices",
        lambda frm, subj, dom: (
            dom in {"digitalocean.com", "railway.app", "cloudflare.com", "supabase.com", "n8n.io"}
            or "your invoice" in subj
            or "billing statement" in subj
            or "invoice" in subj
        ),
    ),
    FilingRule(
        "Finance", "Tax",
        lambda frm, subj, dom: (
            dom == "cra-arc.gc.ca"
            or "canada.ca" in frm
            or "t4 " in subj
            or "t4a " in subj
            or "tax slip" in subj
            or "tax receipt" in subj
            or "charitable donation" in subj
            or "donation receipt" in subj
            or "rrsp" in subj
            or "investment statement" in subj
            or "contribution receipt" in subj
        ),
    ),
    FilingRule(
        "Finance", "Personal",
        lambda frm, subj, dom: (
            (dom == "patreon.com" and "receipt" in subj)
            or ("apple.com" in frm and ("receipt" in subj or "invoice" in subj))
            or ("microsoft.com" in frm and "receipt" in subj)
            or dom in {"costco.ca", "costco.com"}
            or (("amazon.com" in dom or "amazon.ca" in dom) and ("order" in subj or "receipt" in subj))
            or ("doordash" in frm and ("receipt" in subj or "order" in subj))
            or ("ubereats" in frm and ("receipt" in subj or "order" in subj))
            or ("skipthedishes" in frm and ("receipt" in subj or "order" in subj))
            or ("pizzahut" in frm and ("receipt" in subj or "order" in subj))
            or "bank statement" in subj
            or "credit card statement" in subj
            or "interac" in subj
            or "e-transfer" in subj
            or ("trulocal" in frm and "receipt" in subj)
        ),
    ),
)

# The complete set of category labels the seeds can produce — the closed set the
# LLM fallback is constrained to (plus the "no category" skip). Pinned.
SEED_CATEGORY_LABELS: frozenset[str] = frozenset(r.label for r in _SEED_RULES)


# --- Sender / subject extraction (mirrors n8n's Build-Request-Body parseAddr) -

_ANGLE_ADDR_RE = re.compile(r"<([^>]+)>")
_FROM_LINE_RE = re.compile(r"^\*\*From:\*\*\s*(.*)$", re.MULTILINE)
_HEADING_RE = re.compile(r"^#\s(.*)$", re.MULTILINE)


def _parse_addr(raw: str) -> str:
    """Reduce a From header value to the bare address, mirroring n8n's ``parseAddr`` EXACTLY.

    First ``<addr>`` if present, else the first comma-separated part, trimmed. This is the SAME
    reduction the parity harness pins (``mail.fetcher._normalize_addr``) — kept local here so the filing
    axis carries no import dependency on the mail module (structural orthogonality)."""
    if not raw:
        return ""
    m = _ANGLE_ADDR_RE.search(raw)
    if m:
        return m.group(1).strip()
    return raw.split(",")[0].strip()


def extract_sender_and_subject(inbox_content: str) -> tuple[str, str]:
    """Return ``(from_addr_bare_lower, subject)`` from an email inbox record.

    ``from_addr`` is the parsed bare address, lowercased — exactly n8n's ``_emailFrom``. ``subject`` is
    the raw ``# <heading>`` text (the matcher lowercases it). Returns ``("", "")`` components that are
    absent. The record shape is the fetcher/webhook markdown (``**From:**`` line + ``# `` heading)."""
    from_addr = ""
    m = _FROM_LINE_RE.search(inbox_content or "")
    if m:
        from_addr = _parse_addr(m.group(1).strip()).lower()
    subject = ""
    hm = _HEADING_RE.search(inbox_content or "")
    if hm:
        subject = hm.group(1).strip()
    return from_addr, subject


def match_category(from_addr: str, subject: str, rules: list[FilingRule]) -> tuple[str, str] | None:
    """First-match-wins over ``rules``. Returns ``(parent, child)`` or ``None`` (→ LLM fallback).

    Mirrors n8n: ``domain = from_addr.split('@')[1]``; subject is lowercased here. A raising predicate
    (only possible from a malformed operator ADDITION, never a seed) is skipped so one bad addition can't
    break matching — the seeds and the remaining additions still apply."""
    frm = (from_addr or "").lower()
    subj = (subject or "").lower()
    dom = frm.split("@", 1)[1] if "@" in frm else ""
    for rule in rules:
        try:
            if rule.match(frm, subj, dom):
                return (rule.parent, rule.child)
        except Exception:  # noqa: BLE001 — a bad ADDITION predicate must not break matching
            log.warning("email_filing.rule_predicate_error", rule=rule.label)
            continue
    return None


# --- Rule loader: seeds first (always win), then operator-approved additions -


def _load_additions(additions_path: str | Path) -> list[FilingRule]:
    """Read operator-approved rule additions from a JSON file. TOTAL / fail-safe: a missing, empty, or
    malformed file yields ``[]`` (seeds-only), never raises.

    Additions schema (written by 7c-i-b's approval CLI, never auto-generated)::

        [{"parent": "Finance", "child": "Personal", "match_type": "domain_eq", "match_value": "wealthsimple.com"}]

    ``match_type`` ∈ {``domain_eq``, ``from_substr``, ``domain_substr``, ``subject_substr``}. Each builds a
    single-clause predicate. Additions are appended AFTER the seeds by :func:`load_rules`, so a seed always
    takes precedence (n8n parity is never overridden by an addition)."""
    path = Path(additions_path)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        log.warning("email_filing.additions_unreadable", path=str(path))
        return []
    if not isinstance(data, list):
        return []
    out: list[FilingRule] = []
    for row in data:
        rule = _addition_to_rule(row)
        if rule is not None:
            out.append(rule)
    return out


def _addition_to_rule(row: object) -> FilingRule | None:
    """Build a single-clause FilingRule from one additions row, or None if malformed."""
    if not isinstance(row, dict):
        return None
    parent = row.get("parent")
    child = row.get("child")
    match_type = row.get("match_type")
    match_value = row.get("match_value")
    if not (isinstance(parent, str) and isinstance(child, str)
            and isinstance(match_type, str) and isinstance(match_value, str)):
        return None
    value = match_value.lower()
    if match_type == "domain_eq":
        pred: FilingPredicate = lambda frm, subj, dom, _v=value: dom == _v
    elif match_type == "from_substr":
        pred = lambda frm, subj, dom, _v=value: _v in frm
    elif match_type == "domain_substr":
        pred = lambda frm, subj, dom, _v=value: _v in dom
    elif match_type == "subject_substr":
        pred = lambda frm, subj, dom, _v=value: _v in subj
    else:
        return None
    return FilingRule(parent, child, pred)


def load_rules(additions_path: str | Path | None = None) -> list[FilingRule]:
    """Return the active rule list: the four seeds FIRST (always win → n8n parity), then any
    operator-approved additions appended after them.

    In 7c-i the additions read path is live but the file is empty/absent by default (the write side —
    approval CLI + proposal generation — is 7c-i-b), so this returns the seeds unchanged. Passing
    ``additions_path=None`` returns seeds only."""
    rules = list(_SEED_RULES)
    if additions_path:
        rules.extend(_load_additions(additions_path))
    return rules
