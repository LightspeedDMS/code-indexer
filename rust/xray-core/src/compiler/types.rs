//! Issue #1934: the compile-pipeline's public result/error types, extracted
//! verbatim out of `compiler.rs` (pure move -- no behaviour change, see the
//! module doc comment on `super`). Kept in their own module so both
//! `assemble` (mode detection needs to construct `CompileError`) and
//! `rustc_driver`/`pipeline` (the actual compile pipeline) can depend on
//! them without a circular module reference.

use std::path::PathBuf;

/// Result of a successful compilation.
#[derive(Debug)]
pub struct CompileResult {
    pub so_path: PathBuf,
    pub compile_ms: u128,
    pub cached: bool,
}

/// Distinguishes a genuine problem in the USER's evaluator source from a
/// problem in the xray-cli/toolchain/filesystem infrastructure (Bug #1827,
/// Codex H2). Labelling EVERY compile-pipeline failure "CompileError"
/// regardless of cause tells an agent to debug perfectly valid Rust when
/// the real problem is e.g. a broken cache directory -- defeating the
/// exact feedback loop the fix exists to restore.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CompileErrorKind {
    /// A genuine problem in the user's evaluator source: a sandbox
    /// validation rejection, an ambiguous/missing evaluator mode, an
    /// oversized source, or a real rustc diagnostic. The agent should
    /// read `details` and fix its own code.
    Compile,
    /// A problem in the xray-cli/toolchain/filesystem infrastructure,
    /// unrelated to the content of the user's source (rustc could not be
    /// invoked or timed out, the cache/build directory could not be
    /// created or written, the compiled artifact could not be published
    /// or loaded). The user's evaluator code is not necessarily at fault.
    Infrastructure,
}

/// Error from the compilation pipeline.
#[derive(Debug)]
pub struct CompileError {
    pub message: String,
    pub details: Vec<String>,
    pub kind: CompileErrorKind,
}

impl std::fmt::Display for CompileError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.message)?;
        for d in &self.details {
            write!(f, "\n  {}", d)?;
        }
        Ok(())
    }
}
