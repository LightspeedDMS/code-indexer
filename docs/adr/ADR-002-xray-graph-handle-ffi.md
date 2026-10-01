# ADR-002: Expose CodeGraph to evaluator dylibs through an opaque GraphHandle, not a mirrored layout

Status: accepted
Date: 2026-09-06
Context: Epic #1786, Story #1787 (S2), acceptance criteria AC7, AC8, AC18

## Context

Slice S2.5 implemented AC7 (analyze_graph in its own killable process, mmap graph
handoff, distinct terminal statuses) and the classifier/validator half of AC8. It stopped
before the graph-mode PREAMBLE/EPILOGUE, the `GraphDynlibEvaluator` loader, and the
`xray-cli --analyze-graph` subcommand, and reported why instead of guessing.

The blocker is structural. Evaluator code is compiled against a PREAMBLE string literal in
`rust/xray-core/src/compiler.rs` that declares, in source text, every type the evaluator may
touch. Today that is `OwnedNode` and `EvalFinding` -- small, stable, and already flagged as
duplication debt by AC18 ("strike three" under Messi Rule 4, anti-duplication).

Graph mode would require the evaluator to receive a `CodeGraph`. Mirroring `CodeGraph` the
same way means reproducing its entire internal layout in PREAMBLE text: `StringTable`,
`SymbolTable`, the CSR arenas, `HashMap`, `Vec<BinderDepth>`, `ReferencedBits`,
`AnalysisCompleteness`. That is a large, fast-moving surface that four separate S2 slices
have already changed.

## Decision

Evaluator dylibs receive an **opaque `GraphHandle`** plus a set of **accessor functions**.
They never see `CodeGraph`'s fields, and its layout is never reproduced in PREAMBLE text.

Consequences of the shape:

- The PREAMBLE declares an opaque handle type and accessor signatures only. Adding a field
  to `CodeGraph`, or changing `StringTable`'s representation, does not touch the PREAMBLE.
- The bounded graph operations already built for AC7 (`callees_of`, `callers_of`,
  `reachable_from(roots, max_depth)`, `shortest_path_to_any`,
  `strongly_connected_components`) are the natural accessor surface. They are bounded by
  construction, which is what keeps Rule 14 (anti-unbounded-loop) enforceable at the ABI
  boundary rather than by asking evaluator authors to behave.
- Accessors return `SymbolId` values and `&str` borrowed from the shared string table,
  preserving AC5's rule that no query on an O(edges) path returns an owned `String`.

## Rejected alternative: mirror CodeGraph's layout into the PREAMBLE

Rejected because it makes the exact defect AC18 exists to remove strictly worse, and
because the failure mode is already demonstrated rather than hypothetical.

Bug #1795 is the precedent. `OwnedNode`'s traversal methods were recursive in BOTH the real
type and its PREAMBLE mirror, and fixing it required a hand-synchronized, byte-level edit
across two files -- including getting `.rev()` placement identical in each. That was
manageable for two small methods. It would not be manageable for the graph substrate, and a
silent divergence there would not produce a compile error; it would produce a dylib reading
a stale type layout and returning wrong answers, which is the confidently-wrong failure
direction this epic exists to eliminate.

A layout mismatch across the dylib boundary is also memory-unsafe, not merely incorrect.

## Consequence for sequencing: AC18 now precedes AC8

AC18 was written as debt to repay before AC8 widened the mirror. The S2.5 finding upgrades
it: AC18 is a **prerequisite** for finishing AC8, not cleanup to schedule afterward.

Order of remaining work:

1. AC18 -- generate the PREAMBLE from the real types, or add a build-time check that fails
   when the two diverge. Establish the mechanism while the mirrored surface is still small.
2. AC8 -- graph-mode PREAMBLE/EPILOGUE, `GraphDynlibEvaluator`, and the
   `xray-cli --analyze-graph` subcommand, built on the `GraphHandle` accessor ABI.

Attempting them in the reverse order means designing the handle ABI against a mirror that is
simultaneously being replaced.

## Notes

- This does not alter ADR-001. There are still exactly two execution modes: the frozen
  legacy `evaluate_node`, and graph mode. `GraphHandle` is how graph mode's callbacks
  receive their input, not a third mode.
- `XRAY_ABI_VERSION` is already at 4 (S2.5). Introducing the handle and accessors changes
  the ABI contract again and requires its own bump, which the existing single-source-of-truth
  mechanism and the #1784 assembled-source cache identity handle automatically.
- The AC7 wire format deliberately omits `binder_depths`. If an accessor is later added to
  expose per-language binder depth to evaluators, the wire format must carry it first --
  otherwise the accessor would report empty depth on any graph reconstructed from a file,
  which would be a silent lie rather than a visible failure.
