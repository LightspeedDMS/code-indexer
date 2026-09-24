---
name: feedback_never_assert_unverified_facts_in_briefs
description: Never state a fact in a subagent brief or an issue body without having verified it in this session - twice in one day a fabricated premise nearly produced a wrong fix
metadata:
  node_type: memory
  type: feedback
  originSessionId: 51a123fc-6e33-494f-85a8-3b7d83248eee
  modified: 2026-09-24T16:30:17.764Z
---

A subagent brief and an issue body are both PUBLISHED ARTEFACTS that other agents
and humans act on. A plausible-sounding claim in either one is treated as ground
truth by the reader. Verify every factual premise BEFORE writing it down, or mark
it explicitly as a hypothesis to be checked.

**Why:** this failed twice in a single session (2026-09-24).

1. I told a tech-writer agent "the issue gives five exact replacements -- use them
   verbatim." Issue #1961 contained no such list; I invented it. The agent
   correctly refused to guess and flagged the contradiction, costing a round trip.
2. I told a tdd-engineer "`Element` is not an `Iterable`" and published the same
   claim into issue #1923. The real declaration is
   `public class Element extends Node implements Iterable<Element>`. `Element` IS
   an `Iterable`, so the "bug" was not a bug and the requested fix would have
   introduced a FALSE NEGATIVE in a P1 binder -- the exact direction the X-Ray
   soundness doctrine forbids. Caught only because I checked the source afterwards.

Note the second case also means an ISSUE'S OWN REPRO CAN BE WRONG. #1923's table
presented that candidate as obviously false. Inheriting a premise from an issue is
not verification.

**How to apply:**
- Before asserting a type relationship, API shape, file content or issue content
  in a brief or issue, go read it. One `get_file_content` or `issue_manager.py read`
  is cheaper than a wrong fix to a P1.
- Write briefs so a premise is falsifiable: "My hypothesis -- VERIFY IT, do not
  assume it" plus "if this contradicts the code, believe the code and say so".
  That framing is what saved case 2's agent from acting blindly.
- When a premise turns out false, correct the PUBLIC artefact too, not just the
  agent. An issue body left asserting a false fact keeps costing people time.
- A subagent that pushes back on a premise is doing its job. Check its claim
  before overriding it.

Related: [[feedback_grep_absence_is_not_evidence]],
[[feedback_prove_root_cause_before_fix]], [[feedback_study_anomalies_deeply]].
