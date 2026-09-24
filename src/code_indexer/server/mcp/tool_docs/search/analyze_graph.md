---
name: analyze_graph
category: search
required_permission: query_repos
tl_dr: "Multi-file, whole-repository graph analysis -- builds a real CSR reference graph across every file in the repo, then runs your Rust evaluator's fn analyze_graph over the whole graph. Use for cross-file questions single-file AST search (xray_search) cannot answer: dead code, unreachable/unwired components, layering violations, endpoint-to-sink reachability, blast radius."
slim_description: "Whole-repository graph-mode analysis (Java + Kotlin): builds a real cross-file reference graph, then compiles and runs your Rust evaluator's `fn collect_facts(node: &OwnedNode, file: &str) -> Vec<UserFact>` (per-file) and `fn analyze_graph(g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> GraphResult` (whole-graph reduce); an optional third `fn refine` re-examines flagged symbols with real source access. Answers cross-file questions xray_search cannot: dead code, unwired components, layering violations, reachability, blast radius -- honestly, via fact_graph_complete. Full contract and examples: cidx_quick_reference(tool=\"analyze_graph\")."
inputSchema:
  type: object
  properties:
    repository_alias:
      oneOf:
      - type: string
      - type: array
        items:
          type: string
      description: 'Repository identifier(s) to analyze. String for a single repository; array of strings to analyze several. JSON-encoded string arrays (e.g. ''["repo-a","repo-b"]'') are also accepted and parsed as arrays. Use list_global_repos to see available repositories. NOTE: several repositories are analyzed ONE AT A TIME, each against its own graph -- this is NOT a union graph, and dense symbol ids are per-repository, so they never mean anything across repositories. A multi-repository request returns a different envelope: {mode: "multi_repo", repositories: [...], results: {alias: <single-repo result>}, errors: [{repository_alias, error, message}]}, with ok true only when every repository succeeded. A single string, or a one-element array, returns the ordinary single-repository shape.'
    evaluator_code:
      type: string
      description: 'Inline Rust graph evaluator defining TWO REQUIRED functions: fn collect_facts(...) and fn analyze_graph(...). Mutually exclusive with pattern_name. Both functions must be defined together; graph evaluators must not define fn evaluate_node. The same Rust security whitelist as xray_search applies.'
    pattern_name:
      type: string
      description: 'Stored graph-mode pattern name to resolve from the X-Ray pattern library. Mutually exclusive with evaluator_code. Repository-specific patterns take precedence over __any__. A legacy pattern (including a pre-existing pattern with no execution_mode) is rejected with pattern_mode_mismatch.'
    pattern_params:
      type: object
      description: 'Optional typed parameter overrides for the stored graph pattern, using the same substitution semantics as xray_search. Ignored unless pattern_name is supplied.'
      additionalProperties: true
    include_patterns:
      type: array
      items:
        type: string
      description: 'Glob patterns for files to include in the graph (e.g. ["*.java", "*.kt"]). These patterns DO narrow what is read into the graph. PASS ["*.java", "*.kt", "*.kts"] -- the graph extractor covers Java and Kotlin, so on a repository holding other languages an empty list pulls in every other file, inflates the files_with_unsupported_language degradation counter, and makes fact_graph_complete: true unreachable. NEVER narrow to one of the two on a mixed Java/Kotlin repository: excluding the other language does not make its absence safe, it only hides it -- a Java method called solely from Kotlin then has no inbound edge and can be reported definitely dead. Empty list means include all files.'
      default: []
    exclude_patterns:
      type: array
      items:
        type: string
      description: 'Glob patterns for files to exclude from the graph (e.g. ["*/test/*", "*/vendor/*"]). Empty list means exclude none.'
      default: []
    timeout_seconds:
      type: integer
      description: 'Wall-clock timeout in seconds for the WHOLE pipeline (repo-alias resolution, file collection, evaluator compile, --build-graph, --analyze-graph, and --refine when refine=true). Range 10..600. Default 120.'
      minimum: 10
      maximum: 600
      default: 120
    refine:
      type: boolean
      description: 'Opt-in: when true AND your analyze_graph populated a non-empty GraphResult.refine, runs the optional refine phase as a THIRD subprocess -- a real per-file AST pass over exactly the files a flagged symbol belongs to (never the whole repo), with FULL access to both the file''s OwnedNode and the whole GraphHandle/FactsHandle simultaneously, which analyze_graph alone cannot provide (it never receives file source). Use this when a finding needs source-level detail (the actual method body, a specific line, an AST-derived risk signal) that dense ids and cached signatures cannot carry. Deliberately NOT the default: it re-opens the timeout_seconds budget (a genuine second traversal, on top of build+analyze) and costs real time at fleet scale, so it only runs when you explicitly ask AND there is something to refine -- refine=true against an evaluator that never populates GraphResult.refine spawns nothing. See the refine_status/refine_findings output fields for the response shape, and fn refine in the "Optional third function" section of the full reference (cidx_quick_reference(tool="analyze_graph")) for how to write one.'
      default: false
    await_seconds:
      type: number
      description: 'Reserved for future async job-polling parity with xray_search. Currently accepted but INERT: analyze_graph always runs synchronously to completion or until timeout_seconds -- it never returns a bare {job_id}. Do not rely on this parameter changing behavior yet.'
      minimum: 0
      maximum: 45.0
      default: 0
  required:
    - repository_alias
outputSchema:
  type: object
  properties:
    ok:
      type: boolean
      description: 'True when the graph pipeline completed without an error. This includes status="ran_ok" and the server-derived status="no_supported_files" (no candidate file reached a supported-language extractor, AND none had a genuine parse error -- see "no_supported_files status" below for the exact two-condition rule). False for every error path (validation, missing repo, build failure, timeout, internal error) -- check `error` for the reason.'
    error:
      type: object
      description: 'Present iff ok=false. Shape: {error_type, error_message} for pipeline-level failures (ValidationError, BinaryNotFound, CompileError, GraphBuildError, XRayCliError, Timeout, InternalError), or a synchronous rejection shape {error, message} for input-validation failures (invalid_params, auth_required, evaluator_code_required, repository_alias_required, include_patterns_invalid, exclude_patterns_invalid, timeout_seconds_invalid, refine_invalid, xray_evaluator_validation_failed, xray_cell_queue_timeout, repository_not_found, no_candidate_files, mutually_exclusive_params, pattern_mode_mismatch). One rejection applies only when you pass SEVERAL repositories and is refused up front before any repository is touched: `repo_count_cap_exceeded` (more repositories than the server''s omni cap allows). Two more error codes are multi-repo-ONLY but are NEVER a top-level rejection -- each surfaces per-repository inside that repository''s own `errors[]` entry. Every `errors[]` entry carries a non-empty `message`, but `error` itself is NOT always a string code: for a real compile/build/analyze failure it is the SAME `{error_type, error_message}` OBJECT the pipeline-level shape above uses, and `message` is synthesized from it when the underlying failure carries no top-level message of its own -- always check `error`''s type before treating it as a bare code string. The two multi-repo-only codes: `multi_repo_deadline_exceeded` (this repository was never started because a BETWEEN-REPOSITORY ADMISSION GATE observed the request''s elapsed wall clock already at or past the server''s 600s threshold before this repository''s turn came up -- earlier repositories'' real results are preserved; split the request into smaller batches and retry the remainder. This is an admission gate, not a hard ceiling on total request time: the repository that was ALREADY RUNNING when the gate last passed is not itself time-bounded by this threshold and can still take substantially longer) and `multi_repo_pipeline_exception` (an unhandled error was raised while analyzing this specific repository; other repositories in the same request are unaffected; the message never includes server-internal detail such as filesystem paths). Note `timeout_seconds` is PER REPOSITORY, not shared across them.'
    status:
      type: string
      description: 'The real --analyze-graph ChildReport status, OR the server-derived "no_supported_files": "ran_ok" (your analyze_graph executed), "no_supported_files" (ok=true, but no candidate file reached a supported-language extractor AND none had a genuine parse error -- see "no_supported_files status" below for the exact two-condition rule; a candidate set with even ONE genuine parse error keeps "ran_ok" regardless of how many other files were unsupported-language), "absent" (evaluator does not export analyze_graph -- should not happen given evaluator_code validation), "load_failed" (dylib failed to load), "graph_invalid" (the built graph file was corrupt), "panicked" (your analyze_graph panicked -- caught, never crashes the server).'
    findings:
      type: array
      description: 'Your analyze_graph function''s GraphResult.findings -- a list of ReduceFinding {pattern, message, involved, signatures}. involved is the ordered chain of SymbolIds the finding''s path walks through (single element for a simple flag, multi-element for a reachability/blast-radius path). signatures[i] is involved[i]''s cached signature line, parallel to involved.'
      items:
        type: object
    refine:
      type: array
      description: 'SymbolIds your analyze_graph flagged via GraphResult.refine for a follow-up per-file look. Always populated (or empty) regardless of the refine request parameter -- this is analyze_graph''s own output, not the refine pass''s output (see refine_findings for that). Pass refine: true to actually run the follow-up pass over these symbols'' files.'
      items:
        type: integer
    refine_status:
      type: string
      description: 'Whether/how the opt-in refine phase ran: "not_requested" (the refine request parameter was false/omitted -- the default), "skipped_empty_refine_set" (refine: true was passed but GraphResult.refine was empty -- nothing to refine, so no subprocess ran), "skipped_no_matching_files" (refine''s flagged symbols resolved to no file in the candidate set -- should not happen against a graph built from that same set, but reported honestly rather than silently), "skipped_analysis_failed" (analyze_graph itself did not succeed, so refine was never attempted), "skipped_timeout" (the request''s timeout_seconds budget was already exhausted by build+analyze, so refine was skipped rather than started with no time left), "absent" (the compiled evaluator does not export fn refine -- refine is OPTIONAL, this is not an error), "ran" (refine executed; check refine_findings), "error" (a real refine failure -- see refine_error; the primary analyze_graph result above is unaffected and still reflects ok=true).'
    refine_findings:
      type: array
      description: 'Present only when refine_status is "ran": the flattened per-file findings your fn refine callback returned, each {pattern, file, line, snippet} (mirrors xray_search''s own finding shape) -- file is the repo-relative path fn refine actually ran against. Empty (but refine_status still "ran") is a legitimate outcome: your refine callback simply found nothing in the narrowed file set. Capped at 500 entries total; see refine_findings_truncated.'
      items:
        type: object
    refine_files_examined:
      type: integer
      description: 'Present only when refine_status is "ran": the number of DISTINCT files refine actually re-parsed -- the narrowed intersection of (files a flagged GraphResult.refine symbol belongs to) with (the same driver-matched candidate file set the build/analyze phases already used), never the whole repository.'
    refine_findings_truncated:
      type: boolean
      description: 'Present and true only when refine_findings was capped at 500 entries -- more real findings existed than were returned inline.'
    refine_error:
      type: string
      description: 'Present only when refine_status is "error": a sanitized description of why the refine subprocess itself failed (never a raw exception or a server-internal path). The primary analyze_graph result is unaffected either way -- a refine failure degrades gracefully rather than failing the whole request.'
    fact_graph_complete:
      type: boolean
      description: 'THE honesty signal this tool exists to provide. True only when the graph build hit NO degradation (no truncation, no parse errors, no extractor/collector panics, no read errors, no index-budget trip, no unsupported-language files). False means the graph is INCOMPLETE -- an empty findings[] in that case means "the index was too incomplete to trust a negative", NOT "nothing was found". Always check this before treating an empty findings[] as a clean bill of health, especially for dead-code-style analyses (see "Directional asymmetry" below). NOTE: since the graph extractor currently supports JAVA AND KOTLIN ONLY, this will be false for any repo containing candidate files in other languages -- see files_with_unsupported_language under degradation. When false, check completeness_reasons for the specific named cause(s) (one or more of repo_index_incomplete, index_budget_exceeded, resolution_ambiguous) instead of inferring a reason from the degradation counters alone -- a build can be degraded by more than one cause at once, and completeness_reasons lists every one that applied.'
    build_status:
      type: string
      description: '"ok" on a successful build, or the specific failure: "repo_root_invalid", "load_failed", "file_id_collision", "graph_write_failed", "facts_write_failed". Present even on some overall failures so a caller can distinguish a build-time problem from an analyze-time one.'
    degradation:
      type: object
      description: 'The 10 real degradation counters/flags from the build, verbatim -- never masked with a default. Keys: files_with_parse_errors, unreadable_or_unsupported_files, files_with_read_errors, files_with_extractor_panics, files_with_collector_panics, files_with_unsupported_language, truncated_by_max_files, index_budget_exceeded, files_excluded_with_extractor, files_excluded_without_extractor. files_with_unsupported_language counts files with a RECOGNIZED source-language extension for which the graph engine has no extractor yet (currently every language except Java and Kotlin) -- distinct from unreadable_or_unsupported_files (a genuinely unsupported/no extension). index_budget_exceeded is true iff completeness_reasons (see below) contains "index_budget_exceeded" -- a direct boolean for this one particularly actionable condition, alongside the full list at the top level. files_excluded_with_extractor counts a DIFFERENT thing from files_with_unsupported_language: a file that was never even a candidate because include_patterns/exclude_patterns excluded it BEFORE the Rust build ever saw it, even though its language DOES have a graph extractor -- a nonzero value here always forces fact_graph_complete: false, since that file could have contributed a real call edge had it been read. files_excluded_without_extractor counts the same kind of pre-candidate exclusion but for a file whose language has NO extractor either way (e.g. a stray README.md) -- excluding it never affects completeness, since including it would not have produced a real call edge regardless. See languages_excluded_with_extractor below for which languages a nonzero files_excluded_with_extractor names. Any missing/None value under a "ok" build_status indicates malformed data, not a clean build.'
    completeness_reasons:
      type: array
      items:
        type: string
      description: 'The lossless list of every completeness condition that held for this build. Unlike fact_graph_complete (a single boolean) or any one degradation counter, a build can be degraded by MORE THAN ONE independent cause at once (e.g. an index-budget trip AND a repo-level read error) -- this field lists every one that applied, never just whichever one a first-write-wins internal collapse happened to keep. Values that can appear today: "repo_index_incomplete" (the file set itself was incomplete before binding -- truncation, an extractor panic, a read error, a genuine parse error, or an unsupported-language file), "index_budget_exceeded" (the repo-wide raw-candidate-count ceiling was breached -- see candidate_count/candidate_budget_limit below), "resolution_ambiguous" (an inheritance-family expansion was capped). An empty list means the build was fully complete (fact_graph_complete: true). Three further values are reserved on the underlying engine enum for a future slice and cannot appear here today -- their absence is not evidence they are impossible in general, only that this build did not trip them.'
    candidate_count:
      type: integer
      description: 'The raw candidate-edge total the index budget ladder measured this build against.'
    candidate_budget_limit:
      type: integer
      description: 'The configured ceiling candidate_count is measured against (currently 2,000,000). Compare the two to judge how far over budget a build was, and therefore how much narrowing include_patterns/exclude_patterns would need to bring it back in budget.'
    truncated_by_max_files:
      type: boolean
      description: 'Top-level convenience mirror of degradation.truncated_by_max_files, OR-ed with a Python-side candidate-file-collection cap (50,000 files, applied before any file ever reaches the Rust build) that Rust itself never sees and therefore cannot report inside degradation. In the one scenario where the Python-side cap is what actually truncated the request, this top-level field is true while degradation.truncated_by_max_files stays false (Rust''s own counter, verbatim, unaffected by a cut it never observed) -- always prefer this top-level field to judge whether the FILE SET was truncated for any reason; degradation.truncated_by_max_files tells you only whether Rust''s own --build-graph step independently truncated it. Either cause also pushes "repo_index_incomplete" onto completeness_reasons and sets fact_graph_complete: false.'
    languages_excluded_with_extractor:
      type: array
      items:
        type: string
      description: 'The sorted, deduplicated list of language names (e.g. ["Kotlin"]) that had at least one file excluded by include_patterns/exclude_patterns BEFORE it ever became a graph candidate, where that language DOES have a real graph extractor. Names WHICH language went unread, not merely that something did -- pair with degradation.files_excluded_with_extractor for the count. Always empty when that counter is zero. A nonzero entry here always means fact_graph_complete: false and "repo_index_incomplete" in completeness_reasons for this same reason.'
    cached:
      type: boolean
      description: 'True when the evaluator .so was served from the compile cache instead of freshly compiled.'
    compile_ms:
      type: integer
      description: 'Milliseconds spent compiling the evaluator (0 on a cache hit).'
    cache_handle:
      type: string
      description: 'Present (non-null) only when findings/refine were truncated for size. Opaque handle for retrieving the FULL, uncapped findings[]/refine[] via the cidx_fetch_cached_payload MCP tool. Null when the result fit inline (see truncated).'
    has_more:
      type: boolean
      description: 'True iff findings/refine were truncated (synonym for truncated) -- more data exists beyond what is inlined here.'
    truncated:
      type: boolean
      description: 'True when the combined findings[]/refine[] JSON exceeded the server''s single payload character budget (Web UI payload_max_fetch_size_chars, default 5000 chars) and was truncated to a whole-entry inline prefix; false when the full arrays are inline. See "Result truncation and paging" below for the exact contract.'
    total_pages:
      type: integer
      description: 'Present only when truncated is true: the number of independently-fetchable cache pages the full findings[]/refine[] content was split across (each page a whole-entry slice, never a partially-cut entry). Pass page=1..total_pages to cidx_fetch_cached_payload to retrieve every page.'
    inline_entry_truncated:
      type: boolean
      description: 'True only in the rare case where the SINGLE FIRST entry (across findings then refine, in that order) alone exceeds the character budget -- it is then adaptively shrunk (string/list fields capped) for THIS inline preview only, never mutating what is stored under cache_handle, where it remains whole and unmodified. Absent or false in the ordinary case (the inline prefix is one or more UNMODIFIED whole entries).'
    fetch_tool_hint:
      type: string
      description: 'Present only when truncated is true: human-readable guidance naming the cidx_fetch_cached_payload MCP tool and describing its independently-parseable-page format (see below).'
---

Whole-repository, multi-file graph analysis. `xray_search` inspects one file's AST at a time -- it structurally cannot answer "is this method called from anywhere in the OTHER files of this repo?" `analyze_graph` builds a REAL cross-file reference graph (CSR arena, receiver-type resolution, inheritance-family expansion) spanning every indexed file, then runs your evaluator's `fn analyze_graph` as a single whole-graph reduce over it.

**Language support: JAVA and KOTLIN (`.kt`/`.kts`).** The graph extractor that populates each file's declarations implements two languages. Java and Kotlin bind to EACH OTHER -- a Kotlin call to a Java method and a Java call to a Kotlin function both produce edges -- so a mixed repository is analysed as one graph, not two.

Every other engine-supported language (Python, TypeScript, JavaScript, Go, C#, etc.) has no graph extractor yet: files in those languages parse fine but contribute zero declarations, and are counted via the `files_with_unsupported_language` degradation counter. A repository containing any candidate file outside Java/Kotlin will never report `fact_graph_complete: true`. Scope `include_patterns` to `["*.java", "*.kt", "*.kts"]` (or restrict the repo) to get a genuinely complete graph today.

**Kotlin scope limits, stated honestly.** Kotlin is extracted at bind levels 0-2 (declarations, references, imports, inheritance). It has no receiver-type substrate (its extractor never populates `LocalIndex::typed_names`), so Kotlin hops never carry `RECEIVER_TYPE_MATCH` from receiver narrowing -- but that costs EVIDENCE QUALITY only, never an EDGE, for an ordinary qualified call: `apply_receiver_type_narrowing` is permanently tag-only in this binder (it may set `RECEIVER_TYPE_MATCH`, it never deletes a candidate), so a KOTLIN instance-qualified call (`g.helper(x)`, a variable receiver) still binds by name+arity exactly like a Kotlin type-qualified one (`Type.helper(x)`), and a target reachable only through the instance form is not reported definitely dead. Deliberately different for JAVA only, gated on the CALLING file's language: a call DEFINITELY qualified by a type reference (`Type.helper(x)`, `Type::helper`) hard-narrows to exactly that type's own tagged declarations, but ONLY when BOTH hold: the qualifier positively resolves to a type declared somewhere in this repo, AND at least one candidate is already confirmed to match it. Every other case -- the qualifier resolves to no known in-repo type (an external/JDK receiver, or a same-named repo declaration the extractor could not positively rule out) or resolves to a type with zero matching candidates (an incomplete-extraction gap, not proof of absence) -- is a deliberate NO-OP: the pool is left exactly as tag-only narrowing would have left it, never emptied. The qualifier-is-a-type conclusion itself guards against the shadowing sources this extractor can see: a same-file local/parameter/field (including an interface's implicit `public static final` constant fields) and a single-member static import (`import static pkg.Holder.NAME;`) anywhere in the file. The field check is REPO-WIDE, but only WITHIN the files actually analysed this run -- an INHERITED field is invisible to it whenever the declaring supertype sits outside the analysed set (`include_patterns` scoping excluded it) or is declared by a KOTLIN file (the Kotlin extractor never populates `typed_names` at all, so it records no fields, ever, even for a Kotlin type that IS in the analysed set). Per JLS 6.4.2 a field always wins over a same-named type reference at that syntax position, so this gap is exactly why hard-narrowing carries two further, WHOLE-FILE guards before it may fire at all: (1) the caller's file must have NO static WILDCARD import (`import static pkg.Holder.*;` -- unlike a single-member import, it names no specific identifier, so nothing can be positively matched against it; its mere presence disables hard-narrowing for every call site in that file), and (2) **narrowing applies only in files where NO type -- including a nested, local, or anonymous class -- declares ANY `extends`/`implements` clause at all.** Matching a supertype's NAME against a set of declared types is never trustworthy evidence for this guard: a file can declare its own unrelated type sharing the exact bare name of a call's real, externally-qualified supertype, or the real supertype can be reached only through a sibling nested class, an anonymous class body, or plain same-package resolution with no import -- every one of these can make a name-based check pass while the real supertype (the one actually declaring a shadowing field) stays invisible to this binder. So the guard asks a purely syntactic question instead: does this file record ANY supertype clause anywhere in it, or any clause the extractor could not resolve to a name at all? If so, the WHOLE file falls back to tag-only, not just the one affected call site -- the clause's target NAME is irrelevant. A type with no supertypes at all (an ordinary static facade, or any class implicitly extending only `java.lang.Object` -- no `extends`/`implements` clause means no inheritance edge is ever recorded) passes trivially and still hard-narrows; an enum or record with no explicit `implements` clause also passes trivially (its implicit `Enum`/`Record` supertype is never recorded as an edge and can never carry an uppercase field), while one that DOES declare `implements` disables the guard like any other type. None of this is an exhaustive proof against every legal Java shape. A DOTTED (multi-level nested or fully-qualified) receiver (`Outer.Inner.m()`, `com.example.Target.m()`) IS consulted (issue #1931), under the identical guards above, via two structural resolution rules: a two-segment chain (`Outer.Inner`) hard-narrows only when the repo recorded a REAL nesting edge proving `Inner` is declared inside `Outer`'s own top-level private-access domain; a fully-qualified chain of any length (`com.example.Target`) hard-narrows only when a repo-declared type named the final segment is declared in a file whose package exactly equals every segment before it, joined by `.` (never a prefix/suffix match). A fully-qualified NESTED spelling (`com.example.Outer.Inner.m()`, package + nested type together) is NOT resolved by either rule and stays exactly tag-only, unchanged from before #1931 -- the two-segment nested-type rule requires exactly two segments, and the fully-qualified rule's own package-equality check never matches (a nested type's recorded package is its declaring FILE's package statement, e.g. `com.example`, never `com.example.Outer`). Either rule additionally requires EVERY segment (not merely the first) to clear the same shadowing guards a bare qualifier's own identifier does: no local/parameter binding anywhere in the file, no known field/interface-constant name anywhere in the repo, no single-member static import naming it. This is NOT a shortcut for "the chain is structurally always a field access, never a type" -- Java lets a field and a nested type share one bare name (JLS 6.3, separate namespaces), and per JLS 6.5.2 the field wins at that syntax position whenever one exists, so a chain like `A.B.run()` where `A` declares BOTH a field `B` and an unrelated nested class `B` genuinely COULD be misread as the nested type by a guard that only inspected the first segment -- checking every segment is what catches this. A field-access chain (`obj.field.m()`, `Outer.FIELD.m()` where `FIELD` is a static field) still stays untouched, but because the full-chain guard positively detects the field, not because such a collision is structurally impossible. Two further guards close a real gap the per-segment field/local/import checks above still cannot see on their own: a segment resolving to a repo TYPE is checked against its FULL ancestor chain, transitively and cycle-safely (walking every direct parent's own direct parent, and so on, not merely the segment's own immediate superclass) -- "unresolved" here means EITHER a syntactically-unparseable supertype clause anywhere in that chain OR an ancestor, AT ANY DEPTH, whose bare name is not itself a repo-declared type (an external/JDK/excluded-from-analysis class). Whenever the chain is unresolved this way, that ancestor may itself have an INHERITED field invisible to every check above no matter how many segments are inspected -- so the whole chain bails (`class a extends ExternalBase {}`, where `ExternalBase` is excluded from analysis but declares the shadowing field, is the direct-parent shape; `class Outer extends IndexedBase {}` / `class IndexedBase extends ExternalBase {}`, where `Outer`'s own DIRECT parent `IndexedBase` IS indexed but `IndexedBase`'s OWN parent `ExternalBase` is not, is the grandparent shape a direct-only check alone would miss). The FQN rule separately bails outright whenever ANY prefix segment is ALSO a known repo TYPE name, since a package name and a real declared type's bare name occupy the identical lowercase-identifier syntax space and Java never falls back to reading an accessible type name as a package fragment. Downstream tagging remains BARE-NAME-keyed like every other pass in this binder: proving `com.example.Target` or a specific `Outer.Inner` resolves correctly never disambiguates the resulting hard-narrow from an unrelated, same-bare-name type elsewhere in the repo (a different `Target` in another package, an `Inner` nested under a different outer) -- the real target is never lost, but such a decoy can still be tagged alongside it, exactly as a bare qualifier's own hard-narrow already tolerates for a bare-name collision. Once a qualifier (bare or dotted) positively confirms a subset this way, that subset is FINAL for this evidence tier: it is never subject to a second round of import-context re-narrowing (SAME_FILE/SAME_PACKAGE/import preference), which is reserved for genuinely UNQUALIFIED references -- a same-bare-name decoy declared in the CALLER's own file can no longer win over a real, fully-qualified, cross-package target just because it happens to be closer by file/package proximity. This has a real cost: a same-bare-name TYPE declared in an UNIMPORTED, unrelated OTHER package can now also receive an edge that import-context narrowing previously excluded on file/package-proximity grounds alone -- always ADDITIVE (the pass never removes an edge, only sometimes admits one more decoy alongside a real one it was already keeping), so this never turns a real edge into a false negative. A Java instance-qualified call (`g.helper(x)`) is untouched by this and stays tag-only exactly as described above. `infix` calls (`a matches b`) and essentially every OPERATOR CONVENTION are extracted: `a + b`/`a - b`/`a * b`/`a / b`/`a % b` (reaching `plus`/`minus`/`times`/`div`/`rem`), `a < b`/`a <= b`/`a > b`/`a >= b` (reaching `compareTo`), `a == b`/`a != b` (reaching `equals`), `m[k]`/`m[k] = v` (reaching `get`/`set`, discriminated by assignment context), `!f`/`-x`/`+x`/`x++`/`--x` (reaching `not`/`unaryPlus`/`unaryMinus`/`inc`/`dec`), `a..b`/`a..<b` (reaching `rangeTo`/`rangeUntil`), `x in y`/`x !in y` (both reaching `contains`), and `x += y`/`x -= y`/etc. on a non-indexed target (reaching BOTH the `plusAssign`-family name AND the plain `plus`-family name, since which one Kotlin actually picked cannot be told apart without receiver-type evidence this extractor does not track) all produce a real edge. Still NOT extracted: a COMPOUND assignment onto an INDEXED target (`m[k] += v`, which still falls back to a plain `get`) and the `invoke` convention (`f(x)` where `f` is a value of a type with `operator fun invoke` -- indistinguishable from an ordinary call without receiver-type information) -- a member reachable ONLY through one of these two remaining forms can still be reported definitely dead while genuinely called. Treat a Kotlin-only `definitely_dead` verdict on an `operator fun` reachable only via `m[k] += v` or `invoke` as unproven.

**Kotlin grammar limitation: an `object : X { fun m() {} }` literal written on ONE line.** When an object-literal expression's own opening brace, a function member inside it, and its closing brace all sit on a single source line (e.g. `val o = object : Runnable { override fun run() {} }`), the Kotlin grammar cannot parse it and loses the rest of that file's declarations. Such a file is counted in `degradation.files_with_parse_errors` and the build reports `fact_graph_complete: false`. Reformatting the SAME object literal across multiple lines (its opening brace, the member, and its closing brace each on their own line) parses correctly with full extraction -- no other change needed.

## Quick Start

Find symbols with no reference anywhere in the repository (dead code):

```json
{
  "repository_alias": "backend-global",
  "evaluator_code": "fn collect_facts(node: &OwnedNode, file: &str) -> Vec<UserFact> {\n    Vec::new()\n}\nfn analyze_graph(g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> GraphResult {\n    let mut result = GraphResult::default();\n    let mut i: u32 = 0;\n    while i < 100000 {\n        match g.resolve_symbol(i) {\n            None => break,\n            Some(sym) => {\n                if g.is_definitely_dead_code(i) == Some(true) {\n                    let sig = g.signature_for(i).unwrap_or(\"\").to_string();\n                    result.findings.push(ReduceFinding {\n                        pattern: \"dead_code\".to_string(),\n                        message: sig.clone(),\n                        involved: vec![sym],\n                        signatures: vec![sig],\n                    });\n                }\n            }\n        }\n        i += 1;\n    }\n    result\n}",
  "include_patterns": ["*.java"]
}
```

## Response envelope

Every response is a flat JSON object carrying the keys below. This is the FULL envelope -- the tool's `tools/list` entry itself does not currently publish an `outputSchema`, so this table (not the frontmatter this file is generated from) is the authoritative reader-visible list. Detailed semantics for the ones marked "below"/"above" live in the correspondingly-named section elsewhere on this page; every other key's one-line meaning here is complete on its own.

| Key | Type | Meaning |
|-----|------|---------|
| `ok` | bool | True when the pipeline completed without error (includes the honest `no_supported_files` status). False on every error path -- check `error`. |
| `error` | object, present iff `ok=false` | `{error_type, error_message}` for a pipeline failure, or `{error, message}` for an input-validation/multi-repo rejection. See the full error-code list in this tool's `inputSchema`/`outputSchema` frontmatter (readable via the source `.md` file) or the specific rejection codes named throughout this page. |
| `status` | string | `"ran_ok"` \| `"no_supported_files"` \| `"absent"` \| `"load_failed"` \| `"graph_invalid"` \| `"panicked"` -- see "no_supported_files status" below. |
| `findings` | array of `ReduceFinding` | Your `analyze_graph`'s output -- see "GraphResult / ReduceFinding" below. |
| `refine` | array of `SymbolId` (int) | Symbols your `analyze_graph` flagged via `GraphResult.refine` -- always populated (or empty) regardless of the `refine` request parameter. |
| `refine_status` | string | `"not_requested"` \| `"skipped_empty_refine_set"` \| `"skipped_no_matching_files"` \| `"skipped_analysis_failed"` \| `"skipped_timeout"` \| `"absent"` \| `"ran"` \| `"error"` -- see "Optional third function: fn refine" below. |
| `refine_findings` | array, present when `refine_status="ran"` | Flattened `{pattern, file, line, snippet}` findings your `fn refine` returned (mirrors `EvalFinding` plus `file`). |
| `refine_files_examined` | int, present when `refine_status="ran"` | Distinct files `refine` actually re-parsed (the narrowed intersection, never the whole repo). |
| `refine_findings_truncated` | bool | True only when `refine_findings` was capped at 500 entries. |
| `refine_error` | string, present when `refine_status="error"` | Sanitized description of why the refine subprocess failed. |
| `fact_graph_complete` | bool | THE honesty signal -- false means the graph build was degraded; see "AnalysisCompleteness" below. |
| `build_status` | string | `"ok"` \| `"repo_root_invalid"` \| `"load_failed"` \| `"file_id_collision"` \| `"graph_write_failed"` \| `"facts_write_failed"`. |
| `degradation` | object | The 10 raw degradation counters/flags from the build -- see "Indexing scope vs finding scope" and "AnalysisCompleteness" below for the full key list and meanings. |
| `completeness_reasons` | array of strings | Every completeness condition that held: `"repo_index_incomplete"` \| `"index_budget_exceeded"` \| `"resolution_ambiguous"`. Empty means fully complete. |
| `candidate_count` / `candidate_budget_limit` | int / int | The raw candidate-edge total the index-budget ladder measured, and its configured ceiling (currently 2,000,000). |
| `truncated_by_max_files` | bool | Top-level file-set truncation flag -- may be true even when `degradation.truncated_by_max_files` is false (a Python-side pre-Rust file-collection cap); always prefer this field. |
| `languages_excluded_with_extractor` | array of strings | Language names (e.g. `["Kotlin"]`) that had at least one file excluded by `include_patterns`/`exclude_patterns` before it ever became a candidate, where that language DOES have a graph extractor. |
| `cached` | bool | True when the evaluator `.so` was served from the compile cache. |
| `compile_ms` | int | Milliseconds spent compiling the evaluator (0 on a cache hit). |
| `cache_handle` | string, nullable | Opaque handle for `cidx_fetch_cached_payload` when `findings`/`refine` were truncated for size; null otherwise. |
| `has_more` | bool | True iff `findings`/`refine` were truncated (synonym for `truncated`). |
| `truncated` | bool | True when the combined `findings[]`/`refine[]` JSON exceeded the payload character budget; see "Result truncation and paging" below. |
| `total_pages` | int, present when `truncated=true` | Number of cache pages the full content was split across. |
| `inline_entry_truncated` | bool | True only when even the single first entry alone exceeded the budget and was adaptively shrunk for the inline preview ONLY. |
| `fetch_tool_hint` | string, present when `truncated=true` | Human-readable guidance naming `cidx_fetch_cached_payload`. |

## Result truncation and paging

For results larger than the server's single payload character budget (Web UI `payload_max_fetch_size_chars`, default 5000 chars), `findings[]`/`refine[]` are truncated and the FULL set is stored in PayloadCache as a series of whole-entry pages. The inline budget IS the page budget -- there is no separate, smaller preview threshold. `findings[]`/`refine[]` come back inline as many WHOLE leading entries as fit within `payload_max_fetch_size_chars` -- a dead-code sweep with many small findings can inline dozens of them. **No entry is ever partially cut to fit a page**: an entry that alone exceeds the budget gets its own, necessarily oversized, page in the cache, intact and unmodified. The ONE exception is the inline response itself when even the very first entry alone exceeds the budget -- that single entry is adaptively shrunk (string/list fields capped) for the inline preview ONLY, flagged via `inline_entry_truncated: true`; the cached copy remains whole regardless. See the **Response envelope** section above for `cache_handle`/`has_more`/`truncated`/`total_pages`/`inline_entry_truncated`/`fetch_tool_hint`.

To fetch the full content, use the discoverable `cidx_fetch_cached_payload` MCP tool with `cache_handle`, incrementing `page` from 1 through `total_pages` (or until `has_more` is `false`). Each page is stored as its own independent cache row and is returned WHOLE and unsliced regardless of the server's CURRENT `payload_max_fetch_size_chars` setting -- a config change between when a result was produced and when you fetch it cannot corrupt or misalign a page.

**The real wire shape needs TWO parses, not one.** `cidx_fetch_cached_payload`'s own response is `{"success": true, "content": "<json string>", "page": N, "total_pages": M, "has_more": bool}` -- `content` is a JSON STRING, not the findings object itself. `json.loads(page)["findings"]` raises `KeyError`, because `"findings"` is a key of the PARSED `content` string, not of the outer page object. The correct sequence per page: `outer = json.loads(raw_page_response)`, then `inner = json.loads(outer["content"])`, then read `inner["findings"]`/`inner["refine"]` -- each `inner` holds a whole-entry slice (never a partially-cut entry). Concatenate every page's `inner["findings"]`/`inner["refine"]` lists, in page order, to reconstruct the full arrays exactly, in original order.

When `truncated: false` (or absent), the full `findings[]`/`refine[]` arrays are returned inline, unmodified.

**Cache degradation and failure:** two DISTINCT conditions can affect this truncation path, both worth distinguishing from an ordinary `truncated: true` result. `cache_unavailable: true` means PayloadCache itself was down when the result was built -- you still get a bounded, honest inline page 1 (`truncated: true`, `cache_handle: null`), but no further pages exist to fetch (retry the request once the cache is back to get the rest). `success: false, error: "cache_store_failed"` means the OPPOSITE -- the cache was reachable but the atomic page-set write itself failed (single-repo: the whole response carries this; multi-repo: only that alias's entry lands in `errors[]`, keyed by `repository_alias`, with the top-level `ok` false). In both cases, and unlike an unrelated pipeline failure, every non-array metadata field the analysis already produced (`fact_graph_complete`, `ok`, `degradation`, `cached`, `compile_ms`, `build_status`, ...) is still present alongside the failure -- only `findings[]`/`refine[]` are genuinely undeliverable.

## Indexing scope vs finding scope

**`include_patterns`/`exclude_patterns` DO narrow which files are read into the graph.** By default (both empty) indexing covers the whole repository; passing `include_patterns: ["*.java", "*.kt", "*.kts"]` restricts indexing to just the matching files (this is the recommended usage on a repo holding other languages -- see "Language support" above: an empty `include_patterns` there pulls in every unextractable file too, inflating `files_with_unsupported_language` and making `fact_graph_complete: true` unreachable). Narrow to the EXTRACTABLE set, never to one language: dropping `*.kt` from a mixed Java/Kotlin repo does not remove the Kotlin dependency, it only removes your ability to see it, and a Java method called solely from Kotlin then looks dead.

What patterns do NOT narrow is which files can appear as the *target* of a resolved reference. A reference from an included file to a symbol declared in an EXCLUDED file still resolves as "external to the graph", which is a real, meaningful signal your evaluator can act on; it is never silently dropped. So narrowing `include_patterns` shrinks what gets indexed and searched, but a symbol outside that scope can still be correctly identified as an external dependency rather than vanishing from the analysis.

## Glob Pattern Semantics

`include_patterns`/`exclude_patterns` use the exact same selector as `regex_search` and
`xray_search` (`PathPatternMatcher`, gitignore-style globs) -- a pattern produces the same file set
here as it would for either of those tools:

- `*` does not cross `/` when the pattern has a trailing suffix -- `src/*.java` matches only
  `src/Foo.java`, never `src/sub/Foo.java`. Same for both `include_patterns` and
  `exclude_patterns`.
- A BARE trailing `*` with no suffix (e.g. `src/*`) behaves DIFFERENTLY depending on which list
  it is used in (verified directly against the shared selector, both directions):
  - As `include_patterns`, `src/*` matches only files directly under `src/` by name --
    `src/Foo.java`, never `src/sub/Foo.java` or `src/sub/sub2/Foo.java` (ripgrep `-g` reference
    semantics: a directory match never implies "and everything under it" for an include).
  - As `exclude_patterns`, `src/*` instead excludes the WHOLE subtree -- `src/Foo.java`,
    `src/sub/Foo.java`, and `src/sub/sub2/Foo.java` are all dropped (gitignore containment
    semantics: excluding a directory excludes everything inside it).
  Use `src/**` to deliberately INCLUDE the whole subtree (matches at every depth under `src/`,
  including direct children).
- A pattern with no `/` at all (e.g. `*.java`) matches the basename at any depth: `Foo.java`,
  `src/Foo.java`, and `src/sub/Foo.java` all match. Same for both `include_patterns` and
  `exclude_patterns`.
- A trailing-slash directory marker (e.g. `src/tests/`) selects that directory's CONTENTS in
  BOTH `include_patterns` and `exclude_patterns` -- including when the marker itself also
  carries a wildcard (e.g. `src/*/` selects files under any direct subdirectory of `src/`,
  never a file directly in `src/` itself; `*/tests/` selects any `tests/` directory's contents
  at any depth). See `regex_search`'s tool docs for the full trailing-slash / leading `*/`
  reference -- the underlying matcher is shared, so those rules apply here unchanged.
- Brace groups are supported (e.g. `*.{java,kt}`), capped at 64 expanded variants per pattern.

See `regex_search`'s tool docs for the full semantics reference (leading `*/` any-depth rewriting,
trailing-`/` directory markers -- root-anchored only when MULTI-segment, e.g. `src/main/`; a
SINGLE-segment marker like `docs/` or `tests*/` matches at any depth -- bare-token ambiguity
handling) -- the underlying matcher is shared, so those rules apply here unchanged.

## Running a stored graph pattern

`pattern_name` resolves a previously stored evaluator from the X-Ray pattern library instead of inlining `evaluator_code` (mutually exclusive with it). Resolution tries the REPOSITORY-SPECIFIC scope first (`cidx-meta/xray-patterns/{repository_alias}/{pattern_name}.yaml`), then falls back to the cross-repo `__any__` scope (`cidx-meta/xray-patterns/__any__/{pattern_name}.yaml`) -- a repo-specific pattern always takes precedence over a same-named `__any__` pattern.

A stored pattern declares its own `execution_mode` (`"legacy"` for `xray_search`-style single-file evaluators, or `"graph"` for the two-function `collect_facts`/`analyze_graph` contract this tool requires). `analyze_graph` checks the declared mode BEFORE preparing the evaluator code; a pattern authored for `xray_search` cannot be run here.

`pattern_params` supplies optional typed overrides for the pattern's declared parameters, using the exact same substitution semantics `xray_search` uses for its own stored patterns: each resolved value is injected as a Rust `const` declaration prepended to the evaluator source before compilation. Ignored unless `pattern_name` is also supplied.

Error codes specific to pattern resolution:

- `mutually_exclusive_params` -- both `pattern_name` and `evaluator_code` were supplied; provide exactly one.
- `pattern_mode_mismatch` -- the stored pattern's declared `execution_mode` is not `"graph"` (this includes a legacy pattern predating `execution_mode`, which is treated as non-graph).
- `pattern_not_found` -- `pattern_name` does not exist in either the repository-specific scope or `__any__`.
- `invalid_pattern_params` -- `pattern_params` was supplied as a TRUTHY, non-empty JSON value that is not an object (e.g. a non-empty array like `["a"]` or a non-empty string like `"foo"`); rejected before any per-parameter validation runs, so this never surfaces alongside `unknown_parameter`/`parameter_type_mismatch`. An EMPTY array (`[]`), empty string (`""`), or any other falsy value is indistinguishable from omitting `pattern_params` entirely -- it is treated as absent (defaults to no overrides) and never reaches this check. Response: `{"error": "invalid_pattern_params", "message": "invalid_pattern_params: pattern_params must be a dict, got <type>"}`.
- `path_traversal_rejected` -- `repository_alias` (used as the pattern-resolution scope) or `pattern_name` contains `/`, `\`, or `..`. Checked before the filesystem lookup, so it takes priority over `pattern_not_found` for the same request. Response: `{"error": "path_traversal_rejected", "message": "path_traversal_rejected: <field> '<value>' contains path traversal sequences"}`, where `<field>` is `repo_alias` or `pattern_name`.

(`unknown_parameter` and `parameter_type_mismatch` can also surface from `pattern_params` validation, mirroring `xray_search`'s own stored-pattern parameter errors.)

## Two-function evaluator contract

Unlike `xray_search`'s single `fn evaluate_node`, graph mode uses two cooperating functions. `collect_facts`'s `node` parameter, like `refine`'s below, is the SAME `OwnedNode` type `xray_search`'s `fn evaluate_node` receives -- see `xray_search`'s tool doc, "OwnedNode reference" section, for the full accessor table (`text()`, `start_line`, `named_children()`, `child_by_kind`, `has_descendant_of_kind`, `descendants_of_kind`).

```rust
// REQUIRED: runs once per file, BEFORE the graph is built. Collects
// auxiliary evidence (annotations, config keys, structural hashes) that
// analyze_graph can look up per-symbol via FactsHandle. Even an evaluator
// with no use for facts must still define this function (an empty body
// returning Vec::new() is fine) -- graph mode requires BOTH functions
// together; neither is optional.
fn collect_facts(node: &OwnedNode, file: &str) -> Vec<UserFact> {
    Vec::new()
}

// REQUIRED: runs ONCE over the whole built graph. This is where your
// actual analysis logic lives.
fn analyze_graph(g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> GraphResult {
    GraphResult::default()
}
```

Execution modes are fixed at exactly two: legacy (`fn evaluate_node`, used by `xray_search`) and graph (`fn collect_facts` + `fn analyze_graph`, used here). Do not define `fn evaluate_node` alongside these two -- a genuine MIXED-mode evaluator (one that satisfies one mode's complete entry-point set while ALSO defining the other mode's function) reaches the Rust compiler and is rejected there with a real `CompileError` (`ok: false`, `error: {error_type: "CompileError", error_message: ...}`).

**Defining only ONE of `collect_facts`/`analyze_graph` is a DIFFERENT, earlier rejection -- never a `CompileError`.** It is caught by a Python-side pre-compile gate before the Rust compiler is ever invoked, and returns:

```json
{"error": "xray_evaluator_validation_failed", "error_code": "missing_entry_point",
 "offending_construct": "analyze_graph", "message": "missing required entry point: ..."}
```

Note there is no `ok` field here at all (unlike the `CompileError` shape above, which carries `ok: false`). `offending_construct` names whichever of `collect_facts`/`analyze_graph` you actually left out -- e.g. if your source defines only `fn collect_facts`, `offending_construct` is `"analyze_graph"`; if it defines only `fn analyze_graph`, `offending_construct` is `"collect_facts"`. It falls back to `"evaluate_node"` only when your source defines NEITHER graph-mode function at all, in which case the legacy entry point is the honest thing to name. A real compile error's `CompileError` detail lines are remapped to YOUR OWN line numbers wherever a diagnostic location falls inside your code, and an out-of-span location (inside generated support code, never yours) is left unchanged and labeled `(evaluator support code, not user code)` -- but that label is only ever attached to the `-->` arrow line itself; a numbered source-context "gutter" row rustc prints alongside it for an out-of-span line keeps its raw, unlabeled assembled line number, so treat any gutter row you cannot match to your own source as support code too, not just the ones explicitly marked.

### UserFact struct (collect_facts output)

```rust,fragment
pub struct UserFact {
    pub kind: String,           // e.g. "deprecated", "todo"
    pub line: usize,            // 1-based line number
    pub message: String,        // free-text detail
    pub custom_key: Option<String>,  // Some(name) for a non-symbol key (config/event topic); None to attribute to the enclosing symbol
}
```

### GraphResult / ReduceFinding (analyze_graph output)

```rust,fragment
pub struct GraphResult {
    pub findings: Vec<ReduceFinding>,
    pub refine: Vec<SymbolId>,  // symbols flagged for a future per-file follow-up look
}

pub struct ReduceFinding {
    pub pattern: String,           // your label for this finding
    pub message: String,           // free-text detail
    pub involved: Vec<SymbolId>,   // the path/chain this finding is about (1 element for a flag, N for a path)
    pub signatures: Vec<String>,   // parallel to involved -- each symbol's cached signature line
}
```

### GraphHandle reference

| Method | Signature | Description |
|--------|-----------|--------------|
| `g.callees_of(symbol)` | `(u32) -> Vec<u32>` | Dense ids this symbol calls. Each DISTINCT callee appears EXACTLY ONCE, even when reached from several call sites inside `symbol` -- there is no separate per-call-site accessor; count `.len()` directly as "how many distinct things does this call". |
| `g.callers_of(symbol)` | `(u32) -> Vec<u32>` | Dense ids that call this symbol. Each DISTINCT caller appears EXACTLY ONCE, even when it calls `symbol` from several call sites -- count `.len()` directly as "how many distinct callers does this have", never as a call-site count. |
| `g.reachable_from(roots, max_depth)` | `(&[u32], usize) -> Vec<u32>` | Every dense id reachable from `roots` within `max_depth` hops, following CALLEES -- answers "what do `roots` depend on". Root-inclusive, monotonic, convergent. Answers the OPPOSITE question from `reachable_to`; see "Filtered vs unfiltered" below for which of the two counts as your evidence. |
| `g.reachable_to(targets, max_depth)` | `(&[u32], usize) -> Vec<u32>` | The mirror of `reachable_from` over the REVERSE graph: every dense id that can transitively reach `targets` within `max_depth` hops, following CALLERS -- answers "what depends on `targets`" (the usual blast-radius question). Root-inclusive, monotonic, convergent. **Its raw count can OVER-count**: on a real repository this can report the identical count for two unrelated symbols when a same-named, same-arity method exists elsewhere and this binder could not rule it out -- see "Filtered vs unfiltered" below before treating a large or suspiciously-uniform count as ground truth. |
| `g.callees_of_filtered(symbol, required_bits, forbidden_bits)` | `(u32, u16, u16) -> Vec<u32>` | The evidence-FILTERED counterpart of `callees_of`: a callee is included when AT LEAST ONE contributing edge occurrence satisfies `(bits & required_bits) == required_bits && (bits & forbidden_bits) == 0` (checked per occurrence, never merged across occurrences of the same target -- a genuine edge is never dropped just because a separate, weaker occurrence of the same pair exists). `required_bits: 0, forbidden_bits: 0` reaches the same SET of targets as `callees_of` -- both are deduplicated to one entry per DISTINCT target, but do not assume identical order (the filtered pass walks evidence-tagged occurrences, the unfiltered one walks raw CSR edges). Never deletes anything from the raw graph -- `callees_of` on the same symbol is unaffected. |
| `g.callers_of_filtered(symbol, required_bits, forbidden_bits)` | `(u32, u16, u16) -> Vec<u32>` | The evidence-FILTERED counterpart of `callers_of`, same filter contract. |
| `g.reachable_from_filtered(roots, max_depth, required_bits, forbidden_bits)` | `(&[u32], usize, u16, u16) -> Vec<u32>` | The evidence-FILTERED counterpart of `reachable_from` -- a bounded traversal that only ever crosses edges satisfying the filter. Narrows to the subgraph you can require evidence on (e.g. `required_bits: RECEIVER_TYPE_MATCH, forbidden_bits: RECEIVER_TYPE_MISMATCH`) without a hand-rolled per-hop filter loop -- see "Filtered vs unfiltered" below for what this narrowing costs you. |
| `g.reachable_to_filtered(targets, max_depth, required_bits, forbidden_bits)` | `(&[u32], usize, u16, u16) -> Vec<u32>` | The evidence-FILTERED counterpart of `reachable_to`, same filter contract and same tradeoff -- see "Filtered vs unfiltered" below. |
| `g.strongly_connected_components_filtered(required_bits, forbidden_bits)` | `(u16, u16) -> Vec<Vec<u32>>` | The evidence-FILTERED counterpart of `strongly_connected_components` -- a cycle only counts if every edge in it satisfies the filter. Suppresses a cycle fabricated purely by weak/mismatched evidence (e.g. a `String` receiver's `.equals()` binding to every same-named repo override) -- but see "Filtered vs unfiltered" below: it can just as easily suppress a REAL cycle closed only by an unqualified or static-import call, which carries no receiver evidence to test at all. |

**A `required_bits` filter UNDER-counts -- never conclude "no callers" or "unreachable" from an empty filtered result.** A `*_filtered` primitive can only see evidence this binder actually recorded; a real call can carry NONE of the bits you require (an unqualified call has no receiver to tag at all, and many genuine edges carry only weak structural evidence like `SAME_PACKAGE`/`ARITY_MATCH`), so `callers_of_filtered(sym, RECEIVER_TYPE_MATCH, 0).is_empty()` proves only "no caller reached this required bit" -- it does NOT prove `sym` has no real callers. The UNFILTERED primitive has the OPPOSITE failure mode: it can OVER-count, attributing a call to a same-named method on an unrelated owner when the receiver could not be resolved. Neither primitive is "the correct one" in general -- see "Filtered vs unfiltered" immediately after this table for a worked example and a decision rule for which one to reach for.
| `g.shortest_path_to_any(from, targets, max_depth)` | `(u32, &[u32], usize) -> Option<Vec<u32>>` | Shortest call-graph path from `from` to any of `targets` -- use this to report the PATH for a reachability finding (directional asymmetry, see below). |
| `g.shortest_path_to_any_filtered(from, targets, max_depth, required_bits, forbidden_bits)` | `(u32, &[u32], usize, u16, u16) -> Option<Vec<u32>>` | The evidence-FILTERED counterpart of `shortest_path_to_any` -- every hop of the returned path satisfies the filter, so a path this returns is the strong-evidence form of "X reaches Y" (use it when a false-positive reachability claim is the expensive mistake, e.g. a security finding). **`None` does NOT prove `targets` is unreachable from `from`** -- it proves only that no path satisfying the filter exists within `max_depth`; a real path may still exist through hops this filter excludes (an unqualified call, a static-import call, anything with no receiver evidence to test). Cross-check against unfiltered `shortest_path_to_any` before reporting non-reachability. |
| `g.strongly_connected_components()` | `() -> Vec<Vec<u32>>` | Cycle detection over the POST-CAP candidate arena. A multi-node component is a POSSIBLE cycle among proposed candidates, NOT a confirmed source-level reference cycle -- an unresolved candidate window can fabricate one, so verify against source before reporting. A self-recursive method is returned as a SINGLETON, so `component.len() > 1` misses all self-recursion; test `g.callees_of(d).contains(&d)`. |
| `g.resolve_symbol(dense_id)` | `(u32) -> Option<u64>` | Dense id to real global `SymbolId`. |
| `g.symbol_count()` | `() -> usize` | Exact number of symbols; dense ids are in `0..g.symbol_count()`. |
| `g.dense_id_for(symbol)` | `(u64) -> Option<u32>` | Reverse lookup from a real global `SymbolId` to its dense id. |
| `g.resolve_string(string_id)` | `(u32) -> Option<&str>` | Interned string lookup. |
| `g.is_symbol_referenced(dense_id)` | `(u32) -> bool` | True if ANY inbound edge exists, regardless of graph completeness. |
| `g.is_definitely_dead_code(dense_id)` | `(u32) -> Option<bool>` | `Some(false)` = the symbol has an inbound reference edge. `Some(true)` = unreferenced, its declaration kind is `Method` or `Type`, and its visibility is provably `Private`. `None` = every other case: `Public`, `Protected`, or `Unknown` visibility, and every `Field`, `Constant`, `Package`, or unknown-kind symbol. Java extraction creates inbound edges for direct calls, method references, `new` expressions, explicit `this(...)`/`super(...)` constructor invocations, `Type::new`, and annotation usages (an edge to the annotation type's declaration); it preserves plausible overload and varargs targets. `super` calls bind only to a KNOWN, recorded superclass edge; the conservative "no evidence" fallback applies only when a class has NEITHER an `extends` NOR an `implements` clause (e.g. implicit `java.lang.Object`, never tracked) -- a class with no `extends` but a real `implements` clause still narrows against its recorded interfaces. With neither clause, `super.m()` falls into the same "no supertype evidence" case as a genuine extraction gap, so it can still self-loop when the enclosing type's own method is the sole matching candidate. Java-private candidates from a different known top-level type are excluded. Two Java-specific false-positive classes are addressed. First: a `private Foo() {}` no-arg constructor that is the ONLY constructor its class declares -- the standard non-instantiable-utility-class idiom, where "unreferenced" is intentional, not dead -- is excluded from the `Some(true)` verdict above via a SEPARATE per-symbol fact the predicate additionally consults, never by changing the constructor's own declared visibility: `g.visibility_of()` still reports `Private` for it exactly as extracted, even though `is_definitely_dead_code` reports `None` instead of `Some(true)`. Second: a method named by a JUnit5 `@MethodSource` string literal (bare, array, `value = "..."`, the empty-value same-name default, or a `Class#method` form self-qualified to the annotation's own enclosing class) is marked as referenced evidence at extraction time, so a reflection-invoked parameterized-test data provider is not reported dead either -- this only suppresses the provider's OWN `is_definitely_dead_code` verdict; it is NOT a `Reference`/CSR edge, so it never appears in `callers_of`/`callees_of` or any reachability result. An explicit but unresolvable argument (e.g. naming a different class) leaves the target unreferenced rather than guessing. **This predicate does NOT consult `fact_graph_complete`** -- it returns `Some(true)` on an incomplete graph exactly as it would on a complete one. Field and constant reads therefore cannot produce `Some(true)`: those declaration kinds are outside the predicate's allowlist, regardless of whether their reads are represented by graph edges. A `Some(true)` for an allowed private `Method` or `Type` can still be falsified by reflection, JNI, dependency injection, or other runtime behavior invisible to the graph. |
| `g.signature_for(dense_id)` | `(u32) -> Option<&str>` | Cached declaration signature line, for reporting AND for substring matching. For a METHOD this is `Owner.name(ParamType, ...)` -- declaring type, method name, and bare (generic-stripped, never fully-qualified) parameter type names, e.g. `TimeUtil.parse(XMLGregorianCalendar)`. A VARARGS parameter renders with its REAL per-language spelling, never a bare type name indistinguishable from a genuine one-arg overload: Java spells it with a trailing ellipsis (`Reader.consumeToAny(char...)`, the real `char... chars` source syntax) -- ALWAYS the last parameter (JLS 8.4.1 guarantees this) -- while Kotlin spells it with a leading `vararg` keyword (`Api.logAll(vararg Int)`, the real `vararg xs: Int` source syntax) at whatever position it ACTUALLY occupies: unlike Java, Kotlin allows exactly one `vararg` parameter at ANY position (`Api.mid(vararg Int, String)` for `fun mid(vararg xs: Int, tail: String)`, where every parameter after it must be passed by name at the call site) -- never assume it is last for a Kotlin signature. Falls back to `Owner.name(N params)` when the extractor captured fewer parameter types than the recorded arity -- a partial list is NEVER presented as complete, so this fallback text never shows a varargs marker either -- and to `name(...)` with no prefix when the declaring type is unknown (a Kotlin top-level function). Non-method declarations keep their extractor-supplied text unchanged. An anonymous or enum-constant-body class's `Owner` renders as `Enclosing$<anon@L<line>:<file_id>:<byte>>` -- the immediately enclosing type's real bare name plus the anonymous body's own real source line up front (human-chaseable), with `file_id`/`byte` kept after that prefix only to guarantee global uniqueness across the whole analysed set (never parse/rely on their exact values). No annotations, no return type, no modifiers. |
| `g.location_for(dense_id)` | `(u32) -> Option<(&str, usize)>` | The DECLARATION's own repo-relative file path and 1-based line -- not a call site's. `None` when the symbol has no recorded location. Use it so every finding you report can be chased to source. |
| `g.declaration_kind(dense_id)` | `(u32) -> Option<DeclarationKind>` | What kind of declaration this symbol is. The same value `is_definitely_dead_code` consults internally, so filtering by kind cannot drift from the predicate. |
| `g.visibility_of(dense_id)` | `(u32) -> Visibility` | Declared visibility, also as the dead-code predicate sees it. |
| `g.edge_reason(from, to)` | `(u32, u32) -> Option<EdgeReason>` | How many candidates a contributing reference had -- a COUNT, not a verdict. `EdgeReason::SoleCandidate` -- at least one contributing reference matched exactly ONE declaration in this repo by name and arity. `EdgeReason::MultipleCandidates` -- every contributing reference matched several. `None` -- no such edge. **`SoleCandidate` does NOT mean the edge is real**: a call on an external or JDK receiver (`someMap.put(k, v)`) whose name and arity happen to match one repo declaration reports `SoleCandidate`, because the binder never saw the receiver's real type. Use `edge_evidence` to tell those apart. |
| `g.edge_evidence(from, to)` | `(u32, u32) -> Option<u16>` | The reason bits the binder actually recorded, ORed across every contributing reference. **This is what to audit a hop with.** Test against the mirrored constants. `RECEIVER_TYPE_MATCH` is the strongest bit -- the binder resolved the receiver's declared type and this candidate's OWNER (enclosing type) matched it -- and it is set on BOTH binding paths: on a multi-candidate pool by receiver narrowing, and on a single-candidate unique-name shortcut whose receiver type was independently confirmed (so `UNIQUE_NAME_IN_REPO | RECEIVER_TYPE_MATCH` means the shortcut fired AND the receiver corroborated it -- strictly stronger than either alone). **It proves the OWNER, never the OVERLOAD**: when a receiver's type has several same-named, same-arity overloads, every one of them earns `RECEIVER_TYPE_MATCH` identically -- it is `OVERLOAD_ARG_TYPE_MATCH` (below) that discriminates between them by argument type, a separate evidence path entirely. It is NOT set when the receiver's supertype evidence was incomplete, nor for an unqualified call, which has no receiver to check. So the absence of `RECEIVER_TYPE_MATCH` is NOT evidence against a hop. `UNIQUE_NAME_IN_REPO` means only that one in-repo declaration bears that name and arity; for an UNQUALIFIED call (a bare `doThing(x)` inherited from a superclass outside the repo, or a static import of an external method) it is set identically whether the real target is in this repo or not. `OVERLOAD_ARG_TYPE_MATCH` means NO ARGUMENT WITH A KNOWN DECLARED TYPE is provably incompatible with this candidate's declared parameter types, checked per position for every argument this binder can resolve a type for at all (a literal, a cast, `new T(...)`, `this`, or a same-file local/parameter/field with a real declared-type record). This bit NEVER removes a candidate from the pool -- named-type argument evidence is permanently TAG-ONLY, exactly like `RECEIVER_TYPE_MATCH` above, and for the same reason: this binder cannot generally prove two class/interface NAMES are unrelated (a repo type can share its bare name with an unrelated external type, hiding the external type's real relationships), so incompatibility is only ever proven via a small CLOSED-WORLD rule -- Java primitive/String/boxed-wrapper types only (exact match, `Object`, primitive/boxing widening including `char`, `String -> CharSequence`, arrays by closed-world element type, varargs `T...` accepting both `T` and `T[]`). For any two NAMED CLASS/INTERFACE types outside that closed-world rule, this bit is set whenever BOTH simply have a known declared type, regardless of whether they are actually related. It is a "not provably incompatible" signal, never a "confirmed match" one: an argument whose type this binder cannot resolve at all (a chained call, a field access through an arbitrary receiver, a lambda) is always treated the same way, so the bit routinely appears on a candidate this binder simply had no evidence against; several same-owner overloads (e.g. differing only by autoboxing, or all class/interface-typed) are routinely tagged TOGETHER. A hop carrying only `ARITY_MATCH \| OVERLOAD_ARG_TYPE_MATCH` (no receiver/context bits at all) matched on argument count and an absence-of-proven-mismatch alone -- for an unqualified call this still says nothing about whether the true target is even in this repo, and does not by itself distinguish the real overload from a same-arity sibling. **No bit combination proves a hop for an unqualified call** -- treat evidence as a ranking, cite `location_for` per hop, and have a human confirm anything you would act on. `RECEIVER_TYPE_MISMATCH` is set ONLY when ALL FOUR of the following hold (same grouping and count as `graph::reasons::RECEIVER_TYPE_MISMATCH`'s own doc comment, the source of truth): **(a)** the call's receiver has a POSITIVELY known declared type that is CLOSED-WORLD (`String`, a primitive, a boxed wrapper, or any array type) whose bare simple name is NOT ALSO a repo-declared type, an explicitly imported type (ordinary OR single-member static), or a known generic type parameter (a repo can legally declare its own class named `String`, shadowing `java.lang.String` for code in that package); **(b)** the receiver binding is a genuine method/constructor PARAMETER, never a block-scoped local variable or a field (a field can be misresolved to an unrelated, same-method local's declared type under this binder's own per-method-not-per-block scoping limits); **(c)** the parameter's declared type was written UNQUALIFIED or qualified exactly as `java.lang.*` (this binder's type model only ever records a bare simple name, so `com.lib.String s` and `String s` are indistinguishable, even though `com.lib.String` is a different, unproven type that may legally have a repo-declared subtype); **(d)** EVERY type declared in the call site's own FILE has fully repo-resolved supertype evidence, on ANY nesting level (a type extending an external/unindexed supertype may have a nested type privately shadowing a closed-world name this binder cannot see into) -- judged by SIMPLE NAME only, so a repo-declared type sharing the same bare name as the real external supertype can make it look "resolved" when it is not actually the same type. Under all four conditions its PRESENCE is real evidence the candidate is unrelated, even though (like every bit here) it never deletes the candidate itself. Pair it with the `*_filtered` primitives above (e.g. `forbidden_bits: RECEIVER_TYPE_MISMATCH`) to drop these fabricated edges from your own analysis. **`OVERLOAD_ARG_TYPE_MATCH`'s literal-shape half DOES filter, unlike the named-type half above**: for a LITERAL argument (an int/string/boolean/char literal, whose type the binder knows directly and unambiguously, never via a name that could collide with an unrelated type), the same closed-world rule is not merely tag-only -- a literal PROVABLY incompatible with a candidate's declared parameter type removes that candidate from the pool outright. This check is gated on the candidate's parameter types having actually been RECORDED by the extractor at all -- an unresolved/unrecorded parameter is treated as unknown (no filtering), never as a mismatch -- and it applies to JAVA callees ONLY: a non-Java (Kotlin) callee is never subject to this argument-type check regardless of receiver evidence, consistent with Kotlin's broader lack of receiver-type substrate documented above. Available constants: `SAME_FILE`, `SAME_PACKAGE`, `ARITY_MATCH`, `OVERLOAD_ARG_TYPE_MATCH`, `RECEIVER_TYPE_MATCH`, `RECEIVER_TYPE_MISMATCH`, `UNIQUE_NAME_IN_REPO`, `QUALIFIED_NAME`, `STATIC_IMPORT`, `SAME_CLASS_OR_SUPER`, and others -- all usable unqualified in evaluator code. |

### DeclarationKind / Visibility

Both enums derive `Debug` (and `Clone, Copy, PartialEq, Eq`), so `format!("{:?}", kind)` / `format!("{:?}", vis)` renders the bare variant name -- e.g. `"Method"`, `"Private"` -- with no wrapping (never `"Some(Method)"`; unwrap the `Option` from `declaration_kind` first).

```rust,fragment
pub enum DeclarationKind { Type, Method, Field, Constant, Package }
pub enum Visibility { Public, Protected, Private, Unknown }
```

**Root kinds matter, and picking the wrong one fails silently.** Type, Package and Field nodes typically carry no outbound edges, so `reachable_from` on a class-level root usually returns the root alone -- a bare `1`, with nothing in the response indicating the root kind was unsuitable. Pass METHOD dense ids to `reachable_from`/`reachable_to`. ("Typically", not "never": an invocation/method-reference site is attributed to the enclosing method the extractor recorded for it; only when no such recorded enclosing method resolves -- e.g. a call inside a field or property initializer, which has no enclosing method -- does attribution fall back to the nearest-preceding-declaration line heuristic, landing on that Field, or on the Type when the Type is nearest.)

## Filtered vs unfiltered: pick based on which error you can tolerate

Every `*_filtered` primitive (`callees_of_filtered`/`callers_of_filtered`/`reachable_from_filtered`/`reachable_to_filtered`/`strongly_connected_components_filtered`/`shortest_path_to_any_filtered`) requires at least one evidence bit (typically `RECEIVER_TYPE_MATCH`) on every edge it walks. That is a real, OPPOSITE-direction tradeoff against its unfiltered sibling, never an unconditional improvement -- **there is no default to reach for**, and neither form is "precise" or "exact": both are approximations of the true call graph, wrong in different directions.

- **Unfiltered is a SUPERSET of the truth.** It can attribute a call to a same-named, same-arity method belonging to an unrelated owner whenever this binder could not resolve the caller's receiver type -- a false POSITIVE.
- **`*_filtered` is a SUBSET of the truth.** Requiring `RECEIVER_TYPE_MATCH` throws out every call this binder could not resolve a receiver type for -- which includes an ordinary UNQUALIFIED call reached through a static import, a call on an inherited or dynamically-dispatched receiver, and any call an extraction gap left untyped. Those are REAL calls with no receiver evidence to test, not weak evidence -- filtering on the bit discards them by construction, a false NEGATIVE. **An absent or reduced filtered result never proves "no such caller" / "unreachable"; it proves only "no caller carried the required evidence bit".**

Measured on jsoup (v12.69.0): `StringUtil.normaliseWhitespace(String)` has 6 real callers -- 3 explicitly `StringUtil.`-qualified (`Document.java`, `TextNode.java` x2) and 3 UNQUALIFIED via static import (`Evaluator.java` x2, `NodeEvaluator.java`). The UNFILTERED `reachable_to`/`callers_of` finds all 6, correctly. `callers_of_filtered(sym, RECEIVER_TYPE_MATCH, 0)` finds only the 3 qualified callers -- the 3 static-import callers vanish, because a static-import call carries no receiver to type-check at all. Meanwhile an unrelated, same-named, same-arity DECOY on this repo's `TextNode` class with ZERO real callers is reported by the UNFILTERED primitive as having the IDENTICAL 6 callers -- every one a false positive, since none of the 6 real call sites actually resolves to the decoy. Filtered on `RECEIVER_TYPE_MATCH` correctly reports 0 for the decoy. So for the REAL target unfiltered is exactly right and filtered under-reports by 3; for the DECOY unfiltered is entirely wrong and filtered is exactly right -- neither primitive is "the correct one" in general.

Pick based on which mistake costs you more:

- **You cannot afford to MISS a real caller** (deleting code, changing a signature, "is this safe to remove"): use the UNFILTERED primitive, then verify each hit against source with `location_for`/`signature_for` -- a false positive here is cheap to rule out by reading one call site; a false negative silently breaks something.
- **You cannot afford to ACT on a false hit** (a security finding, a reported layering-violation cycle, anything a human will act on without re-reading the whole call graph): use the `*_filtered` primitive, and say plainly that the result may be INCOMPLETE -- an absent finding is never a clearance.
- **Run both and compare.** Identical sets are a strong confidence signal (the name is unambiguous in this codebase). A large gap between them means the name is shared across owners, or reached only through evidence-free call shapes (unqualified / static-import / dynamic dispatch) -- treat that gap itself as the finding, and read the actual call sites before reporting anything either way.

`shortest_path_to_any_filtered` follows the SAME asymmetry: a path built only from strong-evidence hops is the right tool for "does X definitely reach Y" (a false positive is the expensive error there), but `None` from it means only "no strong-evidence path was found within `max_depth`" -- it does NOT prove X cannot reach Y. Cross-check with unfiltered `shortest_path_to_any` before reporting non-reachability.

### FactsHandle reference

| Method | Signature | Description |
|--------|-----------|--------------|
| `facts.for_symbol(symbol_id)` | `(u64) -> Vec<UserFact>` | Facts your `collect_facts` attributed to this symbol's enclosing declaration. |
| `facts.for_custom(name)` | `(&str) -> Vec<UserFact>` | Facts attributed to a `custom_key` (non-symbol key) instead. |

## Optional third function: fn refine

`analyze_graph` never sees file source -- it only has `GraphHandle`/`FactsHandle`, never an `OwnedNode`, so it structurally cannot read a method body, cite an AST-derived risk signal, or point at a specific line. `fn refine` closes that gap: define it alongside `collect_facts`/`analyze_graph`, push symbols worth a follow-up per-file look onto `result.refine` inside `analyze_graph`, then pass `refine: true` on the request. `refine` is OPTIONAL (unlike the other two) -- an evaluator with only `collect_facts`/`analyze_graph` is still a valid graph-mode evaluator; omitting `fn refine` while passing `refine: true` reports `refine_status: "absent"`, never an error.

```rust,fragment
// OPTIONAL: runs once per file in the narrowed refine set (the files a
// symbol in GraphResult.refine belongs to, intersected with the files
// already indexed -- never the whole repo). Has BOTH the file's real
// OwnedNode (so you can walk the AST, check for a catch block, read a
// literal, etc.) AND the same GraphHandle/FactsHandle analyze_graph used,
// simultaneously -- the only callback with both at once.
fn refine(node: &OwnedNode, ctx: &FileContext, g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> Vec<EvalFinding> {
    Vec::new()
}
```

```rust,fragment
pub struct FileContext {
    pub file: String,  // this file's repo-relative path
}
```

`EvalFinding` is the SAME shape `xray_search` evaluators return:

```rust,fragment
pub struct EvalFinding {
    pub pattern: String,   // your label for this finding
    pub line: usize,       // 1-based line number
    pub snippet: String,   // free-text detail / matched text
}
```

`refine_findings[]` in the response adds `file` (from `FileContext.file`) to each one, giving `{pattern, file, line, snippet}`. See the **Response envelope** section above for `refine`/`refine_status`/`refine_findings`/`refine_files_examined`, including the two cost guards (opt-in, and skipped when `GraphResult.refine` is empty).

## AnalysisCompleteness: the honesty contract

The whole point of this tool is that a caller can tell "no findings" apart from "the index was too incomplete to trust a negative". **Always check `fact_graph_complete` and `degradation` before treating an empty `findings[]` as a clean result** -- especially for dead-code-style analyses. **`is_definitely_dead_code` does NOT gate itself on completeness** -- it returns `Some(true)` on a degraded graph exactly as it would on a complete one, so an evaluator that only checks `== Some(true)` WILL emit false positives when the graph is incomplete. The completeness check is yours to apply: read `fact_graph_complete` and `degradation` yourself, and surface `fact_graph_complete=false` to the human rather than presenting either an empty list as "verified clean" or a populated list as "verified dead".

```json
{
  "ok": true,
  "status": "ran_ok",
  "findings": [],
  "fact_graph_complete": false,
  "build_status": "ok",
  "degradation": {
    "files_with_parse_errors": 2,
    "unreadable_or_unsupported_files": 0,
    "files_with_read_errors": 0,
    "files_with_extractor_panics": 0,
    "files_with_collector_panics": 0,
    "files_with_unsupported_language": 0,
    "truncated_by_max_files": false
  }
}
```

The response above means "your evaluator found nothing dead, but 2 files had real parse errors -- this is NOT a verified-clean result."

## no_supported_files status

Many repositories on this server are neither Java nor Kotlin (Vue/Spring Boot, .NET/Angular, Node/Lambda), and the graph extractor covers only those two. Running `analyze_graph` against one of them previously returned `ok: true, status: "ran_ok", findings: []` -- a response that reads as a clean bill of health when in fact the analysis had nothing to analyse at all. The unsupported files were counted only inside `degradation.files_with_unsupported_language`, which the caller had to know to go read.

When EVERY candidate file (after `include_patterns`/`exclude_patterns`) is an unsupported language AND none of them had a genuine parse error, `status` is `"no_supported_files"` instead of `"ran_ok"` (see the exact two-condition rule below -- a single candidate file with a real parse error keeps `status: "ran_ok"` even if every OTHER file is unsupported-language). `ok` stays `true` -- nothing failed, the request ran correctly and simply had zero supported input. Treat this status the same way you would treat `fact_graph_complete: false`: an empty `findings[]` under it is not a verified-clean result, it is "there was nothing this tool could look at".

```json
{
  "ok": true,
  "status": "no_supported_files",
  "findings": [],
  "fact_graph_complete": false,
  "degradation": {
    "files_with_unsupported_language": 41,
    "unreadable_or_unsupported_files": 0,
    "files_with_parse_errors": 0,
    "truncated_by_max_files": false
  }
}
```

`no_supported_files` is deliberately scoped to "no candidate file reached a supported-language extractor, AND no candidate file had a genuine parse failure". The graph builder's `files_with_unsupported_language` and `unreadable_or_unsupported_files` counters are **NOT disjoint**: a file with an unsupported-language extension is still parsed with a generic/fallback grammar to compute `has_syntax_error`, so the SAME file can land in both `files_with_unsupported_language` and `files_with_parse_errors` at once (e.g. a `.cs` file the engine cannot extract, whose fallback parse also trips a syntax error). Because of that overlap, the status requires TWO conditions together, not one: `files_with_unsupported_language + unreadable_or_unsupported_files == <candidate file count>` AND `files_with_parse_errors == 0`. A mixed repo containing any successfully extracted Java file, or any candidate file with a genuine parse error, therefore keeps `status: "ran_ok"` even if it yields zero findings -- a nonzero `files_with_parse_errors` is real signal and is never silently absorbed into "wrong language". Extractor and collector failures remain independently signaled via their own `degradation` counters and `fact_graph_complete`.

A related case that deliberately does NOT get its own status: a repo with real Java files present, correctly parsed, that simply contains zero declarations for your evaluator's query to match. This is a legitimate empty result (e.g. a directory containing only interfaces with no method bodies, or a narrow `include_patterns` scope) -- structurally different from "no supported language was even present" -- so it stays `"ran_ok"`. `fact_graph_complete` and `degradation` already give the caller everything needed to judge that outcome; a third status would add a distinction without a corresponding difference in what the caller should do next.

## Directional asymmetry (dead-code vs reachability)

This tool's dead-code and reachability analyses are held to deliberately OPPOSITE safety guarantees: **dead-code analysis must UNDER-report (safe)**. The engine enforces the declaration-kind and visibility floor for `is_definitely_dead_code`: only an unreferenced private `Method` or `Type` can produce `Some(true)`; every field, constant, package, unknown-kind, or non-private symbol produces `None` unless it has an inbound reference, which produces `Some(false)`. The predicate still does not consult `fact_graph_complete`, so your evaluator must check completeness before trusting any `Some(true)`; runtime behavior such as reflection, JNI, or dependency injection can remain invisible even for an allowed kind. **Reachability analysis must OVER-report (unsafe in the other direction)** -- when your evaluator reports that endpoint X can reach sink Y, it must ship the PATH (`involved`) and identify the weakest link's evidence, using `g.edge_evidence(from, to)` for the reason bits and `g.edge_reason(from, to)` for the candidate count, since a caller relying on a reachability claim needs to audit exactly how strong that claim is rather than trusting a bare boolean. The example below does exactly that; a finding that ships a bare path is not complete.

```rust
fn collect_facts(node: &OwnedNode, file: &str) -> Vec<UserFact> {
    Vec::new() // this example has no use for facts -- collect_facts is still required
}

fn analyze_graph(g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> GraphResult {
    let mut result = GraphResult::default();
    let endpoint_dense_id = 0u32; // resolve your real endpoint's dense id
    let sink_dense_ids = vec![7u32, 12u32]; // resolve your real sink dense ids
    if let Some(path) = g.shortest_path_to_any(endpoint_dense_id, &sink_dense_ids, 20) {
        let mut signatures = Vec::new();
        let mut involved = Vec::new();
        for dense_id in &path {
            if let Some(sym) = g.resolve_symbol(*dense_id) {
                involved.push(sym);
                // location_for gives file:line so a reader can check the hop against source.
                let where_ = match g.location_for(*dense_id) {
                    Some((file, line)) => format!("{}:{}", file, line),
                    None => "<no location>".to_string(),
                };
                signatures.push(format!("{} @ {}", g.signature_for(*dense_id).unwrap_or("?"), where_));
            }
        }

        // Audit EVERY hop. A hop is only as trustworthy as its evidence, and the
        // binder deliberately over-binds -- a bare path proves nothing on its own.
        // RECEIVER_TYPE_MATCH means the receiver's declared type was resolved and
        // matched -- on a narrowed pool OR on a corroborated unique-name shortcut.
        // Its ABSENCE still proves nothing (an unqualified call has no receiver to
        // check), so rank hops rather than asking for a proof the binder cannot give.
        let mut strong_hops = 0usize;
        let mut weakest = "receiver-resolved";
        for pair in path.windows(2) {
            let evidence = g.edge_evidence(pair[0], pair[1]).unwrap_or(0);
            let sole = g.edge_reason(pair[0], pair[1]) == Some(EdgeReason::SoleCandidate);
            if evidence & RECEIVER_TYPE_MATCH != 0 {
                strong_hops += 1;                 // disambiguated AGAINST the receiver's real type
            } else if evidence & UNIQUE_NAME_IN_REPO != 0 && sole {
                weakest = "unique-name-only";     // one repo match; target may still be external
            } else if sole {
                weakest = "shape-only";           // name+arity shape alone
            } else {
                weakest = "guessed";              // one of several candidates
                break;
            }
        }

        result.findings.push(ReduceFinding {
            pattern: "endpoint_reaches_sink".to_string(),
            message: format!(
                "path length {}, {}/{} hops receiver-verified, weakest link: {}",
                path.len(), strong_hops, path.len().saturating_sub(1), weakest
            ),
            involved,
            signatures,
        });
    }
    result
}
```

**Read the weakest link before acting on the claim.** The four ranks, strongest first: `"receiver-resolved"` -- every hop was disambiguated against the receiver's real declared type, the only rank where the binder actively ruled other candidates out. `"unique-name-only"` -- some hop matched exactly one declaration in this repo by name and arity, with no receiver evidence; a bare call inherited from a superclass OUTSIDE the repo, or a static import of an external method, produces exactly this, so the hop may not exist at all. `"shape-only"` -- matched on name and arity alone. `"guessed"` -- one of several candidates.

Only `"receiver-resolved"` is worth acting on unreviewed, and it is rarer than it looks: a call whose name is unique in the repo never runs receiver narrowing at all, so a perfectly genuine hop routinely reports `"unique-name-only"`. Treat the rank as a ranking, not a verdict -- ship the path with `location_for`'s `file:line` per hop and let a human confirm.

The example above audits an UNFILTERED path hop by hop after the fact. `g.shortest_path_to_any_filtered(endpoint, sinks, max_depth, RECEIVER_TYPE_MATCH, RECEIVER_TYPE_MISMATCH)` does the equivalent filtering UP FRONT instead: every hop it returns already carries the required evidence, so there is no weakest-link ranking left to compute. The tradeoff is the one "Filtered vs unfiltered" above describes -- `None` from the filtered form means only "no strong-evidence path found", never a proof that `endpoint` cannot reach `sinks`.

## Use cases

- **Orphan/dead symbols**: iterate dense ids, report `is_definitely_dead_code(i) == Some(true)`.
- **Unwired components**: report `is_symbol_referenced(i) == false` for a specific declaration kind (e.g. Spring `@Component` classes with zero inbound edges).
- **Layering violations / package cycles**: `g.strongly_connected_components()` over module-level symbol ids. **Read the two caveats before acting on a component.** (1) `callers_of`/`callees_of` read the POST-CAP candidate arena, where a reference's candidate window may include more than one proposed target when the binder could not disambiguate -- an SCC over these edges is a POSSIBLE cycle among proposed candidates, not a confirmed source-level reference cycle. Name your findings accordingly (the shipped `find-reference-cycles` template calls them `possible_candidate_cycle`, not `reference_cycle`) and verify each component against source before reporting it. (2) Tarjan returns a self-recursive method as a SINGLETON component, so the common `component.len() > 1` filter silently misses ALL self-recursion -- check `g.callees_of(d).contains(&d)` before treating a one-node component as nothing to report. If you would rather under-report than ship an unverified cycle, `g.strongly_connected_components_filtered(RECEIVER_TYPE_MATCH, RECEIVER_TYPE_MISMATCH)` drops a cycle formed purely by weak/mismatched evidence -- at the cost of also dropping a REAL cycle closed only by an unqualified or static-import call (see "Filtered vs unfiltered" above).
- **Endpoint -> sink reachability**: `g.shortest_path_to_any(endpoint, sinks, max_depth)`, always ship the path and audit every hop (see Directional asymmetry below) -- a false positive here is expensive, so when that is the failure you most need to avoid, use `g.shortest_path_to_any_filtered(endpoint, sinks, max_depth, RECEIVER_TYPE_MATCH, RECEIVER_TYPE_MISMATCH)` instead; either way, `None` means "no path found", never a proof of unreachability (see "Filtered vs unfiltered" above).
- **Blast radius** ("how much of the codebase can a change to `roots` affect"): `g.reachable_to(roots, max_depth).len()` -- the transitive CALLERS closure. Pass METHOD dense ids. `g.reachable_from` answers the OPPOSITE question ("what do `roots` themselves depend on") and is the wrong primitive here; pointed at a class-level root it returns `1`, silently. A change-safety question like this one tolerates a false positive (an extra symbol to double-check) far better than a false negative (a caller you never noticed), so the UNFILTERED count is the right default here -- but if the count looks implausibly large or identical across several different roots, that is a real signal to cross-check with `reachable_to_filtered` and read the actual call sites before trusting it (see "Filtered vs unfiltered" above).

## Templates

Ready-to-adapt graph-mode evaluators. Every one is real, compilable source (proven by an automated test that compiles every example on this page through the real evaluator pipeline) -- copy one as `evaluator_code` and edit the parts that need editing.

The Quick Start example above is the **dead-code sweep** template. The Directional asymmetry example above is the **endpoint-to-sink reachability** template, with a full evidence audit. Below is the **possible reference cycles** template referenced by the layering-violations use case:

```rust
// Walks every strongly-connected component unconditionally -- nothing to
// edit. When fact_graph_complete: false, an empty or sparse findings list
// is untrustworthy. strongly_connected_components traverses the same
// POST-CAP CSR candidate arena as callers_of/callees_of.
fn collect_facts(node: &OwnedNode, file: &str) -> Vec<UserFact> {
    Vec::new()
}

// The negative control is computed and emitted BEFORE any per-component
// finding, so it always lands in findings[0] regardless of how many
// components the graph has.
//
// A singleton SCC with a genuine self-edge
// (g.callees_of(*dense_id).contains(dense_id)) is a real one-node cycle,
// not a suppressed acyclic node -- Tarjan cannot distinguish the two by
// component length alone, so this template checks the edge itself before
// treating component.len() < 2 as "nothing to report".
//
// possible_candidate_cycle (not reference_cycle): callers_of/callees_of
// read the POST-CAP candidate arena, where a reference's candidate window
// may include more than one proposed target when the binder could not
// disambiguate -- an SCC over these edges is a possible cycle among
// proposed candidates, not a confirmed source-level reference cycle.
fn analyze_component(
    g: &GraphHandle<'_>,
    component: &[u32],
    acyclic_singletons: &mut usize,
    self_loop_singletons: &mut usize,
) -> Option<ReduceFinding> {
    if component.len() < 2 {
        for dense_id in component {
            if !g.callees_of(*dense_id).contains(dense_id) {
                *acyclic_singletons += 1;
                return None;
            }
            *self_loop_singletons += 1;
            return g.resolve_symbol(*dense_id).map(|symbol| ReduceFinding {
                pattern: "possible_candidate_cycle".to_string(),
                message: "component_size=1 unresolved_drop_count=0 self_loop=true".to_string(),
                involved: vec![symbol],
                signatures: vec![g.signature_for(*dense_id).unwrap_or("<no-signature>").to_string()],
            });
        }
        return None;
    }
    let true_size = component.len();
    let mut involved: Vec<u64> = Vec::new();
    let mut signatures: Vec<String> = Vec::new();
    for dense_id in component {
        if let Some(symbol) = g.resolve_symbol(*dense_id) {
            involved.push(symbol);
            signatures.push(g.signature_for(*dense_id).unwrap_or("<no-signature>").to_string());
        }
    }
    let unresolved_drop_count = true_size - involved.len();
    Some(ReduceFinding {
        pattern: "possible_candidate_cycle".to_string(),
        message: format!("component_size={} unresolved_drop_count={}", true_size, unresolved_drop_count),
        involved,
        signatures,
    })
}

fn analyze_graph(g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> GraphResult {
    let mut result = GraphResult::default();
    let components = g.strongly_connected_components();
    let mut acyclic_singletons: usize = 0;
    let mut self_loop_singletons: usize = 0;
    let mut buffered: Vec<ReduceFinding> = Vec::new();
    for component in &components {
        if let Some(finding) = analyze_component(g, component, &mut acyclic_singletons, &mut self_loop_singletons) {
            buffered.push(finding);
        }
    }
    result.findings.push(ReduceFinding {
        pattern: "reference_cycle_negative_control".to_string(),
        message: format!(
            "acyclic_singletons_suppressed={} self_loop_singletons={}",
            acyclic_singletons, self_loop_singletons
        ),
        involved: Vec::new(),
        signatures: Vec::new(),
    });
    result.findings.append(&mut buffered);
    result
}
```

The template above walks the UNFILTERED `strongly_connected_components()`, which is the right default when a missed cycle is worse than a false one -- it already names its findings `possible_candidate_cycle` and tells you to verify each one. Swapping in `g.strongly_connected_components_filtered(RECEIVER_TYPE_MATCH, RECEIVER_TYPE_MISMATCH)` suppresses a cycle formed purely by weak or mismatched evidence before it ever reaches `findings[]` -- but it will just as readily suppress a REAL cycle closed only by an unqualified or static-import call, which carries no receiver evidence to test at all (see "Filtered vs unfiltered" above). Prefer the filtered form only when an unverified false cycle in this report would itself be the costly mistake.

## Execution model

This tool runs SYNCHRONOUSLY within `timeout_seconds` (off the server's event loop) -- it does not yet submit a `BackgroundJobManager` job the way `xray_search` does, so `await_seconds` is accepted but currently inert. Full async job-polling parity may be added in a future release; for now, plan for `timeout_seconds` (max 600s) to cover the whole pipeline: repo resolution, file collection, evaluator compile, `--build-graph`, `--analyze-graph`, and -- only when `refine: true` AND `GraphResult.refine` came back non-empty -- a THIRD subprocess, `--refine`, inside the SAME budget (its own deadline is whatever remains of `timeout_seconds` after the first two phases, never additional time). A `refine` request that would otherwise exceed the budget reports `refine_status: "skipped_timeout"` rather than extending the deadline -- the primary `findings`/`fact_graph_complete` result is unaffected either way.

## Related

- See `xray_search` for single-file AST pattern matching (regex-driven candidate selection, one evaluator call per file).
- See `xray_explore` for AST structure discovery to help craft `collect_facts`/`analyze_graph` logic.
- Every ready-to-adapt graph-mode template lives inline in this doc (Quick Start, Directional asymmetry, and Templates above), reachable on every deployment -- this page is the cookbook; adapt an example directly rather than fetching an external one.
- Full contract, evidence-bit guidance, and every example on this page: `cidx_quick_reference(tool="analyze_graph")`.
