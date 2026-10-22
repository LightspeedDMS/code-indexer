---
name: feedback-reports-subdirs-not-gitignored
description: Only top-level reports/*.md is gitignored; reports/<subdir>/ files are NOT -- put real staging/production detail directly under reports/ or verify with git check-ignore
metadata:
  node_type: memory
  type: feedback
  originSessionId: 469b8128-2710-4307-9c15-5e1dfb0f1a0b
  modified: 2026-10-22T06:13:53.958Z
---

Only `reports/*.md` at the top level is gitignored (`.gitignore`: `/reports/*.md`). A file under a
subdirectory such as `reports/staging/` is NOT ignored, so a validation report with real hostnames,
IPs or repository names written there could be committed to the public repository.

**Why:** on 2026-10-22 a staging-validation brief named `reports/staging/...` as the report path; the
executing agent noticed the directory was not ignored and wrote the report under `reports/` instead.

**How to apply:**
- Briefs that produce reports with real environment detail name a top-level `reports/<name>.md` path
  (or `.analysis/`), never a new `reports/<subdir>/`.
- Before writing sensitive detail anywhere, run `git check-ignore -v <path>`; no output means it would
  be tracked.
- Related: [[feedback-no-secrets-in-memory]].
