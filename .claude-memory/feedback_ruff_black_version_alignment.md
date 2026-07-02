---
name: Ruff/Black version alignment
description: Pre-commit ruff version must match system ruff version to avoid formatting conflicts in automation scripts
type: feedback
---

Pre-commit ruff version and system-installed ruff version MUST match.
Different versions produce incompatible formatting output that causes
automation scripts to fail even when pre-commit hooks pass.

**Why:** ruff v0.8.4 (old pre-commit) and v0.14.1 (system) disagree on
243+ files. Black and ruff also have incompatible formatting rules.
server-fast-automation.sh was switched from black to ruff format.

**How to apply:** When updating ruff version, update BOTH .pre-commit-config.yaml
AND ensure the system `ruff` matches. Current: v0.14.1 in both.
Also: server-fast-automation.sh uses `ruff format --check` (not black).
