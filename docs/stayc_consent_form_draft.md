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

## What happens to your recording afterward

- **What we keep.** After your visit we keep three things: the audio recording, a written
  transcript of it, and the clinical note in your chart. We keep them so your record is complete
  and accurate, and so we can check the recording if there is ever a question about your care.
- **How it is protected.** The audio recording is **locked with encryption** as soon as your
  note is written. The key that *locks* it stays on the computer here, but the key that *unlocks*
  it is kept **offline, separate from the computer** — so the computer can seal your recording
  away but **cannot open it back up on its own**. The transcript and note are also kept on an
  encrypted drive.
- **Who can open it, and when.** Your sealed recording can only be opened by bringing the offline
  key together with it — and that key is held securely by two named people at the practice. It is
  only opened for a specific, legitimate reason (such as a dispute, an audit, or a clinical
  review), and **every time it is opened, that is recorded**, along with the reason.
- **How long we keep it.** We keep your record for **10 years**, in line with the standard for
  clinical records. If you are a minor, we keep it until you turn 19 plus 10 years — whichever is
  longer. After that, records are reviewed and securely destroyed.

## Your rights over the recording

- **You can withdraw your consent at any time.** If you do, we **stop recording from that point
  forward.** The part of the recording taken *before* you withdrew is kept, sealed and protected,
  as part of your record.
- **You can ask us to securely destroy** the recording, transcript, and note of a visit. This is
  a separate request from withdrawing consent. When we destroy a record, we securely delete it,
  and we keep only a small, information-free log entry showing *that* the record was destroyed and
  when — with none of your health information in it. *(There may be limited situations — such as a
  legal hold — where we are required to keep a record even if you ask us to destroy it; we will
  explain if that ever applies. **[exact wording to confirm at legal review]**)*

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
- The retention/destruction mechanics are now folded in above ("What happens to your recording
  afterward" + "Your rights over the recording"), from the #13 retention arc. The claims map to the
  as-built mechanism as follows (confirm each at legal review):
  - *"locked with encryption … key that unlocks it is kept offline"* — a per-encounter age (X25519)
    asymmetric seal: the daemon holds only the public key; the private key lives offline on USB (two
    copies, two custodians). A compromised running box cannot decrypt. Do **not** simplify this into
    wording that implies the box can open the recording on its own — the offline-key property is the
    load-bearing protection.
  - *"every time it is opened, that is recorded"* — each unseal writes a durable `retention.unsealed`
    event (reason code + ticket reference) to the medico-legal chain.
  - *"withdraw … we stop recording from that point forward … the earlier part is kept"* — withdrawal
    is prospective; pre-withdrawal audio is retained (sealed), not destroyed. Keep the
    withdrawal-vs-destroy distinction crisp — they are different rights.
  - *"ask us to securely destroy"* — the PHIA s.49 destroy-on-request path (a separate, explicit,
    audited action). "Small, information-free log entry" = the surviving PHI-free chain, which proves
    *that* the record was destroyed without holding any content.
  - *"10 years … minor until 19 + 10"* — the s.50 schedule v1 windows (effective 2026-07-19),
    operator-confirmed, still subject to PHIA/CMPA advisor ratification.
  - **Honesty guardrail:** do not claim perfect/guaranteed erasure. "Securely delete" is accurate;
    the operator-facing destroy playbook states the on-disk residual-block limitation honestly.
  - Full operator procedures live in `docs/scribe_retention_destroy_playbook.md`,
    `docs/scribe_retention_retrieval_runbook.md`, and `docs/scribe_breach_runbook.md`.
