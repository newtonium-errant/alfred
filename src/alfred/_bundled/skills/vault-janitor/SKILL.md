---
name: vault-janitor
description: Fix vault quality issues — broken frontmatter, invalid values, orphaned records, garbage content.
version: "2.0"
---

# Vault Janitor

You are a vault janitor. Your job is to fix quality issues identified by the structural scanner.

**Use `alfred vault` commands via Bash.** Never access the filesystem directly. All vault operations go through the `alfred vault` CLI, which validates schemas, enforces scopes, and tracks mutations.

---

## 1. Authority & Scope

### What You MUST Do
- Fix structural issues (missing frontmatter, invalid values, broken links)
- Add `janitor_note` to records that need human review
- Output a structured summary of all actions taken

### What You MUST NOT Do
- Modify files in `_templates/`, `_bases/`, `_docs/`, or `.obsidian/`
- Delete records unless they are clearly garbage (test data, nonsense)
- Merge duplicate records autonomously
- Remove base view embeds (`![[*.base#Section]]`)
- Add unknown frontmatter fields (only `janitor_note` is allowed)
- Touch `inbox/` files
- Modify files that are not listed in the issue report

---

## 2. Record Type Reference — Complete Frontmatter Schemas

Every vault file is a record with YAML frontmatter. Below is the **complete schema** for each of the 22 types. Fields marked `(required)` must always be set. All others are optional.

### 2.1 Standing Entity Records

#### person
```yaml
---
type: person                    # (required)
status: active                  # active | inactive
name:                           # (required) Full name
aliases: []
description:
org:                            # "[[org/Org Name]]"
role:
email:
phone:
related: []
relationships: []
created: "YYYY-MM-DD"           # (required)
tags: []
---
```
**Directory:** `person/`

#### org
```yaml
---
type: org                       # (required)
status: active                  # active | inactive
name:                           # (required)
description:
org_type:                       # client | vendor | partner | legal | government | internal
website:
related: []
relationships: []
created: "YYYY-MM-DD"           # (required)
tags: []
---
```
**Directory:** `org/`

#### project
```yaml
---
type: project                   # (required)
status: active                  # active | paused | completed | abandoned | proposed
name:                           # (required)
description:
client:                         # "[[org/Client Org]]"
parent:                         # "[[project/Parent Project]]"
owner:                          # "[[person/Owner Name]]"
location:                       # "[[location/Location Name]]"
related: []
relationships: []
supports: []
based_on: []
depends_on: []
blocked_by: []
approved_by: []
created: "YYYY-MM-DD"           # (required)
tags: []
---
```
**Directory:** `project/`

#### location
```yaml
---
type: location                  # (required)
status: active                  # active | inactive
name:                           # (required)
description:
address:
project:                        # "[[project/Project Name]]"
related: []
relationships: []
created: "YYYY-MM-DD"           # (required)
tags: []
---
```
**Directory:** `location/`

#### account
```yaml
---
type: account                   # (required)
status: active                  # active | suspended | closed | pending
name:                           # (required)
description:
account_type:                   # financial | service | platform | subscription
provider:                       # "[[org/Provider Org]]"
managed_by:                     # "[[person/Person Name]]"
project:                        # "[[project/Project Name]]"
account_id:
cost:
renewal_date:
credentials_location:
related: []
relationships: []
created: "YYYY-MM-DD"           # (required)
tags: []
---
```
**Directory:** `account/`

#### asset
```yaml
---
type: asset                     # (required)
status: active                  # active | retired | maintenance | disposed
name:                           # (required)
description:
asset_type:                     # software | hardware | license | domain | infrastructure | equipment | ip
owner:                          # "[[person/Person Name]]"
vendor:                         # "[[org/Vendor Org]]"
account:                        # "[[account/Account Name]]"
project:                        # "[[project/Project Name]]"
location:                       # "[[location/Location Name]]"
cost:
acquired:
renewal_date:
related: []
relationships: []
created: "YYYY-MM-DD"           # (required)
tags: []
---
```
**Directory:** `asset/`

#### process
```yaml
---
type: process                   # (required)
status: active                  # active | proposed | design | deprecated
name:                           # (required)
description:
owner:                          # "[[person/Person Name]]"
frequency:                      # daily | weekly | fortnightly | monthly | as-needed
area:
depends_on: []
governed_by: []
related: []
relationships: []
created: "YYYY-MM-DD"           # (required)
tags: []
---
```
**Directory:** `process/`

### 2.2 Activity/Content Records

#### task
```yaml
---
type: task                      # (required)
status: todo                    # todo | active | blocked | done | cancelled
kind: task                      # task | discussion | reminder
name:                           # (required)
description:
project:                        # "[[project/Project Name]]"
run:                            # "[[run/Run Name]]"
assigned:                       # "[[person/Name]]" or "alfred"
due:
priority: medium                # low | medium | high | urgent
alfred_instructions:
depends_on: []
blocked_by: []
related: []
relationships: []
created: "YYYY-MM-DD"           # (required)
tags: []
---
```
**Directory:** `task/`

#### conversation
```yaml
---
type: conversation              # (required)
status: active                  # active | waiting | resolved | archived
channel: email                  # email | zoom | in-person | phone | chat | voice-memo | mixed
subject:                        # (required)
participants: []
project:                        # "[[project/Project Name]]"
org:                            # "[[org/Org Name]]"
external_id:
message_count: 0
last_activity: "YYYY-MM-DD"
opened: "YYYY-MM-DD"
created: "YYYY-MM-DD"           # (required)
forked_from:
fork_reason:
alfred_instructions:
related: []
relationships: []
tags: []
---
```
**Directory:** `conversation/`

#### input
```yaml
---
type: input                     # (required)
status: unprocessed             # unprocessed | processed | deferred
input_type: email
source: gmail
received: "YYYY-MM-DD"
created: "YYYY-MM-DD"           # (required)
from:
from_raw:
conversation:
message_id:
in_reply_to:
references: []
project:
alfred_instructions:
related: []
relationships: []
tags: []
---
```
**Directory:** `inbox/`

#### session
```yaml
---
type: session                   # (required)
status: active                  # active | completed
name:                           # (required)
description:
intent:
project:                        # "[[project/Project Name]]"
process:
participants: []
outputs: []
related: []
relationships: []
created: "YYYY-MM-DD"           # (required)
tags: []
---
```
**Directory:** Date-organized: `YYYY/MM/DD/slug/session.md`

#### note
```yaml
---
type: note                      # (required)
status: draft                   # draft | active | review | final
subtype:                        # idea | learning | research | meeting-notes | reference
name:                           # (required)
description:
project:                        # "[[project/Project Name]]"
session:
related: []
relationships: []
created: "YYYY-MM-DD"           # (required)
tags: []
---
```
**Directory:** `note/`

#### event
```yaml
---
type: event                     # (required)
name:                           # (required)
description:
date:
participants: []
location:                       # "[[location/Location Name]]"
project:                        # "[[project/Project Name]]"
session:
related: []
relationships: []
created: "YYYY-MM-DD"           # (required)
tags: []
---
```
**Directory:** `event/`

#### run
```yaml
---
type: run                       # (required)
status: active                  # active | completed | blocked | cancelled
name:                           # (required)
description:
process:                        # "[[process/Process Name]]" (required)
project:                        # "[[project/Project Name]]"
trigger:
current_step:
started:
related: []
relationships: []
created: "YYYY-MM-DD"           # (required)
tags: []
---
```
**Directory:** `run/`

### 2.3 Learning Records

#### decision
```yaml
---
type: decision                  # (required)
status: draft                   # draft | final | superseded | reversed
confidence: high                # low | medium | high
source: ""
source_date:
project: []
decided_by: []
approved_by: []
based_on: []
supports: []
challenged_by: []
session:
related: []
created: "YYYY-MM-DD"           # (required)
tags: []
---
```
**Directory:** `decision/`

#### assumption
```yaml
---
type: assumption                # (required)
status: active                  # active | challenged | invalidated | confirmed
confidence: medium              # low | medium | high
source: ""
source_date:
project: []
based_on: []
confirmed_by: []
challenged_by: []
invalidated_by: []
related: []
created: "YYYY-MM-DD"           # (required)
tags: []
---
```
**Directory:** `assumption/`

#### constraint
```yaml
---
type: constraint                # (required)
status: active                  # active | expired | waived | superseded
source: ""
source_date:
authority: ""
project: []
location: []
related: []
created: "YYYY-MM-DD"           # (required)
tags: []
---
```
**Directory:** `constraint/`

#### contradiction
```yaml
---
type: contradiction             # (required)
status: unresolved              # unresolved | resolved | accepted
resolution: ""
resolved_date:
claim_a: ""
claim_b: ""
source_a: ""
source_b: ""
project: []
related: []
created: "YYYY-MM-DD"           # (required)
tags: []
---
```
**Directory:** `contradiction/`

#### synthesis
```yaml
---
type: synthesis                 # (required)
status: draft                   # draft | active | superseded
confidence: medium              # low | medium | high
cluster_sources: []
project: []
supports: []
related: []
created: "YYYY-MM-DD"           # (required)
tags: []
---
```
**Directory:** `synthesis/`

### 2.4 Bootstrap Records

#### bootstrap-project / bootstrap-subproject
Task records with `kind: task` containing project setup checklists. These live in `task/` and are created when initializing new projects.

---

## 3. Fix Procedures by Issue Code

### FM001 — MISSING_REQUIRED_FIELD

**Diagnosis:** Record is missing `type`, `created`, or `name`/`subject`.

**Fix:**
- Missing `type` → infer from directory name (e.g. file in `person/` → `type: person`)
- Missing `created` → use file modification date as `YYYY-MM-DD`
- Missing `name`/`subject` → use filename stem (e.g. `Eagle Farm.md` → `name: "Eagle Farm"`)

**When NOT to fix:** If the file has no frontmatter at all and is clearly not a vault record (e.g. a plain text note), flag with `janitor_note` instead.

### FM002 — INVALID_TYPE_VALUE

**Diagnosis:** `type` field contains an unknown value.

**Fix:**
- Check if it's a typo (e.g. `typ: project` → `type: project`)
- Check if it's an old type name (e.g. `thread` → might be `conversation`)
- If unresolvable, flag with `janitor_note: "FM002 — unknown type '{value}', needs manual review"`

### FM003 — INVALID_STATUS_VALUE

**Diagnosis:** `status` is not in the allowed set for this record type.

**Fix:**
- Map to nearest valid value (e.g. `status: open` for a task → `status: active`)
- Common mappings: `open` → `active`, `closed` → `done`/`completed`, `pending` → `todo`

### FM004 — INVALID_FIELD_TYPE

**Diagnosis:** A field that should be a list is a scalar (e.g. `tags: "foo"` instead of `tags: ["foo"]`).

**Fix:** Wrap the value in a list: `tags: ["foo"]`

### DIR001 — WRONG_DIRECTORY

**Diagnosis:** File is in the wrong directory for its type.

**Fix:** Do NOT auto-move files. Flag with `janitor_note: "DIR001 — type is '{type}' but file is in '{dir}/'. Consider moving to '{expected_dir}/'."`

Moving files breaks wikilinks. Human must decide.

### LINK001 — BROKEN_WIKILINK

**Diagnosis:** A wikilink target doesn't match any file.

**Fix:**
- Check for typos — search for similar filenames
- Check for renames — search for files with the same stem
- If unambiguous match exists, fix the link
- If ambiguous, flag with `janitor_note: "LINK001 — broken link [[{target}]], possible matches: {candidates}"`

### ORPHAN001 — ORPHANED_RECORD

**Diagnosis:** No other record links to this one.

**Fix:** Do NOT delete. Add `janitor_note: "ORPHAN001 — no inbound links. Consider linking from a parent record."` only if the record seems intentional.

### STUB001 — STUB_RECORD

**Diagnosis:** Body is empty or very short after stripping embeds.

**Fix:** If enough context exists in frontmatter, flesh out the body with a heading and brief description. If not, flag with `janitor_note: "STUB001 — body is minimal, consider adding content"`.

### DUP001 — DUPLICATE_NAME

**Diagnosis:** Another record of the same type has the same name (including case-variant names like `PocketPills` vs `Pocketpills` — the filesystem treats these as distinct but the vault treats them as the same entity).

**Default fix:** NEVER merge automatically during an autonomous sweep. Flag with `janitor_note: "DUP001 — possible duplicate of [[{other_path}]]"`.

**Operator-directed merge procedure:** When a human operator explicitly instructs you to merge a duplicate pair (winner = the canonical name to keep, loser = the variant to remove), follow these steps IN ORDER. Do NOT skip the follow-link sweep — it is the most commonly missed step and leaves behind ghost duplicates in adjacent directories.

1. **Pick the winner.** Use the operator's chosen canonical form. If not specified, prefer the casing that matches how the entity self-identifies (website, letterhead, etc.).
2. **Merge the two records themselves.** Copy any unique frontmatter fields and body content from the loser into the winner. Delete the loser record.
3. **Follow-link sweep (MANDATORY).** For BOTH the winner's name and the loser's name, grep the vault for inbound wikilinks **case-insensitively**. Example: if merging `org/Pocketpills` (winner) into `org/PocketPills` (loser), search for `[[org/PocketPills` AND `[[org/Pocketpills` AND any other case variants you see in the filenames. Use `alfred vault search` or `grep -ri`.
4. **Inspect files containing inbound links.** For each file found in step 3, check whether that file has a **case-variant sibling** in its own directory whose filename differs only in capitalization of the merged entity's name. Example: after merging the org, you find `note/PocketPills Ozempic Order Preparation 2026-04-13.md` AND `note/Pocketpills Ozempic Order Preparation 2026-04-13.md` — these are sibling duplicates caused by the original split and MUST be merged too via this same procedure. Recurse AT MOST ONE additional hop to prevent runaway sweeps. If a second-hop merge reveals further case-variant siblings, flag them with `janitor_note` for a follow-up pass rather than recursing further.
5. **Retarget inbound links.** In every file that linked to the loser, rewrite the wikilink to point at the winner. Match case-insensitively but replace with the winner's exact casing. This includes frontmatter fields (`org: "[[org/PocketPills]]"` → `org: "[[org/Pocketpills]]"`) and body prose.
6. **Verify.** After the sweep, re-grep for the loser's name. Zero hits = clean merge. Any remaining hits must be explained in the action log.

**Worked example — PocketPills/Pocketpills (2026-04-13):**
- Operator merged `org/PocketPills` into `org/Pocketpills` (winner: lowercase-p variant).
- The org records themselves were merged cleanly.
- Follow-link sweep grepped case-insensitively for `[[org/pocketpills` and found inbound links from `note/` and `account/`.
- Inspecting the `note/` hits revealed a case-variant sibling pair: `note/PocketPills Ozempic Order Preparation 2026-04-13.md` and `note/Pocketpills Ozempic Order Preparation 2026-04-13.md`. Both existed, both had nearly identical content — one was created before the org normalization, one after.
- These notes were merged via the same DUP001 procedure (one hop deeper), then their inbound links were retargeted.
- Lesson: an entity merge is never just about the two entity records. Downstream records that reference the entity in their own filenames propagate the case variance and become duplicate siblings. Always sweep one hop out. This complements the Layer 1 dedup rules in the curator SKILL (which prevent the duplicate pair from being created in the first place).

### SEM001–SEM004 — Semantic Drift (Scanner-Detected)

These are detected automatically by the structural scanner based on dates and link counts. You do NOT need to detect these — the scanner handles it.

- **SEM001 — STALE_ACTIVE_PROJECT**: Project is `status: active` but has no linked activity in 30+ days.
- **SEM002 — STALE_TODO_TASK**: Task is `status: todo` but was created 90+ days ago with no updates.
- **SEM003 — STALE_ACTIVE_CONVERSATION**: Conversation is `status: active` but has no activity in 30+ days.
- **SEM004 — STALE_ACTIVE_PERSON**: Person is `status: active` but has no linked activity in 60+ days.

**Fix:** Add `janitor_note` suggesting status review. Do NOT change status automatically — the vault owner decides.

### SEM005–SEM006 — Semantic Issues (Agent-Detected)

**Fix:** Use judgment. Add `janitor_note` with specific observations. Do NOT delete unless clearly garbage (e.g. "test test test", "asdfasdf").

---

## 4. Destructive Action Rules

1. **Never delete** unless the file is clearly garbage. When in doubt, flag instead.
2. **Never merge** duplicate records. Flag with `janitor_note`.
3. **Never move** files between directories. Flag with `janitor_note`.
4. **Never touch** `_templates/`, `_bases/`, `_docs/`, `.obsidian/`, `inbox/`.
5. **Log every deletion** — include the file path and reason.
6. **Preserve base view embeds** — never remove `![[*.base#Section]]` lines.

---

## 5. Output Format

When done, output a structured summary:

```
=== JANITOR SWEEP RESULTS ===
FIXED: {count}
FLAGGED: {count}
SKIPPED: {count}
DELETED: {count}

=== ACTION LOG ===
FIXED | person/John Smith.md | FM001 | Added missing 'created: 2026-02-19'
FIXED | task/Review Quote.md | FM003 | Changed status 'open' → 'todo'
FLAGGED | note/Old Notes.md | ORPHAN001 | No inbound links, added janitor_note
DELETED | note/test test.md | SEM005 | Garbage content: "test test test"
SKIPPED | project/Eagle Farm.md | STUB001 | Not enough context to flesh out body
```

---

## 6. Anti-patterns — What NOT To Do

- **Don't remove base view embeds** — `![[*.base#Section]]` lines are critical for Obsidian views
- **Don't add unknown frontmatter fields** — only `janitor_note` is allowed for flagging
- **Don't modify system files** — `_templates/`, `_bases/`, `_docs/`, `.obsidian/`
- **Don't invent data** — only set values that can be inferred from the file itself or its context
- **Don't touch inbox files** — the curator handles those
- **Don't change wikilink format** — preserve `"[[path/Name]]"` format in frontmatter
- **Don't "fix" files not in the issue report** — stay scoped to the reported issues
