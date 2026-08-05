# Board snooze — interaction options for the pick

**Status: awaiting operator pick. No snooze UI is built yet** — #14 is design-first
by ruling, and this is the artifact that ruling asks for. Pick one of A / B / C
(or reject all three) and the FE slice follows.

---

## What is already settled — so the pick is only about the interaction

Four things are decided and none of them are on the table here:

| Settled | What it means for the mock |
|---|---|
| **Skip and snooze are BOTH explicitly labelled** (ruled) | No control may do both. No "swipe left means one of them." Two visible verbs, two different words. |
| **Delta-breakthrough urgency** (ratified) | A row snoozed *while already overdue* stays quiet for the full duration. Only NEW urgency breaks through — the backend names which: `crossed_due` or `moved_earlier`. |
| **Three fixed durations** | `snooze_1d`, `snooze_3d`, `snooze_7d`. An explicit until-a-date picker is v2, not v1. |
| **Backend is shipped and inert** | `alfred.tier.snooze` exists with `add_snooze` / `remove_snooze` / `is_snoozed` / `breakthrough_reason`. It needs `tier.snooze.path` wired on the box at deploy; nothing here changes that. |

One consequence worth stating plainly, because it constrains all three options:
**`Skip` already exists** on the slot card (`rejectLabel: 'Skip'`, `rejectParks:
true`). So this is not "add a verb to an empty row" — it is "add a second,
adjacent verb that must never be confused with the first."

The distinction the operator has to be able to feel:

- **Skip** — not this one. A judgement about the *suggestion*.
- **Snooze** — yes, but not now. A judgement about the *timing*.

---

## Option A — action row (three buttons inline)

The card's action row carries the durations directly.

```
┌────────────────────────────────────────────┐
│ Pay the water bill                    T1   │
│ routine/Bills.md · due today               │
│                                            │
│ [ Take it ]  [ Skip ]                      │
│ Snooze:  [ 1d ]  [ 3d ]  [ 7d ]            │
└────────────────────────────────────────────┘
```

**For.** One tap to a snoozed row — the fastest path of the three, and snoozing is
a *high-frequency, low-deliberation* act. Every option is visible, so there is
nothing to discover. `Skip` and `Snooze` are separated by label and by row, which
is exactly what the ruling asks for. It is also what the backend author already
assumed: the duration ladder's own comment says three fixed buttons exist to
*"keep the card's action row from becoming a menu."*

**Against.** Five controls on a card that had two. On a phone that is a crowded
row, and the durations are visually equal in weight to `Take it` — arguably the
most important verb on the card. Risks making every row look like a decision with
five branches when usually it is one.

---

## Option B — long-press

Tap does nothing new. A long-press on the row opens a small duration sheet.

```
   (long-press the row)
        ↓
┌────────────────────────────────────────────┐
│  Snooze "Pay the water bill"               │
│                                            │
│    Tomorrow            (1d)                │
│    In three days       (3d)                │
│    Next week           (7d)                │
│                                            │
│    Cancel                                  │
└────────────────────────────────────────────┘
```

**For.** Zero added visual weight — the card is untouched. The sheet has room for
plain-language labels ("Next week" rather than "7d"), which read better than a
bare ladder.

**Against, and this is the strong one: long-press is a poor fit for mobile web.**
It collides with the browser's own text-selection and context-menu gestures, and
suppressing that reliably across Android Chrome and iOS Safari is fiddly and
easy to regress. It is also **undiscoverable** — nothing on screen says the
gesture exists, so a feature the operator forgets is a feature that is not there.
And it gives snooze *no visible label at all*, which sits badly against a ruling
whose point is that both verbs be explicit.

---

## Option C — one Snooze button that opens a duration menu

The action row gains a single `Snooze` next to `Skip`; the durations live one tap
deeper.

```
┌────────────────────────────────────────────┐        ┌──────────────────┐
│ Pay the water bill                    T1   │        │  Tomorrow        │
│ routine/Bills.md · due today               │   →    │  In three days   │
│                                            │        │  Next week       │
│ [ Take it ]  [ Skip ]  [ Snooze ▾ ]        │        └──────────────────┘
└────────────────────────────────────────────┘
```

**For.** Three verbs, three words, all visible — the cleanest possible reading of
"both explicitly labelled." The card stays legible; the durations are present but
not shouting. Extends to the v2 until-a-date picker without redesign: it becomes a
fourth item in a menu that already exists.

**Against.** Two taps to snooze, every time, for the common case. A menu is more
machinery than a three-item choice really warrants, and it introduces
open/close/dismiss state that the other two do not have.

---

## Comparison on the axes that actually differ

| | A · action row | B · long-press | C · menu |
|---|---|---|---|
| Taps to snooze | **1** | 2 (+ gesture) | 2 |
| Snooze visibly labelled | yes | **no** | yes |
| Discoverable without being told | yes | **no** | yes |
| Card visual weight added | **high** (5 controls) | none | low (1 control) |
| Mobile-web gesture risk | none | **real** | none |
| **Cost of a mis-tap** | **high** — Skip is adjacent | low | **low** — Skip is a menu away |
| Room for plain-language durations | poor (`1d`/`3d`/`7d`) | good | good |
| Path to the v2 date picker | needs redesign | fits | **fits** |

### Mis-tap cost deserves its own paragraph, because it is asymmetric

Not all mis-taps are equal, and the difference is *what a control's neighbours
are* — the same reasoning the scope matrix applies to capabilities, applied to
gestures.

A mis-tap **inside** the duration ladder is cheap and self-correcting: you meant
3d, you got 7d, the row comes back a few days late and you can un-snooze it. Wrong
by days.

A mis-tap **between Skip and a duration** is a different kind of error. Skip and
snooze are near-opposites — *not this one* versus *yes, but later* — and Skip
**parks** the suggestion (`rejectParks: true`). So the slip doesn't give you the
wrong timing, it gives you the wrong *answer*, with a durable consequence, and the
row does not come back to tell you.

That is exactly the adjacency Option A creates: on a phone, `Skip` sits one
thumb-width from `1d`. Option C puts a menu boundary between the destructive verb
and the harmless ones — you cannot fat-finger from `Skip` into `7d`. Option B has
no adjacency problem at all (it adds nothing to the card), but trades it for the
gesture misfiring instead: a long-press the browser reads as text selection, or a
scroll that lands as a press.

This axis is the strongest single argument for C over A, and it is worth weighing
against A's one-tap speed rather than assuming speed wins.

---

## Recommendation: **C**, with A as the fallback

C is the only option that satisfies the ruling's spirit at full strength — three
verbs, three words, nothing hidden behind a gesture — while leaving the card
readable and giving v2's date picker somewhere obvious to land.

**A is a legitimate pick** and I would build it without complaint if the operator
weights one-tap snoozing above card calm; it is also what the backend comment
anticipated. The question that decides between them is genuinely a preference I
cannot settle from the code: *is snooze frequent enough to deserve a permanent
row of its own?* If the answer is yes, A. If it is "a few times a week," C.

**B I would argue against being picked**, and would want a specific instruction
before building it — not on taste, but because the gesture conflict is a real
cross-browser maintenance cost and the invisible-verb problem works directly
against the ruling that put us here.

---

## What the FE slice does once a letter comes back

Unchanged by the pick:

- `Skip` and `Snooze` stay separate controls with separate labels — no combined verb.
- Snoozing a row **stages** it rather than removing it, consistent with #26: a
  snoozed row is reachable, and un-snooze lives on it. (Same rule as done.)
- The breakthrough reason is **surfaced, not swallowed** — when a snoozed row
  returns early, the card says which delta fired (`crossed_due` /
  `moved_earlier`), so "why is this back?" has an answer on the face of it.
- Pins: the two verbs are distinctly labelled; each duration writes the matching
  `snooze_Nd`; a snoozed row stages rather than vanishes; un-snooze restores it;
  and an already-overdue row snoozed today does **not** break through tomorrow on
  the strength of the overdue it already had.

Still needed at deploy regardless of the pick: **`tier.snooze.path` wired on the
box.** Until that lands the backend is inert and the affordance would write
nowhere — worth sequencing so the UI does not ship ahead of its store.

The config line, per instance (`resolve_snooze_path` reads exactly this nested
key and nothing else — it is the single parse point for both the writer and the
reader, so the two cannot drift to different files):

```yaml
tier:
  snooze:
    path: ./data/board_snooze.salem.json    # per INSTANCE — never a shared file
```

The filename must differ per instance for the same reason the feed store does:
every instance shares one WorkingDirectory on the box and differs only by
`--config`, so one shared default would let one instance's snoozes suppress
another's rows.
