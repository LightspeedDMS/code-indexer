---
name: Bug report
about: Report a problem with CIDX (CLI, daemon, server, MCP)
title: "[BUG] "
labels: ["bug"]
assignees: []
---

## Summary

<!-- One sentence: what is broken? -->

## Steps to reproduce

1.
2.
3.

## Expected behavior

<!-- What should have happened? -->

## Actual behavior

<!-- What actually happened? Include exit codes, error output, stack traces, screenshots. -->

## Environment

- CIDX version: <!-- output of `cidx --version` -->
- Mode: <!-- CLI / Daemon / Server (solo) / Server (cluster) -->
- OS + version:
- Python version: <!-- output of `python3 --version` -->
- Install method: <!-- pipx / pip / source -->

## Logs

<!--
For server bugs, paste relevant excerpts from ~/.cidx-server/logs.db
(SQLite solo) or the PostgreSQL log table (cluster). Filter for
ERROR/WARNING entries around the failure timestamp.
-->

```
```

## Anything else?

<!-- Workarounds, recent changes, related issues. -->
