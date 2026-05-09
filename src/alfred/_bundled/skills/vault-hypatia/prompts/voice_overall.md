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
