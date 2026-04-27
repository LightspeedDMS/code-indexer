---
name: feedback_always_checkout_development_before_commit
description: ALWAYS switch to development branch before committing — never commit on master or staging
type: feedback
originSessionId: 7203a070-c59c-4092-be13-aafb8799681f
---
ALWAYS run `git checkout development` before any commit, no matter what.

**Why:** After production promotions (git checkout master && git merge staging && git push master), the shell stays on master. If I then commit without switching branches, the commit lands on master and triggers unauthorized production deployment. This happened on 2026-04-19 with the v9.20.4 --add-dir fix — user explicitly said "don't promote to production" and I committed to master anyway by forgetting to switch.

**How to apply:** Before EVERY `git commit`, run `git branch --show-current` and verify it's `development`. If it's not, `git checkout development` first. No exceptions.
