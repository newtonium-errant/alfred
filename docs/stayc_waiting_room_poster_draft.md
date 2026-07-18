# STAY-C — Waiting-Room Poster (DRAFT)

> ⚑ **LEGAL-FLAG — NOT FOR DISPLAY YET.** This is a **DRAFT** produced during the synthetic build
> (2026-07-12 scope directive). It **must** be reviewed and signed off against the **NSCN "Using AI"
> guideline**, **PHIA**, and **CMPA** guidance **before it is displayed to real patients**. A poster
> is a *notice*, not a substitute for the per-visit verbal consent — the consent still happens in
> the room. See §9.3 of the consent-capture design.

---

## Poster copy (plain language)

# This practice uses a secure AI scribe

**Your clinician may use an AI assistant to help write up your visit.**

- 🖥️ **It runs on a computer here in the office.** Your information is **not sent to the cloud** and
  **stays in this practice.**
- 🎙️ **Recording only happens if you agree.** Your clinician will ask for your **verbal okay** at
  the start of the visit. You can say no — your care is exactly the same either way — and you can
  ask to stop at any time.
- 👩‍⚕️ **A person is always in charge.** Your clinician **reviews and edits every note** before it
  goes in your record. The AI only drafts.
- 🔒 **Your information is kept secure** and handled under the same privacy rules as the rest of
  your health record.

**Questions? Just ask your clinician.**

---

## Design notes for the reviewing advisor (remove before display)

- Cribbed from the **Mika / NSH patient-facing AI-scribe poster** (build-vs-buy report §7 item 5),
  adapted to STAY-C's **on-box, no-cloud-egress** posture — which can be stated **honestly** here
  (the differentiator is real: STAY-C runs locally and does not egress PHI).
- Keep the language at a plain-reading level; the poster is a **notice of the practice's use of AI**,
  and the operative consent remains the per-visit verbal agreement (and the signed consent form,
  `stayc_consent_form_draft.md`).
- Confirm the "not sent to the cloud / stays in this practice" claim matches the finalized retention
  architecture (#13) before display — the poster must not over-state.
- Confirm no wording implies the AI makes clinical decisions (it drafts; the clinician decides).
