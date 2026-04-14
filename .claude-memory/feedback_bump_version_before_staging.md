---
name: feedback_bump_version_before_staging
description: ALWAYS bump version and tag BEFORE promoting to staging — auto-deployer requires version tag
type: feedback
---

ALWAYS bump version + create git tag BEFORE merging to staging. The auto-deployer on the staging server (.20) triggers on version tags — without a new tag, the deployment does not happen.

**Why:** Twice in the same session, code was promoted to staging without a version bump. The staging server did not auto-deploy because there was no new tag. User had to ask "did you bump the version?" both times.

**How to apply:** Every time you push to staging, the sequence MUST be:
1. Bump `__version__` in `src/code_indexer/__init__.py` (patch increment)
2. `git commit` the version bump
3. `git tag vX.Y.Z`
4. `git push origin development --tags`
5. THEN merge to staging and push

Never merge to staging without a version bump + tag first.
