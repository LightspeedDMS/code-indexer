---
name: Never add time.sleep() to production code
description: NEVER add time.sleep() for UI visibility — fix the display logic instead
type: feedback
---

NEVER add `time.sleep()` to production code for UI visibility purposes. Fix the display logic instead.

**Why:** Adding sleeps to "make progress visible" is a hack that degrades performance. The correct approach is to fix the progress callback or display mechanism.

**How to apply:** If progress output is hard to see or flashes too fast, fix the rendering logic (buffering, refresh rate, progress bar implementation). Never slow down the actual work to make it visible.
