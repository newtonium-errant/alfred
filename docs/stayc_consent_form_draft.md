# STAY-C — Patient Consent to AI-Assisted Visit Recording (DRAFT)

> ⚑ **LEGAL-FLAG — NOT FOR PATIENT USE YET.** This is a **DRAFT** produced during the synthetic
> build (2026-07-12 scope directive). It **must** be reviewed and signed off against the **NSCN
> "Using AI" guideline**, **PHIA** (Nova Scotia *Personal Health Information Act*), and **CMPA**
> guidance **before it is used with a real patient**. Wording, retention claims, and the custodian
> statement below are placeholders for that legal review — do not treat any statement here as
> legally settled. Sign-off is the operator's and their legal advisor's determination, not the
> build's. See §9.3 of the consent-capture design.

---

## About this form

Your clinician uses **STAY-C**, an AI-assisted scribe that runs **entirely on a computer in this
practice**. With your agreement, it records the audio of your visit and drafts clinical notes to
help your clinician keep an accurate record. This form explains what that means and asks for your
consent. **You may decline** — your visit will go ahead exactly the same way without it.

## What happens if you agree

- **The visit is recorded.** Audio of the conversation is captured while you and your clinician
  talk.
- **The purpose is an accurate record.** The recording supports accurate documentation and an AI
  drafting aid; it does not replace your clinician's judgement.
- **A person reviews the note.** Your clinician **reviews and edits every note before it enters
  your health record.** The AI produces a draft only.
- **The audio stays in the practice.** The recording is held **encrypted, in your clinician's
  secure records on-site. It is not sent to the cloud and is not shared outside this practice**,
  except where the law requires or permits.
- **Your clinician is the custodian** of this information under PHIA (a nurse practitioner acting
  as a health-information custodian, PHIA s.3(f)(i) *[to confirm at legal review]*), and handles
  it under the same duties as the rest of your health record.

## What happens if you decline

Nothing changes about your care. The visit proceeds without any recording, and your clinician
documents the visit the usual way. You may also **withdraw at any time during the visit** — just
say so, and recording stops.

## Your choice

- [ ] **I consent** to the visit being recorded and to STAY-C drafting notes, on the terms above.
- [ ] **I decline.** Please do not record my visit.

Patient name: ______________________________  Date: __________________

Signature: _________________________________

Clinician (obtaining consent): ______________________________

> *For a retained recording, the NSCN "Using AI" guideline expects **express, documented
> consent**. This written form is the documented record; the spoken script below is the verbal
> counterpart obtained at the start of the visit.*

---

## Verbal consent script (spoken counterpart — DRAFT)

*Read at the start of the visit; the per-visit verbal agreement is the operative consent. Adapt to
the operator's ratified 2026-07-12 wording at legal review.*

> "Before we start — I use a secure AI scribe that runs on a computer here in the office. With your
> okay, it records our conversation and helps me write up the visit. The recording stays encrypted
> in my own records here and doesn't leave the practice, and I review every note myself before it
> goes in your chart. It's completely up to you — we can go ahead without it, and you can tell me to
> stop the recording at any time. Is it alright if I record today?"

If the patient agrees, the clinician marks **Confirmed** in STAY-C; if not, **Declined**, and no
recording is captured.

---

## Notes for the reviewing advisor (remove before patient use)

- Cribbed from the **Doctors of BC AI-scribe patient consent template** (build-vs-buy report §7
  item 5), adapted to a Nova Scotia NP-custodian context.
- Confirm the PHIA custodian citation (s.3(f)(i)) and the retained-encrypted characterization
  against the current NSCN "Using AI" guideline and CMPA guidance.
- Confirm whether a single combined consent (recording **and** AI dictation) is acceptable, or
  whether separable consent is required (design Q2 resolved to a single per-visit Confirmed/Declined
  — reconfirm at legal review).
- The retention/destruction mechanics (how long audio is kept, how a withdrawal-marked encounter is
  destroyed on request) are a separate deliverable (#13) and must be described accurately here once
  finalized.
