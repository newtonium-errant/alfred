"""#7 7c-i — rule-port fidelity (THE load-bearing artifact) + the seeds-first loader.

Fixtures ported straight from the n8n "Categorize Email - Code" rule cases. These pin the CATEGORY
DECISION byte-equivalent to n8n: all four categories, the order-sensitive cases, each of the three
predicate forms as-written, and the no-match→LLM-fallback boundary. A reordering or a predicate-form swap
MUST red a pin. UNCONDITIONAL.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from alfred.email_filing.rules import (
    SEED_CATEGORY_LABELS,
    FilingRule,
    _parse_addr,
    extract_sender_and_subject,
    load_rules,
    match_category,
)

RULES = load_rules()


def _cat(from_addr: str, subject: str):
    return match_category(from_addr, subject, RULES)


# ===========================================================================
# All four categories fire
# ===========================================================================

def test_seed_category_labels_are_exactly_the_four():
    assert SEED_CATEGORY_LABELS == frozenset(
        {"Business/Receipts", "Business/Invoices", "Finance/Tax", "Finance/Personal"}
    )


@pytest.mark.parametrize("from_addr,subject,expect", [
    ("billing@digitalocean.com", "Your receipt", ("Business", "Receipts")),
    ("noreply@vendor.com", "Software License renewal", ("Business", "Receipts")),
    ("billing@supabase.com", "anything", ("Business", "Invoices")),
    ("x@vendor.com", "Your invoice is ready", ("Business", "Invoices")),
    ("noreply@cra-arc.gc.ca", "notice", ("Finance", "Tax")),
    ("x@vendor.com", "Your T4 slip is available", ("Finance", "Tax")),
    ("no-reply@costco.ca", "order shipped", ("Finance", "Personal")),
    ("x@vendor.com", "bank statement ready", ("Finance", "Personal")),
])
def test_each_category_fires(from_addr, subject, expect):
    assert _cat(from_addr, subject) == expect


# ===========================================================================
# Order-sensitivity: Receipts before Invoices (first-match-wins)
# ===========================================================================

def test_digitalocean_receipt_hits_receipts_not_invoices():
    # digitalocean.com matches BOTH the Receipts rule (receipt+domain) and the Invoices rule (domain).
    # First-match-wins → Receipts. A reordering would flip this to Invoices.
    assert _cat("billing@digitalocean.com", "Your receipt") == ("Business", "Receipts")


def test_digitalocean_bare_invoice_hits_invoices():
    # No "receipt"/"payment confirmation"/"software license" → Receipts rule misses → falls to Invoices.
    assert _cat("billing@digitalocean.com", "Monthly statement") == ("Business", "Invoices")


def test_cloudflare_receipt_vs_bare_order_sensitivity():
    assert _cat("billing@cloudflare.com", "receipt for you") == ("Business", "Receipts")
    assert _cat("billing@cloudflare.com", "hello") == ("Business", "Invoices")


# ===========================================================================
# Each of the THREE predicate forms, preserved per-rule as-written
# ===========================================================================

def test_predicate_form_domain_equality():
    # domain === X  (exact). costco.ca matches; a subdomain does NOT (exact-equality, not substring).
    assert _cat("x@costco.ca", "anything") == ("Finance", "Personal")
    assert _cat("x@mail.costco.ca", "hello") is None  # exact domain != mail.costco.ca


def test_predicate_form_from_substring():
    # X in from_addr  (substring on the bare from). apple.com + receipt → Personal.
    assert _cat("receipts@apple.com", "your receipt") == ("Finance", "Personal")
    assert _cat("noreply@doordash.com", "your order") == ("Finance", "Personal")
    # canada.ca as a from-substring → Tax (distinct from the cra-arc.gc.ca domain-equality clause).
    assert _cat("noreply@subdomain.canada.ca", "notice") == ("Finance", "Tax")


def test_predicate_form_domain_substring():
    # X in domain  (substring on the domain). amazon.ca / amazon.com + order/receipt → Personal.
    assert _cat("auto@amazon.ca", "your order") == ("Finance", "Personal")
    assert _cat("auto@marketplace.amazon.com", "your receipt") == ("Finance", "Personal")


# ===========================================================================
# No-match → None (the LLM-fallback boundary); case-insensitive subject
# ===========================================================================

def test_no_match_returns_none():
    assert _cat("friend@gmail.com", "lunch tomorrow?") is None
    # "receipt" alone, from an unlisted domain, matches nothing (Receipts needs a listed domain).
    assert _cat("shop@randomstore.com", "your receipt") is None


def test_subject_matching_is_case_insensitive():
    assert _cat("x@vendor.com", "YOUR INVOICE") == ("Business", "Invoices")
    assert _cat("x@vendor.com", "Interac e-Transfer received") == ("Finance", "Personal")


def test_empty_sender_still_matches_subject_only_rules():
    # No From (domain=""): subject-only clauses still fire (e.g. "invoice").
    assert _cat("", "your invoice") == ("Business", "Invoices")
    assert _cat("", "hello") is None


# ===========================================================================
# _parse_addr / extraction mirror n8n parseAddr
# ===========================================================================

def test_parse_addr_mirrors_n8n():
    assert _parse_addr("Jamie Newton <jamie@example.com>") == "jamie@example.com"
    assert _parse_addr("bare@example.com") == "bare@example.com"
    assert _parse_addr("a@x.com, b@y.com") == "a@x.com"
    assert _parse_addr("") == ""


def test_extract_sender_and_subject_from_record():
    rec = "# Your receipt\n\n**From:** Billing <billing@digitalocean.com>\n**To:** a@b.com\n\n---\n\nbody"
    frm, subj = extract_sender_and_subject(rec)
    assert frm == "billing@digitalocean.com"  # parsed bare + lowercased
    assert subj == "Your receipt"


# ===========================================================================
# The seeds-first loader + operator-approved additions (read path ships in 7c-i)
# ===========================================================================

def test_load_rules_returns_four_seeds_by_default():
    assert len(load_rules()) == 4
    assert len(load_rules(None)) == 4


def test_additions_append_after_seeds(tmp_path: Path):
    add = tmp_path / "adds.json"
    add.write_text(json.dumps([
        {"parent": "Finance", "child": "Personal", "match_type": "domain_eq", "match_value": "wealthsimple.com"},
    ]), encoding="utf-8")
    rules = load_rules(add)
    assert len(rules) == 5
    # The seeds are first; the addition is last.
    assert [r.label for r in rules[:4]] == [
        "Business/Receipts", "Business/Invoices", "Finance/Tax", "Finance/Personal",
    ]
    # The new domain now matches via the addition.
    assert match_category("x@wealthsimple.com", "anything", rules) == ("Finance", "Personal")


def test_seeds_always_win_over_additions(tmp_path: Path):
    # An addition that would recategorize a SEED-covered case must NOT override the seed (seeds are first).
    add = tmp_path / "adds.json"
    add.write_text(json.dumps([
        {"parent": "Finance", "child": "Personal", "match_type": "domain_eq", "match_value": "digitalocean.com"},
    ]), encoding="utf-8")
    rules = load_rules(add)
    # digitalocean bare still hits the Business/Invoices SEED, not the Finance/Personal addition.
    assert match_category("x@digitalocean.com", "statement", rules) == ("Business", "Invoices")


def test_additions_all_four_predicate_forms(tmp_path: Path):
    add = tmp_path / "adds.json"
    add.write_text(json.dumps([
        {"parent": "Finance", "child": "Personal", "match_type": "domain_eq", "match_value": "wealthsimple.com"},
        {"parent": "Finance", "child": "Personal", "match_type": "from_substr", "match_value": "venmo"},
        {"parent": "Finance", "child": "Personal", "match_type": "domain_substr", "match_value": "paypal"},
        {"parent": "Business", "child": "Receipts", "match_type": "subject_substr", "match_value": "purchase confirmation"},
    ]), encoding="utf-8")
    rules = load_rules(add)
    assert match_category("x@wealthsimple.com", "z", rules) == ("Finance", "Personal")
    assert match_category("venmo-noreply@x.com", "z", rules) == ("Finance", "Personal")
    assert match_category("x@mail.paypal.com", "z", rules) == ("Finance", "Personal")
    assert match_category("x@unlisted.com", "your purchase confirmation", rules) == ("Business", "Receipts")


def test_additions_fail_safe_on_missing_or_malformed(tmp_path: Path):
    # Missing file → seeds only.
    assert len(load_rules(tmp_path / "nope.json")) == 4
    # Malformed JSON → seeds only (fail-safe, no raise).
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert len(load_rules(bad)) == 4
    # A malformed ROW is skipped; valid rows in the same file still load.
    mixed = tmp_path / "mixed.json"
    mixed.write_text(json.dumps([
        {"parent": "Finance"},  # missing fields → skipped
        {"parent": "Finance", "child": "Personal", "match_type": "domain_eq", "match_value": "ok.com"},
        {"parent": "X", "child": "Y", "match_type": "unknown_form", "match_value": "z"},  # bad form → skipped
    ]), encoding="utf-8")
    assert len(load_rules(mixed)) == 5  # 4 seeds + 1 valid addition


def test_raising_addition_predicate_does_not_break_matching():
    # A predicate that raises (only possible from a bad addition) is skipped; matching continues.
    boom = FilingRule("X", "Y", lambda frm, subj, dom: (_ for _ in ()).throw(RuntimeError("boom")))
    rules = [boom] + load_rules()
    # The raising rule is skipped; the digitalocean invoice seed still matches.
    assert match_category("x@digitalocean.com", "statement", rules) == ("Business", "Invoices")
