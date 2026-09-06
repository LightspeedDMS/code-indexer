//! Binder "reasons" bitflags (Story #1787, S2, AC4 -- types only, no binder
//! resolution logic).
//!
//! Each flag records ONE independent piece of evidence a (future, S2/S4)
//! binder found when proposing a candidate definition for a reference.
//! Multiple flags can be set on the same `Candidate.reasons` value at once
//! (e.g. a call resolved via both `SAME_PACKAGE` and `ARITY_MATCH`), which is
//! why these are bit flags rather than an enum: `Confidence` (see
//! `super::confidence`) is the single ordered value DERIVED from whichever
//! flags are set, never set independently of them.
//!
//! Plain `u16` constants are used here rather than a `bitflags!`-macro type,
//! per Messi Rule 17 (anti-magic): the bit layout stays a fact anyone can
//! read directly off the source, with no macro expansion standing between
//! the constant name and the value stored in `Candidate.reasons`.

/// The reference and its resolved candidate are declared in the same file.
pub const SAME_FILE: u16 = 1 << 0;
/// The reference and its resolved candidate are declared in the same
/// package/namespace/module.
pub const SAME_PACKAGE: u16 = 1 << 1;
/// The candidate's enclosing type/module is reachable via an ordinary
/// (non-wildcard, non-static) import in the referencing file.
pub const IMPORTED: u16 = 1 << 2;
/// The candidate is reachable via a `static import` (or language equivalent)
/// in the referencing file.
pub const STATIC_IMPORT: u16 = 1 << 3;
/// The candidate is reachable via a wildcard/glob import in the referencing
/// file.
pub const WILDCARD_IMPORT: u16 = 1 << 4;
/// The candidate's declared arity (parameter count) matches the call site's
/// argument count.
pub const ARITY_MATCH: u16 = 1 << 5;
/// The candidate's bare name is unique across the entire repository -- no
/// other declaration anywhere shares it.
pub const UNIQUE_NAME_IN_REPO: u16 = 1 << 6;
/// The reference used a fully qualified name that matches the candidate's
/// declared qualified name exactly.
pub const QUALIFIED_NAME: u16 = 1 << 7;
/// The candidate was proposed via a weaker, string-based heuristic (e.g. a
/// reflection-style string literal matching a symbol name) rather than any
/// of the structural signals above.
pub const STRING_HEURISTIC: u16 = 1 << 8;

/// Every reasons flag, for iteration in tests and future observability code.
/// Ordering here is purely presentational (declaration order); it carries no
/// meaning for `Confidence` derivation (see `super::confidence`, which reads
/// each flag independently rather than iterating this list).
pub const ALL_FLAGS: &[u16] = &[
    SAME_FILE,
    SAME_PACKAGE,
    IMPORTED,
    STATIC_IMPORT,
    WILDCARD_IMPORT,
    ARITY_MATCH,
    UNIQUE_NAME_IN_REPO,
    QUALIFIED_NAME,
    STRING_HEURISTIC,
];

#[cfg(test)]
mod tests {
    use super::*;

    /// Every named flag must occupy exactly one bit and no two flags may
    /// share a bit. A wrong implementation that accidentally duplicated a
    /// shift value (e.g. copy-pasting `1 << 2` for both `IMPORTED` and
    /// `STATIC_IMPORT`) would let setting one flag silently also read as the
    /// other being set -- this test catches that by requiring every flag's
    /// popcount to be 1 and the bitwise-OR of all flags to have exactly
    /// `ALL_FLAGS.len()` bits set (proving zero overlap).
    #[test]
    fn every_flag_is_a_single_disjoint_bit() {
        for &flag in ALL_FLAGS {
            assert_eq!(flag.count_ones(), 1, "flag {flag:#06b} is not a single bit");
        }
        let union = ALL_FLAGS.iter().fold(0u16, |acc, &flag| acc | flag);
        assert_eq!(
            union.count_ones() as usize,
            ALL_FLAGS.len(),
            "flags overlap: union has fewer set bits than the flag count"
        );
    }

    /// Every flag is typed `u16` (the exact storage type
    /// `Candidate.reasons` uses, AC5), so fitting is guaranteed by the type
    /// system alone. What this guards against is silently growing past 16
    /// flags without anyone revisiting the CSR layout: with 9 flags
    /// declared today, there is headroom for 7 more before a 17th flag
    /// would no longer fit and this count would need to change.
    #[test]
    fn nine_flags_are_declared_leaving_headroom_in_the_u16_reasons_field() {
        assert_eq!(ALL_FLAGS.len(), 9);
    }
}
