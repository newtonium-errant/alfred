---
type: clinical_note
status: ai_draft # ai_draft | attested | amended
title: "{{title}}"
name: "{{title}}"
created: "{{date}}"
# --- AI-draft provenance (the sovereign scribe contract) ---
ai_draft: true # true until a clinician attests; flips to false on attestation
synthetic: true # PROVENANCE / the mode line — true = synthetic input (fail-closed default). A clinical (real-PHI) note carries synthetic: false ONLY once scribe.mode is flipped to clinical (gated on the legal de-id standard).
# --- Attestation (set ONLY on the attest flip — the sole editable metadata) ---
attested_by: null # clinician identity that reviewed + signed the note; null while ai_draft
attested_at: null # ISO timestamp of attestation; null while ai_draft
# --- Retain-the-diff (anti-spoliation) ---
draft_original: null # the verbatim original machine draft, preserved beside the signed version so the pre-attestation content is never lost
tags: []
---

# {{title}}

<!--
SOVEREIGN AMBIENT SCRIBE — clinical_note.

This record is drafted on-box by the sovereign scribe (local STT + local
LLM) and NEVER leaves the box: it is denied cloud egress by the P1-a
boundary, denied cross-instance transit by schema._NEVER_PUSH_TYPES, and
denied deletion/relocation/body-mutation by the vault scope + denysets.

The BODY below is written ONCE at draft time and is FROZEN thereafter. The
ONLY permitted post-draft change is the attestation flip (attested_by /
attested_at / status) via the stayc_clinical_attest_only scope gate. A
correction is a NEW clinical_note with status: amended that supersedes this
one — never an in-place body rewrite.
-->

## Subjective

## Objective

## Assessment

## Plan
