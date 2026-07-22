# STAY-C — Secure Destruction Playbook (PHIA s.49)

> ## ⚠ DRAFT — REQUIRES NSCN / PHIA / CMPA REVIEW BEFORE REAL-PATIENT USE
>
> This document is a **draft operator playbook**. The software mechanism it describes is
> built and tested on **synthetic data only**. It must be reviewed and signed off by a
> **PHIA privacy advisor, the Nova Scotia College of Nursing (NSCN), and CMPA/legal
> counsel** before it is used to destroy any real patient's record. Nothing here is legal
> advice; the request-verification and retain-vs-destroy determinations are the
> **custodian's** (Jamie's) call in consultation with those advisors — never the software's.
>
> **Schedule / version anchors this playbook assumes**
> - PHIA s.50 retention schedule: **v1**, effective **2026-07-19** (`schedule_version: "v1"`).
> - Retention window: **10 years** for adults (from last encounter activity); for a minor,
>   **to age of majority (19 in NS) + 10 years**, whichever is longer than the adult window.
> - Destruction is the **explicit, audited exception** to the default (which is *retain,
>   sealed*). It is never automatic — the software surfaces over-window encounters for
>   review but only ever destroys when an operator runs the command in this playbook.

---

## 0. Who this is for, and the one rule to hold in your head

**Reader:** Jamie (the custodian / NP) is the decision-maker. Andrew (the operator) runs
the box. In a small clinic one person may do both — the commands below are written so you
can follow them start to finish, with each command explained in plain language.

**The one rule:** *A consent withdrawal is NOT a destroy request.* When a patient withdraws
consent, we **keep** the audio and note that were captured while they were consented, sealed
in the archive. We only **destroy** when there is an explicit destroy request (or a legal
order, or a schedule expiry that you have reviewed and elected to act on). Withdrawal just
makes the encounter **findable** so that *if* it later escalates to a destroy request, you
can locate the exact record. Withdrawing and destroying are two different things; this
playbook is only about destroying.

---

## 1. Receive and verify the request (custodian determination — LEGAL)

A destruction is triggered by exactly one of three things. Write down which one applies —
you will pass it to the command later as the `--reason`:

| Trigger | `--reason` value | What it means |
|---|---|---|
| The patient exercises their PHIA right to have their record destroyed | `patient_request` | A patient (or their authorized representative) asks, in a way you can verify, to have their record securely destroyed. |
| A legal order requires destruction | `legal_order` | A court order, tribunal direction, or equivalent. |
| The s.50 schedule window has expired and you have elected to destroy | `schedule_expiry` | The 10-year window has passed, the encounter was surfaced for review, and you decided to destroy rather than continue retaining. |

**This is the custodian's call, not the software's (LEGAL-FLAGGED).** Before you go any
further you must satisfy yourself, with your PHIA/CMPA advisor where there is any doubt, that:

1. The requester **is** the patient or their authorized representative (verify identity).
2. There is **no legal hold, dispute, or CMPA matter** that requires the record to be
   retained despite the request. Destruction is irreversible; a retain obligation overrides
   a destroy request.
3. The request is for **destruction**, not merely withdrawal of consent (see the rule in §0).

Record the outcome of this determination in your ticketing / incident log **before**
touching the system, and note the ticket reference — you will pass it as `--ticket`.

---

## 2. Locate the encounter (built commands — these exist today)

Every encounter has an **opaque encounter id** — a scrambled code (a salted HMAC) that is
**not** a patient name and contains no PHI. All destruction commands use this id. The
patient-named folder that *used* to exist on disk is gone once the encounter was sealed;
the id is how we refer to a record without writing the patient's name anywhere.

All commands below are run on the box. The `--config config.stayc-clinical.yaml` part tells
`alfred` to use the STAY-C clinical instance.

**If the request came from a patient who previously withdrew consent**, list the withdrawn
encounters to find the id (the withdrawn marker is the "destroy-addressability" the system
promised):

```bash
alfred --config config.stayc-clinical.yaml scribe events list --family consent --kind consent.withdrawn
```

Each row's `subject_id` is an encounter id. This does **not** destroy anything — it only
lists. (Withdrawal ≠ destroy; this list just tells you which sealed encounters *can* be
addressed if a destroy request arrives.)

**Confirm the encounter exists and see its full history** before you destroy it. This is the
cross-family timeline — consent → capture → attestation → seal, in one ordered view:

```bash
alfred --config config.stayc-clinical.yaml scribe audit encounter <encounter_id>
```

You should see the encounter's life story ending (for a retained encounter) in a
`retention.sealed` event. If you also want the raw per-encounter event list:

```bash
alfred --config config.stayc-clinical.yaml scribe events list --encounter <encounter_id>
```

**On disk** (for the configured STAY-C layout), a sealed encounter's artifacts live at:

- Sealed audio: `/data/algernon/stayc-clinical/data/retained/<encounter_id>.age`
- Manifest sidecar: `/data/algernon/stayc-clinical/data/retained/<encounter_id>.manifest.json`
- Transcript ledger: `/data/algernon/stayc-clinical/data/retained/transcripts/<encounter_id>.transcript.json`
- Clinical note (in the vault): `/data/algernon/stayc-clinical/vault/clinical_note/…md`
  (the note whose frontmatter `source_id` equals `<encounter_id>`)

---

## 3. Run the two-phase destroy

Destruction happens in **two phases**, with the audit event written **first**, so a crash
can never destroy a record without leaving proof that it was destroyed:

1. **Phase 1 — intent.** The system writes a durable `retention.destroy_intent` event to the
   medico-legal chain. This is written to permanent storage before a single file is touched.
2. **Unlink.** The system deletes every PHI-bearing artifact of the encounter:
   - the sealed audio blob (`<encounter_id>.age`),
   - the **manifest sidecar** (`<encounter_id>.manifest.json`) — deleted alongside the blob,
   - any residual plaintext audio chunks (only present for an abandoned-before-seal or
     transient-mode encounter),
   - the transcript ledger,
   - the **sealed-backup staging copies** of the transcript and note
     (`<encounter_id>.transcript.age` and `<encounter_id>.note.age`, in the backup-staging
     dir — these are the age-sealed off-box copies waiting to be shipped; see §5),
   - the vault clinical note (deleted through the privileged `stayc_clinical_destroy` scope),
   - and the **backup copies** of the above already shipped off-box (see §5).
3. **Phase 2 — destroyed.** Only after every unlink **and** the backup purge succeed does the
   system write the durable `retention.destroyed` event. If any on-box unlink fails or the
   backup purge is incomplete, the system **fails loud and does not** write `retention.destroyed`
   — the destruction is left in the incomplete state (see below), never falsely marked done.

**Safety pre-flight (automatic).** Before it writes the intent, the command refuses to proceed
if **any** clinical note in the vault's `clinical_note/` folder cannot be parsed. The reason is
careful: an unparseable note's `source_id` is unknowable, so it *might be* the very record you
are trying to destroy — destroying now could leave that PHI behind while the system reports
success (a false proof-of-destruction). If you see this refusal, fix or remove the unparseable
note first, then re-run.

Run it:

```bash
alfred --config config.stayc-clinical.yaml scribe retention destroy <encounter_id> \
    --reason <patient_request|legal_order|schedule_expiry> \
    --ticket <your-ticket-ref> \
    --justification "<optional free-text detail>"
```

**You will be asked to confirm.** Because this is irreversible, the command prints the
encounter id and asks you to **type it back** before it does anything. This is a deliberate
guard against destroying a fat-fingered neighbour's record. If what you type does not match, it
aborts and nothing is destroyed. (For scripted / non-interactive use only, `--yes` skips this
prompt — do not use it for a real single-record destruction you are doing by hand.)

**Preview first if you want to.** Add `--dry-run` to see exactly what *would* be destroyed
(every file path plus the backup snapshots that would be purged) without emitting anything,
deleting anything, or touching the backups:

```bash
alfred --config config.stayc-clinical.yaml scribe retention destroy <encounter_id> \
    --reason <patient_request|legal_order|schedule_expiry> --ticket <ref> --dry-run
```

Plain-language notes on the flags:

- `<encounter_id>` — the opaque id from §2.
- `--reason` (required) — one of the three trigger words from §1: `patient_request`,
  `legal_order`, or `schedule_expiry`. **Verified against the shipped code:** the `--reason`
  is recorded in the box's mutation-provenance log (`vault_audit.log`), **not** on the permanent
  chain. The chain's destroy events (`retention.destroy_intent` / `retention.destroyed`) carry
  only `{schedule_version, manifest_sha256}` — they have **no** reason field — so the permanent
  chain stays free of any free text. (This is the opposite of the *unseal* `reason_code`, which
  *is* a closed enum recorded on the chain — see the retrieval runbook.)
- `--ticket` (required) — your ticket / incident reference from §1, so the destruction ties back
  to the request that authorized it.
- `--justification` (optional) — free-text detail; it joins the `--reason` in `vault_audit.log`,
  never the chain.

**If the command is interrupted** (power loss, crash) between Phase 1 and Phase 2, the record
is left in an "incomplete destruction" state: the intent is on the chain but the completed
event is not. This is **safe** — nothing was left half-deleted in a way that hides it. The next
verify will flag it (see §4), and simply **re-running the same command finishes the job**
(deleting an already-deleted file is a harmless no-op, and the backup purge re-runs). A fully
completed destruction is also safe to re-run — the command notices it is already done and exits
without doing anything.

---

## 4. Verify completion (13d + built commands)

**a. Confirm the completed event landed** and the encounter's story now ends in destruction:

```bash
alfred --config config.stayc-clinical.yaml scribe audit encounter <encounter_id>
```

The timeline should now end in `retention.destroyed`. (You will still see the *earlier*
events — consent, attestation, seal, destroy-intent, destroyed. That is intentional and
correct; see §6.)

**b. Run the retention verify**, the integrity report:

```bash
alfred --config config.stayc-clinical.yaml scribe retention verify
```

It prints a JSON report and (verified against the shipped code) covers four inconsistency
classes plus an informational over-window count:
- `incomplete_destructions` — an intent with no matching destroyed (a crash between the two
  phases). **After the destroy you just ran, this list must be empty for your encounter.**
- `blob_without_sidecar` / `sidecar_without_blob` — orphaned retained artifacts (a sealed blob
  missing its manifest sidecar, or vice-versa).
- `dangling_schedule_pin` — the chain-pinned schedule drifted from what is on disk.
- `over_window_due` (a count, plus `oldest_over_window`) — sealed encounters past the s.50
  window. This is **informational** (a normal review signal) and does **not** by itself mark the
  report inconsistent.

The command exits non-zero if any of the four inconsistency classes is non-empty (over-window
alone does not cause a failure exit). A clean report prints an explicit "nothing to report".

**c. Confirm the chain itself was not tampered with** (this proves the audit trail is intact,
which is what makes the destruction *provable*):

```bash
alfred --config config.stayc-clinical.yaml scribe events verify
```

This exits cleanly (code 0) on an intact chain and non-zero if anything was altered.

---

## 5. Purge the backups (13d step, seal-before-backup ruling of 2026-07-21)

Because the operator ruled that the sealed archive **is** backed up durably (so a disk
failure does not lose the dispute-protection archive), a destruction is not complete until
the **backup copies** are also gone. The destroy command in §3 is designed to do this for you
as part of the same run; this section explains what it does and how to confirm it.

**What backs STAY-C up:** a dedicated backup job (`stayc-backup`) ships the sealed archive and
the vault to a **separate, dedicated restic repository** with a **10-year keep policy**. The
voice-enrollment (biometric) directory is **structurally excluded** and is never backed up.

**Seal-before-backup (important, and it changes the risk picture):** under the 2026-07-21
ruling, the **off-box copies of the transcript and note are age-sealed to the same offline
key** before they leave the box. So the *entire off-box archive is uniformly encrypted with a
key that does not exist on the box or in the backup*. A stolen or leaked backup is therefore
**crypto-shredded ciphertext** — it cannot be opened without the offline private key. This
means that even if a backup copy were *not* purged, it would not be a readable PHI leak. The
purge below is **defense-in-depth on top of an already-crypto-shredded copy**, not the primary
control.

**How the purge works** (the destroy command does this for you against the **dedicated**
STAY-C backup repo — never the general `algernon-backup` nightly repo; shown here so you
understand it and can confirm it independently). For each of the encounter's artifact paths it
excludes the file from every backup snapshot, then reclaims the space:

```bash
restic rewrite --exclude '<absolute artifact path>' --forget
restic prune
```

Verified against the shipped code: the destroy command runs this purge (`purge_encounter`) and
then **asserts the encounter is gone from the dedicated repo before it writes
`retention.destroyed`**. If the purge is incomplete, the command **fails loud and does not**
emit `retention.destroyed` — exactly the fail-loud behaviour the ruling requires (a destruction
that leaves a backup copy is not "destroyed").

**Confirm the purge succeeded** — the encounter id must no longer appear in any snapshot:

```bash
restic find <encounter_id>
```

This should return **nothing**. If it still finds the encounter, the destruction is
**incomplete**: re-run the destroy command (it is idempotent — the on-box unlinks are no-ops and
the purge re-runs) until `retention verify` and `restic find` are both clean.

> **Note on backup location (flagged for legal review).** The dedicated repo currently lives on
> off-shore storage (Hetzner, Germany/Finland). Because of seal-before-backup, everything in it
> is age-sealed ciphertext, so this is defensible — but a **Canadian / in-jurisdiction backup
> target is a pre-commercialization item** still to be decided with your PHIA advisor.

---

## 6. What survives, and why that is correct (proof of destruction)

After a completed destruction, **every PHI-bearing artifact is gone** — audio, transcript,
note, and their backups. But the **medico-legal event chain for that encounter survives on
purpose.** You will still see, for that encounter id: the consent events, the attestation, the
encounter open/close, the `retention.sealed`, the `retention.destroy_intent`, and the
`retention.destroyed`.

This is **not** a leak. Those events are **PHI-free by construction** — they hold only the
opaque encounter id, cryptographic digests, enumerated codes, and counts. There is no patient
name, no audio, no note text anywhere in them. What survives is the **record that you
destroyed the record** — which is exactly what PHIA s.50's "documented destruction" requires.
You keep proof *that* you destroyed it, not the destroyed content. The surviving chain lets
you show an auditor, years later, that the encounter existed, was properly consented, was
attested, and was securely destroyed on the authority of a specific ticket — without holding
one byte of the patient's information.

---

## 7. The honest limits of on-disk deletion (state this, never overclaim)

Be truthful, in writing, about what deletion does and does not guarantee. This language is
required verbatim-in-spirit; do not soften it:

- **The sealed audio is the strong case.** The audio was already encrypted (sealed) at rest,
  and its plaintext was wiped when it was sealed. Unlinking the `.age` blob leaves, at most,
  residual **age-ciphertext** blocks on the drive — and those are **undecryptable without the
  offline private key**, which never touches the box. Destroying a sealed encounter is
  therefore strong even against low-level residual-block recovery: it is **crypto-shredded by
  construction**.

- **The transcript and note are the honest-limitation case.** These are kept as LUKS-encrypted
  plaintext so the clinician can use them. When they are destroyed the tooling makes a
  **best-effort overwrite of the file's bytes with zeros (then fsync) before unlinking it**, and
  the note is **always permanently removed — never sent to a trash/recycle folder** (the destroy
  bypasses any Obsidian trash routing, so the destruction never depends on whether Obsidian
  happens to be running). The overwrite is **a mitigation, NOT a guarantee**: on SSD storage,
  wear-levelling, journaling, and copy-on-write mean the zero-write may not land on the original
  physical blocks, so residual plaintext blocks can persist. After the unlink, whatever residual
  blocks remain are protected **only** by the **whole-disk encryption** (LUKS), which defends the
  *powered-off, stolen-disk* threat but is **not** a per-file cryptographic erase. A stronger
  per-file guarantee would require re-encrypting the whole volume, which is out of scope for
  on-demand single-encounter destruction. So: best-effort overwrite + forced permanent unlink,
  with the residual-block limit stated plainly — never described as a guaranteed secure wipe.

- **Every electronic medical record has this same limitation.** The correct posture is to
  **state it plainly and never overclaim** perfect erasure. This is why seal-before-backup
  matters: it means the *off-box* copies are all age-sealed and therefore uniformly
  crypto-shredded, closing the weaker plaintext path for everything that leaves the box.

---

## 8. Record the ticket and close out

1. In your ticket / incident log, record: the encounter id, the `--reason`, the date/time, who
   authorized it (the custodian determination from §1), and confirmation that
   `retention.destroyed` landed and `retention verify` + `restic find` are clean.
2. Retain the ticket per your practice's records policy. The `--ticket` reference now links the
   permanent chain, the `vault_audit.log` entry, and your ticket together, so the destruction
   is reconstructable end to end.
3. If any step failed (incomplete destruction flagged by verify, or `restic find` still finds
   the encounter), **do not close the ticket** — escalate to the operator and re-run the
   destroy command, which safely completes the interrupted job.

---

*End of draft. Pending NSCN / PHIA / CMPA sign-off. The post-13d CLI accuracy pass is COMPLETE —
every command here was verified against the shipped 13d CLI (master `1426432`).*
