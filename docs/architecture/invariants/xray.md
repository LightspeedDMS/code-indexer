# X-Ray Invariants

Rules the X-Ray code (AST-aware search over user-supplied Rust evaluators) must keep. How X-Ray works:
[X-Ray architecture](../xray/architecture.md), [Sandbox](../xray/sandbox.md),
[Graph binder internals](../xray/graph-binder-internals.md). Index of all invariant groups: [README](README.md).

## Lazy loading of tree-sitter

- `tree_sitter` and `tree_sitter_languages` are imported only inside `AstSearchEngine.__init__`
  (`src/code_indexer/xray/`). CLI startup must not load them.
- Gate: `tests/unit/xray/test_lazy_load.py` imports the CLI in a subprocess and asserts tree-sitter is absent from
  `sys.modules`. It runs in a subprocess because an in-process check is defeated by modules an earlier test already
  imported.
- Raw `tree_sitter.Node` objects are never handed to evaluator code; the Python engine wraps them in `XRayNode`.
- Python dependencies are pinned in `pyproject.toml`: `tree-sitter>=0.21,<0.22`, `tree-sitter-languages==1.10.2`.

## Evaluator execution path

- MCP and REST evaluate through the Rust engine (`rust/xray-core`, `rust/xray-cli`), called from
  `src/code_indexer/xray/rust_backend.py` (`RustNativeBackend`). The evaluator is user Rust code with the signature
  `fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding>`.
- `validate_rust_evaluator()` runs before any job is submitted (MCP handlers in
  `src/code_indexer/server/mcp/handlers/xray/`, REST in `src/code_indexer/server/routes/xray_routes.py`).
- The Python `PythonEvaluatorSandbox` (`src/code_indexer/xray/sandbox.py`, `HARD_TIMEOUT_SECONDS = 5.0`) is retained
  but is not on the MCP/REST request path. See [Sandbox](../xray/sandbox.md).
- The Rust engine supports 17 languages by extension (`rust/xray-core/src/languages.rs`): Java, Kotlin, Python,
  TypeScript (ts, tsx), JavaScript (js, jsx, mjs, cjs), Go, C#, Bash, HTML, CSS, HCL/Terraform, YAML, SQL, XML,
  Groovy, C (`.c`, `.h`) and C++ (`.cc`, `.cpp`, `.cxx`, `.c++`, `.hpp`, `.hh`, `.hxx`, `.h++`). A `.h` file parses
  with the C grammar.
- Search jobs are asynchronous: the handler returns a `job_id` and clients poll the jobs API. `await_seconds` is
  accepted in `[0.0, 45.0]`; values above `_AWAIT_SECONDS_WARN_THRESHOLD` log a warning
  (`handlers/xray/_search.py`, `_explore.py`).
- `repository_alias` accepts a string, a list of strings, or a JSON array string (multi-repo search).

## Rust engine boundaries

- The compiler module (`rust/xray-core/src/compiler.rs` and `compiler/`; `compile_evaluator` in
  `compiler/pipeline.rs`) compiles evaluator code to a `cdylib` with `rustc`; `dynlib.rs` loads it with
  `libloading` and checks the ABI version before trusting any function pointer; `validator.rs` rejects `unsafe`,
  `std::fs`/`net`/`process` and raw pointers in evaluator code.
- The host and the compiled evaluator must use the same allocator. Custom global allocators (jemalloc, mimalloc) are
  incompatible with this design: owned types cross the library boundary. Keep the system allocator.
- `OwnedNode` (`owned_node.rs`) shares one `Arc<str>` of file source per file and slices text by byte range.

## Compile cache identity

- The compile cache key is `compute_cache_identity(assembled_source, XRAY_ABI_VERSION, rustc_version)`, defined in
  `rust/xray-core/src/compiler/assemble.rs` and re-exported from `compiler.rs`: one SHA-256 over the full assembled source (preamble + user code + epilogue), the ABI version and the
  rustc version. Never derive a key from the raw user code alone; any preamble or epilogue change must go through
  this function (or `cache_identity_info_from_source` / `cache_identity_info`).
- `XRAY_ABI_VERSION` has exactly one definition (in `compiler/assemble.rs`, used as `compiler::XRAY_ABI_VERSION`).
  The preamble carries the placeholder `ABI_VERSION_PLACEHOLDER`, substituted at assembly time; `dynlib.rs` reads the
  same constant.
- A cached `.meta` file without a parseable `abi_version` (written before the composite identity existed) is always a
  cache miss, never a silent match (`rust/xray-core/src/cache.rs`).
- Python never re-implements the hash. `RustNativeBackend` obtains it from `xray-cli --print-cache-identity`, at most
  once per `run_batch()` (bounded per-instance `_identity_cache`, `_IDENTITY_CACHE_MAX_ENTRIES = 32`), with the
  subprocess timeout clamped to what remains of the caller's deadline. It is not invoked at all when no cluster cache
  backend is configured (solo/CLI). Every failure path records the `cidx.xray.cache_identity_failures` counter
  (`_record_identity_failure_metric`).
- The PostgreSQL cluster cache (`xray_cache_backend.py`) stores the identity as the value of its `source_hash` primary
  key. TTL is enforced on read only (the `fetch()` cutoff). Expired rows are deleted lazily by `_cleanup_expired()`,
  which runs only as a side effect of `store()`: on an idle cluster old rows remain. Do not describe them as aging
  out automatically.
- Concurrent compiles of the same identity are isolated: each compile builds in a private temporary directory inside
  the cache directory and publishes only the finished `.so` by atomic rename; the build directory is removed on every
  exit path. `.meta` files are written through a per-call unique temporary file. No locks are used.

## Pattern library

- Service: `XrayPatternService` (`src/code_indexer/server/services/xray_pattern_service.py`). Patterns live in
  cidx-meta under `xray-patterns/{scope}/{name}.yaml`; `__any__` is the cross-repository scope.
- Resolution order is repository scope first, then `__any__`. Never reverse it.
- Scope and name reject `/`, `\` and `..` before any filesystem access; a non-string `repo_alias` is rejected with
  `invalid_repo_alias_type`.
- Declared parameters become typed `const NAME: type = value;` lines prepended to the evaluator (types `usize`, `i64`,
  `f64`, `bool`, `str`).
- `pattern_name` and `evaluator_code` are mutually exclusive; the handlers normalise `repository_alias` before
  resolving a pattern (`_pattern_scope_alias()` in `handlers/xray/_infra.py`), and a genuine multi-repo list resolves
  against the `__any__` scope.
- MCP `store_xray_pattern` defaults to `overwrite=false` (an existing pattern returns `pattern_already_exists`), and
  the evaluator code is validated with `validate_rust_evaluator()` before anything is stored.
- The built-in seed patterns are ensured once per process (`_seeds_ensured` in `handlers/xray/_infra.py`).
- Pattern writes take the coarse cidx-meta write lock (`_run_with_coarse_lock`), so they serialise with refresh,
  memory-store and dependency-map writers.
