# Live-Coordination Mode (opt-in, per-session, near-real-time msgbus)

Cross-project agent-workflow convention. Any project's session agent may enter it when the
operator asks. It is **not** a vault-tool SKILL and **not** wired into any daemon — it is a mode
the running session agent performs, standing on two primitives already shipped: the **`/loop`**
skill (self-paced cadence) and the **`alfred msg` / `alfred contract` CLI** (transport, master
`f9fc0fb`). No new verb, no watcher, no merged session.

## What it is (and what stays default)

The KAL-LE msgbus routes messages between projects. **Default behavior** (unchanged, always on):
the **plain mailbox** — drain your inbox **once at session start**, then only on natural turns.
Cross-project delivery over the plain mailbox runs at the 5-min cron latency.

**Live-Coordination Mode** is a temporary upgrade to ~30–60s round-trip for the length of an
operator-requested window, then it drops back to the plain mailbox. It combines:
- **Route-on-send** — the `--route`/`--now` flag routes an outbound message to the peer's inbox
  *immediately* (skips the cron).
- **A self-paced poll-loop** — while live, you run a `/loop` (~45s cadence) that drains your inbox,
  answers any coordination message instantly, interleaves one short work-slice, and re-fires.

**Opt-in and per-session.** Live-mode is entered **only** when the operator asks, in **that**
session, and is scoped to it. Nothing persists across sessions. Ending the session or the loop
ends live-mode. There is no always-on live mode.

## Transport cheat-sheet (exact CLI — always `--config config.msgbus.yaml`)

`<self>` = your own project slug (defaults to `message_bus.self_project`, which is `alfred` in
this repo). `<peer>` = the project you are coordinating with (e.g. `aftermath-rrts`,
`aftermath-lab`). `<cid>` = the correlation id threading the exchange.

```bash
# Drain your own inbox as machine-readable records (INCLUDING body) — the loop surface.
alfred --config config.msgbus.yaml msg inbox <self> drain --json      # <self> optional; defaults to self_project

# Reply to a message, routed instantly.
alfred --config config.msgbus.yaml msg send --route --kind reply \
    --to <peer> --reply-to <message-id> --correlation-id <cid> \
    --subject "..." --body "..."

# One-shot presence ping (used on ENTER and EXIT).
alfred --config config.msgbus.yaml msg send --route --kind fyi --to <peer> --subject "live-mode online"

# Contracts (real-time negotiation) — see the Contracts section.
alfred --config config.msgbus.yaml contract propose --route --to <peer> --seam <seam-slug> \
    --subject "..." --item "task-name:owner-project" [--item ...]     # prints a contract_id
alfred --config config.msgbus.yaml contract counter --route --contract-id <contract_id> --subject "..." --item "..."
alfred --config config.msgbus.yaml contract accept  --route --contract-id <contract_id>
```

Notes on the real surface (verified against master `f9fc0fb`):
- `msg send --kind` accepts **`handover|request|fyi|reply`** only. `--reply-to` threads a reply to
  a specific message id; `--route`/`--now` are aliases for the same instant-route flag.
- `msg inbox` puts the **project positional first and optional**: `msg inbox <self> drain --json`
  and `msg inbox drain --json` both work; the bare form uses `self_project`. Use `--json` only on
  `drain` — it emits full records with body (`list`/`read` ignore it).
- **Contracts thread on `--contract-id`, not `--reply-to`.** `contract propose` **requires
  `--seam`** (the coordination seam slug) and prints the `contract_id` that `counter`/`accept`
  reference. Do not omit `--seam`.

## ENTER — on the operator trigger "go live coordinating with `<project>`"

1. **Confirm context.** State the peer slug and the active correlation-id / contract you are
   coordinating on (or that this is a fresh thread). If the peer slug is ambiguous, confirm it
   against the registry before starting.
2. **Send the presence ping** (one-shot):
   `alfred --config config.msgbus.yaml msg send --route --kind fyi --to <peer> --subject "live-mode online"`.
   This is the tell that lets the peer (and operator) see whether the other side is also live.
3. **Kick the self-paced loop.** Start a **self-paced `/loop`** bound to the RUN tick below —
   invoke `/loop` with **no interval** so the model paces itself, targeting ~45s. Self-pacing works
   by scheduling the next wake: to **continue**, schedule the next wake ~45s out; to **exit**,
   **omit** the next wake and the loop ends. That is precisely how the agent self-exits.
   **Do not use a hard `/loop 45s`** — a fixed, operator-stop-only interval cannot self-exit, so
   convergence-auto-exit and idle-timeout would become impossible and a forgotten loop would run
   until the operator kills it.

Initialize a **consecutive-empty-drain counter = 0** in your working notes (used by idle-timeout).

## RUN — one light, non-blocking tick per wake

1. **Drain your own inbox:** `alfred --config config.msgbus.yaml msg inbox <self> drain --json`.
2. **If messages arrived:** for each — read → decide → if it needs a reply or handover, respond
   **instantly, routed**:
   - a normal answer: `msg send --route --kind reply --to <peer> --reply-to <id> --correlation-id <cid> ...`
   - a contract move: `contract counter --route --contract-id <id> ...` or `contract accept --route --contract-id <id>`.
   Reset the empty-drain counter to 0.
3. **If the drain was empty:** do **ONE short, interruptible** slice of real project work, then let
   the loop re-fire. Increment the empty-drain counter.
4. **Re-schedule** (continue) unless an EXIT condition below is met.

### Work-slice discipline (load-bearing — state it plainly)

**A work-slice inside live-mode MUST be short and interruptible.** The ~45s round-trip target holds
only if each tick finishes before the next one fires. A long slice (e.g. a 10-minute refactor)
delays the next drain and **silently blows the latency target** — the peer's routed message sits
unread in your inbox until your slow tick finally drains it. So: **defer any multi-minute task
until live-mode exits.** If a task can't be chopped into <~45s interruptible pieces, don't start it
while live — note it and pick it up after exit.

This is **agent-discipline, not code-enforced.** Nothing in the CLI bounds your slice length or
counts your empty drains for you; the mode works only if you hold this discipline yourself.

## EXIT — any one of these

- **Operator says so** — "done" / "exit live mode" → stop the loop.
- **Thread converges** — the contract is accepted/ratified, or the reply thread closes (nothing
  left to answer) → **auto-exit and report** the outcome to the operator.
- **Idle timeout** — **~13 consecutive empty drains (≈10 min at ~45s)** → **auto-drop to the plain
  mailbox** with a one-line notice (e.g. "live-mode idle 10 min — dropping to plain mailbox"), so a
  forgotten loop can't run forever.

**On exit (every path):**
1. Send a final presence ping:
   `alfred --config config.msgbus.yaml msg send --route --kind fyi --to <peer> --subject "live-mode off"`.
2. Do **one last drain** so nothing that landed on the final tick is missed.
3. **Omit the next wake** — the self-paced loop ends here — and revert to plain-mailbox behavior
   (drain only on natural turns for the rest of the session).

## Both-sides kickoff + graceful degradation

Real-time back-and-forth needs **BOTH** agents in live-mode. The operator issues the trigger in
**each** session independently — "go live coordinating with `<peer>`" in your session, and the same
in the peer's session. Two independent scoped sessions each run their own loop; **the projects
never merge** (no shared coordination room — that path stays rejected).

**If only one side is live, nothing breaks — it just degrades to today's latency for the other
side:**
- Your outbound `--route` message **still delivers instantly** — it lands as an unread file in the
  peer's inbox the moment you send. Delivery already happened; the cron is irrelevant to it.
- But a **non-live** peer only **reads** it on **its own next natural turn / session-start drain**
  (plain-mailbox latency). **Zero message loss** — asymmetric, never broken; the worst case is
  exactly today's latency for the non-live side (a strict superset, never a regression).
- The **presence ping is the tell.** If the peer does not ack (no reply/message back) within **~2
  ticks** of your "live-mode online" ping, surface to the operator:
  **"peer `<peer>` not in live-mode — messages land but they read on their own turn."** The operator
  can then kick the peer's session off with the same trigger.

## Contracts ride on top (real-time negotiation)

Contract semantics are **unchanged** — only delivery latency shrinks. Contract messages arrive as
ordinary kind-tagged records in the same `drain --json` surface, and the router already dispatches
them to the contract solver. With `--route` on both the `msg send` and the contract emit paths, a
propose→counter→accept exchange converges in a **handful of ~45s ticks** instead of a handful of
5-min cron cycles:

1. You: `contract propose --route --to <peer> --seam <seam-slug> --subject "..." --item "..."` →
   routes instantly → lands in the peer's inbox. Note the printed `contract_id`.
2. Peer's loop drains it next tick (~45s), surfaces the proposal.
3. Peer: `contract counter --route --contract-id <id> ...` or `contract accept --route --contract-id <id>`
   → instant back to you.
4. Your loop drains it, converges. **The operator ratifies** —
   `alfred --config config.msgbus.yaml contract ratify <contract_id>`. Ratify stays the
   human-in-the-loop gate: propose/counter/accept are agent-side; **ratify is operator-side only.**

Once the contract is accepted (or ratified), the thread has converged → **auto-exit** per EXIT.

## Concrete walkthrough

Operator is running an **`alfred`** (Algernon dev) session and an **`aftermath-rrts`** session, and
wants them to agree a division of labor live.

1. Operator, in the alfred session: *"go live coordinating with aftermath-rrts."* → you confirm
   peer=`aftermath-rrts`, send `msg send --route --kind fyi --to aftermath-rrts --subject "live-mode online"`,
   start the self-paced loop.
2. Operator, in the RRTS session: *"go live coordinating with alfred."* → the RRTS agent sends its
   own "live-mode online" ping and starts its loop. Both pings drain within a tick — both sides see
   the peer is live.
3. You propose the split:
   `contract propose --route --to aftermath-rrts --seam labor-split --subject "split: alfred owns transport, rrts owns vault schema" --item "transport:alfred" --item "vault-schema:aftermath-rrts"`
   → routes instantly; RRTS drains it ~one tick later.
4. RRTS counters one line: `contract counter --route --contract-id <id> --subject "rrts takes schema + migration, alfred takes transport + config" --item ...`. You drain it next tick and agree:
   `contract accept --route --contract-id <id>`.
5. Both loops see "thread converged" → each **auto-exits**, sends a final "live-mode off" `fyi`,
   does a last drain, and reports the agreed split to its operator.
6. Operator **ratifies** the contract. Total wall-clock: well under two minutes vs. ~15+ min over
   the bare cron. Between ticks, each agent ran short real-work slices — live-mode interleaved, it
   did not idle-wait.

## What this is NOT

- **Not always-on.** Opt-in per session, auto-times-out on idle, never persists.
- **Not a merged session.** Projects stay independent scoped sessions; no shared room.
- **Not agent-to-agent addressing.** Messages remain project-to-project (`--to <project>`).
- **Not cross-machine.** Single dev-box, shared spool on one filesystem.
- **Not code-enforced.** Work-slice bounding and the empty-drain count are agent-discipline, held by
  you in this doc — not guardrails in the CLI. Acceptable for PHI-free single-box dev tooling;
  stated plainly so it isn't mistaken for a hardened mechanism.

## Home + portability

This doc is the canonical copy for the `alfred` repo. It is loaded on-trigger via the pointer in
`CLAUDE.md` ("Cross-Project Live-Coordination Mode"). Because real-time needs **both** sides,
**each participating project should mirror this convention into its own repo's agent instructions**
(the msgbus registry lists `alfred`, `aftermath-rrts`, `aftermath-lab` as separate repos, each with
its own `.msgbus/inbox`). Cross-project canonicalization (a single shared copy in `aftermath-lab`,
which KAL-LE curates) is a team-lead follow-up, not part of this ship.
