---
name: Progress reporting is delicate — ask before changing
description: Ask confirmation before ANY changes to progress reporting. Single line at bottom, NO scrolling, specific callback pattern.
type: feedback
---

Ask confirmation before ANY changes to progress reporting code. This area is delicate and has broken before.

Pattern:
- Setup: `progress_callback(0, 0, Path(""), info="Setup")` — scrolling mode
- Progress: `progress_callback(current, total, file, info="X/Y files...")` — progress bar mode

Rules:
- Single line at bottom with progress bar + metrics
- NO scrolling console feedback EVER during progress
- Files involved: BranchAwareIndexer, SmartIndexer, HighThroughputProcessor

**Why:** Progress reporting has been broken multiple times by well-intentioned changes. The callback pattern is non-obvious (setup vs progress mode) and changes can cause visual regressions that are hard to test automatically.

**How to apply:** If a task involves touching progress_callback, BranchAwareIndexer, SmartIndexer, or HighThroughputProcessor progress logic, ask the user before making changes. Show proposed changes and get approval.
