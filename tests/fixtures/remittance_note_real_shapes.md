<!--
WHOLLY INVENTED DATA. Every name, claim number, invoice number, EOB code and
dollar figure below was made up for the test suite. No real claimant,
provider, company or payment appears here, and none ever may — the reason is
in the header of remittance_note_synthetic.md and it does not depend on who
can currently read this repository.

WHY THIS FILE EXISTS, separately from the other fixtures. A read-only dry run
of the seeder against a genuine provider payment summary skipped 146 rows.
Every loss was named — the fail-loud promise held — but three structural
shapes were responsible, and NONE of them was present in the synthetic
population, which is exactly why 277 green tests said nothing about any of
them. This file carries all three, with invented content:

  1. BOLDED AGGREGATES. Real statements bold every SUB-TOTAL and
     STATEMENT-TOTAL figure: `**1150.00**`, `**$40,641.00**`,
     `**-52440.00**`. The first build refused them outright, which zeroed
     every subtotal row and left the report's cross-foot — the only
     independent check there is — with nothing to check. A bolded NEGATIVE
     subtotal is included because that is real money going quiet.
     The labels are bolded too (`**SUB-TOTAL**`, `**Aldenshaw**`), which
     matters separately: un-bolding the amount without un-bolding the label
     fixes half the shape and leaves claimant matching broken.

  2. A HEADERLESS CONTINUATION TABLE. A multi-page statement resumes its
     rows after a page break WITHOUT repeating the header, so the table's
     first row is data. Read as a header it maps nothing, so the whole
     table was skipped and its claim lines never reached the ledger. This
     is the shape that lost real money rows, and the continuation here
     carries a multi-unit row (`Units = 2`) and a text date for the same
     reason the real one did.

  3. A TWO-COLUMN STATEMENT-TOTALS BLOCK — headings `['', 'Amount']`, rows
     that are label/amount pairs. Captured as declared totals; deliberately
     NOT mapped onto payment_total, because which labelled figure is "the"
     payment total is a question the statement does not answer.

The arithmetic is internally consistent, and the figures below were RUN
rather than reasoned about: the first statement's claim lines sum to
2,932.50 (1150.00 + 57.50 + 1150.00 + 575.00), which is what the
"Clearinghouse Payment Amount" declared total says, so the report's
declared-total comparison has something true to find. The carrier figure
deliberately does NOT match — a fixture where every figure agrees cannot
tell a working comparison from a broken one.
-->

## Statement — 26 Feb 2026

**Statement Date:** 2026-02-26
**Provider:** Wren Alderly
**Company:** Northbay Therapy Services

|  | Amount |
| --- | --- |
| Carrier Statement Amount | **$40,641.00** |
| Clearinghouse Payment Amount | **2,932.50** |

| Claim # | Date of Service | Surname | First Name | Benefit Code | Units | Total Billed | Amt Excluded | Deduct | Amt Eligible | % PD | Amount Paid | EOB | Comments |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 00000101 | 23 Feb 2026 | Aldenshaw | Marisol | 700409 | 2 | 1150.00 | 0.00 | 0.00 | 1150.00 | 100 | 1150.00 | — | Invoice #501 |
| 00000101 | 23 Feb 2026 | Aldenshaw | Marisol | OGST | 1 | 57.50 | 0.00 | 0.00 | 57.50 | 100 | 57.50 | — | OGST on Invoice #501 |
| **SUB-TOTAL** | — | **Aldenshaw** | — | — | — | **1,207.50** | — | — | — | — | **1,207.50** | — | — |

| 00000197 | 24 Feb 2026 | Brightwater | Tomas | 700409 | 2 | 1150.00 | 0.00 | 0.00 | 1150.00 | 100 | 1150.00 | — | Invoice #502 |
| 00000198 | 25 Feb 2026 | Corvallis | Dev | 700409 | 1 | 575.00 | 0.00 | 0.00 | 575.00 | 100 | 575.00 | — | Invoice #503 |
| **SUB-TOTAL** | — | **Brightwater** | — | — | — | **1,150.00** | — | — | — | — | **1,150.00** | — | — |
| **SUB-TOTAL** | — | **Corvallis** | — | — | — | **575.00** | — | — | — | — | **575.00** | — | — |

## Statement — 15 Apr 2026

**Statement Date:** 2026-04-15
**Provider:** Wren Alderly

| Claim # | Date of Service | Surname | First Name | Benefit Code | Units | Total Billed | Amt Excluded | Deduct | Amt Eligible | % PD | Amount Paid | EOB | Comments |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 00000210 | 2 Mar 2026 | Dunmoor | Wren | 700409 | 4 | 2300.00 | 0.00 | 0.00 | 2300.00 | 100 | -52440.00 | ZZ22 | Reversal of earlier payment — Invoice #511 |
| **SUB-TOTAL** | — | **Dunmoor** | — | — | — | **2,300.00** | — | — | — | — | **-52,440.00** | — | — |
