<!--
WHOLLY INVENTED DATA. Every name, claim number and dollar figure below was
made up for the test suite. No real claimant, provider or payment appears
here. The reason does not depend on who can read this repository: this is
shared multi-instance infrastructure and shared code travels; real claims
data belongs in VERA's own fenced stores at any visibility.

THIS FIXTURE REPRODUCES A REAL FINDING'S ARITHMETIC TO THE CENT. The dry run
against a genuine payment summary put one statement 45,178.00 out, and the
decomposition was exact: 28,284.00 of absorbed aggregate rows plus 16,894.00
of a second same-day statement's lines folded into the first. The shapes and
the sums below are built to reproduce both, so the fixture fails by a STATED
amount if either fix regresses rather than merely failing somehow.

THE TWO SHAPES:

  1. FULL-WIDTH AGGREGATE ROWS. Three per-claimant aggregates (2,052.00 /
     1,026.00 / 4,344.00) and one grand total (20,862.00) print as rows with
     the SAME column count as claim lines, so the two-column totals-block
     detector cannot see them, and their id cells are WORDS rather than the
     string "SUB-TOTAL", so a literal-label check misses them too. Absorbed
     as claim lines they add 28,284.00 to the statement's paid sum — the
     provider's own arithmetic double-counted into ours, which is the one
     error a cross-foot cannot catch, because it corrupts the number being
     checked. Their tell is field population: no date of service, no benefit
     code, no units, and no digit in the id.

     Note the grand total equals the declared payment_total exactly. That is
     what makes it identifiable as the grand total rather than guessable.

  2. TWO STATEMENTS ISSUED ON ONE DATE. Both blocks are headed 2026-04-23;
     their payment totals differ (20,862.00 and 16,894.00), which is the
     conflict that must keep them apart. Keyed on the date alone the second
     block's header overwrites the first's and its claim lines are
     attributed to the first — 16,894.00 of someone else's money inside a
     statement that never paid it.

THE ARITHMETIC, run rather than reasoned about:

  statement A claim lines            20,862.00  (1026+1026+1026+2172+2172+13440)
  + absorbed aggregates              28,284.00  (2052+1026+4344+20862)
  + statement B claim lines          16,894.00  (8447+8447)
  = the pre-fix sum                  66,040.00
  - the declared payment total       20,862.00
  = the pre-fix delta                45,178.00

  Post-fix, each statement reconciles to zero against its own declared
  total. A fixture where the sums merely "look plausible" could not tell a
  working fix from a partial one.

The third statement is the CONTROL: a same-date block whose header carries no
conflicting fact, which must FOLD rather than split — otherwise the fix would
turn every re-printed continuation header into a phantom statement, trading
one silent error for a noisy one.
-->

## Statement — 23 Apr 2026

**Statement Date:** 2026-04-23
**Provider:** Wren Alderly
**Payment Total:** **20,862.00**

| Claim # | Date of Service | Surname | First Name | Benefit Code | Units | Total Billed | Amt Excluded | Deduct | Amt Eligible | % PD | Amount Paid | EOB | Comments |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 00000401 | 1 Apr 2026 | Aldenshaw | Marisol | 700409 | 2 | 1026.00 | 0.00 | 0.00 | 1026.00 | 100 | 1026.00 | — | Invoice #601 |
| 00000402 | 2 Apr 2026 | Aldenshaw | Marisol | 700409 | 2 | 1026.00 | 0.00 | 0.00 | 1026.00 | 100 | 1026.00 | — | Invoice #602 |
| Aldenshaw Group | — | — | — | — | — | **2,052.00** | — | — | — | — | **2,052.00** | — | — |
| 00000403 | 3 Apr 2026 | Brightwater | Tomas | 700409 | 2 | 1026.00 | 0.00 | 0.00 | 1026.00 | 100 | 1026.00 | — | Invoice #603 |
| Brightwater Group | — | — | — | — | — | **1,026.00** | — | — | — | — | **1,026.00** | — | — |
| 00000404 | 4 Apr 2026 | Corvallis | Dev | 700409 | 4 | 2172.00 | 0.00 | 0.00 | 2172.00 | 100 | 2172.00 | — | Invoice #604 |
| 00000405 | 5 Apr 2026 | Corvallis | Dev | 700409 | 4 | 2172.00 | 0.00 | 0.00 | 2172.00 | 100 | 2172.00 | — | Invoice #605 |
| Corvallis Group | — | — | — | — | — | **4,344.00** | — | — | — | — | **4,344.00** | — | — |
| 00000406 | 6 Apr 2026 | Dunmoor | Wren | 700409 | 8 | 13440.00 | 0.00 | 0.00 | 13440.00 | 100 | 13440.00 | — | Invoice #606 |
| Payment Summary | — | — | — | — | — | — | — | — | — | — | **20,862.00** | — | — |

## Statement — 23 Apr 2026

**Statement Date:** 2026-04-23
**Provider:** Wren Alderly
**Payment Total:** **16,894.00**

| Claim # | Date of Service | Surname | First Name | Benefit Code | Units | Total Billed | Amt Excluded | Deduct | Amt Eligible | % PD | Amount Paid | EOB | Comments |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 00000501 | 7 Apr 2026 | Everly | Sana | 700409 | 4 | 8447.00 | 0.00 | 0.00 | 8447.00 | 100 | 8447.00 | — | Invoice #610 |
| 00000502 | 8 Apr 2026 | Falkirk | Ivo | 700409 | 4 | 8447.00 | 0.00 | 0.00 | 8447.00 | 100 | 8447.00 | — | Invoice #611 |

## Statement — 30 Apr 2026

**Statement Date:** 2026-04-30
**Provider:** Wren Alderly
**Payment Total:** **500.00**

| Claim # | Date of Service | Surname | First Name | Benefit Code | Units | Total Billed | Amt Excluded | Deduct | Amt Eligible | % PD | Amount Paid | EOB | Comments |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 00000601 | 9 Apr 2026 | Greymouth | Nils | 700409 | 2 | 250.00 | 0.00 | 0.00 | 250.00 | 100 | 250.00 | — | Invoice #620 |

## Statement — 30 Apr 2026

**Statement Date:** 2026-04-30

| Claim # | Date of Service | Surname | First Name | Benefit Code | Units | Total Billed | Amt Excluded | Deduct | Amt Eligible | % PD | Amount Paid | EOB | Comments |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 00000602 | 10 Apr 2026 | Greymouth | Nils | 700409 | 2 | 250.00 | 0.00 | 0.00 | 250.00 | 100 | 250.00 | — | Invoice #621 |
