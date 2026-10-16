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

- `missing_entry_point`: neither `fn evaluate_node` (single-file mode) nor both `fn collect_facts` and
  `fn analyze_graph` (graph mode). Mixed-mode sources pass here and are rejected by the Rust compiler's own mode
  detection.
- The constructs in `_RUST_FORBIDDEN_PATTERNS`: `unsafe`; `std::fs`, `std::net`, `std::process`, `std::env`,
  `std::io`; raw pointers (`*const`, `*mut`); `extern`; `mod`; `static`; `macro_rules!`; and named macros such as
  `include!`, `include_str!`, `include_bytes!`, `env!`, `option_env!`, `print!`, `println!`, `eprint!`,
  `eprintln!`, `panic!`, `todo!`, `unimplemented!`.
- Any macro invocation other than a bare `vec!`, `format!` or `matches!`.

The scan runs on the source with string and character literal contents and comments blanked out, so text inside a
string or comment is not mistaken for code. Layer 2 is the authoritative check; this layer rejects the same macro
names early.

## 2. Authoritative validation

`validate_evaluator_source()` parses the source with `syn` and walks the AST. It rejects:

- `unsafe` blocks and `unsafe fn`;
- `use std::{fs,net,process,env,io}` (and sub-paths) and any path expression starting with those modules;
- `static` items, raw pointer types, `extern` blocks and `extern` ABI functions, `mod` declarations;
- `macro_rules!` definitions;
- macro invocations other than bare, unqualified `vec!`, `format!` and `matches!`, with the arguments of allowed
  macros inspected as well, and macro nesting deeper than 32;
- attributes other than doc comments.

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
`rust/xray-core/src/dynlib.rs`) and calls the entry points. When the request's timeout expires, the backend sends
`SIGKILL` to the child's process group. A crash or panic inside the evaluator terminates or fails only that child.
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
