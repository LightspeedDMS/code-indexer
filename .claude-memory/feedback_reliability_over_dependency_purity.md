---
name: feedback-reliability-over-dependency-purity
description: "When an install/dependency footprint decision trades architectural purity against reliability (e.g. installing an 'unneeded' extra dependency), default to installing it -- the user cares about zero known-bug-class recurrence, not minimal footprint"
metadata:
  type: feedback
  originSessionId: ede890d5-0358-4ba6-a741-0ed8d5e2db8f
---

During the #1440-#1442 production-topology investigation, it was discovered that the code-indexer server unconditionally imports `psycopg` (via `search_event_log_writer.py` -> `connection_pool.py`) even in pure SQLite/solo storage mode, because the import chain isn't gated by `storage_mode`. The "architecturally pure" framing would be to fix the code so a solo install never needs `psycopg` at all. The user's explicit reaction: "I don't have a problem if there's an extra dep in production, what I care is that AFTER this entire package of fixes, production works without known bugs of this kind that we keep facing."

**Why**: The user's priority is recurrence-of-bug-class elimination, not minimal dependency footprint or textbook-correct architecture. An extra unused dependency sitting in production is a non-issue to them; a fresh crash from a missing one is the actual, recurring pain (this is now the 3rd variant of "two Python environments/install paths silently drifted apart" seen this session: #1440 PATH/hnswlib, #1441 CLI embedding-stats psycopg crash, #1442 CLI general dependency staleness).

**How to apply**: When a fix could go two ways -- (a) remove/avoid an "unnecessary" dependency to keep the install minimal/pure, or (b) just ensure the dependency is always present so nothing ever crashes on it being absent -- default to (b) unless the user says otherwise. Don't file a new bug or propose a refactor purely to eliminate an extra dependency that is otherwise harmless. Reserve architectural-purity pushes for cases with a real cost (security surface, license conflict, genuine maintenance burden) rather than "it's not technically required for this mode." See also [[feedback_bootstrap_changes_need_installer_and_autoupdater]] for the adjacent rule about self-heal completeness for this exact class of environment-drift bug.
