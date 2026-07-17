# STAY-C Event Store — Synthesized Design (tasks #11 / #12 / #13, one schema)

Status: SYNTHESIZED 2026-07-16 from three judged proposals. Spine = **minimal-first** (highest aggregate: 22.5 vs 21.5 product-boundary vs 20.5 compliance-first), with judge-ratified grafts from both rivals. Every judge-flagged weakness is explicitly adopted or rejected in §13 (the adjudication ledger). All source anchors verified against HEAD `f9c9d60`; the disputed read-hook anchor was re-verified in source (see §7.1).

Operator rulings 1–5 are binding and implemented verbatim: (1) separable module, (2) access log scoped to the STAY-C clinical vault only, (3) one schema review covers all three families, (4) retention = sealed-encrypted lifecycle, (5) per-visit encounter-scoped consent, never a patient ledger.

---

## 0. Preamble — the auditor demand table (requirements derivation)

*(Grafted from compliance-first §0 — all three judges endorsed lifting this nearly verbatim as the productized module's requirements root.)*

| Statutory demand | Auditor question | Artifact that answers it | Query |
|---|---|---|---|
| PHIA s.63 (user-activity record) | "Show every access to this patient's record between X and Y. Who viewed it?" | `access.read` events, actor-attributed, encounter-scoped | `alfred scribe events list --stream access --encounter <enc>` |
| PHIA s.50 (retention schedule + documented destruction) | "Show your written retention schedule, its versions, and evidence each recording is under seal or was destroyed per schedule." | schedule artifact + `retention.schedule_published` / `retention.sealed` / two-phase destroy events | `alfred scribe events list --family retention` |
| PHIA ss.69–70 (safeguards / breach) | "How do you know this audit trail wasn't edited after the fact?" | Hash-chained streams + `verify` + printed per-attest tips + offline anchor exports | `alfred scribe events verify` |
| NS Reg s.11(3) (audit logs ≥ 1yr) | "How long do you keep the audit trail itself?" | Never-auto-pruned store; the s.50 schedule artifact carries an `audit_log` class | schedule artifact |
| Ontario AG Rec 6 (IT-enforced review-attestation) | "Prove the clinician reviewed and signed THIS version. Prove any post-signature change is visible." | `attest.recorded` pins `body_sha`; the bounded post-attest sweep emits `note.post_attest_edit_detected` | `alfred scribe audit encounter <enc>` |
| Consent (per-visit verbal, ruling 5) | "Show me consent for encounter E. Show capture stopped at withdrawal." | `consent.*` state-machine events + chunk-refusal events, chain-ordered on the clinical stream | `alfred scribe audit encounter <enc>` |
| CMPA / dispute | "Reconstruct encounter E end-to-end: consent → capture → draft → edits → signature → seal → who viewed it since." | The single-encounter cross-family timeline | `alfred scribe audit encounter <enc>` — **the demo query** |

---

## 1. Design thesis

One generic, separable, append-only JSONL module (**`alfred.evstore`**) with per-stream SHA-256 hash-chaining, flock-serialized appends, and structural PHI enforcement pushed into the module itself; plus a thin STAY-C facade (**`alfred.scribe.events`**) that owns the clinical vocabulary, identity threading, and every emission point. Two streams: `clinical` (attestation / note / encounter / consent / retention — human-action-anchored, precious, low-volume, one total order for everything with legal-ordering significance) and `access` (reads — mechanically generated, chatty, different retention character). Chain, no signing (signing reserved as protocol 2 at #13). Always-on when scribe runs — **no disable knob**. Off-box evidence via a printed chain tip at every attest plus an `anchor` export verb.

---

## 2. Module boundary (Ruling 1 — separable)

### 2.1 New package: `src/alfred/evstore/`

```
src/alfred/evstore/
  __init__.py    # exports: EventStore, Actor, AppendReceipt, VerifyReport, EventStoreError
  store.py       # EventStore: register_kind / append / query / latest / tail / tip / verify / preflight / anchor
  chain.py       # canonicalization (c14n-v1), entry hashing, chain-link + torn-tail rules
```

- **Imports: stdlib (`json`, `hashlib`, `fcntl`, `os`, `pathlib`, `datetime`, `dataclasses`) + `structlog` only. Zero `alfred.*` imports.** Pin test: AST scan asserting no `import alfred` / `from alfred` in the package. Extraction later is a `git mv` + pyproject.
- Owns: envelope + chain protocol, storage layout, durability (fsync per stream policy), flock serialization, verify, tolerant query, permissions (0700 dirs / 0600 files — the `enroll_learning._append_jsonl` umask discipline, enroll_learning.py:90–112), torn-tail recovery, anchor export.
- **Owns structural PHI enforcement** *(graft from compliance-first, judges 2+3)*: `append` refuses (fail-loud `EventStoreError`) any unregistered kind, any payload field outside the kind's registered frozenset, and any payload value that is not `str|int|float|bool|None` or a flat list thereof. The module cannot *know* PHI, but it makes undeclared free text structurally impossible — PHI-free-by-construction becomes a property of the separable product, not of STAY-C discipline. The registered field sets are the one reviewed schema (Ruling 3). **The design's own meta events comply as flat scalars** — `stream.genesis` carries `predecessor_file`/`predecessor_sha256` (§3.3) and `store.heartbeat` carries per-family `count_*` ints (§4/§5.1), never nested dicts — so this scalar rule is enforced **unweakened** (not relaxed to admit one-level dicts).
- Does NOT own: which kinds exist, family semantics, actor resolution, choke-point wiring, config-file loading, CLI registration. Those are glue.

### 2.2 STAY-C glue: `src/alfred/scribe/events.py`

- The **KINDS registry** — family → kind → payload-field frozenset + posture (durable vs best-effort) + stream routing. Registered into the store at construction. Frozen by a widening pin test (the STAYC field-set pin discipline, scope.py:1615–1616; the frozen-event-set precedent, enroll_learning.py:255–258).
- `ScribeEvents.from_config(raw, log_dir) -> ScribeEvents` — construction + activation posture (§2.4).
- Typed emitters (one function per kind — the ONLY constructors of clinical events). **There is deliberately NO generic `alfred scribe events emit` CLI, ever** *(graft from product-boundary, all three judges)*: a casual-forgery surface would gut the chain's meaning. Pin tests assert each production emitter's exact (stream, kind) pairs — the emission-authority matrix as tests, not just doc.
- Posture wrappers: `emit_durable` (raises) vs `emit_capture` (try/except + structlog `scribe.events.emit_failed` at ERROR — a dead emitter is loudly distinguishable from a quiet day).
- The `access_actor` ContextVar (identity threading, §7) and the vault read-hook closure.
- The derived attested-digest index (§7.4).

**Facade plumbing (how each emitter reaches the constructed `ScribeEvents`, no globals):**
- **attest:** `cmd_scribe` constructs the facade from `raw` and threads it into `attest()` as a new keyword arg — `attest()`'s signature gains `events=<ScribeEvents>` after the existing `enrollment_dir` kwarg (attest.py:104), passed at the `scribe_attest(...)` call site (cli.py:2677). No module global; the facade rides the same explicit-arg path as `clinician_ids`/`audit_path`.
- **ingest_web:** handlers read the facade from `request.app["scribe_events"]`, set once in `create_ingest_app` alongside the existing `app["scribe_config"]` (ingest_web.py:704) — the same accessor pattern the four handlers already use for config (`request.app["scribe_config"]`, :360/:481/:570/:624).
- **daemon:** the sweep/heartbeat emitters hold the facade on the daemon instance, constructed once at boot beside the read-hook + ContextVar registration (§8 row 14).

### 2.3 Import direction (frozen)

```
alfred.scribe.{attest,pipeline,ingest_web,daemon}  →  alfred.scribe.events  →  alfred.evstore
alfred.vault.ops  →  (nothing new; gains register_read_hook, a registration point only)
top-level alfred.cli (dispatcher)                  →  alfred.scribe.events  (clinical-config-gated registration, §7.1)
```

The vault layer never imports evstore or scribe (attest.py:21–23 invariant preserved). attest.py gains an import of `alfred.scribe.events` — scribe→scribe, which does not touch vault-agnosticism; **the attest.py docstring (:21–23) is amended in the same commit** *(graft from product-boundary, judge 2)*.

### 2.4 Config surface + activation

Sub-block under `scribe:` — no `SOVEREIGN_ALLOWED_SECTIONS` change (scribe already allowlisted, boundary.py:206); every field is filesystem-only, so the sub-field loopback discipline (scribe/config.py:23–31) imposes no new barrier:

```yaml
scribe:
  events:
    dir: ""     # override; empty = <logging.dir>/events (caller-derivation precedent, cli.py:2670-2674)
```

**That is the whole surface. There is no `enabled` knob** *(graft from product-boundary; judges 1+2 MUST-graft, judge 3 concurring)*: an evidence store that can be configured off is not evidence, and a reachable `false` in clinical mode is a med-legal footgun. Activation:

- **Clinical mode: required, fail-loud AT OPEN** — daemon boot refuses (sovereign-gate posture, daemon.py:66–74; ordinary restartable exit, not 78/79 — likely a perms problem); the attest CLI constructs the store before any vault read and refuses the attest if construction fails (matches empty-clinicians fail-closed). This resolves judge 3's "fail-loud at first append discovers misconfig mid-attest" — failure surfaces at open, never mid-operation.
- **Non-clinical modes: active by default** (dev exercises the store — a feature), but open failure degrades to disabled + structlog ERROR (fail-open-loud). One `scribe.events.degraded` line per lifecycle (intentionally-left-blank).

`ScribeEventsConfig` dataclass in `scribe/config.py`, hand-rolled `_build_events` with the `__dataclass_fields__` tolerance filter (the file already avoids the shared `_DATACLASS_MAP` `_build` — keep that, config.py:8–15). Test fixtures must include required scribe fields (the `_build` empty-dict trap, CLAUDE.md).

**Deployment siting:** default resolves to `/data/algernon/stayc-clinical/data/events/` — under `<STAYC_DATA>`, already inside the systemd unit's `ReadWritePaths` (template :43). This deliberately avoids the `enrollment_dir` sibling-of-data EROFS trap (config example :133). No installer/template change. Doc warning: an operator `dir` override must stay under a `ReadWritePaths` root; the fail-loud open catches violations at boot.

---

## 3. Storage format

### 3.1 Layout

```
<events_dir>/                       0700
  clinical.jsonl        # families: attestation, note, encounter, consent, retention, meta — ONE total order
  clinical.lock         # flock target
  access.jsonl          # family: access (+ its meta)
  access.lock
  attested_digests.json # derived operational index (§7.4) — rebuildable from clinical.jsonl
  anchors/              # anchor exports (§4)
```

Files 0600. **No head.json cache** *(judge 1 graft #8, judge 2 weakness — REJECTED from spine)*: the tip is resolved by tail-reading the last ≤8KB block under flock and taking the final complete line — the log is the sole source of truth, no cache-recovery invariant to build or test. **No rotation/segments in v1** *(product-boundary's monthly segments REJECTED, judges 1+2)*: volumes are tens of rows/day → tens of MB per decade; no sink in the repo rotates. Sealed-segment rollover is **reserved, not built**: a future segment's genesis pins the predecessor via **flat scalar fields** — `predecessor_file`/`predecessor_sha256` (already in the v1 genesis frozenset, §3.3) plus `predecessor_head_seq`/`predecessor_head_sha` added at the segment boundary (additive, no nested dict — the §2.1 scalar rule holds) — because `seq`/`prev` ship from row 1, rollover is a pure additive change later (the append-only-migration lesson, enroll_learning.py:17–63).

**Two streams, why** *(spine decision, judges 1+2; judge 3's single-file preference resolved)*: the precious clinical chain never contends with (or shares corruption blast-radius with) chatty read logging; and everything with **legal-ordering significance** — consent → capture → attest → seal — lives on the ONE clinical chain, so "consent preceded capture preceded attestation" is proven by chain seq, not timestamp comparison. Only reads sit on the second chain; read-vs-mutation ordering by timestamp is acceptable because reads never gate legality. The cross-stream encounter timeline is materialized by `audit encounter` (§8), merged by `ts`, tiebroken by `(stream, seq)`.

### 3.2 Envelope (shared across all families — the ONE schema, Ruling 3)

One JSONL line per event:

```json
{
  "v": 1,
  "seq": 42,
  "ts": "2026-07-16T14:03:22.123456+00:00",
  "stream": "clinical",
  "family": "attestation",
  "kind": "attest.recorded",
  "subject_id": "enc-ab12cd34ef56aa77",
  "actor": "jdoe",
  "actor_kind": "clinician",
  "payload": { },
  "prev": "<64-hex sha256 of previous entry, or 0*64 for genesis>",
  "entry_sha": "<64-hex>"
}
```

- `v` — envelope protocol int (close_manifest discipline: strict int, bool-is-int excluded, close_manifest.py:98–99; unknown `v` → row renders opaque but still chain-verifies, §9).
- `seq` — per-stream monotonic from 1. A gap is deletion evidence.
- `ts` — UTC isoformat (repo convention); injectable clock for tests (the attest.py `now` pattern, :148).
- `subject_id` — generic name in the separable module; STAY-C always passes the encounter id (`compute_encounter_id` output == the note's `source_id`; identity.py:46–60). `""` when no encounter applies (heartbeats, schedule events, genesis).
- `actor` / `actor_kind` — clinician slug (config-validated, config.py:423–456) / `"stayc_scribe"` / `"operator"` / `""`; `actor_kind ∈ {clinician, pipeline, operator, system, unknown}`.
- `payload` — family-specific, allowlisted per kind, scalar-enforced by the store (§2.1).
- `prev` / `entry_sha` — §4.

### 3.3 Genesis

First entry of every stream: `kind: "stream.genesis"`, `family: "meta"`, payload `{store_protocol: 1, canonicalization: "c14n-v1", predecessor_file, predecessor_sha256}` — **flat scalar fields, no nested dict** (§2.1 rule). For the `clinical` stream at cutover, `predecessor_file = "clinical_attest_audit.jsonl"` and `predecessor_sha256` pins that legacy file's sha256 as it exists at genesis; both are `""` for a stream with no predecessor — **pin, don't launder** *(product-boundary's argument, adopted by all judges; compliance-first's backfill importer REJECTED — retro-chaining unverifiable legacy rows muddies chain provenance)*. No row import, ever.

---

## 4. Tamper-evidence decision

**Decision: per-stream SHA-256 hash chain + fsync on durable appends + per-attest printed tip + `anchor` export verb + daily clinical heartbeat + strict `verify` CLI. NO entry signing and NO HMAC key in v1 — signing is reserved as store protocol 2 at #13, when the offline seal-keypair custody ceremony exists.**

Mechanism:
- `canonical_json(obj) = json.dumps(obj, sort_keys=True, separators=(",",":"), ensure_ascii=True)` — frozen as **c14n-v1**, pinned by a golden-vector test. A canonicalization change is a store-protocol bump: the verifier's rules are themselves consumer-fields-from-day-one.
- `entry_sha = sha256(prev + "\n" + canonical_json(entry_without_entry_sha))`; genesis `prev = "0"*64`.
- **Durable appends fsync** (file, + dir on first create). Best-effort appends don't. (No existing sink fsyncs; the medico-legal rows justify it here.)
- Every append structlogs `scribe.events.appended {stream, seq, entry_sha}` — a cheap secondary trail in scribe.log.

**Off-box anchoring** *(grafts from product-boundary + compliance-first; closes the spine's biggest judge-flagged weakness — "every verification anchor lives on the box")*:
1. **The attest CLI prints the clinical-stream tip after every attestation** — `chain tip: seq=N sha=…` alongside the existing "Attested:" output (cli.py:2694–2700). ~5 lines; any offline record of a prior tip (clinician's notebook, clinic paper log) makes a full-file re-chain detectable.
2. **`alfred scribe events anchor`** exports `{stream, head_seq, head_sha, ts, store_protocol}` to `anchors/anchor-<ts>.json` and prints it — intended custody: the same offline USB that will hold the #13 seal private key. No reminder config knob; instead `events verify` output includes `days_since_last_anchor` (zero-config ILB nag).

**Daily heartbeat** *(graft from product-boundary, scoped per judge 2; judge 1's rejection resolved by scoping)*: the daemon appends one `store.heartbeat` to the **clinical stream only**, when >24h since the last (tail-region check at sweep) — payload = **flat per-family counts** since the last heartbeat: `{count_attestation, count_note, count_encounter, count_consent, count_retention}`, all ints, explicit-zero (no nested dict — §2.1 rule; the heartbeat row's own existence is the liveness signal, so it does not self-count `meta`). One row/day bounds the tamper window and makes "no events today" provable rather than ambiguous — intentionally-left-blank applied to the med-legal trail itself. The access stream's daily `access.system_reads_summary` (§7.3) doubles as its liveness signal; no second heartbeat.

Why chaining earns its ~60 lines: it converts "silently editable append file" (the verified state of all four existing sinks — clinical_attest_audit.jsonl has literally no reader today) into "any in-place edit, deletion, or reorder breaks every downstream hash in O(n) verify" — precisely the AG-Rec-6 med-legal differentiator and the productized module's headline property. Why NOT signing now: an on-box key adds zero protection (key sits beside the file); an offline key breaks always-available appends; #13's seal-keypair ceremony is the natural moment — protocol 2 adds a per-entry signature field computed **over `entry_sha`**, layered without re-chaining. *(Compliance-first's mutual frontmatter pin REJECTED — judges 1+2: it adds a second privileged post-triad write, widens the frozen `STAYC_CLINICAL_ATTEST_FIELDS` pin (scope.py:1628), and creates a partial-failure window on the platform's most protected path; the tip/anchor combo buys comparable rewrite-resistance at a fraction of the risk. Revisit at #13 alongside protocol 2.)*

**Threat-model honesty (goes in the module docstring — productization credibility, per judge 3 repair #9):** tamper-EVIDENT, not tamper-PROOF. On-box artifacts (chain, scribe.log echo, vault git snapshots) raise the consistency bar; **the printed tips and anchor exports are the only genuinely off-box artifacts**, and a root attacker who rewrites everything consistently is defeated only by them. Say exactly that; never overclaim.

**Concurrency (chaining forces it — no existing sink handles multi-process):** daemon and attest CLI append concurrently. Per-stream protocol: `flock(<stream>.lock, LOCK_EX)` → tail-read tip → build entry (`prev`, `seq`) → append line (+fsync if durable) → unlock. POSIX flock on local ext4 is sufficient. Pin test: two processes × 100 appends → verify ok, 200 entries, no seq gaps.

**Torn-tail / sealed-fragment rule (crash mid-append):** never truncate, never rewrite. If the file doesn't end in `\n` at append time: write a bare `\n` (the fragment becomes one non-parsing line, skipped by tolerant readers — enroll_learning.py:233–236) and chain the new entry to the last VALID entry's sha. Tip resolution (`_last_valid`) requires `entry_sha`+`prev`+`seq` AND **recomputes the candidate tip's sha before chaining onto it** — a forged tail row with a plausible `seq` cannot poison the tip and turn every future legitimate append into a verify-fail (an unrecoverable in-band integrity DoS the never-truncate rule would otherwise create). **Continuity semantics:** a torn/sealed fragment ANYWHERE is skipped; any resulting continuity break (recomputed sha / `prev` / `seq`) FAILS (`first_bad_seq`); skipped fragments are **COUNTED** — the FINAL one as `torn_tail` (crash artifact), every non-final one as `sealed_fragments` (a sealed crash fragment OR smuggled non-entry bytes the chain links across). A nonzero `sealed_fragments` is **pass-with-warning, not a silent pass**: the evidence file carries lines no chain row attests to (the operator eyeballs the raw JSONL). The literal "any non-final non-entry line FAILS" reading is REJECTED — it would make every store that survived one legitimate crash fail verify forever (the sealed fragment is permanent), forcing operators to truncate evidence to get green — the exact anti-spoliation outcome §4 forbids.

**Predicate alignment (H1 — mandatory).** The tolerant readers (`_iter_entries`, feeding `query`/`latest`/`tail`/`audit_encounter`/`rebuild_index`; and `_last_valid`, tip resolution) MUST require the SAME entry predicate as `verify` — `entry_sha` AND `prev` AND `seq`. If they are weaker, a schema-partial forged line (e.g. `{entry_sha, kind: attest.recorded, payload: {body_sha: EVIL, free_text: …}}` with no `prev`/`seq`) is **query-servable as evidence and pinnable into the attested-digest index while `verify` stays green forever** — tamper-evidence defeated on the exact surface auditors consume (`events list` / `audit encounter` / `--rebuild-index`). With alignment, any query-visible row is necessarily chain-covered: a forgery cannot be query-servable yet verify-invisible. Note field/scalar enforcement is append-time only, so the aligned read predicate is the sole guard against a directly-garbaged line reaching the query surface. **Pin: a query-visible forgery MUST break verify (mutation-bind both readers).**

**Verify semantics:** linear scan; recompute every `entry_sha`; check `prev` linkage + seq continuity; report `VerifyReport{ok, entries, head_seq, head_sha, first_bad_seq|None, torn_tail, sealed_fragments, days_since_last_anchor}`. **On success only, append a `store.verified {ok: true, entries}` meta event** *(judge 2/3 graft, judge 1's objection resolved)*: a success append EXTENDS the chain after examination completes — it never modifies what was audited — and makes "when did you last verify" chain-answerable; on failure nothing is appended (a broken chain must not be extended) — result goes to exit code + structlog + morning review.

---

## 5. Event families — kinds + payloads (the ONE schema review, Ruling 3)

Payload values: ids/enums/ints/bools/digests only, store-enforced scalar types; exceptions marked FLAGGED. **[D]** = durable (fsync, raise on failure); others best-effort. Stream `clinical` unless noted.

### 5.1 META family

| kind | emitted at | payload |
|---|---|---|
| `stream.genesis` **[D]** | first open per stream | `{store_protocol, canonicalization, predecessor_file, predecessor_sha256}` (§3.3) |
| `store.heartbeat` | daemon sweep, >24h latch, clinical stream only | `{count_attestation, count_note, count_encounter, count_consent, count_retention}` — flat per-family ints, explicit-zero |
| `store.verified` | verify CLI, success only | `{ok: true, entries: int}` |

All three META payloads are flat scalars (no nested dicts) — they pass the store's own §2.1 field/scalar enforcement, which would otherwise fail-loud on its own genesis at first open. The `store.heartbeat` `count_*` field set is frozen by the widening pin; a new clinical family adds exactly one `count_<family>` field in the same reviewed schema change (Ruling 3).

### 5.2 ATTESTATION family (#11)

| kind | emitted at | payload |
|---|---|---|
| `attest.recorded` **[D]** | attest.py, the existing `_append_attest_audit` slot (:267–284) | `{from_status, to_status, creator, forced: bool, completeness: enum(complete\|incomplete\|absent), body_sha, grounding_flag_count: int, grounding_reasons: [enum ids]}` — `actor` = attester. **`body_sha` is the attested-version pin**: `_body_sha(rec2 body)` is already computed for the CAS bracket (attest.py:47–50, :194) — one field from computed to persisted, exactly as the brief says. `grounding_reasons` = the `reason` enum of each flag ONLY; the `claim` free text is PHI and never leaves frontmatter. Satisfies "grounding-flag state visible at attest time." |
| `attest.refused` | attest.py, wrapping `authorize_attestation` + the CAS-refusal raises (:196–207) — emit best-effort, then re-raise | `{reason: greppable id (incl. new "event_store_unavailable"), from_status, to_status, completeness, forced}` — best-effort by design: a store failure must never mask the refusal itself. |

**Store preflight** *(new — synthesized resolution of compliance-first's `attest.intent`, which is REJECTED per judges 1+2: it doubled durable appends and coupled pre-CAS attest to store health via a heavyweight event)*: `attest()` calls `events.preflight()` (open + flock acquire/release + tip resolution, NO append) **before its first vault_read**; failure → `AttestationError("event_store_unavailable")`, fail-closed. This makes attested-note-without-trail near-impossible (judge 3's underlying concern) without new events or CAS-window impact — the preflight sits entirely before the bracket.

### 5.3 NOTE family (#11) — all best-effort, emitted by pipeline/daemon

| kind | emitted at (pipeline.py) | payload |
|---|---|---|
| `note.draft_created` | after initial vault_create succeeds | `{body_sha}` |
| `note.draft_regenerated` | inside `_update_or_refuse_ai_draft`, after the single atomic body_replace + clear-on-regen vault_edit succeeds (:440–452) | `{body_sha, marker: "regressed", grounding_flag_count}` |
| `note.ready` | READY finalize, after successful marker stamp + `state.set(READY)` (:1208–1228) | `{body_sha, expected_final_seq, folded_through}` |
| `note.human_edit_detected` | clobber-detect (:1004–1011), on the transition into `STATE_HUMAN_EDITED` (state latch — no per-sweep spam) | `{body_sha_before, body_sha_after}` — **observed, not intercepted**: out-of-band editor/Obsidian edits surface only at the next 30s sweep via sha mismatch; the event records detection time, not edit time. Stated as a design fact. |
| `note.post_attest_audio` | :991–1001 and :1050–1058 refuse branches, on transition into `STATE_POST_ATTEST_AUDIO` | `{}` |
| `note.marker_selfheal` | maybe_restamp (:1234–1243) | `{}` |
| `note.post_attest_edit_detected` | **NEW bounded sweep check** (below) | `{attested_body_sha, current_body_sha}` |

**The post-attest-edit mechanism — load-bearing, cross-proposal correction (all three judges):** the existing clobber-detect CANNOT emit this event — its attested/amended branch short-circuits to post_attest_audio BEFORE the sha compare, and the whole path only runs when new audio arrives (pipeline.py:978–1011). Any builder wiring "reuse the detector by status" ships an event that never fires for the silent-edit case. The only working mechanism is index-driven: each sweep compares the current note body sha against the **pinned attested `body_sha`** from the attested-digest index (§7.4). On mismatch: emit + loud structlog + surface in `events list` and morning review. **Detection only — NO auto status mutation** (anti-spoliation; the sanctioned supersede path is `amended`). This is "post-attest edit visibly re-opens review"; pre-attest re-open is already handled by clear-on-regen at the choke.

**Bounding (judge 1 builder-required fix #9 — adopted):** the per-sweep check covers only the hot window — encounters attested within the last 30 days OR whose note file mtime is newer than the last check; a full-index comparison runs at daemon boot and via `events verify --deep`. Placement discipline: this is a VAULT-STATE observation → above any early-return in the sweep (the surveyor lesson, CLAUDE.md), latched per `(encounter, current_sha)`.

Note edit history overall = `note.draft_regenerated` + `note.human_edit_detected` + `attest.recorded(body_sha)` digests + the existing `draft_original` frontmatter retain-the-diff (pipeline.py:367–372, frozen at attest so final-vs-draft_original shows exactly the clinician's edit, scope.py:1636–1644). Rollback capability stays where it is; the store supplies the who/what/when trail around it.

### 5.4 ENCOUNTER family (#11) — best-effort, emitted by ingest_web

| kind | emitted at (ingest_web.py) | payload |
|---|---|---|
| `encounter.opened` | first chunk accepted (seq==1, after atomic chunk+meta write, :450–452) | `{}` |
| `encounter.closed` | **TWO seal paths, both after the `_CLOSED` manifest atomic write — BOTH must emit:** (a) `_handle_close` (:469); (b) the close-flag on the final chunk → `write_close_manifest` inside `_handle_ingest_chunk` (:456–460). A close-flag seal with no event would leave a capture-but-no-`closed` audit timeline (an intentionally-left-blank violation on the CMPA demo query). | `{final_seq}` |
| `encounter.cap_hit` | three cap sites: chunks :426–428, chunk_bytes :431–435, encounter_bytes :441–444 | `{cap: enum(chunk_bytes\|chunks\|encounter_bytes)}` |
| `encounter.post_close_chunk_refused` | :404 | `{seq}` |

### 5.5 ACCESS family (#11) — stream `access` — PHIA s.63 (Ruling 2)

| kind | payload |
|---|---|
| `access.read` | `{record_type, status, path_digest, via: enum(attest\|cli\|daemon\|pwa_view)}` — `subject_id` from frontmatter `source_id` when present; `path_digest` = sha256(rel_path): join/dedup without carrying the potentially-PHI-bearing path/title (attest.py:223–228 precedent). |
| `access.system_reads_summary` | `{count: int, window_start}` — one latched row per UTC day *(graft from compliance-first, all three judges)*: suppressed pipeline self-reads are COUNTED and the count is chained, so "hook alive, zero human views" is provable from the trail itself, not just from scribe.log. |

Mechanism, identity, scoping, performance: §7.

### 5.6 CONSENT family (#12 — contract specified now, built later; Ruling 5)

Encounter-scoped state machine, **never a patient ledger**: `subject_id` = encounter id only; no patient name/DOB/HCN field exists anywhere in the schema, and the KINDS allowlist + store-level field enforcement structurally prevent one being added without tripping the widening pin.

| kind | payload | contract |
|---|---|---|
| `consent.confirmed` **[D]** | `{method: "verbal", captured_by}` | Emitted by the PWA consent route (new POST in ingest_web, #12), durable BEFORE the encounter proceeds. Feeds the auto-inserted note consent line: notegen consumes `latest(subject_id, family="consent")`. **`captured_by` identity is an explicit CONTRACT (open Q4):** today's ingest_web auth is a single shared page-embedded bearer token — no per-clinician identity per request — so `captured_by` degrades to `"shared-session"` by default (honest for the single-clinician clinic). Attributing consent to a named clinician requires a per-clinician PWA session identity (Q4 option b), deferred until multi-clinician use arrives. Do NOT silently assume a per-clinician session exists. |
| `consent.declined` **[D]** | `{method: "verbal", captured_by}` | Capture must not open; a chunk POST for a declined encounter → refuse + `consent.violation_refused` (best-effort, `{seq}`). `captured_by` per the same Q4 identity contract (defaults to `"shared-session"`). |
| `consent.withdrawn` **[D]** | `{at_seq: int}` | **Ordering contract: the durable append MUST succeed before the capture-stop is acknowledged to the PWA**; every subsequent chunk POST is refused, each emitting `consent.violation_refused {seq}`. Withdrawal-stops-capture is the enforcement (#12 builds it); the event is its evidence. |

Legality (`∅→confirmed|declined`; `confirmed→withdrawn`; terminal states) enforced by the FACADE at emit time via `latest()` — mirrors the forward-only attestation lifecycle (attestation.py:67–70). The queryable per-visit consent registry — the greenfield differentiator no vendor ships — is exactly `events list --family consent`.

### 5.7 RETENTION family (#13 — contract specified now; Ruling 4)

Retention = SEAL lifecycle, not deletion. Audio today persists plaintext forever (verified: zero unlink in pipeline/ingest/ledger) — #13 builds sealing against these contracts:

| kind | payload | contract |
|---|---|---|
| `retention.schedule_published` **[D]** | `{schedule_version: int, schedule_sha256, effective_date}` | The s.50 schedule is a versioned artifact (`<events_dir>/retention_schedule_v<N>.json`, atomic temp→replace write, close_manifest.py:49–54; fail-closed read). It carries classes for encounter audio (~10yr, exact number operator-set per Ruling 4) AND an `audit_log` class governing the store itself (≥1yr Reg s.11(3) floor trivially exceeded — never auto-pruned in v1). |
| `retention.sealed` **[D]** | `{chunk_count, total_bytes, manifest_sha256, sealed_to_key_fp, cipher}` | `manifest_sha256` = digest over the sorted per-chunk `{seq, sha256, bytes}` list (per-chunk shas already computed, pipeline.py:551). **Ordering: encrypt → self-verify blob digest (the enrollment.py:461–469 write-then-verify discipline) → durable event → only then delete plaintext.** |
| `retention.unsealed` **[D]** | `{reason_code: enum(dispute\|audit\|rediarize\|clinical_review), ticket_ref}` — `ticket_ref` FLAGGED (opaque operator string, length-capped) | Offline-key dispute lookups; free-text justification routes to vault_audit.log (two-trail, #58-D2). |
| `retention.destroy_intent` **[D]** / `retention.destroyed` **[D]** | `{schedule_version, manifest_sha256}` | **Two-phase** (spine element, judge 3 graft #6): intent → unlink → destroyed — a crash can never produce unlogged destruction. |

---

## 6. Emission postures + the scope-first emission matrix

### 6.1 Two postures (extends the ratified #58-D2 two-trail / two-posture precedent)

- **DURABLE (raise, fsync):** `attest.recorded`, all `consent.*` state events, all `retention.*`, `stream.genesis`. These ARE the med-legal record. For attest this is posture-parity with today: `_append_attest_audit` already sits post-triad and unwrapped (attest.py:78–88) — the CAS window is not widened because the slot is the same.
- **BEST-EFFORT (swallow + structlog ERROR):** `note.*`, `encounter.*`, `access.*`, `attest.refused`, `consent.violation_refused`, `store.heartbeat`, `store.verified` (success-only; a swallowed self-append must never crash the verify CLI — the verify RESULT itself always reaches exit code + structlog regardless). The pipeline/ingest must never break on observability (the enroll_learning "capture must NEVER affect the pipeline" rule, :182–184).

### 6.2 Emission matrix (the principal artifact — BEFORE the tool surface, per scope-first)

| Emitter (process) | May emit | Enforced by |
|---|---|---|
| `scribe/attest.py` (attest CLI) | `attest.recorded`, `attest.refused`; `access.read` via hook | typed emitters + per-emitter pin tests |
| `scribe/pipeline.py` (daemon) | `note.*` | " |
| `scribe/ingest_web.py` (daemon) | `encounter.*`; #12 adds `consent.*`, `consent.violation_refused` | " |
| scribe daemon sweep | `store.heartbeat`, `access.system_reads_summary`, `note.post_attest_edit_detected` | " |
| vault read hook (STAY-C processes only) | `access.read` | registration gating §7.2 |
| verify CLI | `store.verified` (success only) | " |
| #13 seal tooling | `retention.*` | " |
| everything else in the platform | NOTHING (module never imported outside scribe + the gated dispatcher registration) | import direction §2.3 + pins |

No scope.py changes anywhere in this arc — the store is not a vault surface.

---

## 7. Access-log capture (PHIA s.63) — mechanism, identity, performance

### 7.1 Mechanism

1. `ops.py` gains `register_read_hook(hook)` + a `_READ_HOOKS` fire loop at the end of `vault_read` (:754–763), each hook try/except-swallowed (a hook failure can never fail a read). **Exact shape of the existing precedent — corrected anchors (re-verified in source this session; the recon's :1655–1678 cite is the vault_edit *fire site*):** `register_event_update_hook` defined at **ops.py:99–115**, `_fire_update_hooks` at **:185–210**, fired from vault_edit at **:1660–1672**. `vault_read`'s signature is untouched — no identity param (avoids the hardcoded-scope trap class, CLAUDE.md).
2. **Registration = scoping (Ruling 2, structural):** only STAY-C entry points register — (a) scribe daemon boot (beside guard install, daemon.py:76–80); (b) `cmd_scribe attest` (beside boundary arming, cli.py:2660); (c) *(graft from product-boundary/compliance-first, judges 1+2 — closes the spine's s.63 gap)* the **top-level cli.py dispatcher** (`cmd_vault`, cli.py:1047) registers when the **loaded config identifies a STAY-C clinical instance** — the `scribe:` block is present AND `load_scribe_config(raw).mode == "clinical"` (the fail-closed clinical-mode signal that normalizes to exactly `"clinical"`, scribe/config.py:17–21/50/239), resolved from the `raw` unified config already loaded at cli.py:1051–1053; the closure additionally pins the configured STAY-C vault path (`raw["vault"]["path"]`) before emitting. **Gate on config identity, NOT the env-derived scope:** `cmd_vault` reads `ALFRED_VAULT_SCOPE` only at cli.py:1110, and that resolves to `None` for an interactive `alfred --config config.stayc-clinical.yaml vault read <note>` (only agent backends / `alfred exec` set the env var) — precisely the human PI-viewing read PHIA s.63 targets. A `scope.startswith("stayc_")` gate would leave that read unlogged; the config-identity gate catches it. Registration stays at the dispatcher layer, NOT inside vault/cli.py, preserving vault-never-imports-scribe. Platform (non-clinical) instances never register.
3. **Identity threading:** `contextvars.ContextVar("access_actor")` in `scribe/events.py`. Attest CLI sets `(args.attester, "clinician")` before `attest()` — the CAS-bracket reads are attributed to the attesting clinician, `via: "attest"`. Daemon sets `("stayc_scribe", "pipeline")`. Dispatcher path sets `("operator", "operator")` (no OS auth exists — honest attribution; open question Q3). #12 PWA view routes (none exist today — verified) emit `access.read` with `via: "pwa_view"` — **contract: every PWA view route MUST emit `access.read`**; that is where real s.63 volume arrives, alfred-mediated by construction. **The `actor` identity is the Q4 open contract, not an assumed per-clinician session:** the PWA carries a single shared page-embedded bearer token, so the actor degrades to `"shared-session"` by default until a per-clinician PWA session identity is added (Q4 option b).
4. **Pipeline self-read suppression, counted:** `actor_kind == "pipeline"` reads are not written per-event (the sweep vault_reads every encounter every 30s — daemon.py:48 — and s.63 concerns persons viewing PI, not the system operating); they are COUNTED, and the daily `access.system_reads_summary` row makes the suppression itself auditable in the chain (§5.5).

### 7.2 Coverage boundary — stated honestly

Alfred-mediated reads only. Obsidian/direct-filesystem reads bypass `vault_read` entirely (scope.py:118–119 acknowledges operator fs access as out-of-gate); out-of-band EDITS are sha-detected at the next sweep, not intercepted. Compensating controls: the hardened single-purpose unit (ProtectSystem=strict, UMask=0077), and the #12 PWA view surface becoming the canonical human read path. `vault_search/list/context` also read files; Phase 1 logs `vault_read` only — `access.search {pattern_digest, hits}` is specified as a Phase-1.5 contract (patterns are operator-typed and PHI-risky → digest only). An OS-level watcher (auditd/fanotify) is a productized-module roadmap note, not an #11 item. This limitation goes verbatim in the module docstring — every commercial competitor has it worse.

### 7.3 Performance posture

With suppression, access volume ≈ actual human views (tens/day; PWA-era maybe hundreds): one flock + non-fsync append each, on a stream that never contends with the clinical chain — negligible against a 30s sweep. The hook adds one sha256 of a short path string per read even when suppressed — immeasurable. No batching, no async queue: synchronous append keeps evidence ordering exact. Revisit only past ~10 sustained appends/sec (multi-clinician product scale; the reserved segment rollover is the pre-planned answer).

### 7.4 Attested-digest index

`<events_dir>/attested_digests.json` — `{subject_id: {body_sha, attested_at, seq, rel_path}}`, maintained by the facade on each `attest.recorded`, atomic-replace **under the clinical-chain flock** — the SAME `clinical.lock` already held for the `attest.recorded` append is held across the index update too (the critical section extends to cover the index write before the lock releases). Without this, two concurrent attest CLIs race the atomic-replace last-writer-wins and one encounter's freshly-pinned `body_sha` is dropped until the next boot/`--deep` rebuild — a silent post-attest-edit-detection gap in the interim. **Rebuildable from clinical.jsonl** (`events verify --rebuild-index`). `rel_path` lives ONLY here (operational file in the same PHI trust-zone as the vault) — never in a chained stream. Consumers: the bounded post-attest sweep (§5.3). Judge-flagged "third derived file" concern accepted: it is load-bearing (the only working post-attest-edit mechanism) and the head caches were dropped, so derived-state count is net-neutral.

---

## 8. Integration map (choke point → event → ordering vs existing invariants)

| # | Choke point | Event(s) | Ordering / invariant compliance |
|---|---|---|---|
| 1 | attest CLI start (cmd_scribe, cli.py:2638–2666, beside boundary arming) | store construction (fail-loud) + `preflight()` | Before any vault read; failure = refused attest (`event_store_unavailable`). CAS window untouched. |
| 2 | attest.py `_append_attest_audit` slot (:267–284) | `attest.recorded` **[D]** | AFTER the triad write (:212–221), BEFORE the two fail-silent captures (:300–328) — occupies the existing audit slot with identical fail-loud posture; invariant (c) preserved exactly. **Dual-write:** the legacy `clinical_attest_audit.jsonl` line is written FIRST (independent trail survives any new-store bug), then the durable event. Legacy marked deprecated; retirement per open Q2. CLI then prints the chain tip (§4). |
| 3 | attest.py authorize + CAS refusal raises | `attest.refused` (best-effort) | Emit-then-re-raise; never masks the refusal. attestation.py stays pure (no store import). |
| 4 | pipeline regen choke (:440–452) | `note.draft_regenerated` | After the single atomic body_replace + marker-clear vault_edit SUCCEEDS — records committed reality; rides outside the choke's scope gates (no scope.py change). |
| 5 | clobber-detect (:1004–1011) | `note.human_edit_detected` | On transition into `STATE_HUMAN_EDITED`, after state persists. |
| 6 | READY finalize (:1208–1228) | `note.ready` | After stamp + `state.set(READY)` succeed (stamp-failure → stay DRAFTED → no event). |
| 7 | post-attest-audio refuses (:991, :1050) + self-heal (:1234) | `note.post_attest_audio`, `note.marker_selfheal` | At the existing structlog ILB sites, transition-latched. |
| 8 | NEW bounded sweep check | `note.post_attest_edit_detected` | §5.3: index-driven, hot-window bounded, above-early-return, latched per (encounter, sha). The ONLY working mechanism — do not wire the clobber detector for this. |
| 9 | daemon sweep | `store.heartbeat`, daily `access.system_reads_summary` | >24h / UTC-day latches; capture-posture. |
| 10 | ingest_web chunk/close/cap/refuse (:352 chunk; :456–460 close-flag seal **+** :469 `_handle_close` — BOTH emit `encounter.closed`; :426–444 caps; :409 post-close refuse) | `encounter.*` | After the atomic chunk/manifest writes; HTTP responses never blocked by the store. |
| 11 | ops.py `vault_read` (:754–763, new hook) | `access.read` | Post-parse, fail-isolated; registration gating §7.1–7.2. |
| 12 | #12 PWA consent routes (to build) | `consent.*` **[D]** | Durable-append-before-acknowledge; withdrawal ordering §5.6; consent-append failure → 5xx and capture must not start. |
| 13 | #13 seal lifecycle (to build) | `retention.*` **[D]** | Seal: encrypt → verify blob → durable event → delete plaintext. Destroy: two-phase. |
| 14 | scribe daemon boot | store construction (clinical: refuse boot on failure), genesis on first use, hook + contextvar registration | Beside sovereign re-validation (daemon.py:66–80). |
| 15 | `events`/`audit` CLI verbs | `store.verified` on verify success | Do NOT arm the sovereign guard: **no PHI-vault write and no egress surface** — the `store.verified` append is a local append-only meta row, not a vault mutation or a network call, so arming buys nothing. (Not "read-only": `events verify` DOES append `store.verified` on success; the correct rationale is the absence of a vault-write / egress surface, not read-only-ness.) Rationale recorded so the "mirror attest arming" rule isn't diluted — compliance-first's blanket arming rejected as friction without a threat. |

Invariant checklist for builder + reviewer: triad write remains the only attest mutation; nothing new inside the CAS bracket; post-triad additions fail-silent except the durable trail (which was always fail-loud); attest trail stays PHI-free by construction; free-text override_reason routes to vault_audit.log only (#58-D2 unchanged); empty clinicians fail-closed; marker freeze-at-attest untouched; vault never imports scribe/evstore; zero scope.py changes.

---

## 9. Versioning / migration story

- **Three version axes:** envelope `v` (row), `store_protocol` + `canonicalization` (stream genesis). All strict ints (bool excluded).
- **Additive-only within a version:** new payload fields and new kinds are additive; readers use per-line tolerance + known-field filtering (the platform load-time schema-tolerance contract). **Chain verification is version-agnostic by design** — it recomputes over the raw canonical entry minus `entry_sha`, needing no semantic understanding; unknown-`v` rows render opaque but still verify.
- **Consumer-fields-from-day-one (the 5b match_rate lesson):** `body_sha` (dispute + post-attest sweep), `grounding_reasons`+count (AG Rec 6 evidence), `completeness`/`forced`/`creator` (provenance), `seq`/`prev`/`entry_sha` (verifier), `actor_kind` (suppression audit), `at_seq` (consent-withdrawal audio boundary), `schedule_version`/`manifest_sha256` (retention), `store_protocol`+`canonicalization`+`predecessor_file`+`predecessor_sha256` (genesis upgrade/rollover path) — all ship in v1 even where the first consumer arrives at #12/#13.
- **Store protocol 2 (reserved, #13):** per-entry signature over `entry_sha` using the seal keypair; declared at a genesis/segment boundary; old entries verify under protocol 1. No re-chaining ever. *(chain_mac_key config now REJECTED, judges 1+2: a dead key-custody knob today; the protocol-2 reservation covers the upgrade with zero present cost.)*
- **Rollover (reserved):** new segment file whose genesis pins the predecessor via flat scalar fields — `predecessor_file`/`predecessor_sha256` (already in the v1 frozenset) plus `predecessor_head_seq`/`predecessor_head_sha` added at the segment boundary (additive, no nested dict — §2.1 rule holds).
- **Legacy:** no row import (pin, don't launder); genesis pins the legacy attest-audit digest; dual-write until retirement (Q2).
- **Store's own retention:** never auto-pruned in v1; governed by the s.50 schedule artifact's `audit_log` class (#13).

---

## 10. Query surface — `alfred scribe events` / `alfred scribe audit`

New sub-subparsers in the cli.py `scribe` block (:3648–3695) + branches in `cmd_scribe` (:2619). **JSON output** (vault-CLI convention — this is a machine-queryable registry) with intentionally-left-blank explicit empties. Kept tiny per the brief:

- `alfred scribe events list [--stream clinical|access] [--family F] [--kind K] [--encounter enc-…] [--actor ID] [--since ISO] [--until ISO] [--path REL] [--limit N]` — tolerant reader; `--path` hashes REL locally and filters `path_digest` (so "who viewed this record" stays answerable without paths in the trail). Empty → `[]` + stderr `no events match`.
- `alfred scribe events verify [--stream S] [--deep] [--rebuild-index]` — the strict reader (§4); `--deep` runs the full attested-digest comparison; exit 1 on any `ok:false`; genesis-only stream → `{ok: true, entries: 1, note: "genesis-only"}`. Morning-review cadence — the human-in-the-loop surface.
- `alfred scribe events tip [--stream S]` — `{stream, seq, entry_sha}` from tail-read.
- `alfred scribe events anchor` — anchor export (§4).
- `alfred scribe audit encounter <enc>` *(graft from compliance-first — all three judges: the auditor one-shot / CMPA demo query)* — the full cross-family timeline for one encounter: both streams merged by `ts`, tiebroken `(stream, seq)`, chain-position annotated.

Path resolution mirrors cmd_scribe attest (cli.py:2668–2674). **No `events emit` verb — ever** (§2.2).

Self-correcting-by-design gate, answered: the store makes no judgments — it records; actor kinds are declared by registrars, not inferred. It is the SUBSTRATE other judgment-makers' correction signals land in (attest outcomes, grounding-flag survival, consent friction), and verify-in-morning-review + the `audit` surface exposing `operator`/unknown attributions for identity-mapping fixes is its human loop.

---

## 11. PHI-minimization table

| Field | Class | Notes |
|---|---|---|
| `subject_id` | PHI-FREE (opaque) | salted HMAC encounter id (identity.py:46–60); salt never logged/vaulted (:28–29) |
| `actor` | STAFF identity | required by s.63 (WHO viewed/attested); clinician slugs config-validated; never a patient |
| `body_sha`, `*_sha256`, `manifest_sha256`, `path_digest` | PHI-ADJACENT digest | irreversible; `path_digest` flagged — short titles brute-forceable in principle; accepted, consistent with body_sha posture; raw path/title NEVER in a chained stream |
| `grounding_reasons`, `completeness`, refusal `reason`, `cap`, `method`, `reason_code`, `via`, statuses | PHI-FREE frozen enums | the grounding `claim` free text never leaves frontmatter |
| counts / seqs / bools / ts / versions / key fingerprints | PHI-FREE scalars | store-enforced scalar typing |
| `ticket_ref` (retention.unsealed) | FLAGGED | opaque operator reference; the ONLY non-enum payload string; facade length-caps |
| free-text override/unseal justification | EXCLUDED from store | routes to vault_audit.log — #58-D2 two-trail separation extended, unchanged |
| patient identifiers of any kind | STRUCTURALLY ABSENT | no field exists; KINDS allowlist + store-level field/scalar enforcement + widening pin (Ruling 5) |
| `rel_path` | operational index only (§7.4) | never chained |

Rule of construction for every future emitter: ids/enums/digests/scalars only; needed free text becomes a vault_audit `detail`, and the event carries only the enum plus the fact a detail exists.

---

## 12. Platform-standards compliance checklist

- **Intentionally-left-blank:** explicit `[]`+note on empty queries; genesis-only verify note; dormant/degraded lifecycle logs; suppression counted AND chained daily; heartbeat makes "no events" provable; `days_since_last_anchor` in verify.
- **Self-correcting:** §10 closing paragraph — recorder-not-judge, substrate for others' loops, verify-in-morning-review.
- **Scope-first:** emission matrix (§6.2) is the principal artifact, before the CLI; zero vault-scope changes.
- **Fail-loud config:** clinical mode requires the store at open; no dormancy knob; `_build` traps dodged via hand-rolled loader; fixtures carry required fields.
- **Consumer-fields-day-one:** §9.
- **QA standard:** every phase ships behind the independent code-review gate before fast-forward — no carve-outs. No SKILL-facing surface changes expected (code+config layer throughout); if the talker/PWA ever advertises event queries, the feature-enabling SKILL capability audit fires.
- **Regression pins unconditional:** no module-level importorskip on any pin test; dispatcher-path tests delenv `ALFRED_VAULT_*`.

---

## 13. Adjudication ledger — every judge-flagged weakness, resolved

**Spine (minimal-first) weaknesses:**
1. `head.json` cache (J1, J2) — **REJECTED from spine**: dropped; tip = tail-read under flock; log is sole truth. One less recovery invariant.
2. `enabled` knob reachable in clinical mode (J1, J2, J3) — **ADOPTED fix**: knob deleted; always-on with scribe; clinical fail-loud at open.
3. s.63 gap — vault-CLI reads unlogged (J1) — **ADOPTED fix (re-gated after critique)**: the top-level dispatcher read-hook registration is gated on the **loaded config identity** (`scribe:` block + `mode == "clinical"` + configured STAY-C vault path), NOT on the env-derived `ALFRED_VAULT_SCOPE` (which is `None` for an interactive `alfred --config config.stayc-clinical.yaml vault read <note>` and would leave that human read — the exact s.63 target — unlogged). See §7.1.2.
4. Suppression announced once-per-lifecycle, structlog-only (J1, J3) — **ADOPTED fix**: daily chained `access.system_reads_summary`.
5. Post-attest sweep unbounded (J1) — **ADOPTED fix**: hot-window bounding + boot/`--deep` full scan (§5.3).
6. `attested_digests.json` = third derived file (J1) — **ACCEPTED**: load-bearing (only working post-attest mechanism), rebuildable; net derived-state unchanged after head caches dropped.
7. Cross-stream timeline requires a merge (J1, J3) — **ADOPTED fix**: `audit encounter` one-shot merge; all legality-ordering families share the ONE clinical chain.
8. No off-box anchor (J3) — **ADOPTED fix**: per-attest tip printing + `anchor` export + anchor-staleness in verify.
9. No heartbeat / quiet-month ambiguity (J3) — **ADOPTED fix**: daily clinical heartbeat (scoped: one stream, one row/day — resolves J1's bloat objection to product-boundary's per-stream version).
10. `attest.refused` best-effort, store-down refusals unchained (J3) — **RATIFIED as designed**: a store failure must never mask a refusal; the preflight makes store-down an explicit, greppable refusal reason. Dropped from open questions.
11. Fail-loud at first append, not open (J3) — **ADOPTED fix**: fail at open (§2.4).
12. Silent on `vault_search/list/context` reads (J3) — **ADOPTED fix**: documented gap + `access.search` Phase-1.5 contract (§7.2).
13. Recon's stale hook anchor (J2) — **CORRECTED**: ops.py:99–115 / :185–210 / fire site :1660–1672 (re-verified in source).

**Rejected rival elements (with why):** compliance-first's mutual frontmatter pin (second privileged post-triad write, ATTEST_FIELDS pin widening, partial-failure window — tip/anchor buys comparable resistance; revisit at #13); `attest.intent` (doubles durable appends, pre-CAS store coupling — replaced by the zero-append preflight); backfill importer (retro-chaining unverifiable rows — genesis pin instead); verify-mutates-artifact objection (resolved: success-only append, never on failure); blanket sovereign arming of read-only query verbs; 7-verb audit CLI (kept tiny). Product-boundary's monthly segments + per-stream heartbeats ×4 (speculative at tens of MB/decade — reserved rollover covers it); replace-the-legacy-writer cutover (dual-write is the safe transition); `chain_mac_key` config now (dead custody knob — protocol-2 reservation).

---

## 14. Open questions — RESOLVED (operator rulings 2026-07-16/17)

1. **Tip-anchoring ritual — ✅ RULED (2026-07-17): GIT-SYNC ANCHOR.** ⚠ Standing rule recorded same night: **STAY-C uses NO Telegram — no channel of the clinical stack touches a cloud messenger.** (a) PRIMARY: a daily PHI-free anchor file (chain-tip hash + seq + timestamp, PHI-free BY CONSTRUCTION via the store's field/scalar allowlist) written into the aftermath-lab repo on-box and carried by the EXISTING nightly box→GitHub sync — GitHub commit history is the durable, independently-timestamped off-box record; zero new channels, zero recurring human effort. (b) COLD COPY: monthly `events anchor` export to the #13 seal-key USB once it exists. (c) The per-attest printed tip stays as the incidental human check. Code impact: anchor verbs + heartbeat are Phase 1; the daily anchor-file writer is a small #13-adjacent box artifact riding existing sync plumbing.
2. **Legacy dual-write retirement — ✅ RATIFIED as proposed:** dual-write until #13 ships + one clean production `verify` cycle, then retire the legacy writer.
3. **Vault-CLI read identity — ✅ RULED: ADD `--as <clinician>`.** The stayc-scoped vault read surface gains an `--as` flag; provided → `actor=(<clinician>, "clinician")`, omitted → honest `actor=("operator","operator")` fallback (never fabricate identity). In Phase-1 scope.
4. **PWA / consent actor identity — ✅ RULED: BUILD option (b), per-clinician PWA session identity, in #12.** Consent events (`captured_by`) and `pwa_view` `access.read` rows carry the real clinician identity from the #12 session mechanism; `"shared-session"` survives only as the transitional value until #12 ships. #12's scope formally expands to include the lightweight per-clinician identity (design at #12 kickoff; must respect the no-storage posture — identity is per-session, server-issued, never persisted on-device).

---

## 15. Phased build plan

**Phase 1 — #11 (one builder arc, shippable):**
1. `alfred.evstore`: `chain.py` (c14n-v1 + golden vector), `store.py` (flock append, tail-read tip, fsync policy, register_kind + field/scalar enforcement, verify + torn-tail rules, preflight, anchor, query/latest/tail/tip). Import-purity pin.
2. `scribe/events.py`: KINDS registry + widening pin, typed emitters + per-emitter (stream, kind) pins, postures, ContextVar, digest index. `ScribeEventsConfig` + hand-rolled loader; always-on activation (§2.4).
3. Integrations: attest (preflight + recorded [D] + refused + dual-write legacy-first + tip print + docstring amendment), pipeline `note.*` (6 sites + bounded post-attest sweep), ingest_web `encounter.*`, daemon (boot construction, hook + contextvar registration, heartbeat, daily suppression summary), ops.py `register_read_hook`, dispatcher clinical-config-gated registration (§7.1.2).
4. CLI: `events list|verify|tip|anchor` + `audit encounter` (JSON, ILB empties) in the :3648 block.
5. Tests (adopt wholesale — judge 3 called this the best QA spec in any proposal): c14n golden vectors; 1-byte mid-file edit breaks verify; seq-gap detection; two-process flock concurrency (2×100, no gaps); torn-tail-pass vs mid-file-fail split; index rebuild-from-log; PHI payload-allowlist + scalar-type widening pins; KINDS frozen pin; per-emitter authority pins; CAS-window-unwidened pin (event slot == old audit slot, mutation-tested both directions); #58-D2 free-text-never-in-store pin; preflight-refusal path; always-on/fail-at-open activation; post-attest sweep latch + bounding + above-early-return placement; suppression counter; non-stayc processes never register; unconditional pins; dispatcher delenv hygiene.

**Phase 2 — #12 (consent, consumes contracts):** PWA consent UI + routes; durable append-before-acknowledge; withdrawal-stops-capture + `consent.violation_refused`; notegen consent line via `latest()`; every PWA view route emits `access.read` (the access log's coverage milestone); `access.search` Phase-1.5 contract if search surfaces ship.

**Phase 3 — #13 (retention, consumes contracts):** seal keypair + encrypt→verify→event→delete lifecycle; offline-key unseal + `retention.unsealed`; two-phase destroy; s.50 schedule artifact (audio + audit_log classes) + `retention.schedule_published`; decide protocol-2 signing (and revisit mutual-pin) under the same key custody; retire legacy dual-write per Q2.

**Builder anchor pack:** attest.py:47–50 (`_body_sha`), :91 (attest), :191–207 (CAS bracket — do not widen), :212–221 (triad write), :267–284 (emission slot), :300–328 (fail-silent captures); attestation.py:186–238 (gates + refusal log), :56 (drafter identity), :67–70 (lifecycle set); pipeline.py:404–458 (regen choke), :978–1011 (clobber-detect — NOT a post-attest emitter), :1208–1228 (READY), :1233–1243 (self-heal), :551 (chunk sha); ingest_web.py:352/404/426–444 (three caps)/450–460 (chunk write incl. close-flag `write_close_manifest`)/469 (`_handle_close`)/704 (`app["scribe_config"]` accessor for facade plumbing); ops.py:99–115 + 185–210 + 1660–1672 (hook precedent), :754–763 (vault_read); scope.py:1615–1665 (pins — untouched), :118–119 (fs out-of-gate); close_manifest.py:49–54, :79–101; enroll_learning.py:17–63, :90–112, :148–151, :182–184, :255–258; identity.py:28–29, :46–60; state.py:47–63, :86–98; daemon.py:48, :66–80, :150–154, :226–231; cli.py:1047–1113 (`cmd_vault` dispatcher — config-identity read-hook gate, §7.1.2), :2619–2700, :3648–3695; config.py:8–15, :17–21/50/239 (clinical-mode gate signal), :305–314, :423–456, :459–501; boundary.py:195–218; template :43 (ReadWritePaths); config example :133 (the EROFS trap NOT to repeat).
