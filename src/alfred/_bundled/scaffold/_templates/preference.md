---
type: preference
status: active # active | revoked
shape: # action | voice — action = extraction/inclusion gate; voice = talker system-prompt directive
scope: # universal | instance
applies_to_instance: null # null for universal; "Salem" | "Hypatia" | "KAL-LE" for instance-specific
applies_to_user: null # V1 always null — reserved for V.E.R.A. multi-user differentiation
cites_canonical: null # wikilink to a canonical preference this application overrides/extends/rejects, or null
source_quote: "" # verbatim quote from the source conversation establishing this preference
source_session: # [[session/conversation-...]] wikilink to the originating session
# For shape: action — matcher dispatch (omit for shape: voice records):
matcher:
  domain: # curator | brief | other consumer name
  rule: # skip_event_if | skip_brief_event_if | skip_brief_task_if — see src/alfred/preferences/matchers.py
  args: {} # rule-specific args (e.g. {title_regex: "(?i)\\bopen house\\b"})
name: "{{title}}"
created: "{{date}}"
tags: []
---

# {{title}}

## Policy

<!--
What is the operator's commitment? State plainly. For shape: action
preferences, describe what the gate skips/includes and when an
explicit override applies. For shape: voice preferences, describe
the directive the talker should follow.
-->

## Matcher rationale

<!--
Required for shape: action — explain WHY this matcher catches the
right things without false positives. Note operator override paths
(e.g. "explicit mention of a specific X bypasses this skip").
Omit this section for shape: voice records.
-->
