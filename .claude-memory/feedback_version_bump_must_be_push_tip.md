---
name: feedback_version_bump_must_be_push_tip
description: The __init__.py version-bump commit MUST be the tip of its push or CI skips tag creation
metadata:
  type: feedback
---

The CI tag-creation job (`.github/workflows/main.yml`, `check-version` -> `create-tag`) decides whether to create the `vX.Y.Z` tag by running `git diff --name-only HEAD~1 HEAD | grep src/code_indexer/__init__.py` and then comparing `HEAD:__init__.py` vs `HEAD~1:__init__.py`. It only evaluates the **tip commit of the push** (one workflow run per push, on the tip), NOT every commit in the push.

**Why:** If you bump the version in commit A and then stack another commit B (e.g. a chore/memory commit that does NOT touch `__init__.py`) on top, and push A+B together, CI runs on tip B. `HEAD~1..HEAD` (B vs A) shows no `__init__.py` change -> `version_changed=false` -> `create-tag` is **skipped**. The tag is silently missed. This happened on v10.110.0 (bug #1080): the bump commit `6f5cd501` was buried under a `chore(memory)` tip commit, CI skipped the tag, and it had to be created manually.

**How to apply:**
- When bumping the version on `development`, make the version-bump commit the **LAST** commit in the push. Do NOT push unrelated commits on top of it in the same push.
- If you must include other commits, push the version-bump commit so it lands as the tip (push it last, or order so the bump is HEAD).
- Recovery if CI already skipped: create the tag manually — `git tag vX.Y.Z -m "Release version X.Y.Z"` then `git push origin vX.Y.Z`. This is a NEW tag (allowed; only *replacing* a remote tag is forbidden). The `create-tag` job even guards for "tag already exists on remote (pushed manually) -> skip", so manual creation is an anticipated fallback. Tag the development tip, message format exactly `Release version X.Y.Z`, tag name `vX.Y.Z`.
- Verify after every version push: `git ls-remote --tags origin refs/tags/vX.Y.Z` is non-empty; if empty, CI skipped it — create manually.

Related: [[feedback_bump_version_before_staging]] (bump+tag must exist before promoting to staging; the auto-deployer requires it).
