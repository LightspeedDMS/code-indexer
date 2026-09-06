//! X-Ray code graph substrate (Story #1787, S2, AC2-in-progress + AC3 +
//! AC5 + AC4-types-only).
//!
//! This module holds the DATA SUBSTRATE for the whole-repository heuristic
//! code graph: symbol/file identity and cache keys (AC3), the confidence
//! bitflags and their derivation (AC4 types only -- no binder logic), the
//! CSR (compressed sparse row) memory layout that keeps candidate edges in
//! ONE flat allocation for the whole repository (AC5), and the per-language
//! extraction registry that populates a `LocalIndex` per file (AC2 --
//! `extract`; the fused pipeline that sequences extraction with
//! `collect_facts` is still being assembled in subsequent edits).
//!
//! Explicitly OUT of scope here (later slices of #1787): the binder itself
//! (AC4 resolution logic), the budget ladder (AC6), the killable analyze
//! process (AC7), ABI dylib-loaded callbacks (AC8), the durable graph cache
//! (AC9), and memory-governor wiring (AC12-AC17).

pub mod identity;
pub mod reasons;
pub mod confidence;
pub mod string_table;
pub mod csr;
pub mod extract;
pub mod user_facts;
pub mod fused;
pub mod fused_cache;
pub mod bind;
pub mod budget;
