---
name: project-config-default-flip-is-inert
description: "Changing a dataclass default in config_manager.py is INERT on every existing deployment -- persisted runtime config is a full JSON blob merged OVER defaults, so a stored value always wins"
metadata: 
  node_type: memory
  type: project
  originSessionId: 3d216558-2b0c-4da6-83b0-c76caa9c86c9
  modified: 2026-08-10T16:53:47.988Z
---

Runtime config in cidx-server persists as a **full JSON blob** (`server_config.config_json`) and is
merged OVER the dataclass defaults on load (`config_service._merge_runtime_config`). Every save path
serialises the WHOLE dataclass, so any deployment that has ever saved runtime config has a stored
value for every field.

**Consequence:** changing a default in `config_manager.py` only reaches deployments that have never
saved runtime config -- in practice, none. Shipping a default flip as a behaviour change is inert.

**Why:** confirmed the hard way on 2026-08-10. v12.9.0 flipped `AliasLockConfig.db_backed_enabled`
to `True` for Story #1546, passed all tests, and changed nothing on the clustered staging environment -- a
live race still produced the file-lock split-brain signature. Cost a full release cycle (v12.10.0)
to correct.

**How to apply:** to change behaviour for existing deployments, write a one-time promotion in
`_merge_runtime_config` (the load path every server executes at startup) that updates the value and
persists it via `_save_runtime_to_pg` / `_save_runtime_to_sqlite`. Discriminate on the PRESENCE of a
dedicated marker field in the RAW stored blob, never on the value itself -- the value cannot
distinguish "false because it predates the change" (promote) from "false because an operator
deliberately set it" (never touch). Verify on a deployment whose stored blob actually contains the
old value; a fresh deployment with no stored key gets the new default anyway and proves nothing.

Same defect class as [[feedback-no-half-wired-features]] and
[[feedback-verify-zero-json-chunks-on-indexing]]: the artifact was correct, the effect at the point
of use was absent. Verify the effect, not the artifact.
