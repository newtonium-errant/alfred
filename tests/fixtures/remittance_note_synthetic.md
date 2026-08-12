# Provider Payment Summary — SYNTHETIC FIXTURE

<!--
WHOLLY INVENTED DATA. Every name, claim number, invoice number, EOB code
and dollar figure in this file was made up for the test suite. No real
claimant, provider, company or payment appears here, and none ever may.

The reason is NOT about who can currently read this repository. It is that
this is shared multi-instance infrastructure, and shared code travels —
into clones, worktrees, wheels, other instances, and any future change of
visibility. Real claims data belongs in VERA's own fenced stores, at any
visibility. A rule resting on "nobody can see this" retires itself the day
a setting changes; this one cannot, because it is about where the data
belongs rather than about who is currently looking.

The file's job is to reproduce the STRUCTURAL quirks of a real provider
payment summary so the parser is exercised against the shapes it will
actually meet:

  * several statements in one note, each with its own header block
  * per-claimant SUB-TOTAL rows interleaved with claim lines
  * OGST sibling lines sharing a claim number with their base benefit line
  * a negative Amount Paid (a reversal/clawback), including a large one
  * "(Ambulance Claims)" in the Claim # column instead of a number, TWICE
    on one statement, which collides on the ratified key and must be
    disambiguated rather than silently overwritten
  * "Invoice #N" inside the Comments column — the P2 join key
  * an escaped pipe inside a Comments cell
  * a BEGIN_INFERRED / END_INFERRED span around a statement whose figures
    were inferred at capture rather than transcribed
  * a statement with no provider metadata at all

The arithmetic is internally consistent on purpose: each statement's
declared Payment Total equals the sum of its claim lines' Amount Paid, and
each SUB-TOTAL equals its claimant's lines. That is what lets the report's
cross-foot assert agreement rather than merely not-crashing.
-->

## Statement — 26 Feb 2026

**Statement Date:** 2026-02-26
**Provider:** Wren Alderly
**Company:** Northbay Therapy Services
**Payment Total:** 348.00

| Claim # | Date of Service | Surname | First Name | Benefit Code | Units | Total Billed | Amt Excluded | Deduct | Amt Eligible | % PD | Amount Paid | EOB | Comments |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 90000101 | 2026-02-10 | Aldenshaw | Marisol | 700409 | 2 | 240.00 | 0.00 | 0.00 | 240.00 | 100 | 240.00 | — | Invoice #501 |
| 90000101 | 2026-02-10 | Aldenshaw | Marisol | OGST | 1 | 12.00 | 0.00 | 0.00 | 12.00 | 100 | 12.00 | — | OGST on Invoice #501 |
| SUB-TOTAL | — | Aldenshaw | — | — | — | 252.00 | 0.00 | 0.00 | 252.00 | — | 252.00 | — | — |
| 90000102 | 2026-02-12 | Brightwater | Tomas | 700409 | 1 | 120.00 | 0.00 | 0.00 | 96.00 | 80 | 96.00 | ZZ14 | Partial coverage — Invoice #502 |
| SUB-TOTAL | — | Brightwater | — | — | — | 120.00 | 0.00 | 0.00 | 96.00 | — | 96.00 | — | — |

## Statement — 15 Apr 2026

**Statement Date:** 2026-04-15
**Provider:** Wren Alderly
**Company:** Northbay Therapy Services
**Payment Total:** -30.00

| Claim # | Date of Service | Surname | First Name | Benefit Code | Units | Total Billed | Amt Excluded | Deduct | Amt Eligible | % PD | Amount Paid | EOB | Comments |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 90000201 | 2026-03-02 | Corvallis | Dev | 700409 | 4 | 480.00 | 0.00 | 0.00 | 480.00 | 100 | -480.00 | ZZ22 | Reversal of earlier payment — Invoice #511 |
| SUB-TOTAL | — | Corvallis | — | — | — | 480.00 | 0.00 | 0.00 | 480.00 | — | -480.00 | — | — |
| (Ambulance Claims) | 2026-03-05 | Dunmoor | Wren | 700409 | 1 | 300.00 | 0.00 | 0.00 | 300.00 | 100 | 300.00 | — | Invoice #512 |
| (Ambulance Claims) | 2026-03-05 | Dunmoor | Wren | 700409 | 1 | 150.00 | 0.00 | 0.00 | 150.00 | 100 | 150.00 | — | Invoice #513 |
| SUB-TOTAL | — | Dunmoor | — | — | — | 450.00 | 0.00 | 0.00 | 450.00 | — | 450.00 | — | — |

<!-- BEGIN_INFERRED marker_id="inf-20260812-fixture-aa11bb" -->

## Statement — 30 Jul 2026

**Statement Date:** 2026-07-30
**Payment Total:** 200.00

| Claim # | Date of Service | Surname | First Name | Benefit Code | Units | Total Billed | Amt Excluded | Deduct | Amt Eligible | % PD | Amount Paid | EOB | Comments |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 90000301 | 2026-07-01 | Everly | Sana | 700409 | 1 | 200.00 | 0.00 | 0.00 | 200.00 | 100 | 200.00 | — | Split billing \| see Invoice #520 |

<!-- END_INFERRED marker_id="inf-20260812-fixture-aa11bb" -->
