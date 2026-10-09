# X-Ray Architecture

Maintainer reference for X-Ray, the structural AST search engine: its two evaluator execution modes, the MCP and
REST front doors, the single-file search engine, the cross-file graph pipeline, and the graph's node and edge
model. Request and response schemas are owned by the tool docs (`xray_search`, `xray_explore`, `xray_search_batch`,
`analyze_graph` under `src/code_indexer/server/mcp/tool_docs/search/`); the evaluator security boundary is in
[sandbox.md](sandbox.md); exact binder rules are in [graph-binder-internals.md](graph-binder-internals.md); evaluator
templates are in the [X-Ray cookbook](../../guides/xray-cookbook.md). Decision records:
[ADR index](../../adr/README.md).

## Contents

- [Languages](#languages)
- [Execution modes](#execution-modes)
- [Front doors and execution model](#front-doors-and-execution-model)
- [Single-file engine](#single-file-engine)
- [Graph pipeline](#graph-pipeline)
- [Graph mode: node and edge model](#graph-mode-node-and-edge-model)
- [Candidate-admission contract](#candidate-admission-contract)
- [Result truncation](#result-truncation)

## Languages

The Rust engine (`rust/xray-core/src/languages.rs`) parses 17 languages with tree-sitter: Java, Kotlin, Go, Python,
TypeScript (including TSX), JavaScript, Bash, C#, HTML, CSS, HCL/Terraform, YAML, SQL, XML, Groovy, C and C++. `.h`
maps to the C grammar; C++ headers parse under the C++ grammar only with `.hpp`, `.hh`, `.hxx` or `.h++`.

Graph extraction is narrower: `extractor_for_language()` (`rust/xray-core/src/graph/extract/mod.rs`) registers
extractors for Java (`.java`) and Kotlin (`.kt`, `.kts`) only. `graph_extractor_extensions()` is the single list
Python asks for (`xray-cli --print-graph-extractor-extensions`); files of any other language parse but contribute no
declarations and make `fact_graph_complete` false.

## Execution modes

ADR-001 fixes exactly two modes, and the evaluator source decides which one applies
(`compiler::detect_evaluator_mode`, `rust/xray-core/src/compiler.rs`):

| Mode | Required entry points | Used by |
|------|-----------------------|---------|
| Single-file | `fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding>` | `xray_search`, `xray_explore`, `xray_search_batch` |
| Graph | `fn collect_facts` and `fn analyze_graph`, and no `evaluate_node` | `analyze_graph` |

`EvalFinding` is `{pattern, line, snippet}`. In single-file mode the evaluator runs once per candidate file against
the file's root `OwnedNode` (`kind` and `start_line` are fields, not methods). Graph evaluators read the graph
through the opaque `GraphHandle` accessors (ADR-002).

## Front doors and execution model

| Tool | Handler | Execution |
|------|---------|-----------|
| `xray_search` | `server/mcp/handlers/xray/_search.py` | Registered with `JobTracker.register_job()` (no per-repo conflict gate) and run on the dedicated `xray_executor` thread pool, after taking a slot from `app.state.xray_cell_limiter`. Returns `job_id` (or `job_ids` for a list of aliases); `await_seconds` (0 to 45) can return the result inline |
| `xray_explore` | `server/mcp/handlers/xray/_explore.py` | Same model as `xray_search`; `evaluator_code` optional; adds `max_debug_nodes` (1 to 500, default 50) and per-match `matched_node` / `ast_debug` |
| `xray_search_batch` | `server/mcp/handlers/xray_batch.py` | ONE `BackgroundJobManager.submit_job(operation_type="xray_search_batch", repo_alias=None)` over N aliases x M scan bundles; `timeout_seconds` 10 to 7200 |
| `analyze_graph` | `server/mcp/handlers/xray_graph/__init__.py` | Synchronous within `timeout_seconds`, off the event loop via `anyio.to_thread.run_sync`; not a background job |
| `xray_dump_ast`, `store_xray_pattern`, `cidx_fetch_cached_payload`, `cancel_job` | `server/mcp/handlers/xray/` | Supporting tools |

All of them require the `query_repos` permission. REST shims under `/api/xray` (`server/routes/xray_routes.py`):
`POST /api/xray/search` (fields `driver_regex` and `max_files` instead of MCP `pattern` and `max_results`) and
`POST /api/xray/search/batch`; both return 202 with a job id.

**Evaluator resolution** (`_resolve_evaluator_code`, `server/mcp/handlers/xray/_infra.py`): inline
`evaluator_code`, or a stored `pattern_name` from `cidx-meta/xray-patterns/` (`XrayPatternService`,
`server/services/xray_pattern_service.py`; repo-specific scope first, then `__any__/`); the two are mutually
exclusive. `xray_search`, `xray_explore` and each `xray_search_batch` scan fall back to a default accept-all evaluator when
neither is given.
Pattern loading touches the NFS-backed `cidx-meta`, so it runs on the `xray_executor`, never on the event loop.

Every handler validates the evaluator with `validate_rust_evaluator()` before starting work
(see [sandbox.md](sandbox.md)).

## Single-file engine

`XRaySearchEngine.run()` (`src/code_indexer/xray/search_engine.py`) has two phases:

1. **Driver (Phase 1).** A regular expression selects candidate files, matching file content
   (`search_target="content"`, through ripgrep via `RegexSearchService`) or the relative path
   (`search_target="filename"`). It honours `path`, `include_patterns` / `exclude_patterns`, `case_sensitive`,
   `multiline`, `pcre2` and `context_lines`. `max_results` caps the number of candidate files evaluated; hitting it
   sets `partial` and `max_files_reached`.
2. **Evaluator (Phase 2).** `RustNativeBackend.run_batch()` (`src/code_indexer/xray/rust_backend.py`) compiles the
   evaluator (cached by compile identity) and runs `xray-cli --dynlib ... --json --files-from ...`, which parses each
   file and calls `evaluate_node`. The server adds `file_path`, `language` and `line_content` to every finding.

Per-file failures (`UnsupportedLanguage`, `EvaluatorTimeout`, `ValidationError`, `BinaryNotFound`, `XRayCliError`,
I/O errors) go to `evaluation_errors[]` without failing the job. A job-level timeout returns `partial` and
`timeout`.

The Python module `src/code_indexer/xray/sandbox.py` also contains `PythonEvaluatorSandbox`, which is not on this
path (see [sandbox.md](sandbox.md)).

## Graph pipeline

`analyze_graph` validates the request, resolves the alias, collects candidate files (`xray_graph/_candidates.py`,
honouring `include_patterns` and `exclude_patterns`), and calls `RustNativeBackend.run_graph_analysis()`, which runs
`xray-cli` in stages:

1. `--compile-only`: compile the graph evaluator.
2. `--build-graph`: parse every file, run `collect_facts`, extract declarations and references, bind them across
   files (`rust/xray-core/src/graph/repo_index.rs`, `graph/bind/`), and write the graph (`--graph-out`).
3. `--analyze-graph`: a separate, killable process reads the graph (`--graph-in`, memory-mapped) and runs
   `analyze_graph`. When cgroup v2 delegation is available it is bounded by a `memory.max` ceiling derived from the
   admission estimate (`rust/xray-core/src/graph/analyze/memory_ceiling.rs`, ADR-003).
4. `--refine`, only when the request sets `refine`.

The graph file is a handoff between processes of the same request, not a persistent cache. Its binary format is
versioned by the magic `XRAYGRF4` (`rust/xray-core/src/graph/csr/wire.rs`): a file with any other magic is rejected.
Build admission and the graph cache are integrated with the server memory governor as described in ADR-003
(`server/services/xray_graph_governor/`).

Callers must check `fact_graph_complete` and the degradation counters before treating an empty `findings` list as a
verified negative.

## Graph mode: node and edge model

`CodeGraphBuilder::build` (`rust/xray-core/src/graph/csr/builder.rs`, `code_graph.rs`) produces one `CodeGraph` per
repository.

### What is a node

A node is a declaration recorded by a language extractor (not a tree-sitter parse node). Every declaration has a
`DeclarationKind`: `Type`, `Method`, `Field`, `Constant` or `Package`
(`rust/xray-core/src/graph/extract/local_index.rs`). Bound symbols get a dense `u32` id and carry their kind and a
`Visibility` (`Public`, `Protected`, `Private`, or `Unknown`).

Visibility comes from modifiers. In Java, `private`, `protected` and `public` map directly. In Kotlin
(`visibility_of_modifiers`, `extract/kotlin_fields.rs`) no modifier means `Public`, `private` means `Private`, and
`internal` maps to `Unknown` because a repository-only analysis cannot prove module-internal code unreachable.

`CodeGraph::is_definitely_dead_code` returns:

- `Some(false)` when the symbol has an inbound reference;
- `Some(true)` only for an unreferenced `Method` or `Type` whose visibility is `Private` and which is not a sole
  private no-arg constructor (the non-instantiable utility class idiom);
- `None` for everything else, including every `Field`, `Constant` and `Package`.

It does not consult `fact_graph_complete`.

### What becomes an edge

An edge is a `Reference` in the CSR arena whose candidate window lists the declarations it may target; an empty
window is unresolved, more than one candidate is ambiguous.

**Java** (`extract/java.rs`) creates references for:

- `method_invocation`: a method call;
- `method_reference`: `this::m`, `Type::m`, `expr::m`, `super::m`, `Type::new` (named references keep every
  candidate overload);
- `object_creation_expression`: `new T(...)`, referencing the type and every constructor that could accept the
  call;
- `explicit_constructor_invocation`: `this(...)` / `super(...)`;
- `type_identifier`: a bare type reference;
- `marker_annotation` / `annotation`: an edge to the annotation type's declaration.

A method named by a JUnit5 `@MethodSource` string is marked referenced at extraction time, which suppresses its
`Some(true)` verdict; this is not a CSR edge and does not appear in `callers_of`.

A `super.m()` call resolves only against the enclosing type's recorded supertypes. When the type records no
supertype at all (no `extends` and no `implements` clause, including the implicit `java.lang.Object`), every
candidate is kept, which can produce a self-edge when the enclosing type's own method is the only candidate. When a
supertype clause exists but could not be resolved, super-class narrowing is skipped for that type.

**Kotlin** (`extract/kotlin.rs`, bind levels 0 to 2: declarations, references, imports, inheritance) creates
references for calls, navigation expressions, callable references, constructor delegation calls, type references,
infix calls and operator conventions (arithmetic, comparison, equality, indexing, unary, range, `in`, and
non-indexed compound assignment). Java and Kotlin bind to each other in both directions. The Kotlin extractor never
records typed local names, so Kotlin references never carry `RECEIVER_TYPE_MATCH`. The exact operator list and the
two forms that are not extracted are in [graph-binder-internals.md](graph-binder-internals.md).

### What is invisible to the graph

Edges come only from the constructs above. These are structurally invisible:

- Field and constant reads and writes: Java `field_access` produces no edge.
- Reflection (`Class.forName`, method handles, dynamic proxies).
- JNI-bound native methods.
- Dependency-injection wiring.
- Lombok-generated members, which exist only in bytecode.
- JPA and other annotation-driven use: an annotation creates an edge only to its own type.
- Enum-constant construction: an enum constant's implicit call to the enum constructor is not an
  `object_creation_expression`, so it creates no constructor edge.
- Implicit superclass-constructor calls: only explicit `this(...)` / `super(...)` creates a constructor edge.

A private `Method` or `Type` reachable only through one of these can be reported `Some(true)` although it is live.

### Evidence bits and confidence

Each candidate carries a `u16` set of reason bits (`rust/xray-core/src/graph/reasons.rs`, 15 flags such as
`SAME_FILE`, `ARITY_MATCH`, `UNIQUE_NAME_IN_REPO`, `RECEIVER_TYPE_MATCH`, `RECEIVER_TYPE_MISMATCH`,
`INHERITANCE_FAMILY`, `FAMILY_TRUNCATED`). Its confidence is derived from those bits whenever a candidate is built or
decoded (`Confidence::derive`), so confidence and evidence cannot disagree. Evaluators filter on bits with the
`*_filtered` accessors (`required_bits`, `forbidden_bits`).

**Receiver-type resolution** tags (never removes) candidates whose owner matches the receiver's declared type,
resolved from same-file locals, fields, parameters, chained return types, or a repo-wide field-name index when every
declaration of that field name agrees on its type. **Inheritance-family expansion** adds every override of an
interface or supertype method; it only widens a set, and a capped family sets `FAMILY_TRUNCATED`.

## Candidate-admission contract

Candidate sets are built per reference in `resolve_reference` (`graph/bind/resolve.rs`), then refined in
`graph/bind/mod.rs`. Passes run in this order; "hard" passes remove candidates, "tag" passes only set bits:

| Pass | Module | Effect |
|------|--------|--------|
| `apply_private_visibility_filter` | `bind/narrowing.rs` | Hard: drops a private candidate declared in a different known top-level type |
| `apply_arity_narrowing` | `bind/narrowing.rs` | Hard: keeps exactly the arity matches, down to empty, when both the call's argument count and the declaration's parameter count are known |
| `apply_overload_shape_narrowing` | `bind/narrowing.rs` | Hard only for a provably incompatible literal argument (Java callees), never empties the set; otherwise tags `OVERLOAD_ARG_TYPE_MATCH` |
| `apply_receiver_type_narrowing` | `bind/narrowing.rs` | Tag only: `RECEIVER_TYPE_MATCH` |
| `apply_receiver_type_mismatch_tagging` | `bind/receiver_mismatch.rs` | Tag only: `RECEIVER_TYPE_MISMATCH`, under four closed-world conditions |
| `apply_type_qualifier_narrowing` | `bind/narrowing.rs` | Hard, Java only: a definite type-qualified call narrows to that type's matching declarations, only in files that pass the whole-file safety guard and only when at least one candidate matches |
| `apply_same_class_or_super_narrowing` | `bind/narrowing.rs` | Tag only: `SAME_CLASS_OR_SUPER` |
| `apply_super_class_narrowing` | `bind/narrowing.rs` | Hard for `super` calls against recorded supertypes; skipped when supertype evidence is empty or incomplete |
| `apply_import_context_narrowing` | `bind/narrowing.rs` | Hard preference for same-file / same-package / imported candidates, never empties; skipped when the type-qualifier pass already confirmed the set |
| `apply_inheritance_family_expansion` | `bind/narrowing.rs` | Widens with family members |
| `apply_receiver_qualified_type_narrowing` | `bind/receiver_qualified.rs` | Hard, Java only: for a type-qualified call whose receiver type is named by an ordinary import, no repo field shares the receiver's name, and the caller's whole ancestor chain is analysed |

The design rule is that a wrong deletion is worse than a surplus candidate: a deleted real edge can turn a live
private method into a false `Some(true)`. Receiver-type narrowing and same-class-or-super narrowing are therefore
permanently tag-only.

<a id="what-a-future-round-8-would-need"></a>

### Prerequisites for hard receiver-type narrowing

Code comments cite this section by its former title, "What a future round 8 would need". Making receiver-type
evidence delete candidates needs, at least:

1. Block-scoped, capture-aware local bindings. `FileTypedNames` (`bind/receiver.rs`) keys locals by
   `(enclosing_method, name)`; a local captured by an anonymous or local class is looked up under the wrong key, so a
   `Missing` lookup is not proof of absence. A key that sees two different types across blocks is recorded
   `Ambiguous` and resolves to no evidence.
2. A qualified or unique type identity threaded through the reference-site pipeline. `same_class_context`,
   `MethodOwnerRecord.enclosing_type` and the `TypeIndex` family and supertype maps are all keyed by bare simple
   names, so two same-named nested types (for example two `Builder` classes in one file) cannot be told apart.
3. Proof by adversarial execution against real, compilable source, not only by additional unit fixtures.

### Observability

`RepoIndexResult::narrowed_to_zero_count` and `narrowed_to_nonempty_strict_subset_count` (`graph/repo_index.rs`,
mirrored in the `--build-graph` JSON as `BuildGraphResult`) count references whose candidate set the hard passes
narrowed to zero, or to a strict non-empty subset, of a non-empty bare-name pool. They count narrowing events, not
errors: a non-zero value is a reason to investigate, and zero is not proof of correctness.

All of this works on syntactic evidence only: there is no type inference and no visibility into compiled
dependencies.

## Result truncation

Large X-Ray results are paged into `PayloadCache` (`server/mcp/handlers/xray_truncation.py`,
`truncate_result_fields`): whole entries are packed into pages within the payload character budget
(`payload_max_fetch_size_chars`), and all pages plus a manifest are written in one `store_batch_with_keys()` call.
The response carries an inline prefix, `cache_handle`, `total_pages` and `has_more`; clients page through with
`cidx_fetch_cached_payload`. A failed store returns `cache_store_failed` instead of a handle; without a payload cache
the response is a bounded inline prefix with `cache_unavailable: true`.
