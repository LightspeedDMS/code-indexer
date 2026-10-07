---
name: feedback-release-inclusion-bar
description: What may be added to an in-flight release — only high-severity security items or non-security P1/P2 showstoppers
metadata:
  type: feedback
---

When findings surface during a release, add them to that release ONLY if they are:
1. Security items of high severity (classification lives in the private security tracker, not here).
2. Non-security P1/P2: showstoppers or egregious malfunctions of real functionality.
Everything else (low-probability nitpicks, response shapes, cosmetics, nuisances, esoteric features) is filed and goes to a later release.

**Why:** owner, 2026-10-05, after an earlier "include all" had grown a release well past its plan and delayed staging.

**How to apply:** classify every new finding against this bar before adding it to the release; state the classification to the owner; items already in progress may be kept if nearly done, but say so. Feature priority context: [[project-production-feature-usage]]. Never write severity rationale or weakness descriptions into this public memory directory.
