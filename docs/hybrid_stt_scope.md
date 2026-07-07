Both load-bearing claims check out against source. Synthesis follows.

---

# SCOPE: Deepgram-timing + Groq-Whisper-final hybrid STT for the PWA voice stack

Ground-truthed against source this session. Two facets (A, B) returned; facet C (latency/cost reality) was null, so those numbers are reconstructed from A+B plus the confirmed backend behavior. I read the two seams that the whole design hinges on and confirmed them:

- `GroqWhisperBackend.transcribe(self, audio, mime, vocab)` is a **3-arg** call (`stt_backends.py:221-223`). The CONTEXT brief's `transcribe(audio, mime, vocab, budget)` is **wrong** — there is no `budget` param. Anyone building this must not pass a fourth arg. Facet A had this right.
- `stt_backends.py:230` is a **real blocker**: `filename = "voice.ogg" if mime.endswith("ogg") else "voice.bin"`. A WAV (`mime="audio/wav"`) is sent to Groq as `voice.bin`. This is a hard prerequisite fix, not an optional tweak (see §1). Facet B found this; Facet A under-weighted it.
- The buffer seam is exactly as described: sender feeds + taps at `voice_stt.py:344-346`; pump handles `EVENT_UTTERANCE_END` at `:409`, fires `await self._on_utterance(text)` at `:426`, with a `min_utterance_chars` floor at `:412`, a `_closing` teardown guard at `:414`, and an `utterance_empty` branch at `:428`. All single-loop async, so the sender-append / pump-snapshot bridge is genuinely lock-free.

---

## 1. RECOMMENDATION — order, and the go/no-go rule

**Do NOT build the hybrid yet. Build the SHADOW-CAPTURE increment first, measure on Andrew's real noisy audio, and gate the hybrid on that data.** This is scope-first discipline: the hybrid is a non-trivial build (per-utterance buffering, a serial finalize task, timeout/fallback, a config gate, an utterance-id seam), and every line of it is wasted if Groq doesn't actually beat Deepgram on Andrew's noise. The measurement costs a small, latency-free, live-turn-safe increment plus recording effort — a cheap gate on an expensive build.

**Where the two facets diverge on HOW to collect, and how I reconcile it:**

- **Facet A** says: skip a new build entirely — send noisy Telegram voice notes to Salem; the Telegram path already runs Groq-batch *and* Deepgram-batch per note into `data/stt_corpus/`, and `stt_replay.py score` already computes WER + domain-term error rate. Zero new production code.
- **Facet B** says: build a small **web-side** shadow (`voice_stt_shadow.py`) that captures **Deepgram-STREAMING-final vs Groq-batch** on the actual WebRTC path, with noise tagging.

**I recommend Facet B's web-shadow route as Increment 1, not the Telegram route** — because the Telegram route carries **two proxy gaps** that muddy the exact decision:
1. It compares against **Deepgram BATCH**, but the hybrid replaces **Deepgram STREAMING nova-3** (`stt_deepgram.py`, the `utterance_end` final at `voice_stt.py:410`). Facet A itself concedes this and has to spend a "Tier-2 streaming replay" to close it.
2. Telegram audio is the **48k OGG from Telegram's encoder**, not the **WebRTC-48k→16k-resampled PCM** the PWA feeds Groq. Different acoustic path entirely.

The web-shadow route removes both gaps by measuring the exact thing on the exact surface, **and** the buffer+WAV seam it builds is the identical seam the hybrid reuses — so Increment 1 is the foundation of Increment 2, not throwaway. It also yields the Groq round-trip latency numbers (measured on real web WAVs) that Increment 2's latency budget needs. The only cost is ~a day of build + one code-review gate.

**Keep the Telegram route in your pocket as the zero-build quick-look** if you want a rough directional signal *today* before committing even the small web build. But don't decide the hybrid on it — decide on the web-shadow corpus.

### The go/no-go rule (pre-register BEFORE scoring — reconciling A's WER metric and B's preference metric)

The two facets proposed different decision metrics; I stage them rather than pick one:

- **Primary gate = blind A/B operator preference on the NOISY subset** (Facet B). This is literally what you asked for ("real numbers on my real audio"), and it needs no hand-typed ground truth for the first cut. For each divergent noisy clip: play the WAV, show both transcripts blind (randomized which side is Groq), you pick. Cutting on the **noisy subset only** is load-bearing — if Groq only wins when it's quiet, Deepgram is already fine and there's nothing to build.
- **Confirming metric = domain-term error rate** (Facet A) on those same divergent-noisy clips. A misheard "RRTS" / "Fergus" / a person or project name is the actual downstream failure; preference tells you *which is better*, domain-term error rate tells you *whether it's better on the words that matter*. This needs you to type the truth only for the clips you're already A/B-judging — cheap.

**GREENLIGHT the hybrid IF, on ≥30 scored noisy utterances:** Groq preferred on ≥60% with a ≥+20-point margin over Deepgram, **AND** Groq's domain-term error rate is materially lower, **AND** measured Groq round-trip latency is p50 ≤ 800 ms / p90 ≤ 1500 ms.

**KILL / DEFER (keep Deepgram) IF:** Groq preference < 55% on noisy clips, OR the two tie within noise (≤10-pt gap), OR latency p90 > 1500 ms. On a kill, the cheaper next move is Deepgram tuning (§5), not the hybrid.

**Below 30 scored noisy utterances, do not decide** — emit the "still accumulating" signal and keep collecting (intentionally-left-blank).

---

## 2. INCREMENT 1 — SHADOW CAPTURE (the user-testing deliverable)

Small, default-OFF, Salem-only, **zero latency added to the live turn, zero risk to the reply path.** It produces the decision dataset without building the hybrid.

### What it builds

1. **A per-utterance PCM buffer on `VoiceSttWorker`** (`voice_stt.py`). The sender tees each fed chunk into a bounded `bytearray` — one line right after `_account_input_energy(item)` at `:346`. The pump snapshots + clears it in the `EVENT_UTTERANCE_END` branch at `:409`, just before the unchanged `on_utterance` at `:426`, and also clears on the `utterance_empty` branch at `:428`. Lock-free (both ops are synchronous, single event loop). Bound with a ~30 s ring cap (~960 KB); drop-oldest on overflow (keep the speech tail near end-of-utterance). Because Deepgram is deliberately started without `SpeechStarted`/`vad_events` (`stt_deepgram.py` — no speech-onset marker), the window is "all PCM since the last EOU" — a slight over-capture of leading/trailing silence, which Whisper tolerates fine and which the ring cap bounds.

2. **A pure `pcm16_to_wav(pcm, sample_rate) -> bytes`** next to `PcmChunker` in `voice_stt.py` — stdlib `wave` into `io.BytesIO`, 16-bit mono. No new dependency. Unit-tested in isolation.

3. **A new `src/alfred/web/voice_stt_shadow.py`** mirroring the proven Telegram `stt_shadow.py`: after the **unchanged** live `on_utterance(deepgram_text)` fires (line `:426` is byte-identical — the live turn is untouched), a **fire-and-forget** background task WAV-wraps the buffer, calls the reused `GroqWhisperBackend.transcribe(wav, "audio/wav", vocab)`, and appends a results-shaped record to `data/stt_corpus/corpus.jsonl`. It is never awaited by the turn, and it sits under a top-level catch-all that never raises. **Critical detail from `project_stt_test_series`:** a bare `asyncio.create_task` is held only by a weak ref and can be GC'd mid-flight — use a module-level `_SHADOW_TASKS` set + discard-in-done-callback, same as the Telegram path.

4. **The corpus record** (directly consumable by the existing `stt_replay.py`): `audio_file` (`.wav`), `ts`, `instance`, `voice_session_id`, `duration`, `deepgram` = the **streaming** final already in hand at `voice_stt.py:410` (the load-bearing baseline — this is exactly what the hybrid would replace, at zero extra cost), `groq` = the one new batch call + its measured latency ms, `divergence` (reuse `stt_shadow.divergence()` byte-identical so live numbers match the replay harness), plus a **noise tag**: per-utterance `utt_peak_rms`/`utt_avg_rms` (reuse `pcm_rms`, `web/utils.py:80`; the worker already computes RMS at `voice_stt.py:157`) and a rolling inter-utterance noise-floor estimate → `noisy: bool`.

5. **Config**: a `WebSttShadowCaptureConfig{enabled, dir}` sub-block on `WebVoiceSttConfig` (`config.py:143`), mirroring Telegram's `SttShadowCaptureConfig` (`telegram/config.py:173`). `_build_voice_stt` (`config.py:377`) already filters on `__dataclass_fields__`, so it loads with no loader change. **Default-OFF**; activate on Salem's box config only (non-PHI, the noisy environment). Corpus dir under the LUKS `/data`.

### THE PREREQUISITE FIX (blocks both increments)

`stt_backends.py:230` sends any non-OGG audio as `voice.bin`. Groq's Whisper endpoint validates the file extension; `.bin` is not an accepted audio extension. **Fix it to derive the extension from the mime** (`audio/wav → voice.wav`, `audio/webm → voice.webm`, etc.) before the multipart post. One line, non-breaking, also benefits the Telegram path. Without it, every shadow WAV fails and the corpus records only errors. (The exact HTTP-400 behavior on `.bin` should be confirmed empirically on the first live call, but the fix is safe and required regardless.)

### How Andrew judges / tests it (the deliverable)

1. Flip `shadow_capture.enabled` on Salem's PWA voice config.
2. Use the PWA voice loop normally under your **real ambient noise** — kitchen, outdoors, fan, vehicle — across your real vocab (RRTS, Fergus, person/project names). ~25-30 utterances. Each one passively fills the corpus with the WAV + both transcripts + divergence + noise tag. No extra effort beyond talking.
3. Score with the existing `stt_replay.py` harness (lives in the aftermath review tree per both facets — confirm the exact path before the run): `divergences` mode ranks disagreements; filter to `noisy`; then a **blind A/B `preference` mode** (a ~small extension to the harness — iterate divergent+noisy records, randomized-side prompt, write `operator_preference` back into the record, filling the current `score=None` gap). Optionally surface the top-N divergent noisy clips at morning-review instead of one sitting.

---

## 3. INCREMENT 2 — THE HYBRID (only if the data greenlights)

This is a **flip of Increment 1's seam** from fire-and-forget to blocking-with-fallback. The buffer, WAV wrapper, and Groq backend are already built and proven by Increment 1.

### Architecture

At `EVENT_UTTERANCE_END` (`voice_stt.py:409`), instead of firing the Deepgram text, hand the snapshot PCM + the Deepgram streaming final (as fallback) to a **dedicated serial finalize task**, which WAV-wraps, calls Groq under a bounded timeout, then fires `on_utterance(groq_text_or_fallback)`.

**Facet disagreement to flag — task boundary.** Facet A says run Groq in a **separate serial `_groq_finalize_worker` task** (spawned in `start()` alongside reader/sender/pump), NOT inline in the pump. Facet B defaults to **inline-await in the pump** and says measure first. **I side with Facet A**, and Increment 1's latency data resolves it: the pump is the barge-critical path — it must keep delivering Deepgram partials to drive barge-in (`on_partial → emit_stt_partial`, `voice_turns.py:522`) and detect the next EOU. Blocking it inline on a 0.3-0.8 s Groq call stalls the *next* utterance's barge. A single serial FIFO worker keeps the pump responsive AND guarantees `on_utterance` fires in utterance order and never runs two Groq calls at once. If Increment 1 shows Groq p90 is very small, inline-await becomes a defensible simplification — but default to the separate serial task since barge (V3) is live on Salem.

Everything on the Deepgram timing side stays untouched: partials/barge, endpointing, `speech_final`/`UtteranceEnd` dedup, reconnect. Groq supplies **only** the committed final text.

### Latency budget + fallback (mandatory zero-regression)

- Construct the backend with `timeout_s = final_transcriber_timeout_ms/1000` (httpx level) AND wrap the call in `asyncio.wait_for(...)` for non-HTTP hangs (mirrors `stt_shadow`'s bounding).
- On timeout OR `SttError` OR empty result → fire `on_utterance(deepgram_streaming_fallback)` — the streaming final captured at handoff. The hybrid therefore degrades to **exactly today's behavior** on any Groq hiccup, never worse. Wire through the existing `transcribe_with_fallback` / `build_chain` (`stt_backends.py`) to inherit the M1 fallback chain + empty-contract, with the Deepgram streaming final as the ultimate backstop.

### Config

`WebVoiceSttConfig` (`config.py:143`) gains: `final_transcriber: str = "deepgram"` (enum `deepgram|groq`; default keeps today byte-identical), `final_transcriber_timeout_ms: int = 2500`, `final_transcriber_max_utterance_s: int = 30`. Add range clamps in `normalize_stt_settings` (`stt_stream.py:241`) with `config_clamped` logging.

**Groq credentials/model/vocab are reused, not re-declared** — read from `talker_config.stt` (`telegram/config.py` `STTConfig:199`), whose `effective_chain()` already resolves `${GROQ_API_KEY}`, the model, and `vocab_terms` (the same domain biasing the corpus was built with). **Fail-closed gate** in `_build_assistant_stt` (`routes_voice.py:463`): if `final_transcriber=="groq"` but no resolvable Groq key → log `web.voice.stt.hybrid_disabled reason=groq_key_missing` and **degrade to Deepgram-only** (never mount a hybrid that would 100%-fallback every turn). Mount-time success → `hybrid_enabled{...}`.

### The one real seam interaction to fix (Facet A, confirmed reasoning)

The driver's `_utt_id` is a single mutable slot minted on the first partial (`voice_turns.py:524`) and consumed by `submit_utterance` (`:540`). If the Groq round-trip delays `submit_utterance` and the *next* utterance's partials arrive in that sub-second gap, the wire-label (utterance_id) on `stt_partial`/`stt_final` can mismatch — a **label bug, not a wrong turn** (`submit_utterance` still evaluates `_speaking_turn_id`/echo-grace at fire time, `:551`). Rare in walkie-talkie use. Correct fix: thread a per-utterance token minted at EOU through the `on_utterance` seam so identity is frozen at EOU independent of the Groq delay. Ship it with V1-hybrid or accept the rare mislabel as a fast-follow — an open question for you.

### Tests (all unconditional — fake provider + injected fake Groq, no aiortc/av)

`pcm16_to_wav` pure + round-trips through `wave.open`; hybrid handoff fires Groq text (not Deepgram); timeout → Deepgram fallback (`fell_back_to_deepgram=true reason=timeout`); empty → fallback; **streaming-mode byte-identical (no Groq call, existing worker tests stay green)**; buffer bound (drop-oldest); ordering (serial worker); reset-per-utterance (2nd WAV excludes 1st's audio). Plus `aclose` ordering — pending finalize tasks must be cancelled/drained so a late `on_utterance` can't fire a turn during teardown (the `_closing` guard at `:414` already discards, but the worker must not enqueue post-close).

---

## 4. LATENCY + COST reality (facet C was null — reconstructed)

**Latency.** Groq `whisper-large-v3` batch on a 5-10 s clip is typically ~0.3-0.8 s. In Increment 2 that inserts between EOU and reply-start; today's turn is 2-5 s, so the **median add (~0.5 s) is tolerable** and the timeout caps the tail at 2.5 s. The real regression is on **very short turns** ("yes", "no") where +0.5 s is a proportionally larger fraction — measure this in Increment 1's per-utterance latency distribution. **Increment 1 itself adds ZERO latency** to the live turn (fire-and-forget after the unchanged `on_utterance`).

**Cost.** Groq bills a **10-second minimum per request**, so every utterance — even a 2-second "yes" — bills ~10 s of audio. At Groq `whisper-large-v3` pricing (~$0.111/hr), that's roughly **$0.0003 per utterance** → order-of-magnitude **~$1/month** for a single busy instance. Negligible for one non-PHI instance (Salem). The 10 s minimum is a cost floor, not a latency floor. Keep shadow default-OFF and single-instance; do **not** fleet-activate, and confirm no PHI instance (VERA/Hypatia) ever shadow-activates (Groq batch is a cloud egress of buffered voice).

---

## 5. RISKS + the cheaper Deepgram-tuning alternative to try first

Facet C returned null, so no dedicated latency/cost adversary ran — treat §4's numbers as reconstructed, and let Increment 1's measured Groq latency be the source of truth before committing to Increment 2's budget.

**The cheaper alternative that could kill the hybrid need entirely (try first / in parallel):** if the corpus shows Deepgram's noisy misses are concentrated in **domain terms** (RRTS, Fergus, names), the cheapest fix is **Deepgram nova-3 keyterm prompting + endpointing/smart_format tuning** (`project_per_instance_stt_context`) — **zero added latency, zero extra call, no new code path.** Confirm whether `build_deepgram_url` (`stt_deepgram.py`) currently passes nova-3 keyterms; if it doesn't, that's a free lever that directly attacks the exact failure the hybrid is meant to fix. **Strong recommendation: read the corpus for this signal before greenlighting the hybrid** — if keyterms close the domain-term gap at zero latency cost, you don't need the hybrid at all. This reconciles a point both facets gesture at but neither foregrounds: the hybrid is the *heavy* fix; keyterm-tuning is the *free* fix, and the corpus tells you which failure class you actually have.

**Other risks:** barge-for-next-utterance delay if the hybrid awaits inline (→ use the serial task, §3); the utterance-id label race (→ EOU token, §3); over-capture of silence at the buffer boundary (bounded by the ring cap, reset on EOU *and* `utterance_empty`); deciding on too little data (enforce the ≥30 floor); Groq 10 s billing minimum (bounded, single-instance); and — per your own QA standard — **Increment 1 gets an independent code-reviewer pass before merge despite being "just shadow/isolated"** — the isolation-from-the-live-turn property is exactly what a reviewer must confirm, not self-certify.

---

## 6. What I bring back to you after Increment 1 runs

The numbers that decide Increment 2:

1. **N scored noisy utterances** (must clear the ≥30 floor, else "still accumulating").
2. **Blind A/B operator-preference split** — % Groq vs % Deepgram-streaming on the noisy subset, and the margin.
3. **Domain-term error rate**, Groq vs Deepgram-streaming, on the divergent-noisy clips — the RRTS/Fergus/name misses that actually break downstream.
4. **Groq round-trip latency distribution** (p50/p90) measured on the real web WAVs — feeds both the greenlight latency gate AND the inline-vs-serial-task choice.
5. **Whether Deepgram's misses are keyterm-fixable** — the free-lever signal that could preempt the whole hybrid (§5).
6. **Noise-floor RMS histogram** — to calibrate the "noisy" threshold post-hoc rather than hardcoding it, so the noisy-subset cut is meaningful.

Then the pre-registered rule from §1 fires: greenlight the hybrid, or keep Deepgram and tune it first.

---

## CONCRETE NEXT STEP

Route to the **builder**: (a) the 1-line `stt_backends.py:230` mime→extension fix (prerequisite), (b) the per-utterance PCM buffer + pure `pcm16_to_wav` in `voice_stt.py`, (c) the new `voice_stt_shadow.py` fire-and-forget Groq capture with the `_SHADOW_TASKS` GC-safe set + noise tagging, (d) the `WebSttShadowCaptureConfig` block default-OFF, and (e) the `stt_replay.py` blind-A/B `preference` mode. Independent **code-reviewer** gate before merge (confirm live-turn isolation). Activate on Salem only. Then you record ~25-30 noisy utterances and I bring back the §6 numbers to decide the hybrid.

Open questions for you before build: ship the utterance-id EOU token with V1-hybrid or fast-follow it; reuse `vocab_terms` as the Groq/Whisper `prompt=` bias (parity with the served path) or run vocab-free to measure raw acoustic robustness; and do you want the scoring surfaced as an on-box `stt_replay.py preference` session or a morning-review digest.