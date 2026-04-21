---
type: note
subtype: draft
project: ["[[project/Alfred]]"]
created: '2026-04-21'
intent: Upstream contribution report for ssdavidai/alfred — draft, awaiting Andrew's review
status: draft
tags: [upstream, contribution, writing]
---

# Reply 6 — Voice Stage 2b: capture mode

**Problem shape.** Telegram voice message + Groq Whisper + Claude turn-by-turn (our Stage 2a) is good for journaling and task execution. It's wrong for long continuous brainstorming. Every voice note triggers a full LLM turn, which means every note comes back with an assistant reply — breaks the flow when you're just thinking out loud for ten minutes. The right shape is "capture silently, give me a structured output on demand."

**Solution shape.** A new `capture` session type alongside the existing `note / task / journal / article / brainstorm` set.

- **`pushback_level=0`** (silent — the model never proactively challenges).
- **`supports_continuation=False`** (each capture session is its own record).
- **Router entry:** deterministic `capture:` prefix short-circuits to capture without hitting the LLM classifier. Natural-language cues ("let me brainstorm", "thinking out loud", "ramble") route via the classifier.

**Mid-session behaviour.** `conversation.run_turn` short-circuits when `session_type == "capture"`: appends the user turn, skips the LLM call, skips escalation detection, returns a `CAPTURE_SENTINEL`. The bot recognizes the sentinel and posts a per-message emoji reaction (✔) via Telegram's `set_message_reaction` API instead of a text reply. No assistant turn, just a receipt. User keeps talking.

Slash commands (`/opus`, `/end`, `/status`, `/brief`, `/extract`) still fire during capture. `/opus` in capture applies to the post-`/end` batch pass.

**On `/end`.** Session close triggers an async `asyncio.create_task` batch structuring pass. One Sonnet call, tool-use enforced, returns:

```python
{
    "topics": list[str],          # max 8
    "decisions": list[str],
    "open_questions": list[str],
    "action_items": list[str],
    "key_insights": list[str],    # high-confidence only
    "raw_contradictions": list[str],
}
```

Rendered as markdown under a `## Structured Summary` section wrapped in `<!-- ALFRED:DYNAMIC -->` markers, injected *above* the raw transcript in the session record. `capture_structured: true` frontmatter flag. `/end` returns fast; the structured output arrives as a follow-up Telegram message.

**`/extract <short-id>`.** Opt-in note derivation from a captured session. Max 8 derived notes per capture (distiller dedupes downstream). Each: `created_by_capture: true`, `source_session: [[session/...]]`, `confidence_tier: high|medium`. Session's `derived_notes` list populated. Idempotent: re-running after success replies "Already extracted N notes. Delete first to re-run."

**`/brief <short-id>`.** Compress the structured summary to ~300 words prose via Sonnet, then ElevenLabs Turbo v2.5 TTS, delivered as a Telegram voice note. Default voice Rachel. Failure fallbacks: text reply if the API is down, document upload if audio exceeds 50 MB.

**Calibration still fires at session close** even for capture — fed by the structured summary rather than by turn-by-turn transcript. This was the one non-obvious decision: the user's calibration-relevant signals (direction changes, sensitivities, preferences) show up in capture sessions same as anywhere else.

**Tradeoffs / what we rejected.**

- **One-shot "(capturing)" ack** vs per-message reaction. Rejected the one-shot because it's ambiguous when the model's behavior is silence — the user can't tell the message landed. Per-message reaction is cheap signal.
- **Auto-extract on `/end`.** Rejected. User should decide whether this ramble is worth N derived notes. `/extract` is explicit.
- **LLM during capture for "useful interruptions"** (gotcha catching, redirect prompts). Rejected — breaks the flow this mode exists to preserve. If the user wants that shape, they pick `journal` or `brainstorm`.
- **Building the `alfred_instructions` watcher in the same arc.** Rejected as scope creep; shipped separately (see Reply 3).

**Cost profile (10-min capture session).** STT (Groq Whisper) ~$0.007, Sonnet batch pass ~$0.03 (Opus ~$0.16), ElevenLabs TTS Turbo v2.5 ~$0.30-0.50. Monthly at daily use ~$9-15, TTS dominant.

**Commit range.** `cefe063..c70e81e` (7 commits, bundled session note across all of them per our session-notes-per-commit rule).

Would love to hear how this echoes (or doesn't) in your thinking — especially the silent-during-capture-then-summarize shape vs real-time assistant-interjection during brainstorming.
