<!--
WHOLLY INVENTED DATA. Every name, claim number and figure was made up for
the test suite. No real claimant, provider or payment appears here — the
reason is in remittance_note_synthetic.md and does not depend on who can
read this repository.

THE SHAPES HERE ARE THE THIRD STRATUM the real note surfaced, after the
cross-foot got sharp enough to see them. A "(no date)" statement was
holding 175 claim lines — about 45% of the note — keyed `#0` with no
declared total, and seven dated statements had collateral findings because
their rows and totals were pooled or misattributed.

  A. HEADER VARIANT WITH UNEXTRACTED FACTS. A block whose header carries a
     provider address line, then a bolded MULTI-WORD total label matching no
     known synonym, then a rule, then a heading with no date in it. The
     parser took neither the date nor the total: the block opened undated
     and with nothing declared. Both halves are fixed differently on
     purpose — the amount is CAPTURED under its own label (it is real, its
     meaning is not ours to invent), and the date is recovered from the scan
     batch's own metadata and marked as the weaker claim it is.

  B. `### Page 3 of 4` CONTINUATION-WITH-HEADER. A later page of a
     statement already begun, carrying its own table header — so it is not
     the headerless continuation already handled, and it opened a NEW,
     undated block that fell into the pool. `Page N of M` with N > 1 is the
     provider saying in the document that this continues something.

  C. UNDATED BLOCKS FOLDING WITH EACH OTHER. Two empty headers contradict
     nothing, so the compatible-facts rule judged them one statement and
     every undated block merged into a single pool. That is two blanks
     agreeing — the same error as an empty claimant matching an empty
     aggregate label, one tier up. Absence of conflict is not evidence of
     identity.

THE FIX IS ONE RULE, NOT THREE: a fold requires POSITIVE EVIDENCE of
identity. A shared date is evidence (so dated blocks were accidentally
right); a `Page N of M` marker is evidence (so B attaches); two blanks are
not (so C splits). The three shapes below exercise all three branches.

The last TWO blocks are the control for Shape C, and there are two of them
on purpose. Shape C is "undated blocks fold WITH EACH OTHER", so a single
orphan cannot test it: with one undated block, "exactly one undated block
survives" is true whether the rule works or not. The first draft of this
fixture had exactly one, and the mutation that reverts to the old
compatibility-only rule scored 1 red instead of the 8 it scores now — the
pin was nearly vacuous, and only running the mutation showed it. Two
orphans, neither identifying the other, must stay two.

ARITHMETIC, run not reasoned: statement one pays 1,200.00 (700 + 500) across
its own block and its Page 2; statement two pays 800.00. The orphan block
pays 300.00 and reconciles against nothing, which is the finding.
-->

<!-- BEGIN_INFERRED marker_id="inf-20260812-fixture-11aa22" -->
<!-- batch-04: 'Statement: 05 Jun 2026 — Page 1 of 2' -->

Northbay Therapy Services, 14 Harbour Row

**Remittance Advice Total:** **$1,200.00**

---

## Provider Payment

| Claim # | Date of Service | Surname | First Name | Benefit Code | Units | Total Billed | Amt Excluded | Deduct | Amt Eligible | % PD | Amount Paid | EOB | Comments |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 00000701 | 1 Jun 2026 | Aldenshaw | Marisol | 700409 | 2 | 700.00 | 0.00 | 0.00 | 700.00 | 100 | 700.00 | — | Invoice #701 |
| 00000702 | 2 Jun 2026 | Brightwater | Tomas | 700409 | 0 | 0.00 | 0.00 | 0.00 | 0.00 | 0 | -1700.00 | ZZ30 | Reversal — Invoice #702 |
| 00000703 | 3 Jun 2026 | Brightwater | Tomas | 700409 | 0 | 0.00 | 0.00 | 0.00 | 0.00 | 0 | -3400.00 | ZZ30 | Reversal — Invoice #703 |

<!-- END_INFERRED marker_id="inf-20260812-fixture-11aa22" -->
<!-- BEGIN_INFERRED marker_id="inf-20260812-fixture-33bb44" -->

### Page 2 of 2

| Claim # | Date of Service | Surname | First Name | Benefit Code | Units | Total Billed | Amt Excluded | Deduct | Amt Eligible | % PD | Amount Paid | EOB | Comments |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 00000704 | 4 Jun 2026 | Corvallis | Dev | 700409 | 2 | 500.00 | 0.00 | 0.00 | 500.00 | 100 | 500.00 | — | Invoice #704 |

<!-- END_INFERRED marker_id="inf-20260812-fixture-33bb44" -->

## Statement — 20 Jun 2026

**Statement Date:** 2026-06-20
**Provider:** Wren Alderly
**Payment Total:** **800.00**

| Claim # | Date of Service | Surname | First Name | Benefit Code | Units | Total Billed | Amt Excluded | Deduct | Amt Eligible | % PD | Amount Paid | EOB | Comments |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 00000801 | 5 Jun 2026 | Dunmoor | Wren | 700409 | 4 | 800.00 | 0.00 | 0.00 | 800.00 | 100 | 800.00 | — | Invoice #801 |

## Unattributable Block

| Claim # | Date of Service | Surname | First Name | Benefit Code | Units | Total Billed | Amt Excluded | Deduct | Amt Eligible | % PD | Amount Paid | EOB | Comments |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 00000901 | 6 Jun 2026 | Everly | Sana | 700409 | 2 | 300.00 | 0.00 | 0.00 | 300.00 | 100 | 300.00 | — | Invoice #901 |

## Second Unattributable Block

| Claim # | Date of Service | Surname | First Name | Benefit Code | Units | Total Billed | Amt Excluded | Deduct | Amt Eligible | % PD | Amount Paid | EOB | Comments |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 00000902 | 7 Jun 2026 | Falkirk | Ivo | 700409 | 2 | 400.00 | 0.00 | 0.00 | 400.00 | 100 | 400.00 | — | Invoice #902 |
