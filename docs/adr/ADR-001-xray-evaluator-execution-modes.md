# ADR-001: X-Ray evaluator execution modes

Status: Accepted

## Context

X-Ray currently executes a compiled evaluator once per candidate file:
`evaluate_node(node) -> Vec<EvalFinding>`. The evaluator source is also a
persisted product artifact. `XrayPatternService` stores it in the git-versioned
`cidx-meta/xray-patterns/` tree, with repo-specific scopes taking precedence
over `__any__/`.

The proposed multi-file graph adds extraction/collection, binding, graph
analysis, and optional refinement. Story #1785 separately proposed an
untyped, string-keyed `emit`/`reduce_facts` protocol. Supporting all three as
peer contracts would create three validator surfaces, three ABI families, and
compatibility combinations that grow with every API change. It also violates
#1785's stated rule that a typed schema replaces, rather than accretes on, a
string protocol when X-Ray becomes a graph engine.

There is one genuine exception: configuration keys, event topics, and
structural hashes are facts but are not symbols. They cannot be represented by
a `SymbolId` without inventing a false identity.

## Decision

After S2, X-Ray supports exactly two evaluator execution modes:

1. **Legacy file mode (compatibility mode).** The production contract is
   `evaluate_node(&OwnedNode) -> Vec<EvalFinding>`. It evaluates only the
   driver-selected file trees and remains supported indefinitely for existing
   clients and stored patterns. It is frozen: no new cross-file facts,
   reducers, or graph semantics are added to this mode.
2. **Graph mode (the only extensible mode).** The engine runs fused
   Extract+Collect, Bind, and Analyze, followed by optional Refine. The user
   callbacks are:

   ```rust
   fn collect_facts(node: &OwnedNode, file: &FileContext, index: &LocalIndex)
       -> Vec<UserFact>
   fn analyze_graph(g: &CodeGraph, facts: &FactIndex) -> GraphResult
   fn refine(node: &OwnedNode, ctx: &FileContext, g: &CodeGraph, facts: &FactIndex)
       -> Vec<EvalFinding>
   ```

   `collect_facts` and `analyze_graph` are required for graph mode;
   `refine` is optional when analysis is fully graph-based. A graph evaluator
   must not export `evaluate_node`; a legacy evaluator must not be treated as
   graph mode. The loader rejects a mixed or incomplete callback family.

   The graph fact key is the closed sum type:

   ```rust
   FactKey::Symbol(SymbolId) | FactKey::Custom(InternedStr)
   ```

   Symbol-shaped facts use `SymbolId` and never use formatted strings.
   `Custom` is reserved for genuinely non-symbol values. The old
   `emit`/`reduce_facts` API is not a supported mode and is deleted as a public
   contract. Its reusable mechanics—fact streaming, the second process,
   `--facts-in`, panic containment, explicit reducer status, and
   `child_by_field_name`—are implemented as graph-mode infrastructure.

### Migration of `evaluate_node`

`evaluate_node` is retained indefinitely, but only as a frozen compatibility
lane. This is necessary because it has real callers and because existing
patterns under both `__any__/` and `{repo-alias}/` are user-owned, git-versioned
artifacts. Indefinite retention does not contradict the no-accretion rule:
there are two modes, and only graph mode receives new capabilities. The rule
for new work is absolute: a requirement needing repository-wide joins goes to
graph mode, never to a new legacy callback or a revived string reducer.

The legacy path keeps its current per-file result shape and behavior. It may
be documented as deprecated for new patterns, but removal requires a separate
versioned product decision and a migration of all stored patterns and clients;
S2 does not remove it.

### ABI sequencing and exports

The ABI sentinel is part of the compile/cache identity. A loader compares the
artifact's `xray_abi_version` before resolving or calling any callback. A
mismatch is a named load failure; it is never a fallback to another callback
or execution mode. The cache treats a mismatch as a miss, using the ABI-aware
identity established by #1784.

The export contract is:

| ABI | Required exports | Optional exports | Meaning |
|---|---|---|---|
| 2 (current) | `xray_abi_version`, `xray_evaluate_node` | `xray_drain_debug_log` | Legacy file mode only. |
| 3 (#1785 interim protocol) | `xray_abi_version`, `xray_evaluate_node` | `xray_drain_debug_log`, `xray_drain_facts`, `xray_reduce_facts` | Transitional artifact shape only; it must not be exposed as a third supported mode. `xray_reduce_facts` is not a graph callback. |
| 4 (S2) | `xray_abi_version` plus exactly one complete mode family: either `xray_evaluate_node`, or `xray_collect_facts` and `xray_analyze_graph` | `xray_drain_debug_log`, `xray_refine` in graph mode | Final two-mode contract. `xray_refine` is all-or-none with the graph family when present; graph artifacts do not export `xray_reduce_facts` or `xray_drain_facts`. |

ABI 3 is a rolling-deployment/intermediate compatibility point, not a
permanent execution contract. New compilation after S2 emits ABI 4. A loader
accepting ABI 4 never loads ABI 2 or 3; an ABI-2/3 loader never loads ABI 4.
The error includes artifact ABI, expected ABI, and the required recompilation
action. Old cache rows are misses, not candidates for reuse.

The direct artifact handoff used for graph analysis still uses a separate
`xray-cli` process. Fact-stream truncation, malformed graph data, callback
absence, panic, timeout, cache failure, and ABI mismatch produce explicit
failure/incomplete statuses. They never silently run a different mode or
drop edges to manufacture a result.

### Stored-pattern compatibility

Pattern metadata gains an explicit `execution_mode` (`legacy` or `graph`).
Missing metadata in an existing pattern is interpreted as `legacy`, because
the current schema requires `evaluator_code` and all existing stored patterns
are legacy source. New graph patterns declare `graph` and are validated and
compiled as ABI 4 graph artifacts.

Resolution remains repo-specific scope first, then `__any__`; scope and git
history do not change the compatibility rule. Before compilation/loading,
the requested mode, declared callback family, ABI, and cache identity are
checked together.

The following are hard errors, returned with `partial`/incomplete metadata as
appropriate and never silently downgraded:

- a legacy pattern requested by a graph operation (`pattern_mode_mismatch`);
- a graph pattern requested by legacy file execution;
- missing or mixed callback families;
- a stored mode whose compiled artifact has an older ABI;
- malformed metadata, source, or an artifact whose exports do not match its
  declaration.

Existing patterns therefore continue to run in legacy mode after S2. They do
not acquire graph behavior merely because the engine now supports graph mode.
To migrate one, its YAML is rewritten with `execution_mode: graph` and graph
callbacks, validated, recompiled under ABI 4, and committed as the normal
`cidx-meta` pattern change. The old git revision remains auditable; failure to
migrate is visible rather than a silent semantic change.

### Validator surface and compatibility policy

There are two validators, each with a distinct gate:

- `validate_rust_evaluator` gates legacy `evaluate_node` source at the Python
  front door and remains the fast admission check for legacy stored and inline
  patterns.
- `validate_rust_graph_evaluator` gates graph callback declarations and the
  graph fragment at the Python front door. It checks the callback family,
  graph-specific signatures, forbidden constructs, and the `FactKey`/fact
  surface. It also validates any optional `refine` callback.

`validator.rs` remains the compiler-side security and syntax gate. The Python
front door and Rust validator are not required to reject exactly the same
constructs. For every construct, the policy is recorded as one of:

1. reject at Python and Rust;
2. reject only at Rust (Python permits submission, compilation rejects it);
3. deliberately permit at Python and Rust, with a test proving the intended
   behavior.

There is no unrecorded divergence. Python validation is early UX/admission;
Rust validation is the final authority before native code exists. A source
that fails the final Rust gate cannot be loaded, regardless of front-door
behavior.

### Deletions

The following are deleted as part of the S2 protocol migration, in this order:

- before graph implementation: the standalone #1785 `emit`/
  `reduce_facts` public contract and its symbol-shaped string-fact examples;
- during graph protocol implementation: `xray_drain_facts`,
  `xray_reduce_facts`, reducer-only submission, and the ABI-3-only loader
  branches;
- after the ABI-4 graph path is green: the old reducer result/status plumbing
  that exists only to distinguish the transitional protocol, plus tests that
  assert it as an independently supported mode.

The legacy `xray_evaluate_node` export, legacy validator, legacy stored-pattern
schema interpretation, and their regression coverage are not deleted.

## Alternatives Considered

### Support all three contracts indefinitely

Rejected. It duplicates validators, exports, result handling, and compatibility
testing, and makes the untyped protocol a permanent graph API despite its own
replacement rule.

### Delete `evaluate_node` immediately

Rejected. It silently breaks production callers and git-versioned patterns.
Keeping it as a frozen compatibility mode is a bounded cost; extending it is
not permitted.

### Force every fact through `SymbolId`

Rejected. Config keys, event topics, and structural hashes are not symbols.
The closed `FactKey` sum type preserves those use cases without restoring a
general string-keyed graph protocol.

## Consequences

The system has two validator and callback families rather than three, with a
single extensible graph contract and a stable legacy lane. Existing patterns
continue to work, while graph adoption is explicit and auditable. ABI-aware
cache identity and strict mode checks prevent stale or semantically mismatched
artifacts from running.

This makes implementation stricter: pattern schema and resolution must carry
mode, the loader must classify exports, and rolling ABI transitions cannot
reuse old artifacts. Authors must migrate source to graph callbacks to obtain
cross-file behavior. The legacy lane creates permanent maintenance and test
cost, and graph mode has larger memory/process/governor requirements. Those
costs are accepted to avoid breaking users and to keep negative graph results
fail-closed.

## Migration

1. Land and verify #1784 before changing the preamble or ABI.
2. Add live-path regression coverage for both existing legacy patterns and the
   new mode classifier; do not delete retired coverage before its replacement
   is green.
3. Fold #1785's process, handoff, panic, status, and field-name mechanics into
   the graph protocol; do not ship its symbol-shaped untyped API.
4. Add mode metadata with legacy-by-absence compatibility, graph validation,
   ABI-4 export classification, and explicit mismatch errors.
5. Compile legacy patterns as ABI-4 legacy artifacts when they are rebuilt;
   continue accepting already stored source by recompiling on an ABI/cache
   miss. Never load an ABI-2/3 artifact under the ABI-4 loader.
6. Implement graph mode with `FactKey::Symbol | FactKey::Custom`, the shared
   second-process handoff, and the S2 memory governor requirements. Keep
   `evaluate_node` unchanged in semantics.
7. Delete the transitional reducer exports and compatibility branches only
   after ABI-4 graph and legacy paths pass their mode-specific regression,
   stored-pattern, mismatch, validator-divergence, and failure-status tests.
