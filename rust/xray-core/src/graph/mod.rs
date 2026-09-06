//! X-Ray code graph substrate (Story #1787, S2, AC3 + AC5 + AC4-types-only).
//!
//! This module holds the DATA SUBSTRATE for the whole-repository heuristic
//! code graph: symbol/file identity and cache keys (AC3), the confidence
//! bitflags and their derivation (AC4 types only -- no binder logic), and the
//! CSR (compressed sparse row) memory layout that keeps candidate edges in
//! ONE flat allocation for the whole repository (AC5).
//!
//! Explicitly OUT of scope here (later slices of #1787): fused extract+
//! collect (AC2), the binder itself (AC4 resolution logic), the budget
//! ladder (AC6), the killable analyze process (AC7), ABI callbacks (AC8),
//! the graph cache (AC9), and memory-governor wiring (AC12-AC17).

pub mod identity;
pub mod reasons;
pub mod confidence;
pub mod string_table;
pub mod csr;
