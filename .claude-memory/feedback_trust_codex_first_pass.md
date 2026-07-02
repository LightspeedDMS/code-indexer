---
name: Trust codex on first pressure test — do not commission rubber-stamp reviews
description: When codex flags over-engineering in an initial pressure test, simplify immediately instead of seeking a second opinion that validates the complexity
type: feedback
originSessionId: 6e72a01d-c4b6-4715-870b-2ee0a9de347d
---
**Rule**: When codex code-review or codex pressure-test flags "over-engineered", "too many knobs", "house of cards", or any signal of excess complexity — simplify on the spot. Do not commission a second architect review that validates the existing design. Do not defend. Do not build more tests to "prove" the complexity works. Cut it.

**Why**: During Story #724 (Post-Generation Verification Pass), codex's first pressure test called the design out as over-layered: JSON envelope parsing, multiple fallback branches, evidence filter, discovery_mode rule, multiple safety guards, 30s-delay retry machinery. I wrote a spec that formalized ALL of it as acceptance criteria, then asked an architect to critique codex's review — which downgraded some severities and rubber-stamped the rest. The user (Seba) signed off on the elaborate spec. We built it. 107 unit tests passed. All three codex review passes APPROVED it. Then on staging E2E it all collapsed into silent "fallback" branches nobody asked for, because the actual verification contract was broken and every failure mode just returned the original document untouched. User's literal feedback at that point: "what the fuck is all of this... who the fuck asked you for this fucking house of cards?" and explicitly tracked a frustration intel value OVER 1.0.

**How to apply**:
- If codex's pressure test lists ≥3 "significant" or "critical" findings about scope/complexity, treat that as a hard signal to cut scope — not a prompt for a counter-review
- When the design has multiple defensive "what if Claude returns garbage" branches, collapse them: let it raise, let the caller die, let the operator see a failed job — do not add "fallback" that silently preserves original content
- When the codebase already has a convention (e.g. `invoke_*_file` methods that pass a temp file to Claude and check a sentinel string) follow it — do not invent a parallel JSON-return-and-parse flow
- User explicitly objects to fallback/forced-success patterns (Messi Rule 2 — anti-fallback). "Fallback" as a term triggers hostile escalation. Say "raise" or "fail loud" instead
- Track user intel hints: when frustration >0.7 and task type is refac/bug, the issue is almost always that the previous implementation overbuilt something the user never authorized
