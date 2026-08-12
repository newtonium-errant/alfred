<!--
WHOLLY INVENTED DATA, same rule as the other remittance fixtures: no real
claimant, provider or payment appears here.

THE SHAPE THIS FILE EXISTS FOR: a partial-page statement whose metadata
arrives with NO heading above it. A scan that clips the top of a page, or a
capture that read the header block but not the title, produces exactly this —
and the design's ground truth names partial-page statements explicitly.

It is worth its own file because the parser CRASHED on it
(`UnboundLocalError`) while every other fixture passed. All of them happened
to open with a Markdown heading, so the branch that binds the statement
context always ran first. 271 green tests said nothing about the one shape
that had no heading — the fixtures-must-include-the-shapes-production-runs-in
trap, landing on a lane that had just written that rule down.

Deliberately has no `#` heading anywhere.
-->

**Provider:** Wren Alderly
**Company:** Northbay Therapy Services
**Statement Date:** 2026-05-01
**Payment Total:** 175.00

| Claim # | Date of Service | Surname | First Name | Benefit Code | Units | Total Billed | Amt Excluded | Deduct | Amt Eligible | % PD | Amount Paid | EOB | Comments |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 90000601 | 2026-04-02 | Aldenshaw | Marisol | 700409 | 1 | 100.00 | 0.00 | 0.00 | 100.00 | 100 | 100.00 | — | Invoice #530 |
| 90000602 | 2026-04-03 | Brightwater | Tomas | 700409 | 1 | 75.00 | 0.00 | 0.00 | 75.00 | 100 | 75.00 | — | Invoice #531 |
| SUB-TOTAL | — | Brightwater | — | — | — | 75.00 | 0.00 | 0.00 | 75.00 | — | 75.00 | — | — |
