---
name: feedback-cli-defects-reach-server
description: "Never defer a bug as \"CLI-only\" without proving the server does not reach that code: the server spawns cidx subcommands and runs CLI-layer services in-process"
metadata:
  type: feedback
---
A defect in CLI code is NOT automatically server-safe. The server delegates to the CLI: it spawns `cidx index` (incl. `--fts`, `--reconcile`, `--index-commits`), `cidx init`, `cidx fix-config` and `cidx scip ...` during registration, refresh and activation, and it runs CLI-layer services (SmartIndexer, FileChunkingManager, TantivyIndexManager, watch handlers, temporal search) in-process. Owner correction, 2026-10-06, after two reviewers labelled several public bugs "CLI-only, no server impact" and recommended deferring them.

**Why:** production is a server; a CLI bug on a path the server delegates to is a production bug, often on a workhorse (indexing, FTS, semantic).

**How to apply:**
- Before classifying any bug as CLI-only, trace whether a server front door or background process (refresh, registration, activation, write path, auto-update, agent integrations) reaches the defective code; cite the call path.
- Put this check in every triage and review brief ("CLI-only" requires evidence of NO server path, not absence of a server-looking file name).
- Related: [[feedback-release-inclusion-bar]], [[project-production-feature-usage]].
