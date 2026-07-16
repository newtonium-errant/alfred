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

## Transport cheat-sheet (exact CLI — absolute paths, runs from ANY repo's cwd)

**Invocation — use these absolute paths verbatim.** This doc is read by agents in `alfred`,
`aftermath-rrts`, and `aftermath-lab`. The msgbus config lives in the alfred repo and `alfred` is
**not** on `$PATH`, so every command must use the **absolute binary + absolute config** — a bare
`alfred --config config.msgbus.yaml` only resolves from `/home/andrew/alfred`'s cwd and will fail
from any other repo:

```
/home/andrew/alfred/.venv/bin/alfred --config /home/andrew/alfred/config.msgbus.yaml  <subcommand …>
```

**Your project slug (`<self>`) — set your identity on every command.** `<self>` is whichever repo
THIS session runs in: one of `alfred` | `aftermath-rrts` | `aftermath-lab`. All three share the
**one** config above, whose `self_project` is `alfred`, so the slug **default is `alfred` no matter
which repo you're in.** That means: **from any repo you MUST pass your own slug explicitly** — as the
`msg inbox <self>` positional and as `--from <self>` on `msg send` and every `contract` subcommand —
or you will silently send AS `alfred` and drain `alfred`'s inbox (wrong sender, wrong mailbox).
Passing `<self>` explicitly is correct for all three repos, so **always do it** (do not rely on the
default even from the alfred session). Don't set `<self>`/`<peer>` via a shell variable — an agent's
Bash calls don't share env, so inline the literal slug in each command. `<peer>` = the project
you're coordinating with; `<cid>` = the correlation id threading the exchange.

```bash
# Drain YOUR OWN inbox as machine-readable records (INCLUDING body) — the loop surface.
/home/andrew/alfred/.venv/bin/alfred --config /home/andrew/alfred/config.msgbus.yaml \
    msg inbox <self> drain --json

# Reply to a message, routed instantly.
/home/andrew/alfred/.venv/bin/alfred --config /home/andrew/alfred/config.msgbus.yaml \
    msg send --route --from <self> --kind reply \
    --to <peer> --reply-to <message-id> --correlation-id <cid> --subject "..." --body "..."

# One-shot presence ping (used on ENTER and EXIT).
/home/andrew/alfred/.venv/bin/alfred --config /home/andrew/alfred/config.msgbus.yaml \
    msg send --route --from <self> --kind fyi --to <peer> --subject "live-mode online"

# Contracts (real-time negotiation) — see the Contracts section.
/home/andrew/alfred/.venv/bin/alfred --config /home/andrew/alfred/config.msgbus.yaml \
    contract propose --route --from <self> --to <peer> --seam <seam-slug> \
    --subject "..." --item "task-name:owner-project" [--item ...]     # prints a contract_id
/home/andrew/alfred/.venv/bin/alfred --config /home/andrew/alfred/config.msgbus.yaml \
    contract counter --route --from <self> --contract-id <contract_id> --subject "..." --item "..."
/home/andrew/alfred/.venv/bin/alfred --config /home/andrew/alfred/config.msgbus.yaml \
    contract accept  --route --from <self> --contract-id <contract_id>
```

Prose below shows commands as **sub-command fragments** (`msg …` / `contract …`) for readability —
prefix every one with the absolute invocation
(`/home/andrew/alfred/.venv/bin/alfred --config /home/andrew/alfred/config.msgbus.yaml`) and keep the
explicit `--from <self>` / `<self>` positional.

Notes on the real surface (verified against master `f9fc0fb`):
- `msg send --kind` accepts **`handover|request|fyi|reply`** only. `--reply-to` threads a reply to
  a specific message id; `--route`/`--now` are aliases for the same instant-route flag.
- **Inbound kind tolerance + malformed bounce (task #9, 2026-07-16).** On RECEIPT the router
  is TOLERANT of an *unknown* `kind` (enum drift between projects — the incident where rrts
  sent `kind: propose`): it delivers the message as `fyi` and TAGS it (`kind-drift:
  <original>→fyi`, shown in `msg inbox list`) instead of binning it. A STRUCTURALLY broken
  message (missing `to`/`from`/`correlation_id`/`created`/`subject`) is binned to
  `malformed/` AND a `reply`-kind **BOUNCE** lands in the sender's inbox ("BOUNCED
  malformed: …", body = the validation errors + binned-file pointer). `msg status` and
  `msg inbox list` surface a per-project malformed-bin count ("alfred: 0 unread (1 in
  malformed bin!)") so a routine drain can't miss a quarantined message. Valid kinds are
  unchanged: `handover|request|fyi|reply` + the contract kinds (`propose|counter|accept…`)
  dispatched to the solver.
- `msg inbox` puts the **project positional first**: pass `<self>` explicitly
  (`msg inbox <self> drain --json`). The bare `msg inbox drain --json` defaults to the config's
  `self_project` (`alfred`) — right only for the alfred agent, wrong everywhere else. Use `--json`
  only on `drain` — it emits full records with body (`list`/`read` ignore it).
- **Contracts thread on `--contract-id`, not `--reply-to`.** `contract propose` **requires
  `--seam`** (the coordination seam slug) and prints the `contract_id` that `counter`/`accept`
  reference. Do not omit `--seam`.

## ENTER — on the operator trigger "go live coordinating with `<project>`"

1. **Confirm context.** State the peer slug and the active correlation-id / contract you are
   coordinating on (or that this is a fresh thread). If the peer slug is ambiguous, confirm it
   against the registry before starting.
2. **Send the presence ping** (one-shot):
   `/home/andrew/alfred/.venv/bin/alfred --config /home/andrew/alfred/config.msgbus.yaml msg send --route --from <self> --kind fyi --to <peer> --subject "live-mode online"`.
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

1. **Drain your own inbox:** `/home/andrew/alfred/.venv/bin/alfred --config /home/andrew/alfred/config.msgbus.yaml msg inbox <self> drain --json`.
2. **If messages arrived:** for each — read → decide → if it needs a reply or handover, respond
   **instantly, routed** (prefix each fragment with the absolute invocation):
   - a normal answer: `msg send --route --from <self> --kind reply --to <peer> --reply-to <id> --correlation-id <cid> ...`
   - a contract move: a drained **`[contract]` `fyi` notice** (subject starts `[contract]`) is
     **ACTIONABLE**, not a skip-it heads-up — read the `contract_id` from its body, then run
     `contract counter --route --from <self> --contract-id <id> ...` or `contract accept --route --from <self> --contract-id <id>`.
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
   `/home/andrew/alfred/.venv/bin/alfred --config /home/andrew/alfred/config.msgbus.yaml msg send --route --from <self> --kind fyi --to <peer> --subject "live-mode off"`.
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

Contract semantics are **unchanged** — only delivery latency shrinks. But contracts do **not**
surface as raw records. A routed contract message (`propose`/`counter`/`accept`) is **dispatched to
the contract solver and archived to `routed/` — it is NEVER placed in the counterparty's inbox**, so
it does not appear in `drain --json` as a contract record. What the solver drops into each
counterparty's inbox is a **derived `[contract]` `fyi` notice**: `kind: fyi`, subject
`[contract] <seam> — <kind> → <state> (v<n>)`, body carrying `contract_id: <id>`, `seam`, `state`,
`version`, and any `gaps`. **That `fyi` notice — not a raw contract record — is what surfaces on
`drain --json`.**

**Treat a `[contract]` `fyi` as ACTIONABLE, not a heads-up.** Ordinarily `fyi` means "no reply
expected," but a `[contract]` notice is a coordination event that expects a response: read the
`contract_id` from its body and use it for your `counter`/`accept`. Do not misclassify it as a
skip-it `fyi` — that silently stalls the negotiation.

With `--route` on both the `msg send` and the contract emit paths, a propose→counter→accept exchange
converges in a **handful of ~45s ticks** instead of a handful of 5-min cron cycles:

1. You: `contract propose --route --from <self> --to <peer> --seam <seam-slug> --subject "..." --item "..."` →
   the propose is dispatched to the solver and archived; a **`[contract]` `fyi` notice** (carrying
   the `contract_id`) lands in the peer's inbox. Note the `contract_id` your CLI prints.
2. Peer's loop drains the `[contract]` fyi notice next tick (~45s) and reads the proposal +
   `contract_id` from its body.
3. Peer: `contract counter --route --from <self> --contract-id <id> ...` or `contract accept --route --from <self> --contract-id <id>`
   → routes instantly; its `[contract]` notice lands back in your inbox.
4. Your loop drains that `[contract]` notice (an accept moves the contract to a converged state),
   converges. **The operator ratifies** —
   `/home/andrew/alfred/.venv/bin/alfred --config /home/andrew/alfred/config.msgbus.yaml contract ratify <contract_id>`. Ratify stays the
   human-in-the-loop gate: propose/counter/accept are agent-side; **ratify is operator-side only.**

Once the contract is accepted (or ratified), the thread has converged → **auto-exit** per EXIT.

## Concrete walkthrough

Operator is running an **`alfred`** (Algernon dev) session and an **`aftermath-rrts`** session, and
wants them to agree a division of labor live.

1. Operator, in the alfred session: *"go live coordinating with aftermath-rrts."* → you confirm
   `<self>`=`alfred`, peer=`aftermath-rrts`, send
   `msg send --route --from alfred --kind fyi --to aftermath-rrts --subject "live-mode online"`,
   start the self-paced loop.
2. Operator, in the RRTS session: *"go live coordinating with alfred."* → the RRTS agent sends its
   own "live-mode online" ping and starts its loop. Both pings drain within a tick — both sides see
   the peer is live.
3. You propose the split:
   `contract propose --route --from alfred --to aftermath-rrts --seam labor-split --subject "split: alfred owns transport, rrts owns vault schema" --item "transport:alfred" --item "vault-schema:aftermath-rrts"`
   → dispatched + archived; a `[contract]` fyi notice (with the `contract_id`) lands in RRTS's inbox,
   which RRTS drains ~one tick later.
4. RRTS counters one line: `contract counter --route --from aftermath-rrts --contract-id <id> --subject "rrts takes schema + migration, alfred takes transport + config" --item ...`. You drain the resulting `[contract]` notice next tick and agree:
   `contract accept --route --from alfred --contract-id <id>`.
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

## Home + portability (single canonical doc + pointer-loaders — no copies)

**Canonical home:** this file, at the absolute path
`/home/andrew/alfred/.claude/live-coordination-mode.md` (tracked in the alfred repo, merged to
master). It stays here deliberately — on a single dev box with every repo under `/home/andrew/`,
one tracked source of truth that everyone points to has **zero drift**; a mirrored copy in each repo
would fork on the next edit. (Moving canonical to `aftermath-lab`, which KAL-LE curates, would work
too but buys nothing for the pointer model and adds a move + re-point, so it wasn't done.)

**Pointer-loaders (not copies).** Each participating repo carries only a thin "Cross-Project
Live-Coordination Mode" section in its own `CLAUDE.md` that names the trigger, the opt-in default,
THAT repo's project slug, and points here by absolute path:
- `alfred` → `/home/andrew/alfred/CLAUDE.md`
- `aftermath-rrts` → `/home/andrew/aftermath-rrts/CLAUDE.md`
- `aftermath-lab` → `/home/andrew/aftermath-lab/CLAUDE.md`

Because real-time needs **both** sides live, the operator triggers live-mode in each session
independently; every repo's agent loads this **same** canonical doc, so there is exactly one
convention to maintain — editing it here updates every repo at once.
