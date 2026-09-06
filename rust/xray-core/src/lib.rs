pub mod owned_node;
pub mod graph;
pub mod finding;
pub mod languages;
pub mod scanner;
pub mod evaluators;
pub mod cache;
pub mod validator;
pub mod compiler;
pub mod dynlib;
// AC18 (#1787): structural parity check between compiler::PREAMBLE's mirrored
// OwnedNode/EvalFinding and the real types in owned_node.rs/finding.rs.
// Test-only: it exists to fail `cargo test` loudly on divergence, not to
// ship in the production binary.
#[cfg(test)]
mod preamble_ac18_parity;
