/// Evaluator compilation pipeline: validate → assemble → compile → cache.
///
/// Issue #1934: this used to be a single 2,806-line file. It is now split
/// into coherent submodules (pure move -- no behaviour change; every
/// string constant that ends up compiled into an evaluator artifact is
/// byte-identical to the original, verified by diff at split time and by
/// the byte-identical assembled-source regression test in
/// `tests_identity.rs`):
///
///   - `preamble` / `graph_preamble`: the evaluator PREAMBLE/EPILOGUE text
///     and the GraphHandle/FactsHandle mirror (string literals compiled
///     verbatim into every evaluator artifact).
///   - `types`: the public `CompileResult`/`CompileError`/`CompileErrorKind`
///     result/error types.
///   - `assemble`: evaluator source assembly and the single
///     `XRAY_ABI_VERSION` source of truth, plus compile-cache identity
///     (Bug #1784's `compute_cache_identity`).
///   - `rustc_driver`: building and running the actual `rustc` invocation
///     that compiles an evaluator's assembled source into a `.so`.
///   - `diagnostics`: rustc error line-number rewriting (Bug #1827).
///   - `pipeline`: the top-level `compile_evaluator` pipeline (validate ->
///     hash -> cache check -> assemble -> compile -> save).
///
/// This root re-exports the same public API surface at the same
/// `crate::compiler::*` paths every external call site (dynlib.rs,
/// xray-cli, preamble_ac18_parity.rs, xray-cli integration tests) already
/// used before the split -- no orphan code, no duplicate definitions.
mod preamble;
mod graph_preamble;
mod types;
mod assemble;
mod rustc_driver;
mod diagnostics;
mod pipeline;

pub use types::{CompileError, CompileErrorKind, CompileResult};

pub use assemble::{
    assemble_evaluator_source, cache_identity_info, cache_identity_info_from_source,
    cache_identity_info_graph, compute_cache_identity, detect_evaluator_mode,
    CacheIdentityInfo, EvaluatorMode, XRAY_ABI_VERSION,
};

pub use pipeline::compile_evaluator;

pub use diagnostics::adjust_error_lines;

// Crate-internal re-exports: not part of the public API, but needed so
// sibling modules elsewhere in the crate (`preamble_ac18_parity.rs`, via
// `crate::compiler::PREAMBLE` etc.) and the relocated test files declared
// below (via `use super::*;`, exactly mirroring how the original single
// `mod tests { use super::*; ... }` saw every item in this file) can still
// reach them.
#[cfg(test)]
pub(crate) use preamble::{EPILOGUE, PREAMBLE};
#[cfg(test)]
pub(crate) use graph_preamble::{
    GRAPH_PREAMBLE_EXTRA_1, GRAPH_PREAMBLE_EXTRA_2, GRAPH_PREAMBLE_EXTRA_3, GRAPH_PREAMBLE_EXTRA_4,
    GRAPH_PREAMBLE_EXTRA_5, GRAPH_PREAMBLE_EXTRA_6,
};
#[cfg(test)]
pub(crate) use assemble::{
    assemble_evaluator_source_with_preamble_and_bounds, assemble_graph_evaluator_source,
    assemble_graph_evaluator_source_and_bounds, sha256_hex, ABI_VERSION_PLACEHOLDER,
};
#[cfg(test)]
pub(crate) use rustc_driver::{evaluator_rustc_command, run_rustc_with_timeout};
#[cfg(test)]
pub(crate) use pipeline::{chrono_now_iso, compile_evaluator_with_preamble};
#[cfg(test)]
pub(crate) use diagnostics::adjust_gutter_line;

#[cfg(test)]
#[path = "compiler/tests_identity.rs"]
mod tests_identity;

#[cfg(test)]
#[path = "compiler/tests_pipeline.rs"]
mod tests_pipeline;

#[cfg(test)]
#[path = "compiler/tests_diagnostics.rs"]
mod tests_diagnostics;

#[cfg(test)]
#[path = "compiler/tests_debug_log.rs"]
mod tests_debug_log;
