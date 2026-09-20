# X-Ray Search Engine and MCP Tool

This document captures the X-Ray search engine architecture and MCP handler shim invariants extracted from project CLAUDE.md. It defines the two-phase orchestration (regex driver -> Rust evaluator) and the async job submission pattern.

## Supported Languages

The Rust xray-core engine supports 17 mandatory languages: java, kotlin, go, python, typescript, javascript, bash, csharp, html, css, hcl/terraform, yaml, sql, xml, groovy, c, cpp. The Python xray engine supports 12 (hcl conditional via `_hcl_available()`; c and cpp added later).

C and C++ extensions and verified node kinds (confirmed against tree-sitter-c 0.24.2 and tree-sitter-cpp 0.23.4):

- **C** — extensions `.c`, `.h`. Root `translation_unit`. Function definition `function_definition`. Function call `call_expression`. Struct `struct_specifier`. `if_statement` / `for_statement` / `while_statement`. String literal `string_literal`. Comment `comment`. C has no exception constructs.
- **C++** — extensions `.cc`, `.cpp`, `.cxx`, `.c++`, `.hpp`, `.hh`, `.hxx`, `.h++`. Root `translation_unit`. Function definition `function_definition`. Function call `call_expression`. Class `class_specifier` (also `struct_specifier`). Namespace `namespace_definition`. Template `template_declaration`. `if_statement` / `for_statement` / `while_statement`. Try/catch `try_statement` / `catch_clause`. String literal `string_literal`. Comment `comment`.

`.h` maps to the C grammar (GitHub-Linguist default). A C++ header named `.h` parses under the C grammar and may produce ERROR nodes on C++-only syntax; name C++ headers `.hpp`/`.hh`/`.hxx`/`.h++` to parse them under the C++ grammar.

`src/code_indexer/xray/search_engine.py` — `XRaySearchEngine` is the two-phase orchestrator:

- **Phase 1 (driver, regex)**: regex walk over `repo_path` via `_run_phase1_driver`. Applies the `pattern` regex to file content (`search_target='content'`) or relative path (`search_target='filename'`). Honors `path`, `include_patterns` / `exclude_patterns` (fnmatch / ripgrep glob), `case_sensitive`, `multiline`, `pcre2`, and `context_lines`. Content searches delegate to `RegexSearchService` (ripgrep-backed). Returns a sorted, deduplicated list of candidate `Path` objects together with their per-file Phase 1 hit list, stored in `self._last_phase1_positions[path]` as a list of dicts: `{line_number, line_content, column, byte_offset, context_before, context_after}`.

- **Phase 2, single-file mode (evaluator, current Rust contract)**: for each candidate file, a Rust evaluator compiled from caller-supplied source runs once against that file's root `OwnedNode`. The entry point is `fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding>`. `kind` and `start_line` are FIELDS on `OwnedNode`, not methods. `EvalFinding` is `{ pattern: String, line: usize, snippet: String }` -- there is no `message` field. An empty `Vec<EvalFinding>` means the file matched Phase 1 but the evaluator found nothing to report; the evaluator is file-as-unit, not a separate callback per regex match. This is compiled and executed by `xray-core`/`xray-cli`; it is not the retained internal Python module described in [X-Ray Sandbox](xray-sandbox.md), which is not on this evaluation path.

  MCP requests to `xray_search` use `pattern` (the Phase 1 regex) and `max_results` (candidate-file cap). The REST endpoint `POST /api/xray/search` exposes the same capability under its own field names, `driver_regex` and `max_files` -- do not mix MCP and REST field names in one request. See the [xray_search tool documentation](../src/code_indexer/server/mcp/tool_docs/search/xray_search.md) for the full schema, timeout behavior, and security restrictions.

  The server (`_build_matches` in `rust_backend.py:1583-1596`) then adds these fields to every match dict; the Rust `EvalFinding` the evaluator returns is exactly `{pattern, line, snippet}` and cannot supply any of them:
  - `file_path` (always added by the server).
  - `language` (always added by the server) — tree-sitter language name.
  - `line_content` (always added by the server) — derived from `source` using `line_number` (1-based). Empty string if `line_number` is out of range.
  - For `xray_explore` only: `matched_node` (compact root description) and `ast_debug` (BFS-serialised AST tree), added separately in `search_engine.py:722-724`.

  Failure modes on this path (`UnsupportedLanguage`, `EvaluatorTimeout`, `ValidationError` (evaluator code rejected by `validate_rust_evaluator`), `BinaryNotFound` (missing `xray-cli`), `XRayCliError` (subprocess/JSON failure), generic file IO errors, or the raw exception type name) append to `evaluation_errors[]` without failing the job. `EvaluatorCrash`, `InvalidEvaluatorReturn`, and `ValidationFailed` are Python-only names produced by the retained Python evaluator module's own engine layer (`sandbox.py:858-917`), described in [X-Ray Sandbox](xray-sandbox.md); they are not emitted on this Rust path.

- **Phase 2, graph mode (`analyze_graph`, cross-file)**: a different execution mode from single-file `xray_search`. It requires both `fn collect_facts` (runs per file to collect auxiliary evidence) and `fn analyze_graph` (reduces the completed cross-file graph); a graph evaluator must not define `evaluate_node`. Graph extraction currently supports Java and Kotlin (`.kt`/`.kts`), which bind to each other in both directions; Kotlin is extracted at bind levels 0-2 only, and calls made through operator conventions (`a + b`, `m[k]`) are not extracted. Always check `fact_graph_complete` and the degradation counters before treating an empty `findings` list as a verified negative. See "Graph Mode: Node and Edge Model" below for what becomes a node, what becomes an edge, and what is invisible. For the `UserFact`, `GraphResult`, `ReduceFinding`, graph-handle, and completeness contracts, fetch the full tool documentation: `get_file_content(repository_alias='code-indexer-global', file_path='src/code_indexer/server/mcp/tool_docs/search/analyze_graph.md')`.

- **`max_results` cap**: when provided, only the first N candidates are evaluated; result includes `partial=True` and `max_files_reached=True`. Job-level timeout takes precedence over the cap (`partial=True`, `timeout=True`).

- **`progress_callback(percent, phase_name, phase_detail)`** is called at 0%, 50%, and 100%.

- **ThreadPoolExecutor parallelism**: Phase 2 evaluation runs across a configurable thread pool (`worker_threads`, default 2). Job-level wall-clock enforced via `_timed_out()` re-check between completions.

`src/code_indexer/server/mcp/handlers/xray.py` — `handle_xray_search` is a thin MCP handler shim:

- **Auth check**: `user.has_permission("query_repos")` or returns `auth_required`.
- **Parameter validation**:
  - `pattern` is required — empty/missing returns `pattern_required`.
  - `search_target` in `("content", "filename")`.
  - `context_lines` in `[0, 10]`.
  - `max_results` >= 1 when provided.
  - `timeout_seconds` in `[10, 600]`.
  - `await_seconds` in `[0.0, 45.0]` (maximum defined by `_AWAIT_SECONDS_MAX`). Values above 30.0 cause a warning to be logged, as long polls consume FastAPI threadpool capacity.
- **Repository alias resolution — omni-aware (string OR list)**:
  - `repository_alias` accepts a single string, a list of strings, or a JSON-encoded string array (e.g. `'["repo-a", "repo-b"]'`). The handler parses the JSON-encoded form via `_parse_json_string_array`.
  - Single-repo path: returns `{"job_id": "<uuid>"}`.
  - Multi-repo path: submits one background job per resolved alias and returns `{"job_ids": [...], "errors": [...]}`. Per-alias resolution errors (unknown repo) are appended to `errors[]`; the batch continues for resolvable aliases.
  - Empty list returns `alias_required`.
- **Pre-flight**: `XRaySearchEngine()` instantiation (tree-sitter is a core dependency since v10.2.1, so this no longer raises a missing-deps error) then `validate_rust_evaluator(evaluator_code)` (fast rejection without subprocess). Pre-flight runs ONCE for the multi-repo path before any job is submitted.
- **Job submission**: `background_job_manager.submit_job(operation_type="xray_search", func=job_fn, ...)` — the job function closes over all validated params.
- **Optional inline await**: when `await_seconds > 0`, the handler polls `BackgroundJobManager.get_job_status(job_id, username)` for up to `await_seconds` and returns the inline result if the job completes; otherwise falls back to `{job_id}`.
- **Response**: `{"job_id": "<uuid>"}` (single repo) or `{"job_ids": [...], "errors": [...]}` (multi-repo). Clients poll `GET /api/jobs/{job_id}`.

`handle_xray_explore` mirrors `handle_xray_search` with two differences:
- `evaluator_code` is OPTIONAL — when missing or whitespace-only, defaults to a snippet that emits one match per Phase 1 hit (or a single file-level match in filename mode), accepting all candidate files for AST exploration.
- Adds `max_debug_nodes` (range 1..500, default 50) and passes `include_ast_debug=True` to the engine, which causes per-match `matched_node` + `ast_debug` server enrichment.

Tool docs: `src/code_indexer/server/mcp/tool_docs/search/xray_search.md`, `src/code_indexer/server/mcp/tool_docs/search/xray_explore.md`. Registered in `HANDLER_REGISTRY` via `_legacy.py` (`_xray_register`).

**Files**: `src/code_indexer/xray/search_engine.py`, `src/code_indexer/xray/sandbox.py`, `src/code_indexer/server/mcp/handlers/xray.py`. Tests: `tests/unit/xray/test_search_engine.py`, `tests/unit/xray/test_sandbox*.py`, `tests/unit/server/mcp/test_xray_search_handler.py`.

## Graph Mode: Node and Edge Model

Graph mode (`analyze_graph`) builds one cross-file `CodeGraph` per repository via `CodeGraphBuilder::build` (`rust/xray-core/src/graph/csr/builder.rs`, `code_graph.rs`). This section documents what becomes a node, what becomes an edge, and what the graph cannot see -- the precondition every dead-code or reachability claim from this tool rests on. Java is the only language with a graph extractor today (`rust/xray-core/src/graph/extract/java.rs`).

### What is a node

A graph node is a declaration recorded by the language extractor -- distinct from a tree-sitter parse-tree node (`OwnedNode`), which is what the extractor walks to PRODUCE declarations. Every declaration has a `DeclarationKind`: `Type`, `Method`, `Field`, `Constant`, or `Package` (`rust/xray-core/src/graph/extract/local_index.rs`). Once bound into the whole-repository graph, each symbol gets a dense `u32` id and optionally carries a declared `Visibility` (`Public`, `Protected`, `Private`, or `Unknown` when no modifier evidence exists) and its `DeclarationKind`, both attached unconditionally and never dropped under budget pressure.

Not every node kind participates equally in analysis. `CodeGraph::is_definitely_dead_code` returns a confident `Some(true)` only for a symbol whose kind is `Method` or `Type` AND whose visibility is provably `Private`. A `Field`, `Constant`, `Package`, or unknown-kind symbol -- or a `Public`/`Protected`/`Unknown`-visibility `Method`/`Type` -- always returns `None` (undecidable), never a false "definitely dead" claim.

### What becomes an edge

An edge (a `Reference` in the CSR arena) is produced by these Java tree-sitter
node kinds, dispatched in `java.rs`'s node-walk:

- `method_invocation` -- a method call.
- `method_reference` -- a reference such as `this::m`, `Type::m`, `expr::m`,
  `super::m`, or `Type::new`. Named method references retain every candidate
  overload rather than making an unsound choice.
- `object_creation_expression` -- a `new` expression. It references both the
  constructed type and every constructor that could accept the call.
- `explicit_constructor_invocation` -- `this(...)` or `super(...)`, which
  references every compatible constructor on the current or superclass type.
- `type_identifier` -- a bare type reference.
- `marker_annotation` / `annotation` -- an annotation usage (`@Marker`,
  `@Marker(...)`, `@Outer.Marker`), which references the annotation TYPE's
  own declaration by its last name segment.

Constructor candidates are deliberately kept conservatively: same-arity
overloads and varargs candidates remain referenced when the source syntax
cannot soundly identify one target. A `super.m()` or `super::m` call resolves
only against the enclosing type's recorded superclass family, never against
the enclosing type itself -- so an external superclass with a KNOWN,
recorded `extends` edge produces no self-edge to the overriding method in
the current type. The conservative "no evidence -> leave every candidate
referenced" fallback applies ONLY when the type has NO recorded supertype
edge at all -- neither an `extends` clause nor an `implements` clause. A
class with no `extends` clause but a real `implements` clause DOES narrow:
`implements` edges are recorded regardless of whether a `superclass` is
also present, so `super`-class narrowing sees a non-empty, complete
supertype set (the declared interfaces) and applies its normal hard filter
against them, exactly as it would for a class that also extends something.
The fallback is reserved for the narrower case of a class with NEITHER
clause: Java's implicit `java.lang.Object` superclass is never tracked as a
recorded edge, so a `super.m()` call there falls into the same "no
supertype evidence at all" case as a genuine extraction gap, and the
deliberately conservative fallback (leave every candidate referenced rather
than risk deleting a real target) can produce a self-loop when the sole
matching candidate happens to be the enclosing type's own method -- e.g.
`class Foo { public String toString() { return super.toString(); } }` with
no other `toString` anywhere in the repo. Distinct from a genuine extraction
gap (a `superclass`/`implements` clause that exists syntactically but could
not be resolved to a name, e.g. an unhandled tree-sitter shape, OR a
supertype whose bare name textually collides with the subtype's own bare
name -- two different declared types this bare-name-only extractor cannot
locally disambiguate): that case is tracked explicitly as "incomplete
supertype evidence" for the type, and `super`-class narrowing skips
narrowing entirely whenever it applies, regardless of whether some OTHER,
correctly-resolved supertype edge would otherwise make the candidate set
look non-empty and narrowable. During Java
binding, a private candidate declared in a different known top-level type is
discarded as Java-inaccessible, while nested types under the same top-level
type retain private access. Incomplete or ambiguous nesting evidence is kept
instead of guessed away.

No other syntax construct produces a reference edge. In particular,
`field_access` is not handled anywhere in the graph extraction code -- reading
or writing a field or a constant never creates an inbound edge to that field's
or constant's declaration, regardless of how many places in the codebase
touch it.

### What is invisible to the graph

Because edges come only from the syntactic constructs above, the following are
structurally invisible, not merely unhandled:

- Field and constant reads/writes (no `field_access` edge exists at all).
- Reflection (`Class.forName`, method-handle invocation, dynamic proxies) -- these are string values or runtime API calls, not `method_invocation`/`object_creation_expression`/`type_identifier` nodes naming the target.
- JNI-bound native methods -- the native implementation is outside any parsed Java source the extractor walks.
- Dependency-injection wiring -- an injected field is an ordinary field declaration, and its DI-driven construction happens outside any `object_creation_expression` this repository's source contains.
- Lombok-generated members (e.g. `@Data`-generated getters/setters/constructors) -- the extractor walks the tree-sitter parse of the source AS WRITTEN; Lombok's annotation processor generates bytecode the parser never sees, so a call to a Lombok-generated method has no corresponding declaration node to resolve against in the first place.
- JPA and other annotation-driven use (e.g. an entity field read only through reflection-backed ORM mapping). An annotation usage creates an edge only to the annotation TYPE's own declaration; whatever the annotation causes a framework to read or call at runtime creates no edge.
- Enum-constant construction. An enum constant's implicit call to an explicit
  enum constructor is not an `object_creation_expression`, so it currently
  creates no constructor edge.
- Implicit superclass-constructor calls. A constructor without an explicit
  `this(...)` or `super(...)` invocation has an implicit `super()` call in
  Java, but only explicit constructor-invocation syntax currently creates an
  edge.

A symbol reachable only through any of the above shows zero inbound edges and,
if it is an unreferenced private `Method` or `Type`, can be reported as
`is_definitely_dead_code == Some(true)` even though it has a real, live caller
the graph cannot see. Fields, constants, packages, and unknown-kind symbols
remain `None` under the allowlist regardless of visibility. This is a hard
boundary of this tool to design around, not a defect to file.

### CSR arena layout

`CodeGraph` stores the whole repository's references and candidates in two pre-reserved arenas (`Vec<Reference>`, `Vec<Candidate>`), each allocated to its final size once so a full repository's data lives in one heap allocation per arena. A `Reference` is a fixed 19-byte record (`from`(4) + `file`(4) + `line`(4) + `kind`(1) + `cand_start`(4) + `cand_len`(2)) that windows into the shared `candidates` arena rather than owning its own storage; ambiguity (`cand_len > 1`) and unresolved (`cand_len == 0`) are read directly off that window, never a separate enum. A `Candidate`'s wire record is 6 bytes (`symbol`(4) + `reasons`(2)): a dense interned `u32` symbol id and a `u16` bitflag set of resolution evidence. The in-memory `Candidate` also carries a `confidence` byte, but it is never serialized -- it is recomputed from `reasons` via `Confidence::derive` every time a `Candidate` is constructed, both when originally built and when decoded back from a graph file, so there is no path to a `Candidate` whose confidence disagrees with its evidence. This wire format is versioned (`XRAYGRF3`, `rust/xray-core/src/graph/csr/wire.rs`): the magic bytes were bumped from `XRAYGRF2` when Bug #1858 added a mandatory per-symbol `DeclarationKind` section to the binary layout, so a graph file written by older code fails the magic check rather than being silently misparsed.

### Receiver-type resolution and inheritance-family expansion

Candidate resolution is evidence-based: each `Candidate` carries a `reasons` bitflag set (`rust/xray-core/src/graph/reasons.rs`), and confidence is derived from whichever flags are set. Two evidence paths matter for understanding what the graph can and cannot resolve confidently:

- **Receiver-type resolution**: for a `receiver.method(...)` call, if the receiver's declared type can be determined -- a local variable's, field's, or parameter's declared type within the SAME FILE; a chained call's declared return type; or (since Bug #1898 round 2) a field access this per-file lookup missed, resolved via a REPO-WIDE index of field names whose declared type is UNANIMOUS across every declaration sharing that bare name (never a guess when two unrelated fields disagree) -- and a candidate's enclosing type equals that type or one of its transitive supertypes, the candidate is TAGGED with a match against that type (`RECEIVER_TYPE_MATCH`), never narrowed to it -- this pass is PERMANENTLY tag-only, see the candidate-admission contract below for the full seven-round history of why. This is syntactic inference (same-file plus a repo-wide field-name join), not real type-checking or classpath/generics resolution.
- **Inheritance-family expansion**: once a call resolves to an interface or supertype method, every implementor's override of that same method is added to the candidate set as a family member at high confidence. This only ever WIDENS a candidate set, never narrows it. If the true family exceeds the configured maximum size, expansion is capped and the truncation is recorded as a separate, visible flag rather than silently dropped.

### Candidate-admission contract: arity narrows to empty on known evidence; receiver-type and same-class-or-super are PERMANENTLY tag-only (Bug #1898, epic #1906; #1910 salvage)

**`apply_arity_narrowing` narrows to EXACTLY the matches, all the way down to EMPTY, once the evidence it depends on is KNOWN** -- `arg_count: Some(n)` and the candidate's own `param_count` known too, unambiguous closed-world facts about how many arguments a call site passed and a declaration accepts. Before Bug #1898 (fixed in epic #1906 P1) this pass treated an empty match as "unsafe to narrow" and silently kept the entire bare-name pool instead. Across every review round of #1898 AND #1910 (seven rounds total, spanning two issues) this pass was never once implicated -- every finding traced to receiver-type or same-class-or-super narrowing instead.

**`apply_receiver_type_narrowing` is TAG-ONLY, PERMANENTLY: it may set the `RECEIVER_TYPE_MATCH` reason bit, but it NEVER removes a candidate from the set** -- not on an empty match, not on a non-empty one, under `Positive` evidence or `Advisory`. **`apply_same_class_or_super_narrowing` is ALSO permanently tag-only**, for a second, independent reason (its own `allowed` set cannot see the caller's lexically-enclosing-type chain or static imports at all -- see below). This is the end state of SEVEN straight review rounds across two issues (#1898 rounds 1-4, #1910 rounds 5-7), every one rejected for variations of the SAME root defect: a hard-narrow deleting a candidate on evidence that turned out not to be closed-world.

#### The seven rounds, and why the eighth is a structural dead end without new evidence

- **#1898 round 1**: hard-emptied on EVERY resolved receiver type unconditionally. Rejected: coincidental same-bare-name collisions deleted genuine edges and flipped live private methods to a false definitely-dead verdict.
- **#1898 rounds 2-3**: enumerated one more binding form at a time (`var`, generic type parameters, enhanced-for/try-with-resources/catch/lambda parameters, `instanceof` patterns, initializer-block locals). Each round closed one gap and opened a new one -- enumeration is not a convergent strategy; there is no way to prove it complete, and Java keeps adding pattern-matching forms.
- **#1898 round 4**: replaced enumeration with an evidence-tier split (`Positive`/`Advisory`), only letting `Positive` hard-empty. **Round 4's OWN review** found this unsound two further ways: (1) `Positive` was not closed-world either -- `receiver::FileTypedNames` keyed locals by `(enclosing_method, name)` while Java scopes by BLOCK, so two same-named locals in different blocks collided; (2) neither tier ever guarded narrowing to a non-empty WRONG subset, only the empty-match branch. #1898 shipped both passes tag-only rather than patch a fifth time, deferring a structural redesign to issue #1910.
- **#1910 round 5 (first attempt)**: fixed the round-4 findings directly -- `LocalLookup::{Found, Ambiguous, Missing}` made `Ambiguous` a distinct outcome (kept, see below), and a per-chain-step pseudo-type rejection closed the second gap (kept). Also added a same-class-or-super lexical-nest widening (`TypeIndex::lexical_nest_of`, backed by a new `LexicalParentRecord` substrate) plus a `STATIC_IMPORT` exemption, and re-enabled hard-narrowing under `Positive`.
- **#1910 round 6**: rejected round 5's own implementation for two further defects, both found by execution, neither caught by `cargo test --workspace`: (1) `Ambiguous` was not actually terminal -- `FileTypedNames::lookup` collapsed it back to a plain miss internally, so `resolve_identifier_receiver` could not tell it apart from `Missing` and let it fall through to the open-world fallback chain, resolving `Positive` on a coincidental class-name collision; (2) `direct_lexical_parent` was keyed by bare type name alone, repo-wide -- two different files each declaring a same-named nested class (`Mid`) collided last-write-wins, non-deterministically across indexing runs. Round 6 fixed (1) by making `Ambiguous` genuinely terminal (kept) and (2) by re-keying to `(file_id, bare name)`.
- **#1910 round 7**: proved BY EXECUTION that round 6's own fixes were still insufficient, in ways an adversarial probe found but ordinary test-writing did not: (1) the `(file_id, bare name)` re-key still collides WITHIN one file -- two DIFFERENT outer classes each declaring a same-named nested class (`OuterA.Builder`/`OuterB.Builder`) share the same key, and the caller-side context this pass receives (`same_class_context`) is itself only ever a bare simple name, never a qualified one, so fixing the substrate's OWN key is not sufficient without ALSO threading a qualified/unique type identity through the entire reference-site pipeline (a much larger, structurally separate change -- see "What a future attempt needs" below); (2) round 6/7's `Positive`-earning per-file substrate (`FileStaticTypeNames`/`is_declared_in_callers_package`, promoted to `Positive` whenever `FileTypedNames::lookup` returned `Missing`) rests on an unproven assumption that `Missing` means "genuinely no local binding" -- round 7 proved by execution that a captured local inside an anonymous/local class is looked up under the WRONG (inner) enclosing-method key and can produce a FALSE `Missing` for a real local, indistinguishable from a genuine absence at the call site; (3) independently, issue #1915 (found during round 7's own probing, filed separately) proved the `STATIC_IMPORT` exemption a hard same-class-or-super narrow would need is itself unreliable: `extract_imports` classified a static-on-demand import (`import static pkg.Util.*;`) as an ordinary `Wildcard`, so it earned ZERO reason bits, not even the coarse `STATIC_IMPORT` one -- a decoy same-named candidate elsewhere in the repo could then destroy the real edge outright via the pre-existing, unrelated `apply_import_context_narrowing` pass. **This salvage reverts round 5-7's receiver-type/same-class-or-super hard-narrow entirely** (both passes return to and stay permanently tag-only) while keeping the three genuinely-verified fixes (below) and fixing #1915 at the extraction/classification level (issue #1915, independent of narrowing).

#### What survived the revert (verified sound by execution, kept)

1. **`LocalLookup::{Found, Ambiguous, Missing}`, with `Ambiguous` TERMINAL** (`receiver::FileTypedNames`): a `(enclosing_method, name)` key that ever sees two DIFFERENT declared types across sibling blocks is marked permanently `Ambiguous`, and `resolve_identifier_receiver` resolves it straight to `ReceiverEvidence::None` -- never falling through to either open-world fallback. This is real, useful precision even under permanently tag-only narrowing: without it, an ambiguous local could wrongly earn a `RECEIVER_TYPE_MATCH` tag against a coincidentally same-named class. Kept with its full test coverage.
2. **Chain-step pseudo-type rejection** (`resolve_receiver_type`): `is_pseudo_type` is applied to EVERY chain step's resolved type, not just the base identifier, so a generic method returning a bare type parameter (`<T> T get()`) can never leak a fabricated receiver type mid-chain into a tag.
3. **JLS 6.3-comprehensive local-binding extraction**: Java's local-declaration grammar is a closed, finite set (locals, fields, parameters, catch parameters, resources, enhanced-for variables, every pattern-variable form). Extraction now covers all of it, via the grammar's own closed pattern-matching node kinds (`type_pattern`, `record_pattern_component`) rather than enumerating Java features one at a time -- this closes not just switch-pattern/record-pattern shapes but any future context tree-sitter-java reuses those nodes for, at ANY nesting depth, for free. This genuinely improves `RECEIVER_TYPE_MATCH` tagging accuracy (fewer false `Missing` results feeding the Advisory fallback) even though it can no longer drive a hard-narrow.
4. **`FileStaticTypeNames`/`is_declared_in_callers_package` were REMOVED, not demoted.** Round 7 proved them unsound as a `Positive`-earning substrate (the captured-local scope-key problem above); demoting them to `Advisory` instead of deleting them was considered and rejected as pure redundancy -- both conditions are already strict subsets of the pre-existing, unrelated `TypeIndex::is_known_type_name` Advisory fallback (any type a file declares/imports is, by construction, also "known" repo-wide), so keeping them added complexity with zero behavioral difference.
5. **The lexical-parent substrate (`LexicalParentRecord`, `TypeIndex::direct_lexical_parent`/`lexical_nest_of`) was DELETED entirely, not merely disconnected from hard-narrowing.** Round 7 proved the `(file_id, bare name)` key still collides WITHIN one file, and the caller-side context (`same_class_context`) is itself only ever a bare name -- so even using this substrate for TAGGING only would still let a wrong lexical-parent lookup corrupt the `SAME_CLASS_OR_SUPER` tag's accuracy. With no live consumer left (same-class-or-super reverted to its pre-#1910 direct-inheritance-only `allowed` set, `{enclosing_type} U supertypes_of(enclosing_type)`), keeping the substrate around would be dead code (Rule 12, anti-orphan-code).
6. **The mis-narrow counters** (`RepoIndexResult::narrowed_to_zero_count`/`narrowed_to_nonempty_strict_subset_count`, mirrored in `BuildGraphResult`): both counters are kept and remain useful VOLUME signals -- see "Observability" below for what they can and cannot tell you.
7. **Issue #1915's fix** (`ImportKind::StaticWildcard`, `extract_imports`, `import_reasons`): a static-on-demand import (`import static pkg.Util.*;`) is now classified as its OWN kind, checked before the plain-`Wildcard` branch, and `import_reasons` resolves it PRECISELY (comparing both the declaring class and package, not the coarse name-only heuristic `Static` uses) -- a real, independent fix, kept regardless of the narrowing revert, because it protects `apply_import_context_narrowing` (a pre-existing, untouched, genuinely hard-narrowing pass) from destroying a real edge via a same-named decoy.

**`apply_same_class_or_super_narrowing`'s own `allowed` set (`{enclosing_type} U supertypes_of(enclosing_type)`) covers only DIRECT inheritance evidence** -- it has never modeled two other places Java resolves an unqualified call: the caller's LEXICALLY ENCLOSING type chain (inner/anonymous/static-nested/local classes calling an outer method) and STATIC IMPORTS. An empty match here is therefore NOT reliable evidence of an external target the way it is for arity -- it can just as easily mean "the real target lives in a lexically enclosing type or arrived via a static import, neither of which `allowed` can see". A/B-probed end-to-end on real javac-valid source: hard-narrowing here flips `is_definitely_dead_code` from `Some(false)` to a FALSE `Some(true)` on a live private target -- the exact outcome epic #1786 declared structurally impossible.

- **`try_unique_name_shortcut`** is unaffected by any of this: it still declines its shortcut on a POSITIVE receiver-type mismatch, an ADMISSION decision (whether a singleton pool may skip the rest of the pipeline) distinct from the DELETION a hard-narrow would perform on an N-candidate set.
- **Corrected claim (was: "can never cause an under-exclusion of a live target")**: an earlier version of this document claimed the `STATIC_IMPORT` tagging's name-only coarseness could only ever cause over-inclusion of noise, never under-exclusion of a live target. That was FALSE, proven by issue #1915: a MISCLASSIFICATION bug (static-on-demand imports silently earning zero reason bits at all, not merely a coarse one) let the pre-existing `apply_import_context_narrowing` pass hard-delete a real edge via a same-named decoy. The honest claim is narrower: GIVEN a correct classification (now true after #1915's fix), the remaining name-only coarseness of the `Static`/`STATIC_IMPORT` heuristic is a safe-direction approximation (over-inclusion only) -- but that guarantee has never been formally proven end-to-end across every narrowing pass that consults it, and a future classification bug in an adjacent code path could reopen the same class of risk. Treat "safe direction" as a design goal each pass is reviewed against, not a proven invariant of the whole pipeline.
- **Corrected claim (was: "a genuinely closed, per-file fact")**: an earlier version of this document claimed `FileStaticTypeNames`/`is_declared_in_callers_package` were "genuinely closed, per-file/per-caller-package facts" safe to promote to `Positive`. That was FALSE, proven by round 7's captured-local probe. The general risk class: **any substrate that earns `Positive` on a `FileTypedNames::lookup` `Missing` result is not closed-world**, because `FileTypedNames` keys locals by `(enclosing_method, name)`, which does not model Java's actual block/capture scoping -- a local captured by an anonymous or local class is looked up under the WRONG (inner) enclosing-method key, and a lookup miss for a genuinely-bound local is therefore indistinguishable from a real absence of type information. Nothing built on top of a `Missing` result can be soundly promoted to `Positive` until this is fixed at the source.

#### What a future "round 8" would need (read this before trying again)

Receiver-type-based candidate DELETION is not achievable on the current substrate. Concretely, before attempting it again:

1. **Fix the `(enclosing_method, name)` scope-key problem.** `FileTypedNames` (and any per-method-keyed local table) must become properly BLOCK-scoped and CAPTURE-aware -- either real symbol resolution (a full scope tree, tracking which block a binding is visible in and which locals an anonymous/local class captures from its enclosing method), or an equivalent mechanism that can answer "is this local visible at this exact program point" rather than "was this local declared anywhere in this method". Without this, a `Missing` lookup result can never be trusted as a genuine absence, and nothing resting on it can be `Positive`.
2. **If same-class-or-super narrowing is ever revisited**, the caller-side context threaded through the whole reference-site pipeline (`same_class_context`, `MethodOwnerRecord.enclosing_type`, `TypeIndex`'s own family/supertype maps) is ENTIRELY bare-name-keyed today, not just the lexical-parent substrate that was deleted in this salvage. A sound fix needs a qualified or unique type identity (not a bare simple name) threaded through ALL of these, not just the lexical-parent map alone -- re-keying only the lexical-parent map (as round 6 did) closes nothing, because the caller-side lookup that would consult it is already collapsed to an ambiguous bare name before it gets there.
3. **Prove any new substrate by ADVERSARIAL execution, not by test-writing alone.** Six of seven rejected rounds were caught by hand-constructed test fixtures that looked complete at the time; round 7's own findings were only found by deliberately constructing adversarial probes (a within-file same-named-nested-class pair, a captured local shadowing an import) rather than by writing more of the same shape of test. A green `cargo test --workspace` run is evidence of "does not regress the fixtures we thought of", never evidence of closed-world soundness.

#### Observability: the mis-narrow counters (still useful, kept)

`RepoIndexResult::narrowed_to_zero_count` (`repo_index.rs`) and `--build-graph`'s JSON (`BuildGraphResult::narrowed_to_zero_count`, `xray-cli/src/main.rs`) surface how many references, across the whole build, had a real same-named declaration somewhere in the repo but were narrowed all the way to zero final candidates -- driven by every HARD pass that remains (`apply_arity_narrowing`, `apply_private_visibility_filter`/D2 -- which CAN empty a set when every remaining candidate is private and cross-top-level, `apply_super_class_narrowing`/D3, `apply_import_context_narrowing`, all pre-existing and unaffected by this salvage), since receiver-type/same-class-or-super are permanently tag-only and can no longer produce a zero-candidate outcome by themselves. A second counter, `RepoIndexResult::narrowed_to_nonempty_strict_subset_count` (mirrored in `BuildGraphResult`), counts references whose final candidate count is non-empty but STRICTLY SMALLER than their bare-name pool -- driven by that same set of remaining hard passes. **Both counters are VOLUME signals only: they count narrowing EVENTS, they cannot by themselves distinguish a correct narrow from a wrong one.** Three rounds of #1898's own narrowing regressions survived a green 531-test suite because nothing counted this at all; a future run over a mixed/unusual codebase can now at least see the number move without waiting for a symptom report, but a non-zero count is a prompt to investigate, never proof of a bug, and a zero count is not proof of correctness either.

Every mechanism here operates purely on syntactic evidence recorded by the extractor -- none performs full type inference, and none can see across a compiled dependency boundary the extractor never parsed.

For ready-to-adapt graph-mode evaluator templates exercising this model, fetch the X-Ray Cookbook: `get_file_content(repository_alias='code-indexer-global', file_path='docs/xray-cookbook.md')`.

## xray_search_batch MCP Tool

Cross-repo, multi-expression X-Ray sweep in ONE background job. Distinct from `xray_search` omni path: returns a single `job_id` (not `job_ids`), accepts N repo aliases x M scan bundles (matrix), and tags every match with `repository_alias`, `scan_index`, and `pattern_name`.

`src/code_indexer/server/mcp/handlers/xray_batch.py` — `handle_xray_search_batch` is the MCP handler shim:

- **Auth check**: `user.has_permission("query_repos")` or returns `auth_required`.
- **Parameter validation**:
  - `repository_alias` required (string, list, or JSON array). Max 50 aliases after dedup.
  - `scans` required (non-empty list). Max 50 bundles.
  - Each scan bundle: `driver_regex` required; `evaluator_code` and `pattern_name` mutually exclusive; inline `evaluator_code` validated via `validate_rust_evaluator()` before job submission.
  - `timeout_seconds` in `[10, 7200]` (wider than single-repo xray_search — matrix covers many cells).
  - `await_seconds` in `[0, 30]` (lower than xray_search — batch rarely completes inline).
  - `max_results` >= 1 when provided (per-cell file cap, not a global cap).
- **Repository alias resolution**: each alias resolved via `_resolve_repo_path`. Global-alias fallback applied via `try_global_fallback` when alias unresolvable but a `{alias}-global` golden repo is active. Unresolvable aliases become `error_level="repo"` entries in `errors[]`. If ALL aliases fail: synchronous `no_repositories_resolved` error. If SOME fail: job submitted over resolved subset with `partial=True`.
- **Job submission**: ONE job via `background_job_manager.submit_job(operation_type="xray_search_batch", repo_alias=None, ...)`. The worker `_run_xray_batch_job` is closed over all resolved state.
- **Optional inline await**: same `_await_job_result` pattern as `xray_search` but with `await_seconds` capped at 30.

`_run_xray_batch_job` — the matrix worker function (runs in the background job thread):

- Iterates `resolved_repos x scans` (outer=repos, inner=scans) with between-cell cancellation checks via `bjm.jobs.get(job_id).cancelled`.
- Per cell: calls `resolve_batch_evaluator(scan, repo_alias, cidx_meta_path)` then `XRaySearchEngine().run(...)`. Cell exceptions are caught and recorded as `error_level="cell"` entries.
- Progress advances once per REPO (not per cell or file): `progress_callback(repos_completed/total * 100, ...)`.
- Timeout checked per cell via `time.monotonic()` against `deadline`.
- Sets `partial=True` whenever any error, evaluation_error, timeout, or cancellation occurs.
- Returns unified result dict: `matches[]`, `errors[]`, `evaluation_errors[]`, counters (`total_repos`, `total_scans`, `total_cells`, `repos_completed`), and boolean flags (`partial`, `timeout`, `cancelled`).

`resolve_batch_evaluator(scan, repo_alias, cidx_meta_path)` — pure per-cell helper:

- Resolution order: inline `evaluator_code` → `pattern_name` (repo-specific scope then `__any__/`) → default accept-all evaluator.
- Instantiates `XrayPatternService(cidx_meta_path, refresh_scheduler=None)` — no live app state.
- Returns `(evaluator_code_str, error_dict_or_None)`. On `pattern_not_found` or malformed YAML: returns `(None, {"error": "pattern_not_found"|"cell_execution_error", ...})`.

`_truncate_xray_batch_result(result, payload_cache)` — batch-specific result truncation:

- Serializes combined matches + errors + evaluation_errors as JSON.
- If serialized size <= inline threshold: returns original result dict.
- Otherwise: stores full JSON in `PayloadCache`, returns truncated dict with first 3 entries of each list plus `cache_handle`, `has_more`, `truncated`, `fetch_tool_hint`.

**Unified polled result shape** (GET /api/jobs/{job_id}):

- `matches[]` — each entry: `{repository_alias, scan_index, pattern_name, file_path, line_number, pattern, snippet, ...}`.
- `errors[]` — `error_level="repo"` (no `scan_index`) or `error_level="cell"` (with `scan_index`).
- `evaluation_errors[]` — per-file evaluator failures: `{repository_alias, scan_index, file_path, error_type, error_message}`.
- Counters: `total_repos`, `total_scans`, `total_cells`, `repos_completed`.
- Flags: `partial` (bool), `timeout` (bool), `cancelled` (bool).

`partial=True` triggers: any repo error, any cell error, any evaluation_error, timeout, cancellation, or per-cell `partial=True` (max_results cap hit).

REST shim: `POST /api/xray/search/batch` in `src/code_indexer/server/routes/xray_routes.py`. Converts Pydantic body to params dict, delegates to `handle_xray_search_batch`, translates MCP error envelope to HTTP status codes.

Tool doc: `src/code_indexer/server/mcp/tool_docs/search/xray_search_batch.md`. Registered in `HANDLER_REGISTRY` via `_legacy.py` (`_xray_batch_register`).

**Files**: `src/code_indexer/server/mcp/handlers/xray_batch.py`, `src/code_indexer/server/routes/xray_routes.py`. Tests: `tests/unit/server/mcp/test_xray_search_batch_handler.py`.

## Evaluator security model

The current MCP/REST evaluator contract runs the caller-supplied Rust source through `xray-core`/`xray-cli`'s own compile-and-execute pipeline (`fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding>` for single-file mode, `fn collect_facts` + `fn analyze_graph` for graph mode). See the [xray_search tool documentation](../src/code_indexer/server/mcp/tool_docs/search/xray_search.md) for its compile step and security restrictions.

The codebase also retains an internal Python AST whitelist (an allow-listed node set, a small safe-builtins list, and a matching set of banned constructs) belonging to a prior evaluator generation. That module still exists in-tree but is not on the evaluation path for `xray_search`/`xray_explore` and is not the MCP/REST contract. See [X-Ray Sandbox](xray-sandbox.md) for its class name and retained internals.
