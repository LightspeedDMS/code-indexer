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
/// AC1 (Story #1793, S4): the candidate was added to the set by
/// INHERITANCE-FAMILY expansion -- a call resolved (via the OTHER reasons
/// bits) to an interface/supertype method, and this candidate is one
/// implementor's override of that same method, added by
/// `super::bind::families::TypeIndex::overrides_of`. Never set alone by
/// narrowing; it only ever WIDENS a candidate set, and every candidate
/// carrying it is part of a family bound together at `Confidence::High`.
pub const INHERITANCE_FAMILY: u16 = 1 << 9;
/// AC2 (Story #1793, S4): the candidate's declared parameter type shapes
/// (`Declaration::param_types`) are structurally CONSISTENT with the call
/// site's per-argument shapes (`InvocationSite::arg_shapes`) beyond plain
/// arity -- e.g. a string-literal argument against a `String` parameter,
/// or a cast/constructor argument whose named type matches. Used for
/// candidate-set REDUCTION only, exactly like `ARITY_MATCH`: it carries no
/// dedicated `Confidence` level of its own.
pub const OVERLOAD_ARG_TYPE_MATCH: u16 = 1 << 10;
/// Memory-safety amendment (Story #1793, S4): the candidate is part of an
/// inheritance family (always co-set with `INHERITANCE_FAMILY`) whose true
/// member count exceeded `super::bind::families::MAX_FAMILY_SIZE` --
/// `super::bind::families::TypeIndex::overrides_of` capped the expansion
/// rather than growing it without bound. This is deliberately NOT
/// evidence of confidence (it never participates in `Confidence::derive`,
/// which falls back to `NameOnly` for any bit it does not recognize): it
/// is a visibility flag saying "this family set is known-INCOMPLETE",
/// never a silently-narrowed one. See
/// `super::bind::resolve::apply_inheritance_family_expansion`.
pub const FAMILY_TRUNCATED: u16 = 1 << 11;
/// AC1 (Story #1806, S2b -- FINDING 3's missing narrowing): a
/// `receiver.method(...)` call's receiver expression resolved to a
/// declared TYPE in the SAME FILE (a local variable's, field's, or
/// parameter's declared type, or a chained call's declared return type --
/// no build, no classpath, no generics resolution), and the candidate's
/// enclosing type equals that resolved type OR one of its transitive
/// supertypes (`super::bind::families::TypeIndex::supertypes_of`).
/// Deliberately distinct from `SAME_CLASS_OR_SUPER` below (even though
/// both narrow via the SAME type-membership mechanism) so per-language
/// `BinderDepth` telemetry can tell the two evidence PATHS apart, matching
/// the amendment's own separately-measured contributions (35.4% receiver
/// type vs 10.0% same-class/super).
pub const RECEIVER_TYPE_MATCH: u16 = 1 << 12;
/// AC3 (Story #1806, S2b): an UNQUALIFIED call (no explicit receiver, or
/// an explicit `this`/`super`) resolved against the caller's OWN
/// enclosing type or one of its transitive supertypes -- the "same-class
/// and super resolution" the amendment attributed 10.0% of bound calls to.
pub const SAME_CLASS_OR_SUPER: u16 = 1 << 13;
/// #1924/#1925 (epic #1906): set ONLY when ALL FOUR of the following hold:
///
/// (a) the call's receiver has a POSITIVELY known declared type (a real
///     `TypedNameRecord` hit, never an open-world guess -- see
///     `super::bind::receiver::ReceiverEvidence::Positive`) that is
///     CLOSED-WORLD (`String`, a primitive, a boxed numeric/boolean/char
///     wrapper, or any array type) AND whose bare simple name is NOT ALSO
///     a repo-declared type, an explicitly imported type (ordinary OR
///     single-member static), or a known generic type parameter -- a
///     repo can legally declare its own class literally named `String`
///     in the same package, shadowing `java.lang.String`;
/// (b) the receiver binding is a genuine method/constructor PARAMETER,
///     never a block-scoped local variable or a field -- this binder's
///     own typed-name lookup is keyed per METHOD, not per BLOCK (#1919),
///     so a field access can be misresolved to an unrelated, same-method
///     local's declared type;
/// (c) that parameter's declared type was written UNQUALIFIED, or
///     qualified exactly as `java.lang.*` -- this binder's type model
///     only ever records a declared type's bare simple name, so a
///     parameter written `com.lib.String s` records the same `"String"`
///     as one written `String s`, even though `com.lib.String` is a
///     different, unproven type that may legally have a repo-declared
///     subtype;
/// (d) EVERY type declared in the call site's own FILE has fully
///     repo-resolved supertype evidence -- a type extending an
///     external/unindexed supertype may have a NESTED type privately
///     shadowing a closed-world name from further outside than this
///     binder can see into, on ANY nesting level, not just the call's
///     immediate enclosing type. Judged by SIMPLE NAME only, like every
///     other bare-name lookup in this binder: a repo-declared type
///     sharing the SAME bare name as the real external supertype makes
///     that supertype look "resolved" even though it is not actually the
///     same type.
///
/// Under all four conditions, this candidate's `enclosing_type` naming a
/// REPO-declared type is real evidence of unrelatedness: no repo type can
/// ever have a repo-declared subtype of a genuinely closed-world receiver
/// type (they are `final`, or not reference types at all). Set by
/// `super::bind::receiver_mismatch::apply_receiver_type_mismatch_tagging`,
/// TAG-ONLY like every other bit here -- it never removes a candidate,
/// it only gives an evaluator (via `GraphHandle`'s evidence-filtered
/// traversal primitives, e.g. `callees_of_filtered`) the means to drop
/// these edges from its own analysis.
pub const RECEIVER_TYPE_MISMATCH: u16 = 1 << 14;

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
    INHERITANCE_FAMILY,
    OVERLOAD_ARG_TYPE_MATCH,
    FAMILY_TRUNCATED,
    RECEIVER_TYPE_MATCH,
    SAME_CLASS_OR_SUPER,
    RECEIVER_TYPE_MISMATCH,
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
    /// flags without anyone revisiting the CSR layout: with 15 flags
    /// declared today (#1924 added `RECEIVER_TYPE_MISMATCH`, the 15th),
    /// there is headroom for exactly 1 more before a 17th flag would no
    /// longer fit and this count would need to change.
    #[test]
    fn fifteen_flags_are_declared_leaving_headroom_in_the_u16_reasons_field() {
        assert_eq!(ALL_FLAGS.len(), 15);
    }
}
