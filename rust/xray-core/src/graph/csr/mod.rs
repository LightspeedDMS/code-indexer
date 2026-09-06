//! CSR (compressed sparse row) memory layout for the whole-repository code
//! graph (Story #1787, S2, AC5).
//!
//! `graph.candidates: Vec<Candidate>` is ONE flat allocation for the entire
//! repository -- never one `Vec<Candidate>` per reference. Each `Reference`
//! records a `(cand_start, cand_len)` window into that single arena instead
//! of owning its own candidate collection. See `builder::CodeGraphBuilder`
//! for how that single allocation is reserved up front and never
//! reallocated while appending, and `code_graph::CodeGraph` for the
//! resulting immutable, query-only graph.

pub mod reference;
pub mod candidate;
pub mod symbol_table;
pub mod builder;
pub mod code_graph;
pub mod ops;
pub mod wire;
mod wire_cursor;

pub use candidate::Candidate;
pub use code_graph::CodeGraph;
pub use reference::Reference;
pub use builder::CodeGraphBuilder;
