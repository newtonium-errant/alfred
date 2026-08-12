# Provider Payment Summary — DAMAGED SYNTHETIC FIXTURE

<!--
WHOLLY INVENTED DATA, same rule as the clean fixture: no real claimant,
provider or payment appears here.

This file exists to prove the parser LOSES NOTHING SILENTLY. Every row
below is broken in a different way, and each way must produce a named
entry in the skipped list rather than a missing ledger row nobody notices.
A parser that reads 4 of 8 rows and reports success has not succeeded.

One good row sits among the damaged ones deliberately. An "everything was
skipped" assertion passes just as well against a parser that is entirely
broken; the good row is the positive control that proves the parse could
have produced a row at all.
-->

## Statement — 12 May 2026

**Statement Date:** 2026-05-12
**Provider:** Wren Alderly
**Payment Total:** 100.00

| Claim # | Date of Service | Surname | First Name | Benefit Code | Units | Total Billed | Amt Excluded | Deduct | Amt Eligible | % PD | Amount Paid | EOB | Comments |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 90000401 | 2026-05-01 | Aldenshaw | Marisol | 700409 | 1 | 100.00 | 0.00 | 0.00 | 100.00 | 100 | 100.00 | — | the positive control: this row is well-formed and MUST parse |
| 90000402 | 2026-05-02 | Brightwater | Tomas | 700409 | 1 | 100.00 | 0.00 | 0.00 | 100.00 | 100 | N0T-A-NUMBER | — | unreadable amount paid |
| 90000403 | 03/04/2026 | Corvallis | Dev | 700409 | 1 | 100.00 | 0.00 | 0.00 | 100.00 | 100 | 100.00 | — | ambiguous slash date, no date_order configured |
| 90000404 | 2026-05-04 | Dunmoor | Wren | 700409 | 1 | 100.00 | 100.00 |
| 90000405 | 2026-05-05 | Everly | Sana | 700409 | 2.5 | 100.00 | 0.00 | 0.00 | 100.00 | 100 | 100.00 | — | fractional unit count |
| 90000406 | 2026-13-45 | Falkirk | Ivo | 700409 | 1 | 100.00 | 0.00 | 0.00 | 100.00 | 100 | 100.00 | — | not a real calendar date |

## Statement — 20 May 2026

**Statement Date:** 2026-05-20
**Provider:** Wren Alderly

| Claim # | Surname | Benefit Code | Comments |
| --- | --- | --- | --- |
| 90000501 | Aldenshaw | 700409 | this whole table lacks Amount Paid and Date of Service, so there is no key and nothing to reconcile |
