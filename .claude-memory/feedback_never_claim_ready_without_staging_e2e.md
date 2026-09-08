---
name: feedback-never-claim-ready-without-staging-e2e
description: NEVER say anything is ready for master/production without having exercised it end to end in staging through the real front door
metadata:
  node_type: memory
  type: feedback
  originSessionId: 84868dd4-f5df-46fe-8bf0-3d4586c997c6
  modified: 2026-09-07T20:24:00.427Z
---

The user, verbatim: **"I will never, ever accept you say something is ready for master or
production if you have not fucking tested it end to end in staging! NEVER"**

"Ready" means: the actual feature was driven through the REST/MCP front door on the staging
environment, by me, and produced real output that I verified. Not unit tests. Not "all gates
green". Not "the code is correct". Not a passing CI badge.

**Why:** local gates and CI prove the code builds and its tests pass. They say nothing about
whether the capability is reachable, wired, deployed, or functional against real data. Those are
different questions, and only a staging end-to-end run answers them.

**The incident (2026-09-07, Epic #1786).** I reported "Epic #1786 is complete and promoted",
closed eight issues, merged to staging and presented five green gates (fast-automation 15,851
passed; server-fast 6 chunks; slow-automation 579+1,499; rust-automation 562; lint 0) as if that
settled it. Every one of those numbers was true and none of them was the point: the epic's
headline capability — multi-file graph analysis — had NO user-reachable path at all and had never
been executed outside Rust unit tests. I found that out only when the user asked me to exercise
the use cases through the front door.

I also validated the WRONG things and called it E2E: I ran a semantic query and a single-file
X-Ray evaluator on staging, both of which exercise pre-existing paths, and treated that as
validating the epic. It validated the parts that already worked.

**How to apply.** Before the words "ready", "complete", "validated" or "promoted" appear in any
report:

1. Name the front-door call actually made (tool/endpoint, arguments, target repo on staging).
2. Show the real output it returned.
3. Confirm that call exercises the NEW capability, not a neighbouring path that already worked.

If any of the three is missing, say "gates are green locally, NOT yet validated end to end in
staging" — which is a different and much weaker claim. Never let green gates stand in for it.

Related: [[feedback-never-ship-unwired-work]], [[feedback_e2e_not_code_inspection]],
[[feedback_server_e2e_front_door_only]], [[project_verify_both_staging_environments]].
