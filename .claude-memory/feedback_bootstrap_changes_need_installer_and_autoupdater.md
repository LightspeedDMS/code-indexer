---
name: feedback-bootstrap-changes-need-installer-and-autoupdater
description: "Any bootstrap/systemd/env/PATH/deployment change must be automated in BOTH the installer (fresh installs) AND the auto-updater (idempotent self-heal for existing hosts) -- no exceptions, no manual-operator fixes"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: ede890d5-0358-4ba6-a741-0ed8d5e2db8f
---

Any change to how the server/CLI is bootstrapped -- systemd unit content, environment variables, PATH, file locations, service wiring -- MUST be automated in BOTH places, always, no exceptions:
1. The installer (fresh-install path), so a brand-new host gets it correctly from the start.
2. The auto-updater, via an idempotent self-heal method (check current state -> detect the specific gap -> repair only what's needed -> no-op if already correct), so an ALREADY-DEPLOYED host repairs itself automatically on its next deploy cycle.

**Why**: During the code-indexer #1440 investigation, a template-only fix for a missing systemd `PATH=` line was correctly designed and reviewed -- but it only affects fresh installs. It silently left 3 already-running staging cluster nodes permanently broken, because nothing ever re-renders an already-deployed unit file on its own. The user was explicit and forceful: production cannot rely on a manual operator re-running an install script, ever. A bootstrap-gap fix is NOT complete until an automated self-heal path exists that provably repairs an already-deployed host without a human touching it.

**How to apply**: Whenever fixing any deployment/bootstrap/infra-adjacent bug (systemd units, PATH, env vars, first-boot scripts, auto-update templates), always ask "does this fix reach an already-running host automatically, or does it only help future installs?" If the answer is "only future installs," the fix is incomplete -- add the idempotent self-heal counterpart before considering it done. Validate self-heal claims via the REAL automated mechanism actually firing on its own (e.g. let the real auto-update timer/cycle run and observe it via logs), never by manually SSHing in and hand-editing the target to make a test look like it passed -- that proves nothing about whether the automation itself works. See also [[feedback_ssh_mcp_only]] for how to observe (not touch) infra during this kind of validation, and [[project_cluster_auto_updater_service]] for how the auto-updater is wired as a separate service+timer from the main server.
