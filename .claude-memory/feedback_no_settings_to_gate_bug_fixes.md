---
name: feedback-no-settings-to-gate-bug-fixes
description: NEVER add a new config setting unless the user explicitly asks for that thing to be configurable — and never put a bug fix behind one
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 3d216558-2b0c-4da6-83b0-c76caa9c86c9
  modified: 2026-08-12T13:14:35.893Z
---

**ABSOLUTE RULE: do not add ANY new configuration setting unless the user EXPLICITLY asks to make
that specific thing configurable. Period.** Not a config field, not a mode, not a flag, not an env
var, not a Web UI toggle, not a constructor parameter that exists only to be switched. If I find
myself designing a knob, the correct move is to decide the right behaviour and implement it.

The case that triggered this: **a bug fix must be the DEFAULT and ONLY behaviour.** Never add a
setting that decides WHETHER a fix takes effect. If cleanup/repair/self-heal is correct, it runs
unconditionally.

**Why:** there is no operations team. The user runs this software in production alone across ~900
repos. A setting that gates a fix ships every deployment still broken, waiting for a human to notice
a log line and flip a switch nobody will flip. In the user's words: *"I don't have a fucking army of
people to operate the software in prod"*, *"I have 900 repos, can't continue adding stupid settings
for this edge bugs"*, *"we need correctness, not having to babysit processes"*, and about a setting
I added to gate a deletion fix: *"you had a bug before... so you added a setting to control a bug?...
that should not even be a setting."*

**The mistake I keep making:** destructive-sounding fixes (deleting leaked files, mass cleanup) make
me cautious, and I express that caution as an off-by-default toggle. That instinct is wrong. It turns
a defect into a configurable behaviour and leaves every install broken until someone intervenes.
Twice in one day (2026-08-12) on the SAME reconciler: first a mass-deletion ratio gate, then a
`report`/`delete` mode.

**The distinction that IS legitimate:**

- **Correctness guards -- KEEP.** Rules deciding WHICH items are safe to act on: a minimum-age floor
  so an in-flight publish is not deleted mid-write, keep-last-N retention, never deleting something
  still referenced by a pointer. Nobody configures these, they need no attention, they are simply
  part of doing the job correctly.
- **Behaviour toggles -- DO NOT ADD.** Anything deciding WHETHER the work runs, or letting an
  operator tune it. That is babysitting the user has no capacity for.

If genuinely unsure whether an automatic action is safe, sharpen the correctness guards -- or ASK the
user. Never invent a knob as a substitute for deciding.

**How to apply:** when writing any change, ask "does this require a human to enable, tune or notice
it, ever?" If yes, redesign. In code review, flag every new config field and ask whether the user
asked for it. Related: [[feedback_no_artificial_work_budgets]], [[feedback_no_fallbacks_ever]],
[[feedback_no_half_wired_features]], [[feedback_targeted_scope_discipline]].
