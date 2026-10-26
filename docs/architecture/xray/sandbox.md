# X-Ray Evaluator Security Boundary

Maintainer reference for how the server runs caller-supplied X-Ray evaluator code: Rust source that is validated,
compiled into a shared library and executed in a child process. It applies to every evaluator that reaches
`xray_search`, `xray_explore`, `xray_search_batch` and `analyze_graph`, including stored patterns. The engine around
it is described in [architecture.md](architecture.md).

## Layers

| Layer | Where | What it enforces |
|-------|-------|------------------|
| 1. Pre-flight validation | `validate_rust_evaluator()`, `src/code_indexer/xray/sandbox.py` | Required entry points and a fast forbidden-construct scan, before any job or compile starts |
| 2. Authoritative validation | `validate_evaluator_source()`, `rust/xray-core/src/validator.rs` | AST-based allowlist and blocklist, run by the compile pipeline before `rustc` |
| 3. Compilation | `rust/xray-core/src/compiler/` | Assembly with the fixed PREAMBLE/EPILOGUE, pinned toolchain, bounded compile time |
| 4. Execution | `xray-cli` child process, `src/code_indexer/xray/rust_backend.py` | Evaluator runs outside the server process; killed with its process group on timeout |
| 5. Output handling | `rust_backend.py` | File reads confined to the repository root; server paths removed from error messages |

## 1. Pre-flight validation

Every handler calls `validate_rust_evaluator(code)` before submitting work. It rejects:

- `source_too_large` / `nesting_too_deep`: checked first, before any other scan. A source over
  `MAX_EVALUATOR_SOURCE_BYTES` (131072 UTF-8 bytes, about 18 times the largest documented evaluator), or bracket
  nesting deeper than `MAX_BRACKET_DEPTH` (64; `{`, `(` and `[` counted together by one linear scan of the raw text,
  including strings and comments). The values live in `code_indexer/xray/rust_source_limits.py` and mirror
  `rust/xray-core/src/validator/source_limits.rs` (drift test `python_mirror_matches_source_limits`). Both limits are
  applied before any parsing, in the Python pre-flight and in the Rust validator; source beyond either gets a clean
  validation error.
- `missing_entry_point`: neither `fn evaluate_node` (single-file mode) nor both `fn collect_facts` and
  `fn analyze_graph` (graph mode). Mixed-mode sources pass here and are rejected by the Rust compiler's own mode
  detection.
- `forbidden_stdlib`: any `std`, `core` or `alloc` path outside `PERMITTED_STD_PATHS` (modules such as
  `std::collections`, `std::cmp`, `std::fmt`, `std::iter`, `std::str`, `std::error`, `std::any`, the `core::`
  counterparts that exist, and `alloc::{vec, string, collections, boxed, rc}`). The allowed modules give no file,
  network, process, thread or environment access; `RandomState::new()` reads OS randomness, and `Box`/`Rc` can
  produce raw pointers, which are unusable because `unsafe` is rejected. Rejected in a `use` tree or in code, with
  any rename of a standard-library path. The `use`-tree walk is iterative. The list lives in
  `code_indexer/xray/rust_std_paths.py` and mirrors `rust/xray-core/src/validator/std_paths.rs`, the authority; the
  Rust test `python_mirror_matches_permitted_std_paths` fails when the two lists differ. A `use` tree the Python
  scanner cannot parse gets no Python verdict and is left to the Rust validator.
- The constructs in `_RUST_FORBIDDEN_PATTERNS`: `unsafe`; raw pointers (`*const`, `*mut`); `extern` (including
  `extern crate`); `mod`; `static`; `macro_rules!`; and named macros such as
  `include!`, `include_str!`, `include_bytes!`, `env!`, `option_env!`, `print!`, `println!`, `eprint!`,
  `eprintln!`, `panic!`, `todo!`, `unimplemented!`.
- Any macro invocation other than a bare `vec!`, `format!` or `matches!`.

The scan runs on the source with string and character literal contents and comments blanked out, so text inside a
string or comment is not mistaken for code. Layer 2 is the authoritative check; this layer rejects the same macro
names early.

## 2. Authoritative validation

`validate_evaluator_source()` first applies the same size and bracket-depth limits (from one Rust constant pair in
`validator/source_limits.rs`), then parses the source with `syn` and walks the AST. It rejects:

- `unsafe` blocks, `unsafe` on every function signature (free functions, impl methods, trait methods), `unsafe impl`,
  `unsafe trait`, and `unsafe` function-pointer types;
- every WRITTEN `std`/`core`/`alloc` path outside `PERMITTED_STD_PATHS`, in a `use` tree or a path, and any rename
  of a standard-library path; `extern crate`;
- `static` items, raw pointer types, `extern` blocks, `extern` ABI functions and `extern` ABI function-pointer types,
  `mod` declarations;
- `macro_rules!` definitions;
- macro invocations other than bare, unqualified `vec!`, `format!` and `matches!`, with the arguments of allowed
  macros inspected as well, and macro nesting deeper than 32;
- attributes other than doc comments;
- any syntax node `syn` leaves unparsed (a `Verbatim` item, impl item, trait item, foreign item, expression, type,
  pattern or type bound), since no other rule can inspect it.

The allowlist governs paths the evaluator writes. User code shares the crate root with the trusted PREAMBLE and
EPILOGUE, so names they import or define there (`Arc`, `RefCell`, `PhantomData`, and the preamble's own types and
functions) remain usable by bare name; each of them is part of the evaluator's reachable surface and must be reviewed
like an allowlist entry.

Graph-mode sources go through the same visitor first.

## 3. Compilation

`compile_evaluator()` (`rust/xray-core/src/compiler/pipeline.rs`) validates, computes the compile identity
(`compute_cache_identity`: SHA-256 over the assembled source, `XRAY_ABI_VERSION` and the `rustc` version), reuses a
cached library when one exists, and otherwise compiles. `rustc` runs with `RUSTUP_TOOLCHAIN` pinned to
`rust/rust-toolchain.toml`'s channel, `--crate-type cdylib`, `-C opt-level=2`, in its own process group, and is
killed after `RUSTC_COMPILE_TIMEOUT` (120 s, `compiler/rustc_driver.rs`). The cache lives in
`$CIDX_DATA_DIR/xray-cache/` (default `~/.cidx-server/xray-cache/`, `rust/xray-core/src/cache.rs`). The server allows
at most 4 concurrent compiling `xray-cli` launches per process (`_MAX_CONCURRENT_COMPILES`, `rust_backend.py`); a
request that cannot get a compile slot within its timeout fails with a "compile queue full" error.

The evaluator only sees the types the PREAMBLE declares: `OwnedNode` and `EvalFinding` in single-file mode, and the
opaque `GraphHandle` accessors in graph mode (ADR-002).

## 4. Execution

The compiled library is never loaded into the server process. `RustNativeBackend` starts `xray-cli` with
`subprocess.Popen(..., start_new_session=True)`; `xray-cli` loads the library (`libloading`,
`rust/xray-core/src/dynlib.rs`) and calls the entry points. When the request's timeout expires, or the job is
cancelled, the child's process group is stopped through `terminate_process_group` (`src/code_indexer/utils/process_group.py`:
SIGTERM, then SIGKILL for survivors). `rustc` leads its own process group, so it never depends on that group signal:
it carries a parent-death SIGKILL, and `xray-cli`'s SIGTERM/SIGINT/SIGHUP handler kills and reaps every `rustc` group
before exiting (`rust/xray-core/src/child_group_reaper.rs`). `xray-cli` also inherits the write end of a liveness pipe
that every process it starts inherits in turn; the backend releases the compile slot only after that pipe reports
EOF, so no compile process of a returned request is still running. A crash or panic inside the evaluator terminates or fails only that child.

The killable analyze child (`run_analyze_child`, `graph/analyze/process.rs`) gets the same guarantees: parent-death
SIGKILL, coverage by the termination handler, and a drop guard. Accepted residual: when `xray-cli` itself is SIGKILLed
(its handler cannot run, which happens only if it ignored SIGTERM for the whole grace period), `rustc` dies by the
parent-death signal but a linker `rustc` had already started runs until it finishes on its own. The compile slot stays
held while that linker holds the liveness pipe, at most 120 s, after which it is released and an ERROR is logged.
In graph mode the exported `collect_facts`, `analyze_graph` and `refine` wrappers catch panics (`catch_unwind`,
`compiler/graph_preamble.rs`) and report them as failures. When cgroup v2 delegation is available, the
`--analyze-graph` child is bounded by a `memory.max` ceiling derived from the admission estimate (`rust/xray-core/src/graph/analyze/memory_ceiling.rs`,
ADR-003).

## 5. Output handling

When findings are enriched with `line_content`, the source file is read only if it resolves inside the repository
root (`is_resolved_within_root`). `_sanitize_error_message()` replaces X-Ray cache paths with `evaluator.rs` and
redacts server filesystem paths before an error reaches the caller.

## Retained Python sandbox class

`sandbox.py` also contains `PythonEvaluatorSandbox`, a Python AST-whitelist evaluator runner from an earlier
evaluator contract. `XRaySearchEngine` still constructs it, but evaluation goes through `RustNativeBackend`; no MCP or
REST request executes Python evaluator code. Only `validate_rust_evaluator()` in that module is on the live path.
