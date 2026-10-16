---
name: feedback-eliminate-duplicates-in-same-change
description: "When a fix introduces a shared implementation, convert EVERY duplicate copy of that logic in the same change; never push a duplicate to a later story"
metadata:
  type: feedback
---
When a fix creates (or reveals) the single correct implementation of some logic, every other copy of that logic is converted to call it in the SAME change. Pushing a remaining duplicate onto a later story is wrong even when that story will touch the area anyway.

**Why:** owner, 2026-10-07 ("cidx watch should be reusing all code possible"). The public #2056 fix introduced one shared chunk-level FTS document builder, but `cidx watch` kept its own whole-file / absolute-path FTS writers (`cli.py` `_populate_fts_index_from_disk`, `services/fts_watch_handler.py`), and I moved them to the write-path story instead of converting them. Duplicated paths are exactly where divergent bugs come from.

**How to apply:**
- In every engineer brief that introduces a shared helper, require a grep for other implementations of the same logic and conversion of all of them, with the before/after grep in the report.
- In review triage, a "remaining duplicate" finding is fixed in the current round, not deferred.
- Related: MESSI Anti-Duplication; [[feedback-fix-every-issue-found-no-deferral]].
