Citations verified on-box. All load-bearing claims hold, including the two telemetry gaps and the absence of any durable barge artifact in `data/`. Here is the synthesized scope.

---

# V3.1 — Per-user self-calibration of barge suppression — SCOPE (decide before building)

## 1. Recommendation and order

**Ship a COLLECT+PROPOSE-only increment first; gate the APPROVE+APPLY machinery behind a pre-registered go/no-go on real data. MVP learns one knob: `too_early_ms`.**

The three facets converge on the shape and split on exactly one point, which I reconcile below.

- RECON, Facet A, Facet B all agree: collect first (durable per-user-keyed corpus), then propose at morning-review cadence, then approve→apply — never silent drift. This is the platform self-correcting standard (capture → feed back → operator-approves) and the STT-shadow discipline (collect + pre-registered go/no-go before the heavy part).
- **Facet C dissents on how much of "propose" belongs in Increment 1.** It argues the *proposal calibrator* and *approval wiring* are apply-machinery-ahead-of-data for a single-user instance with a working suppressor, and that the honest first build is a read-only instrument + profile readout, nothing more.

**Reconciliation (the decision):** Facet C is right that you do not build the *approval/apply* wiring until data justifies it — but a *proposal is cheap and has zero drift risk because nothing applies*. So Increment 1 = collect + aggregate + a **read-only proposal surfaced at morning review** (implemented as the lightest possible readout, not the full Daily-Sync-section + reply-dispatch machinery). That readout **is** the go/no-go evidence. Facet B's full approve machinery (Daily Sync fifth-tenant section, `reply_dispatch` resolver, override store, gate seam) is **Increment 2**, built only if Increment 1's numbers show the static 700 ms actually mis-serves the user. This threads all three facets: C's instrument-first caution, the task's collect+propose framing, and B's design as the Increment-2 home.

**Concrete go/no-go (pre-registered now, numbers confirmed against real volume after Increment 1 runs).** A proposal for `too_early_ms` fires only when **all** hold, per user:
- **≥30 confirmed-completed barges** (stable `ms_into_speaking` distribution of genuine barges), AND
- **a consistent-direction correction net over ≥3 distinct sessions**, AND either
  - **≥5 cancel-joined `too_early` false-suppresses** → propose LOWER, or
  - **≥5 sub-2000 ms `confirmed→empty` false-fires** (or a `storm_disabled` cluster) → propose RAISE, AND
- the data's suggested value differs from the current effective value by **> a min effect size (recommend ≥75 ms)**, AND
- the proposed step is **bounded (≤150 ms per cycle)** and re-passes the existing `_clamp` bounds `[0, 5000]` (barge_in.py:210-224).

If those thresholds are never crossed on Salem, **the honest outcome is "park permanently; the instrument stays as passive telemetry."** That is a legitimate and, for single-user Salem, likely result — see §5.

---

## 2. Increment 1 — COLLECT + PROPOSE (small, no gate change, no drift risk)

**What it builds:**

**(a) Phase 0 — per-user-key the telemetry (tiny, the single most important line).** Every barge event currently keys on the ephemeral `voice_session_id` (`self._vid`, a per-offer `uuid4().hex`, routes_voice.py:209) and carries **no user field** — confirmed in `_log_barge` (voice_turns.py fields dict: `voice_session_id`, `utterance_id`, `turn_id`, `ms_into_speaking` only). Add `web_user=deps.identity.user` (identity.py:57 — the stable display name, what the operator will approve against) to `_log_barge`, `_barge_outcome`, and the three cancel logs (`client_cancel`, `cancel_stale`, `cancel_noop` in `_on_cancel`). Without this, aggregation depends on an unpersisted session→user join. `synthetic_chat_id` (identity.py:62) is the non-PII numeric alternative if preferred — an operator ruling, not a blocker.

**(b) Phase 1 — durable per-user sink (mirror the shipped STT shadow-capture).** Add a **fire-and-forget** append at the existing emit sites → `data/barge_corpus/events.jsonl` (tool-scoped path per CLAUDE.md; append-log style like `data/vault_audit.log`). This is **additive** alongside the existing `log.info` — do **not** scrape journald (retention rotates it out; `data/alfred.log` already rotates/gzips — the silent-data-loss trap the intentionally-left-blank rule warns against). **Hot-path constraint (reviewer must check on the PR):** the emit sites run inside the driver's await-free per-event loop body, so the write must be non-blocking — buffer-and-flush or a module-level retain set exactly like STT's `_SHADOW_TASKS` (voice_stt_shadow.py), **never an inline `open().write()`**. Barge events are rare (human-rate, only while speaking), so cost is trivial. Default-off, Salem-only, mirroring `WebSttShadowCaptureConfig` (config.py, `enabled: bool = False`).

**(c) The first real deliverable — a read-only offline profile readout (separate module, like `stt_shadow_score.py` is separate from capture).** On demand it prints, per user: the `ms_into_speaking` histogram of `confirmed→completed` barges vs `suppressed{too_early}`; the count of false-suppress cancel-joins; the count of sub-2s `confirmed→empty` false-fires; and — when the go/no-go bar is crossed — a **proposal line** ("Andrew: 23 confirmed barges median 520 ms over 6 sessions, 8 too_early false-suppresses, 0 false-fires → suggest `too_early_ms` 700→550"). Intentionally-left-blank holds: when the bar is not crossed it emits an explicit "N events tracked, within tolerance, no proposal," never silence.

**Surface (reconciled — lightest first):** Increment 1's proposal surfaces via a **CLI readout** (`alfred voice barge profile [user]`, under `cmd_voice`), optionally emitting a one-line morning-review note when the bar is crossed. **Defer the full Daily Sync fifth-tenant section + reply grammar to Increment 2**, where it becomes load-bearing (you can't reply-approve in Increment 1 anyway, and there's nothing to apply). Facet B's `daily_sync/barge_calibration_section.py` + `reply_dispatch._resolve_barge_calibration_correction` is the Increment-2 home, not Increment-1 scope.

**File touchpoints (Increment 1):**
- `src/alfred/web/voice_turns.py` — add `web_user` in `_log_barge`, `_barge_outcome`, `_on_cancel`; add the fire-and-forget durable append. (Optional-but-cheap while here: add `echo_score(text, spoken)` to the `web.voice.barge.confirmed` emit — see the Increment-2 note in §4; it's a pure fn, barge_in.py:91-110.)
- `src/alfred/web/voice_barge_capture.py` (NEW) — the fire-and-forget corpus writer, near-copy of `voice_stt_shadow.py`.
- `src/alfred/web/config.py` — a `barge_corpus` capture block, hand-rolled construction mirroring `_build_shadow_capture` (avoids the `_build` collision footgun per CLAUDE.md).
- `src/alfred/web/barge_profile.py` (NEW) — the offline read-only aggregator/readout, shaped like `stt_shadow_score.py`.
- `src/alfred/cli.py` — `alfred voice barge profile` subcommand under `cmd_voice`.
- **Not touched:** `barge_in.py` evaluation logic, the driver ctor seam (routes_voice.py:221), and every gate consumption site. Zero behavior change; Salem barge runs byte-identical.

---

## 3. Increment 2 — APPROVE + APPLY (build only if Increment 1's data supports it)

Facet B's design, gated. Built only after the go/no-go clears on real data.

**Approve mechanism (reuse, don't invent).** The Daily Sync reply-dispatch already IS the morning-review learn→propose→operator-approves surface for four self-correcting tenants (email `calibration_ok`, canonical_proposals, routine-match glossary, attribution audit — confirmed: `canonical_proposals_section.py`, `radar_section.py`, `routine_match_section.py`, `reply_dispatch.py`, `assembler.py`, `daemon.py` all present in `daily_sync/`). Barge becomes the fifth tenant: a `barge_calibration_section.py` renders pending proposals as a numbered item (stashed in `last_batch.barge_proposal_items`); the operator replies with the existing terse grammar — `12 confirm` (accept), `12 reject` (keep default), `12 600` (edit to own value). A new `_resolve_barge_calibration_correction` mirrors `_resolve_proposal_correction`. Rejection is remembered in `data/barge_calibration_queue.jsonl` (state `pending|accepted|rejected`, shape mirroring `canonical_proposals.jsonl`, in-place temp+rename per `pending_items/queue.py`), driving a reject-cooldown so a "no, leave it at 700" isn't re-nagged.

**Apply seam (single, clean, confirmed).** At the driver ctor (routes_voice.py:221 — `VoiceTurnDriver(deps, voice_session_id=vid, barge=request.app.get(_KEY_WEB_BARGE))`), `deps.identity` is already in hand. Resolve `identity.user` → approved deltas from a durable override store `data/barge_calibration.json` (tool-scoped, `from_dict` schema-tolerance filter, keyed by `identity.user`, `_meta.from_default` recording the pre-override value for exact revert), **re-clamp each delta through the same `_clamp` bounds** (barge_in.py:210-224 — bound-check on apply, not just config load), then `dataclasses.replace()` the app-global `BargeSettings` (`replace` already imported, barge_in.py:30) and pass the merged object as `barge=`. `evaluate_barge` and every consumption site are untouched. **No override → app-global fallback, byte-identical to today.** A store parse failure must **fail-OPEN to the app-global default** (a bad calibration file must never brick voice). Log the resolved effective settings at session open (extend the `web.voice.barge.enabled` log, routes_voice.py:757, with `web_user` + resolved values + `source=default|calibrated`) so a mis-applied override is diagnosable.

**Anti-drift guardrails (the hard lines):**
1. **Operator-in-the-loop invariant:** `data/barge_calibration.json` is written by **exactly one path** — the approve-reply resolver. Telemetry/aggregator/calibrator only ever write `pending` to the queue. Learn→propose→operator-approves; never learn→apply. Mirrors the routine-match "ONLY path that writes the corpus" guardrail.
2. **Bounded per-cycle delta** (≤150 ms for `too_early_ms`): convergence is multi-step, each operator-approved. Never 700→300 in one jump.
3. **Absolute `_clamp` on both proposed and edited values** (typo `12 50000` → clamped to 5000, stated in the confirmation reply).
4. **Pre-registered min-sample floor** (§1): no proposal on sparse data.
5. **Storm-breaker stays fixed** (voice_turns.py:421-433, `_register_confirmed_barge`, the 3×/2s latch): the independent safety net beneath any mis-learned threshold. Even an approved `too_early_ms` that leaks echo gets the session disabled. **Fixed-structural, never per-user** — as are the §1.4 evaluation order, the `echo_score` algorithm, and the `_clamp` bounds themselves.
6. **Revert:** `alfred voice barge revert <user> [--param]` restores `_meta.from_default`. Plus self-correcting on its own approvals — if an applied delta makes telemetry *worse*, the next aggregate shows it and the calibrator raises a reverse-direction proposal (still operator-gated).
7. **Reject-cooldown:** a rejected `(user, param, direction)` isn't re-surfaced until cooldown + fresh evidence beats the rejected effect size by a margin.

---

## 4. MVP scoping

**Learn `too_early_ms` only in the MVP. Defer everything else.** All three facets agree, and it's the operator's own example. It is the highest-value knob (per-user reaction latency / device audio-clip profile), a clean one-dimensional ms boundary (consumed at barge_in.py:153) learnable directly off the `ms_into_speaking` histogram that's already on every event, and device-agnostic (unlike echo_threshold).

**Defer:**
- **`echo_threshold`** (secondary) — per-user acoustic environment, but it likely needs a **device/acoustic-class sub-key** (Bluetooth vs speaker vs headset score very differently; a single per-user scalar averages across acoustically distinct sessions and could oscillate). Flagged by RECON and both Facet A and B as an open question — collect the per-user echo_score histograms first, inspect for bimodality, then decide. **Note the confirmed telemetry gap:** `web.voice.barge.confirmed` does **not** log `score` today (only `reason=echo` suppressions do — verified: the confirmed emit is `_log_barge("web.voice.barge.confirmed", utterance_id, turn_id=barged_turn)` with no score). Without adding `echo_score` to that event, the false-fire→lower-echo_threshold derivation is a **one-directional blind spot** (can only ever raise, never lower). Since echo_threshold is deferred, this is not an Increment-1 blocker — but add the field cheaply while touching the emit site so the data exists when echo_threshold is greenlit.
- **`echo_grace_s`, `min_words`, `min_chars`** — low value.
- **`interrupt_phrases` / `backchannel_phrases` vocab** — the risky text-level "learned glossary" shape; each addition individually approved; no transcript text is logged (privacy contract barge_in.py:11-12), so it's a different, later increment.

---

## 5. Risks + the honest "maybe the default is just fine" possibility

- **This may be a solution seeking a problem (Facet C's central point, and the most important line in this scope).** There is currently **zero evidence** 700 ms mis-serves Andrew, and it is **unobservable today** — telemetry is journald-log-only, no durable barge artifact exists in `data/` (confirmed), and the pipeline is ~1 day live. "Look at real events first" itself requires the collect increment. Increment 1 exists precisely to make the question answerable; **parking after Increment 1 is a fully legitimate outcome** and should not be treated as failure.
- **A working suppressor produces corrections slowest by design.** V3 works, so false-suppress/false-fire events are rare — plausibly 1-2/week on single-user Salem. The min-sample floor may not be crossed for weeks-to-never. This is the honest tension: the better V3 is, the slower any learner accumulates signal. Do **not** let roadmap-pressure fire Increment 2 — only the readout's evidence bar or operator-observed friction does.
- **Correction-signal ambiguity (aggregate, never trust single events).** A `client_cancel` after a suppression can be a genuine walkie-talkie "never mind," not a missed barge (the driver's own comment). The 10s `last_suppressed_utt` join is weak per-event evidence. Likewise `outcome=empty` can be a legitimately empty LLM reply, not an echo-leak — disambiguated by the sub-2000 ms gate (reusing the storm-breaker's existing 2s boundary) but not eliminated. `storm_disabled` is the only zero-ambiguity false-fire signal. The two signals push each knob in **opposite directions**, so the calibrator nets a signed pressure per knob per user and proposes only on a consistent-sign net — never reacts to one event.
- **Per-user keying is right even for single-user Salem** — free at capture time and the shared precondition for VERA/other-instance multi-user. That's a real reason to build the instrument despite low single-user payoff.
- **Increment-2-specific:** override store as a second frozen-source (mitigated by fail-open + `source=` session-open log); mid-session approval only takes effect on the user's *next* session (document in the confirmation reply); reply-grammar parser collision on the bare-integer edit token (`12 600`) must be resolved by routing-by-section-first — the sharpest edge in Facet B.
- **Scope/capability audit (CLAUDE.md):** per-user learning changes barge behavior, but barge is transport-layer, not agent-facing/SKILL-advertised — low drift risk. Confirm no voice-doc advertises fixed per-instance-only barge behavior before Increment 2 ships.

---

## 6. What I bring back to the operator after Increment 1 runs (the numbers that decide Increment 2)

After ~2-4 weeks of durable corpus, `alfred voice barge profile` gives, per user:
1. **Volume** — total confirmed barges; is it even ≥30? (If not, keep collecting or park.)
2. **The `too_early_ms` friction test** — median and p05 of `ms_into_speaking` for `confirmed→completed` barges vs the `suppressed{too_early}` cluster. **If good barges median well below 700 ms WITH a nonzero false-suppress cancel-join rate → Increment 2 is justified; if the two clouds sit comfortably on the right side of 700 → the default is fine, park permanently.**
3. **The false-fire counter-term** — count of sub-2s `confirmed→empty` and any `storm_disabled`. High here vetoes lowering.
4. **The pre-registered numbers, now confirmable against real data** — lock the exact min-sample (≥30 recommended), min corrections-per-direction (≥5), session-consistency (≥3), min effect size (≥75 ms), and max per-cycle delta (≤150 ms) before any Increment-2 code.

These decide GO (build Increment 2 approve+apply) vs PARK (instrument stays as passive telemetry, cost was ~one day).

---

## Facet disagreements flagged + operator rulings needed

- **Reconciled disagreement:** Facet B put the propose *machinery* in the first increment; Facet C wanted collect-only. Landed: Increment 1 = collect + **read-only** proposal readout (CLI, zero drift); Facet B's Daily Sync section + reply-dispatch approval wiring = Increment 2 (load-bearing only once apply exists).
- **Ruling needed — approved-override storage:** state file `data/barge_calibration.json` + reply/CLI approve (recommended; learned-per-user values are state-like) **vs** a `web.voice.tts.barge_in.per_user` config block (CLAUDE.md says per-instance values belong in config, but these are learned-per-user, not operator-authored). Recommend the state file. Decide before Increment 2.
- **Ruling needed — per-user key:** `identity.user` (human-readable, recommended) vs `synthetic_chat_id` (non-PII numeric).
- **Ruling needed — edit-reply grammar:** bare integer `12 600` (terser, matches email tier shorthand, parser-ambiguity risk) vs explicit `12 set 600`. Decide before wiring the Increment-2 resolver.
- **Deferred question:** echo_threshold as a single per-user scalar vs device-class-keyed. Collect histograms in Increment 1, inspect for bimodality, decide at Increment-2 scoping.

---

## Concrete next step

**Build Increment 1 only:** Phase 0 (`web_user` on every barge event + optionally `echo_score` on the confirmed event) + Phase 1 (fire-and-forget durable `data/barge_corpus/events.jsonl` sink, default-off Salem-only, non-blocking per the await-free-loop constraint) + the read-only `alfred voice barge profile` readout. Ship it via the builder, code-reviewer QA on the hot-path non-blocking-write guarantee before merge (per the QA-gates-the-merge standard). No gate change, no apply seam, no drift risk. Then let it collect for ~2-4 weeks and re-decide Increment 2 against the §1 go/no-go using the §6 numbers.

Key file:line anchors for the builder: emit sites `voice_turns.py` `_log_barge`/`_barge_outcome`/`_on_cancel`; corpus writer mirrors `voice_stt_shadow.py`; readout mirrors `stt_shadow_score.py`; config block mirrors `WebSttShadowCaptureConfig` in `web/config.py`; per-user key `identity.py:57`. Deferred Increment-2 seam (do not touch now): driver ctor `routes_voice.py:221`, `_clamp` re-apply `barge_in.py:210-224`, session-open log `routes_voice.py:757`, Daily Sync tenant pattern in `daily_sync/`.