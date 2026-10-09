---
name: feedback_never_store_leaked_literals_in_public_guards
description: "never write a leaked value into a public guard/denylist/test to \"prevent it coming back\" -- that republishes the leak"
metadata:
  node_type: memory
  type: feedback
  originSessionId: 87afb584-3ac2-483a-aa98-3b98d28f4d88
  modified: 2026-10-06T17:18:20.183Z
---

Owner rule (2026-10-06): a disclosure guard must NEVER contain the leaked values in plain text in this public
repository. The former tree-wide checker (`scripts/check_disclosure_tree.py`, Bug #1916) kept a labelled
BANNED_PATTERNS list of every previously-leaked literal; the owner rejected the approach outright and had it
deleted (no hashed replacement wanted).

**Why:** a public denylist is a labelled index of exactly what is sensitive; it re-leaks the values on every
branch it lands on, defeating the scrub it was meant to protect.

**How to apply:** when scrubbing a leak, fix the occurrences and stop. Do not add the value to any tracked
file (scripts, tests, fixtures, allowlists, comments, issues, memory). If a guard is ever proposed again, it
must hold no recoverable value and needs explicit owner approval first.
Related: [[feedback_no_secrets_in_memory]].
