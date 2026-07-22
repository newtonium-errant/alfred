# STAY-C — Privacy Breach Notification Runbook (PHIA ss.69–70)

> ## ⚠ DRAFT — REQUIRES NSCN / PHIA / CMPA REVIEW BEFORE REAL-PATIENT USE
>
> This is a **draft procedure** for responding to a suspected or confirmed privacy breach
> of STAY-C data. The evidence tooling it references is built and tested on **synthetic data
> only**. It must be reviewed and signed off by a **PHIA privacy advisor, NSCN, and CMPA/legal
> counsel** before it is relied on for any real breach. **The determination of whether an
> incident is a notifiable breach — and who must be notified — is the custodian's (Jamie's)
> call in consultation with those advisors, not the software's.** This runbook tells you how to
> gather the evidence quickly and accurately; it does not decide the legal question for you.
>
> **Schedule / version anchors this runbook assumes**
> - PHIA s.50 retention schedule: **v1**, effective **2026-07-19**.
> - Retention window: **10 years** adult; minor **to age 19 + 10 years**, whichever is longer.

---

## 0. Who this is for

**Reader:** Jamie (custodian / NP) is the decision-maker and, under PHIA, the person who
determines notification. Andrew (operator) runs the evidence-gathering commands. In a small
clinic one person may do both; commands are written to follow start to finish with plain
explanations.

**First move on any suspected breach:** contain first (stop ongoing exposure), then gather
evidence, then decide notification with your advisor. This runbook is the evidence-and-
notification half; containment (e.g. isolating the box, rotating a lost key) comes first.

---

## 1. WHO must be notified, and WHEN (PHIA ss.69–70 — custodian determination, LEGAL)

Under PHIA, when a custodian believes on a reasonable basis that a person's health information
has been **stolen, lost, or subject to unauthorized access, use, or disclosure**, notification
duties can arise. The determination is **yours** (the custodian's), with your advisor:

| Who | When | Notes |
|---|---|---|
| The **affected individual(s)** | As soon as reasonably practicable after you become aware | The people whose information was involved. |
| The **NS Information and Privacy Commissioner (Review Officer)** | Where the breach could reasonably be expected to **cause harm** (PHIA ss.69–70) | The harm-risk assessment is the custodian's call with the advisor. |
| The **NSCN** | Per NSCN's own guidance to nurse practitioners | Consider your College's reporting expectations. |

**WHEN, in one line:** *as soon as reasonably practicable after the custodian becomes aware.*
Do not wait for a complete forensic picture to begin notification if harm is likely; document
what you know and update as the evidence firms up.

---

## 2. WHAT evidence — map each notification question to the exact query (built commands)

PHIA breach notification generally needs you to describe **what** information was involved,
**whose**, **when** it happened, **how** (extent), and **what** you have done. STAY-C's built-in
medico-legal event store answers most of these directly. The commands below **exist today**
(they are the #11 event-store and s.63 access-log queries) and are all read-only. Run them on
the box; `--config config.stayc-clinical.yaml` selects the STAY-C instance.

| ss.69–70 notification need | Exact query that answers it | What it tells you |
|---|---|---|
| **What happened to this specific encounter** (the full lifecycle) | `alfred --config config.stayc-clinical.yaml scribe audit encounter <encounter_id>` | The cross-family timeline for one encounter: consent → capture → attestation → seal → unseal → destroy, in order. The single best "what happened to this record" view. |
| **Who viewed patient records, and when** (the s.63 access log) | `alfred --config config.stayc-clinical.yaml scribe events list --stream access` | Every `access.read` — which record type/status was read, by whom, via what path, when. This is the PHIA s.63 access record. |
| **Who viewed *this particular* record** | `alfred --config config.stayc-clinical.yaml scribe events list --stream access --path <vault-relative-path>` | The path is hashed locally and matched against the stored digest, so you can ask "who viewed this note" without the path itself being in the trail. |
| **Was a sealed encounter opened, and why** | `alfred --config config.stayc-clinical.yaml scribe events list --family retention --kind retention.unsealed` | Every unseal, with its reason code and ticket reference. Unexpected unseals are a red flag. |
| **Full seal / unseal / destroy history** | `alfred --config config.stayc-clinical.yaml scribe events list --family retention` | All retention lifecycle events (sealed / unsealed / destroy_intent / destroyed / schedule_published). |
| **Prove the audit trail itself was not altered** | `alfred --config config.stayc-clinical.yaml scribe events verify` and `alfred --config config.stayc-clinical.yaml scribe events verify --stream access` | Strict chain verification. Exits clean (0) if the chain is intact; non-zero if any entry was tampered with. This is what lets you tell a regulator the evidence is trustworthy. |
| **Narrow to a time window** | add `--since <ISO ts> --until <ISO ts>` to any `events list` | Scope the access/clinical events to the incident window. |
| **Narrow to one actor** | add `--actor <name>` to any `events list` | e.g. did a specific compromised account read records. |

Practical tip: for a specific encounter start with `scribe audit encounter <id>`; for a
"whose data and who saw it" picture across the incident window use `events list --stream access
--since … --until …`. Always run the two `events verify` commands so your evidence package can
state that the logs themselves are intact.

---

## 3. The honest scope limitation (state it in your breach report)

Be truthful, in writing, about **what the access log does and does not cover**:

- The s.63 access log records **alfred-mediated reads only** — reads that went **through the
  STAY-C software** (the attest path, the vault CLI, the daemon). It is the authoritative record
  of software-mediated access.
- It does **not** capture reads that **bypass** the software — for example, someone opening a
  note directly in Obsidian, or reading a file straight off the filesystem. Those leave no
  `access.read` event.
- Therefore, absence of an `access.read` for a record is **not** proof the record was never
  read — only that it was not read *through alfred*. When assessing a breach where direct-
  filesystem or Obsidian access is possible, say so explicitly and rely on other controls
  (OS-level access, disk encryption, physical security) for that part of the picture.

State this limitation plainly in the notification. Overclaiming "our logs prove no one saw it"
is exactly the kind of assertion that does not survive scrutiny.

---

## 4. Backup posture — a stolen or leaked backup is crypto-shredded ciphertext (seal-before-backup, 2026-07-21)

If the incident involves the **off-box backups**, note the following honestly — it materially
lowers the harm assessment for the backup vector:

- STAY-C's off-box archive is backed up under the **seal-before-backup** ruling: the sealed
  audio is already encrypted, and the transcript and note copies that leave the box are
  **age-sealed to the same offline key** before backup. So the **entire off-box archive is
  uniformly encrypted** with a key that is **not on the box and not in the backup**.
- Consequently, a **stolen or leaked backup is crypto-shredded ciphertext** — it cannot be
  opened without the offline private key. On its own, loss of a backup is **not** a disclosure
  of readable PHI.
- **The important caveat:** this protection collapses **if the offline private key is also
  compromised.** If a breach involves *both* a backup copy *and* the offline key (or a key
  copy is lost/exposed), then the ciphertext becomes openable and you must assess it as a real
  disclosure. Treat any suspected key exposure as a high-severity event, plan a key rotation
  (see the retrieval runbook, §5), and factor it into the harm assessment.
- The voice-enrollment (biometric) directory is **structurally excluded** from backup, so a
  backup incident does not expose biometric voice presets.

Verified against the shipped code: the dedicated `stayc-backup` job age-seals the transcript and
note off-box before restic-backing-up the retained tree, and its backup set excludes the
enrollment directory. **One pre-commercialization caveat to flag for legal:** the dedicated repo
currently targets off-shore storage (Hetzner, Germany/Finland). Because of seal-before-backup
everything in it is age-sealed ciphertext, so this is defensible — but a **Canadian /
in-jurisdiction backup target is still to be decided** with your PHIA advisor before real-patient
go-live.

---

## 5. Assemble the notification package

With the evidence from §2 and the honest scoping from §3–§4, assemble for your advisor and the
notification:

1. **What information** was involved (record types, whether audio/transcript/note, and whether
   sealed or plaintext).
2. **Whose** information (the affected individuals — resolve the opaque encounter ids to
   patients through your own records; the encounter ids themselves are not patient names).
3. **When** the incident occurred and **when** you became aware.
4. **Extent** — how many records/individuals, drawn from the access-log and audit-timeline
   queries.
5. **Integrity of the evidence** — the results of both `events verify` runs (clean or not).
6. **Containment and remediation** already taken (isolation, key rotation, etc.).
7. **The honest scope limitation** (§3) and the **backup posture** (§4).

Then make the notification determinations in §1 with your advisor, and notify **as soon as
reasonably practicable.**

---

## 6. After the breach

- Preserve the evidence: export the off-box chain anchor
  (`alfred --config config.stayc-clinical.yaml scribe events anchor` and
  `… scribe events anchor --stream access`) so the chain state at the time of the incident is
  fixed off-box.
- If a key was exposed, complete the **key rotation** and record it.
- Record the incident, the determinations, and the notifications in your ticket / incident log,
  and retain per your records policy.

---

*End of draft. Pending NSCN / PHIA / CMPA sign-off. The post-13d CLI accuracy pass is COMPLETE —
every query here was verified against the shipped 13d CLI (master `1426432`).*
