---
type: clinical_note
status: ai_draft # ai_draft | attested | amended
title: "{{title}}"
name: "{{title}}"
created: "{{date}}"
# --- AI-draft provenance (the sovereign scribe contract) ---
ai_draft: true # set true at create; the live-vs-sealed source of truth is 'status', not this boolean
synthetic: true # PROVENANCE / the mode line — true = synthetic input (fail-closed default). A clinical (real-PHI) note carries synthetic: false ONLY once scribe.mode is flipped to clinical (gated on the legal de-id standard).
# --- Attestation (set ONLY on the attest flip — the sole editable metadata) ---
attested_by: null # clinician identity that reviewed + signed the note; null while ai_draft
attested_at: null # ISO timestamp of attestation; null while ai_draft
# --- Retain-the-diff (anti-spoliation) ---
draft_original: null # anti-spoliation retain-the-diff: the AI's un-edited draft body, written by the PIPELINE (a frontmatter field — NOT the body, so it never perturbs the clobber-detect body-sha) on every checkpoint while status==ai_draft, always set to the pipeline's latest generated body (== body at create; a DRAFT_EDIT_FIELD alongside grounding_flags in STAYC_CLINICAL_DRAFT_EDIT_FIELDS, pipeline-writable ONLY while ai_draft). If the clinician hand-edits the body, clobber-detect freezes auto-evolution so this stays = the pipeline's LAST generated body (the machine's un-edited final work). Sealed with the note at attest → the attest-diff of the signed body vs draft_original shows exactly what the clinician changed. (NOT captured-at-attest, NOT null-until-wired — wired in P3-b3.)
tags: []
---

# {{title}}

<!--
SOVEREIGN AMBIENT SCRIBE — clinical_note.

This record is drafted on-box by the sovereign scribe (local STT + local
LLM) and NEVER leaves the box: it is denied cloud egress by the P1-a
boundary, denied cross-instance transit by schema._NEVER_PUSH_TYPES, and
denied deletion/relocation by the vault scope; its body is denysetted
(_BODY_MUTATE_DENIED_TYPES) except one status-gated carve-out — an in-place
body_replace refresh while status==ai_draft (frozen on attest, below).

The BODY below is MUTABLE while this note is a live, unattested ai_draft: the
checkpoint co-pilot refreshes it in place under the stayc_clinical scope via a
whole-body rewrite (body_replace — the pipeline's mechanism, and the ONLY
permitted body mutation on a live draft). body_append / body_rewriter are DENIED
even while ai_draft (P3-a WARN least-privilege + grounding-integrity: they would
mutate the body WITHOUT refreshing grounding_flags, desyncing the draft from its
grounding report), and mid-document body_insert_at stays denied. grounding_flags
and draft_original are the ONLY draft-editable frontmatter fields
(STAYC_CLINICAL_DRAFT_EDIT_FIELDS), refreshed alongside the body. The body is
SEALED (anti-spoliation) the moment the note is attested or
amended (status in {attested, amended}) — every body mutation is then frozen;
fail-closed on missing/unknown status. The attest triad (attested_by /
attested_at / status) is NEVER flipped in place here — it is orchestrator-only
via 'alfred scribe attest' (the stayc_clinical_attest scope), in ANY status. A
correction to a SEALED note is a NEW clinical_note with status: amended that
supersedes this one — never an in-place rewrite of a sealed body.
-->

## Subjective

## Objective

## Assessment

## Plan
