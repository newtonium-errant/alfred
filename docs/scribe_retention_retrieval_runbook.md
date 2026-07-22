# STAY-C — Sealed-Audio Retrieval (Unseal) Runbook

> ## ⚠ DRAFT — REQUIRES NSCN / PHIA / CMPA REVIEW BEFORE REAL-PATIENT USE
>
> This is a **draft operator runbook** for opening a sealed encounter's audio when there is
> a legitimate reason to review it (a dispute, an audit, a re-diarization, or a clinical
> review). The mechanism is built and tested on **synthetic data only**. It must be reviewed
> and signed off by a **PHIA privacy advisor, NSCN, and CMPA/legal counsel** before it is used
> on any real patient's record. Opening a sealed record is a privileged act that is logged;
> the decision that a reason is legitimate is the **custodian's** (Jamie's) call.
>
> **Schedule / version anchors this runbook assumes**
> - PHIA s.50 retention schedule: **v1**, effective **2026-07-19**.
> - Retention window: **10 years** adult (from last activity); minor **to age 19 + 10 years**,
>   whichever is longer. Sealed audio is retained for this window; retrieval is the on-demand
>   read path into that archive.

---

## 0. Who this is for, and how the seal works in one paragraph

**Reader:** Jamie (custodian / NP) decides *whether* to open a record and for *what reason*.
Andrew (operator) can run the technical steps. In a small clinic one person may do both; the
commands are written to be followed start to finish with plain explanations.

**How the seal works:** each encounter's audio is encrypted ("sealed") to a **public** key
that lives on the box. The matching **private** key — the only thing that can open it — lives
**offline**, on a USB stick, in two copies (one held by Andrew, one by Jamie), and **never**
touches the box while it is running. This is deliberate: even if the running computer were
compromised, an attacker could not decrypt the archive, because the key that opens it is not
there. Retrieval is the controlled procedure for bringing the offline key together with a
sealed blob, opening exactly one encounter, using it, and wiping the plaintext again.

The sealed files use the **age** encryption format — a public, standard format. That matters
for the long haul: a sealed blob can be opened a decade from now with the ordinary `age`
command-line tool, **without needing any of our software**. That is the whole point of the
"stronger path" in §3.

---

## 1. Decide the reason (custodian determination — recorded on the chain)

Every unseal records **why** it happened. The reason is a fixed code — you must pick one of
exactly these four (this is a closed list enforced by the software; anything else is refused):

| `--reason` code | Use it when… |
|---|---|
| `dispute` | A patient dispute or complaint requires reviewing what was actually said/recorded. |
| `audit` | A privacy/quality audit or regulator request requires review. |
| `rediarize` | The audio needs to be re-processed (e.g. to re-separate who-said-what). |
| `clinical_review` | A legitimate clinical reason to re-listen to the encounter. |

The **reason code** goes onto the permanent medico-legal chain (so "why was this audio opened"
is auditable forever). Any **free-text explanation** you write goes to the box's provenance log
(`vault_audit.log`), **not** the chain — this keeps the permanent chain free of any text that
could ever contain PHI. Have your ticket / incident reference ready; you will pass it as
`--ticket`.

---

## 2. Pragmatic single-clinician path (the default runbook)

Use this when you are working on the box itself. It stops the daemon so no running process ever
holds the private key, opens one encounter, and wipes the plaintext when done.

**Step 1 — Stop the STAY-C daemon.** No running process should hold the private key.

```bash
sudo systemctl stop alfred-stayc-clinical.service
```

**Step 2 — Mount the offline USB** that holds the private key. The private key is a short line
of text beginning `AGE-SECRET-KEY-…`. Treat that stick like a controlled drug: it opens every
sealed encounter (see §5).

```bash
# example — confirm your actual device/mount point
sudo mount /dev/sdX1 /media/usb
```

**Step 3 — Unseal exactly one encounter** into a temporary directory:

```bash
alfred --config config.stayc-clinical.yaml scribe retention unseal <encounter_id> \
    --key /media/usb/stayc_seal_priv \
    --out /tmp/stayc-unseal \
    --reason <dispute|audit|rediarize|clinical_review> \
    --ticket <your-ticket-ref> \
    --justification "<optional free-text why>"
```

What this does, in plain language (verified against the shipped code):
- decrypts the sealed blob (for the configured STAY-C layout,
  `/data/algernon/stayc-clinical/data/retained/<encounter_id>.age`) into the `--out` directory
  (the audio chunks come back out as they went in);
- **verifies** the decrypted contents against the recorded fingerprints (`manifest_sha256`, and
  the per-chunk digests) so you know the archive was not corrupted. If verification fails it
  **fails closed** — no plaintext is left behind and **no event is written** (the open did not
  happen);
- **refuses to open a blob that the chain does not attest** — a sealed encounter always has a
  durable `retention.sealed` record, so a blob with no chain record is refused (investigate why
  a blob exists with no record before opening it);
- writes a durable `retention.unsealed` event to the chain, carrying only the reason code
  (`--reason`) and your ticket reference (`--ticket`);
- routes your optional `--justification` free-text to `vault_audit.log` — **never** the chain.

The flags: `--key` is the path to the offline age identity file (`AGE-SECRET-KEY-…`), `--out`
is the temp directory (wiped on exit), `--reason` and `--ticket` are required, and
`--justification` is optional free text for the audit log.

**Step 4 — Do the work.** Review or re-diarize the audio in the `--out` directory.

**Step 5 — Wipe and restore.** The tool wipes the temporary plaintext when it exits; confirm
the `--out` directory is empty, unmount the USB, and restart the daemon:

```bash
ls -la /tmp/stayc-unseal        # confirm the plaintext is gone
sudo umount /media/usb          # remove the private key from the machine
sudo systemctl start alfred-stayc-clinical.service
```

---

## 3. Stronger path (recommended when feasible) — a separate trusted machine

The safest way to open a record keeps the private key **completely away from the box**. Copy
only the one sealed blob to a separate, trusted, offline machine and open it there with the
**stock `age` binary** — no STAY-C software required. This is the decade-scale, codebase-
independent guarantee: as long as `age` (a widely used standard tool) exists, the archive is
openable, even if this project's code is long gone.

**Step 1 — Copy only the one blob** to the trusted machine (e.g. via the USB or a direct
transfer). Copy `<encounter_id>.age` and, if you want to re-verify integrity, its sidecar
`<encounter_id>.manifest.json`. Do **not** move the whole archive.

**Step 2 — On the trusted machine, mount the offline key USB and decrypt** with plain `age`:

```bash
age --decrypt -i /media/usb/stayc_seal_priv -o <encounter_id>.tar <encounter_id>.age
tar -xf <encounter_id>.tar          # recovers manifest.json + the chunk_<seq> audio files
```

**Step 3 — (Recommended) cross-check integrity** using the PHI-free sidecar: confirm the blob's
digest matches the sidecar's `blob_sha256`, and each recovered chunk's digest matches the
sidecar's per-chunk `sha256`. The sealed tar also carries its own internal `manifest.json`;
checking against the *sidecar* as well is belt-and-suspenders.

**Step 4 — Record the off-box open on the box** using `--record-only`. Because the decrypt
happened off-box, the box did not write the `retention.unsealed` event. Record it afterward so
the chain still shows the audio was opened and why:

```bash
alfred --config config.stayc-clinical.yaml scribe retention unseal <encounter_id> \
    --record-only \
    --reason <dispute|audit|rediarize|clinical_review> \
    --ticket <your-ticket-ref> \
    --justification "opened off-box on <named trusted machine> — <why>"
```

Verified against the shipped code: `--record-only` emits the `retention.unsealed` event
**without** a local decrypt (it forbids `--key`/`--out`), and — like the on-box path — it
refuses to record against an encounter that was never sealed (the chain must already hold a
`retention.sealed` record). Be honest about what this is: the box **cannot cryptographically
witness** an off-box decrypt, so `--record-only` is an **operator attestation** that an off-box
open occurred — not cryptographic proof of it. Name the trusted machine in the `--justification`
(which lands in `vault_audit.log`) so the attestation is meaningful.

**Step 5 — Wipe the plaintext** on the trusted machine and unmount the key USB when done.

---

## 4. A note for restores (why the seal date matters)

The system tracks each sealed encounter's age from the **durable `retention.sealed` event on
the chain**, not from the file's timestamp on disk. This is deliberate: if the archive is ever
**restored from backup**, file timestamps can be reset, but the chain still holds the true seal
date. If you ever restore the sealed archive from backup and notice the over-window review list
behaving oddly, know that the authoritative age basis is the chain event; the file mtime is only
a **degraded fallback** used when no chain event is found. Re-running `scribe events verify` and
`scribe retention verify` after a restore confirms the chain is intact and the age basis is
sound.

---

## 5. USB custody discipline (do not skip)

The offline private key is the single most sensitive object in the whole system — it opens
**every** sealed encounter, for the entire 10-year window. Treat it accordingly:

- **Two copies, two custodians** (Andrew and Jamie). Store them separately and securely.
- The key is **never** on the box while the daemon is running. Mount it only for the duration
  of a retrieval, and unmount it immediately after (§2 step 5).
- The USB is also the intended home for the monthly off-box chain anchor (one custody artifact,
  not two). Handle it as a controlled item — logged in/out, physically secured.
- If a key copy is ever lost or possibly exposed, treat it as a potential breach (see the breach
  runbook) and plan a **key rotation**: new encounters seal to a new key; already-sealed
  encounters keep opening with the old key (each sealed event records which key fingerprint it
  used), so rotation is additive and does not require re-sealing the archive.

---

## 6. What the unseal records (so you can prove it later)

After a retrieval, the chain shows a `retention.unsealed` event for that encounter id, carrying:
- `reason_code` — one of the four codes from §1 (a closed enum);
- `ticket_ref` — your ticket reference (a short reference, length-capped — not free text).

To see it in context, view the encounter's full timeline:

```bash
alfred --config config.stayc-clinical.yaml scribe audit encounter <encounter_id>
```

The unseal appears in the encounter's story alongside consent, attestation, and seal. The
free-text *why* lives in `vault_audit.log`. Together they answer "who opened this audio, when,
and why" without ever putting patient content — or free text — onto the permanent chain.

---

## 7. The off-box backup — how it is turned on (operator setup, one time)

The sealed archive is backed up to a **dedicated, separate** restic repository with a 10-year
keep policy (kept apart from the general nightly backup, whose shorter policy would prune the
10-year archive). **Nothing about STAY-C backup runs from a plain install** — it is deliberately
inert until an operator turns it on. The installer stages the backup service and timer but does
**not** create the repository or start the schedule. The operator real-data-gate steps (the
installer prints these; they are done by hand, once, before real-patient use):

```bash
# 1. Provision the dedicated repo location + a credentials env-file first
#    (STAYC_RESTIC_REPO + STAYC_RESTIC_PASSWORD_FILE), and run `retention keygen` (the
#    off-box copies are sealed to the offline key, so the seal public key must exist).

# 2. Stage the units (renders + enables the TIMER; runs no backup, creates no repo):
sudo /data/algernon/stayc-clinical/.venv/bin/python -m alfred.scripts.install_stayc_backup

# 3. Create the dedicated repository, ONCE (operator — NOT done by the installer):
restic -r "$STAYC_RESTIC_REPO" init

# 4. Set a 10-year keep policy on the dedicated repo (e.g. forget --keep-yearly 10 …).

# 5. Gate the schedule live (operator):
sudo systemctl start stayc-backup.timer
```

The scheduled job runs `alfred scribe retention backup-run`, which age-seals each encounter's
transcript and note into the off-box staging copies (seal-before-backup) and then restic-backs
up the retained tree, with the biometric enrollment directory structurally excluded. You can run
it by hand with `--dry-run` to preview:

```bash
alfred --config config.stayc-clinical.yaml scribe retention backup-run --dry-run
```

## 8. Amended-note gotcha (NOTE-2 — read this before assuming a backup is current)

The **on-box** clinical note (on the LUKS-encrypted drive) is always the source of truth and is
always current. The **off-box sealed copy** of a note has one honest limitation to be aware of:

> When a note is first backed up, it is age-sealed into a staging blob named
> `<encounter_id>.note.age`. If you later **amend** that note, the backup job will **not**
> re-seal a fresh copy while that staging blob still exists — so the off-box copy can lag behind
> the amended on-box note until the `<encounter_id>.note.age` staging blob is cleared and the
> backup re-runs.

In practice this only matters in a narrow case: **box loss** *after* a note was first backed up
*and then amended* — the restored copy would be the pre-amendment version. The on-box note is
unaffected. If you amend a note that has already been backed up and you want the off-box copy
refreshed, clear its `<encounter_id>.note.age` staging blob (in the backup-staging dir) so the
next `backup-run` re-seals the current note. (Related limitation: if one encounter somehow has
more than one clinical note, only the matching one is sealed under `<encounter_id>.note.age`;
the others are not in the off-box backup. This is a documented edge case, not the normal
one-note-per-encounter path.)

---

*End of draft. Pending NSCN / PHIA / CMPA sign-off. The post-13d CLI accuracy pass is COMPLETE —
every command here was verified against the shipped 13d CLI (master `1426432`).*
