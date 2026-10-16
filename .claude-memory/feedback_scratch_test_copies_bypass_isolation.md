---
name: feedback_scratch_test_copies_bypass_isolation
description: "a test file copied outside tests/ and run with its own --rootdir loads no repo conftest, so server-home isolation is off and it writes the real ~/.cidx-server"
metadata:
  node_type: memory
  type: feedback
  originSessionId: 41d5247d-a313-4d21-8302-a19984e861bc
  modified: 2026-10-02T11:01:45.047Z
---

Test-home isolation (Bug #1996) lives in `tests/conftest.py` (imports `tests/_isolated_server_home.py` first) and the server conftest. A copy of a test file run from a scratch directory with its own `--rootdir` loads neither, so it runs with no isolation and no write guard and can rewrite the real `~/.cidx-server` (observed: a negative-check run of a copied server test rewrote the real `launch.json`).

**Why:** engineers routinely copy a test to a scratch dir for negative checks (deliberately broken template, reverted fix). Those runs are exactly the ones that bypass the root conftest.

**How to apply:** in every engineer or reviewer brief that may run tests outside `tests/`, require `CIDX_SERVER_DATA_DIR`, `CIDX_DATA_DIR` and `SYSTEMD_UNIT_DIR` pointed at scratch (and preferably a fake `HOME`) for that run. Related: [[feedback_never_touch_other_repos]], [[project_own_local_dev_cidx_server]].
