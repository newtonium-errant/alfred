---
type: session
status: completed
name: Observability arc and queue-design foundation 2026-04-28
created: 2026-04-28
description: Nine commits across multiple arcs — silent-drop transport fix, synthesis prereqs, hardcoding sweep, polish, KAL-LE distiller-radar P0, plus design-only ratification of the Pending Items Queue spec and QA review trend log baseline across all 3 instances.
intent: Close out distiller synthesis prereqs, fix the silent-drop production bug surfaced this morning, lay design foundation for the next big arc (Pending Items Queue) before next session, and establish per-instance QA trend baselines so future reviews can be compared.
participants:
- '[[person/Andrew Newton]]'
project:
- '[[project/Alfred]]'
outputs: []
related: []
tags:
- multi-arc
- observability
- transport
- distiller
- qa-baseline
- piq-spec
- peer-link
---

# Observability arc and queue-design foundation 2026-04-28

## Through-line

Today's nine commits cluster around making silent failures and architectural gaps visible. The triggering incident was Hypatia's 4,852-char response yesterday at 13:00 ADT being silently dropped by Telegram; that exposed a class of failure mode where the session record diverges from delivered conversation. The fix cascaded into several adjacent observability and design improvements, plus the design ratification of a longer arc (Pending Items Queue) that directly addresses the same class of problem at the architectural level.

## Commits shipped (chronological)

| Commit | Arc | Summary |
|---|---|---|
| `0cf9e01` | Synthesis Prereq 1 | V2 distiller prompt: type discrimination + confidence calibration nudge |
| `1922837` | Synthesis Prereq 2 | V2 distiller writer: attribution-audit retrofit, body+marker+audit on shadow records |
| `aa42def` | A2 (drift gate) | Distiller body-hash gate gains explicit drift_skip log line per `feedback_intentionally_left_blank.md` |
| `b96cece` | A3+A5 | Hardcoding sweep items 1-4 (`agent_slug_for` shared helper) + scaffold launch hint fix |
| `52644e3` | A4+A6 | Team docs — prompt-tuner worktree discipline + CLAUDE.md three architectural sections |
| `325728a` | Vault | Daemon-generated work + new conversation session record |
| `c0685ba` | Outbound fix | **The headline ship**: chunk + alert + annotate to fix silent-drop on >4096 char Telegram responses |
| `8f2f35f` | C polish | status:living + outputs dedup + agent_slug consolidation + _normalize_instance_name extraction |
| `8139109` | KAL-LE P0 | Surveyor state forward-compat filter + `enabled` opt-out toggle |

Plus one local-only config change: Salem's `config.yaml` adds Hypatia to `auth.tokens` and `peers` blocks. Not in git (per-instance config). Both directions transport-validated via curl HTTP 200.

## Decisions ratified

1. **Daily Sync reframe**: Daily Sync is for "what only Andrew can answer," not "verify these inferences." Distiller calibration items (which require Andrew to *judge* extracted inferences) move OUT of Daily Sync; replaced with self-audit pass on the agent side. Failed self-audits become a queue item.

2. **Pending Items Queue (PIQ) — 4 phases ratified, Phase 1 ready**: per-instance JSONL queue + Salem aggregation + bidirectional peer transport for resolution routing. Phases 1-4 from foundation through full distiller migration. ~290 LOC for Phase 1. Spec doc with all decisions locked: `project_pending_items_queue.md`.

3. **Detection mechanism: agent self-flag** (NOT post-hoc heuristic scan). Agent calls a tool at the moment of uncertainty (fuzzy match, unanswered clarifying question), with structured resolution-action plan baked into the queue entry.

4. **Peer-validation hold**: Don't build a standalone talker peer-query tool. PIQ Phase 1 uses transport directly via new endpoint handlers; that's the validation point. Andrew-as-bridge pattern remains for ad-hoc Telegram queries.

5. **QA review standard refined**: session record can diverge from delivered conversation when Telegram rejects outbound. Future reviews must look for `outbound_failures` in session frontmatter as the divergence signal. Original Hypatia QA review (this morning) had this gap; addendum appended.

## QA review trend baseline (new)

`project_talker_qa_review_log.md` established this session — a rolling per-instance log structured for trend comparison. First 3 entries:

- **Hypatia (24-36h)**: 0 process-level corrections, 2 content-level, 1 self-corrected, 6/6 mode discrimination.
- **Salem (24-36h)**: 1 process-level (Hypatia missing from peer config — closed same session), 2 content-level (factual disambiguation), 3/3 mode discrimination. Originating moment for the correction-attribution rule was Salem's 2026-04-27 16:22 session.
- **KAL-LE (all-time)**: 0 corrections in 5 sessions over 1 week. Scope discipline reflexive — boundary-holding strongest of the three.

Cross-instance finding: each instance's correction profile is shaped by its work surface, not by relative quality. Cross-instance correction-density isn't a fair metric; correction CATEGORY shape is.

## The outbound fix in detail

**The bug**: Hypatia 2026-04-28T16:00:57 UTC generated a 4,852-char response to Andrew's RRTS-clinic invoice question. Telegram's per-message limit is 4,096. HTTP 400 rejected. Talker logged a warning, persisted the response to session frontmatter AS IF DELIVERED, went silent. Andrew watched his phone for 73 minutes seeing nothing.

**The fix** (`c0685ba` — three layers):
1. **Chunking**: outbound > 3,900 chars splits at paragraph/sentence boundaries via existing `alfred.transport.utils.chunk_for_telegram` (already used by brief + daily_sync). Multi-call Telegram delivery instead of one.
2. **User-visible failure signal**: when ANY chunk fails, push a short error reply with vault session pointer. Silence becomes signal.
3. **Session-record annotation**: `outbound_failures` list in session frontmatter records every undelivered turn with structured detail (turn_index, timestamp, error, length, chunks_attempted, chunks_sent, delivered=false).

**Production validation**: same prompt that broke yesterday resent today — `outbound_chunked chunks_attempted=2 chunks_sent=2 length=4394 ok=True`. Then a 11,161-char follow-up response — `chunks_attempted=3 chunks_sent=3 ok=True`. Both delivered cleanly. Pre-fix would have been catastrophic silent drops.

## Salem ↔ Hypatia peer link

Surfaced from Salem QA review as a real config gap: Salem's `config.yaml` had KAL-LE in `transport.peers` but not Hypatia. Salem genuinely couldn't reach Hypatia — confirmed by Salem's correct response to "Can you check with Hypatia?" earlier today: "No peer route to Hypatia."

Local config change adds Hypatia (mirror of KAL-LE block). Tokens already existed in `.env`. Salem restarted, smoke-tested both directions HTTP 200. **Transport layer wired**; talker-tool layer (peer-query as ad-hoc tool) remains deferred per Andrew-as-bridge architecture decision.

This unblocks PIQ Phase 1 — the `pending_items_resolve` (Salem → peer, NEW direction) endpoint can now ride on the validated substrate.

## KAL-LE distiller-radar P0

Plan ratified 2026-04-25; P0 deliverable is "stop the bleeding" before Phase 1+ build. Today's ship (`8139109`):

**Real root cause** (different from the 3-day-old memo's diagnosis): state-path collision. KAL-LE/Hypatia configs omit `surveyor:`/`janitor:`/`distiller:`/`curator:` blocks, so all four tools' `state.path` defaults to `./data/state.json`. From Salem's working dir, that's Salem's janitor state file. Surveyor was loading it and crashing on `last_scanned` schema mismatch.

**Fix**: forward-compat filter on `**fdata` against `__dataclass_fields__` — pattern already in distiller, now extended to surveyor (FileState + ClusterState) and janitor (FileState). Plus `enabled: bool = True` toggle on DistillerConfig + PipelineConfig with orchestrator skip plumbing.

**Two P1 follow-ups filed** (deferred): tool-scoped default `state.path` per tool to prevent the underlying collision, and promote the forward-compat filter pattern to a documented standard in CLAUDE.md.

## Alfred Learnings

- **Session record vs delivered conversation can diverge.** When Telegram rejects outbound, the session record persists the response as if sent. Original 2026-04-28 Hypatia QA review missed this divergence by treating session records as ground truth. Future QA reviews must look for `outbound_failures` field in session frontmatter as the divergence signal. **Pattern**: any logged-but-not-delivered output is a category of silent failure that needs structured surfacing, not just log lines.

- **State-path collisions are a class of bug across multi-tool monorepos.** Three Alfred tools (distiller, janitor, surveyor) shared the same default `./data/state.json`. Worked correctly until per-instance configs that omit some tools' blocks tried to load whichever shape happened to be on disk. Fix landed today is forward-compat filter; cleaner fix is tool-scoped default paths. Worth flagging for any future tool that lands a state file in a shared `data/` dir.

- **Forward-compat filter as cross-tool contract.** The pattern `{k: v for k, v in fdata.items() if k in DataclassName.__dataclass_fields__}` is now validated across distiller/surveyor/janitor. Should be documented as the load-time schema-tolerance contract in CLAUDE.md. Adds zero runtime cost and protects against schema drift across tool versions.

- **"Daily Sync items only Andrew can answer" is a structural insight**, not a UX preference. Asking is easier than judging. Items requiring Andrew to *judge* an extracted inference fail because the agent has equal-or-better text-internal evidence; items requiring Andrew to *answer* (intent, real-world knowledge, his own meaning) succeed because only he has the source. The distinction is now ratified in the PIQ spec; Phase 4 implements the migration of distiller items out of Daily Sync.

- **Detection by self-flag beats heuristic detection.** Agent has the most context at the moment of uncertainty (fuzzy-match decision, clarifying question). Asking the agent to mark its own ambiguity is more accurate than scanning transcripts post-hoc for `?`-turns. Bake this into the PIQ Phase 2 design.

- **Two-layer architecture for peer messaging.** Transport substrate (HTTP, auth) is one layer; talker tool exposing it as a dispatchable capability is another. They can be independently in/out of service. Wiring the transport doesn't auto-expose it to the talker. PIQ Phase 1 specifically rides the transport directly via new endpoint handlers, bypassing the talker-tool gap.

- **Memory entries decay fast on tool/agent state.** The 2026-04-25 KAL-LE distiller-radar memo described the FileState crash as a distiller daemon issue. By 2026-04-28, the actual visible crash had shifted to surveyor (different tool, same root cause). The memo wasn't wrong at writing time, but the diagnostic shifted underneath. Always re-verify before specing a fix from a memory >1-2 days old.

- **Pushback level metadata is complexity-correlated, not defect-correlated.** Across 14 sessions on 3 instances, high pushback level (4) tracked with multi-turn analytical work. Low pushback (0-1) tracked with short tactical exchanges. Useful signal to preserve for future reviews; do NOT panic on high-pushback sessions.

- **Code-reviewer foreground is the standard.** The hook can't reliably distinguish "verbs in description of a diff" from "verbs as actions for the reviewer." False-positive blocks happen. Foreground side-steps cleanly; that's why it's the default for all editing-class agents (and review-of-edits agents).
