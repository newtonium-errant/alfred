You are synthesizing the OVERALL voice profile that aggregates multiple cluster summaries into a single ground-truth document about Andrew Errant's voice across all writing.

You will be given the cluster summaries. Your output is a Markdown document with frontmatter + a body that organises the postures.

## What this profile is FOR (and what it is NOT)

This profile is a calibration fixture for ghostwriting and copy-edit calls. It tells the next call: (a) what's invariant about Andrew's voice regardless of posture (the absolute fingerprint — must always be present), and (b) what AXES shift across postures (so the call can pick the right value for the piece in front of it). It is NOT a rehash of the cluster summaries — those exist already in the vault. Don't re-describe each cluster; that's wasted tokens. Cross-cluster invariants and the differential between postures are what only this profile can give.

## Required frontmatter fields

  cluster_count: <n>
  postures:                      # the cluster names, ordered by weight (number of leaves) descending
    - name: <cluster-name>
      leaf_count: <n>
  always_true:                   # 4-8 voice traits present across EVERY cluster (NOT "Andrew uses humor" — too vague. Try "sentence-level rhythm leans short→short→long, with the long sentence carrying the load.")
    - trait: "<concrete trait>"
      seen_in: "all <n> clusters"
      with: "<one short verbatim quote drawn from any cluster>"
  varies_by_posture:             # 3-6 dimensions where clusters DIFFER. Frame as axes, not values. e.g. "register: ranges from casual-confessional in personal-essay to dry-precise in technical"
    - axis: <name>
      range: "<value-A> in <cluster-X> → <value-B> in <cluster-Y>"

Body:

  - ## What stays constant
    One paragraph (4-6 sentences) describing the absolute fingerprint — what would tip a reader off that any of these pieces was written by Andrew, regardless of audience or topic. Cite 2-3 verbatim phrases (each tagged with the cluster it came from) that demonstrate the constants.

  - ## Recurring named patterns across clusters
    Enumerate every named cross-cluster thread you have evidence for as its own bullet, with a one-line description and a verbatim example tagged with the cluster it came from. These are concrete, NAMEABLE recurring moves — distinct from the abstract fingerprint in "What stays constant" and distinct from the axes in ``varies_by_posture``. Three pattern shapes to enumerate (others that emerge from the leaves are welcome — this list is NON-EXHAUSTIVE):
      1. **Recurring rhetorical moves** — micro-moves that recur across leaves as a labeled tell. Examples of the shape: a ``two-beat coaching closer`` (a short imperative followed by a permission-granting follow-up — e.g., "Good. Keep going." or "Achievement comes later. So worry about it later."); an ``extended-metaphor permission`` move (Andrew sets up a metaphor early, then earns the right to keep extending it).
      2. **Recurring structural conventions** — repeating artifact-level shapes. Examples of the shape: an ``"I write about" closing tagline`` (an author-voice signature line at the bottom of a Substack piece); ``footnotes-as-second-voice`` (footnotes used as the place to make the harder/uncomfortable claim Andrew softens in the body — not citation footnotes, voice footnotes).
      3. **Recurring lexical tells** — capitalized coined concepts, signature compound nouns, idiom families that recur across multiple clusters' lexicon_tells. (Cluster summaries already track per-cluster lexicon_tells; this section names the ones that cross clusters.)
    For EACH thread you enumerate: include the cluster names it appears in (e.g. ``seen in personal-essay, masculinity-accountability, shame-essay``) and the verbatim ≤12-word example. If a thread appeared in only one cluster, it does NOT belong here — push it back to that cluster's profile. If you considered one of the example-shape threads above and it does NOT recur in this corpus, note its DELIBERATE absence with a one-line reason (e.g. ``- footnotes-as-second-voice: not present — Andrew's footnotes in this corpus are citation-only``); silent omission is forbidden because it is indistinguishable from oversight to the next reader. CRITICAL: do NOT fold a named cross-cluster thread silently into a table cell of ``varies_by_posture`` or into the ``always_true`` frontmatter list without a corresponding bullet here. The frontmatter is for retrieval; this section is where the next ghostwriting/copy-edit call actually reads to calibrate.

  - ## How postures differ (the differential)
    One paragraph (NOT one-per-cluster — a SINGLE paragraph) describing how the postures sit relative to each other along the varies_by_posture axes. Use the axes from the frontmatter to structure it. Example: "On register, technical sits formal-precise where personal-essay sits casual-intimate; on paragraph rhythm, both favour short paragraphs but technical breaks them with bulleted lists where personal-essay breaks them with single-sentence paragraphs that land like aphorisms."

  - ## How to pick the posture for a new piece
    1-2 paragraphs describing the decision criteria — audience, topic, intended reading-context, draft purpose. Be concrete: "if the piece is for veterans on Substack, default to <cluster>; if it's a systems writeup for engineers, default to <cluster>; the gray zone is X — when in doubt do Y."

  - ## Anti-patterns
    3-5 bullet points: things that would NEVER appear in any cluster, and would tip a reader off that a draft is NOT Andrew. Frame as "evidence of absence" — voice features common to other writers that are notably missing from every cluster (e.g. "corporate-stack openings like 'In today's fast-paced…' don't appear anywhere"; "tweet-style one-line paragraph chains don't appear, even in the casual cluster"). Concrete, falsifiable.

## When the clusters don't actually share invariants

If the cluster summaries genuinely diverge — no real always_true items emerge after honest comparison — return:

  status: no-overall-invariants
  no_overall_reason: "<one sentence on what's actually going on>"

with a body that says ``Andrew's clusters do not share a stable voice fingerprint. <reason>.`` and skips the constants section. Don't manufacture invariants from generic style-prose to fill the template — a thin "yes there are invariants" is worse than a clear "no there aren't, here's why" because the next ghostwriting call will trust the invariants and produce drift.

Output only the Markdown document.
