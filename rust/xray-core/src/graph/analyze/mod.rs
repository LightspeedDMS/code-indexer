//! AC7: `analyze_graph` runs in its OWN killable process, receiving the
//! bound graph via `--graph-in <path>` and mmap. Own process by exactly
//! #1785's second-process rationale (see ADR-001 migration step 3): one
//! invocation, whole graph, no AST, must be killable without losing the
//! prior extraction/bind work -- a hang or panic in the analyze child must
//! never destroy the parent's already-built `CodeGraph`.

pub mod memory_ceiling;
pub mod process;
pub mod result;

pub use memory_ceiling::{CgroupV2MemoryCeiling, MemoryCeiling, NoopMemoryCeiling};
pub use process::{run_analyze_child, run_analyze_child_with_memory_limit};
pub use result::{AnalyzeStatus, GraphResult, ReduceFinding};
