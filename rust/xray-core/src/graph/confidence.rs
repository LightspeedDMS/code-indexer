//! `Confidence` -- an ordered projection DERIVED from `reasons` bitflags
//! (Story #1787, S2, AC4 -- types only, no binder resolution logic).
//!
//! `Ambiguous` and `Unresolved` are deliberately NOT variants here: per AC4,
//! ambiguity is `candidates.len() > 1` and unresolved is
//! `candidates.is_empty()` -- properties of a reference's candidate SET, not
//! of any single candidate's confidence. See `super::csr` for where those
//! two conditions are actually read, off `Reference.cand_len`.
//!
//! `Confidence` itself is an ordinary, freely-constructible public enum --
//! Rust's visibility model cannot gate *which variant* external code names,
//! only whether the type is visible at all. The invariant AC4 actually cares
//! about ("a Candidate's confidence can never disagree with its reasons") is
//! therefore enforced one level up, at `super::csr::Candidate`: its only
//! constructor calls `Confidence::derive(reasons)` internally and has no
//! parameter that would let a caller supply a `Confidence` independent of
//! `reasons`. That is what makes an inconsistent pairing unrepresentable --
//! not restricting this enum's own constructors.

use super::reasons;

/// Ordered from weakest (`NameOnly`) to strongest (`Exact`) evidence a
/// binder found for one candidate. The ordering is what `PartialOrd`/`Ord`
/// exist for: callers rank/sort candidates by confidence without needing to
/// know anything about the underlying `reasons` bits.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
#[repr(u8)]
pub enum Confidence {
    NameOnly = 0,
    SameFile = 1,
    Imported = 2,
    SamePackage = 3,
    /// AC1 (Story #1793, S4): a call resolved to an INHERITANCE FAMILY
    /// (an interface/supertype method plus every implementor's override,
    /// bound together as a set via `reasons::INHERITANCE_FAMILY`) --
    /// stronger evidence than mere same-package proximity, but weaker than
    /// the two SINGLE-TARGET-precision levels immediately above it: unlike
    /// them, a family is a known, bounded SET of real declarations, not
    /// one proven target.
    High = 4,
    /// AC3 (Story #1806, S2b): an unqualified/`this`/`super` call resolved
    /// against the caller's own enclosing type or one of its transitive
    /// supertypes (`reasons::SAME_CLASS_OR_SUPER`).
    SameClassOrSuper = 5,
    /// AC1/AC2 (Story #1806, S2b): a `receiver.method(...)` call's
    /// receiver resolved (via a locally-declared type, or AC2 return-type
    /// chaining) to a type whose declarations include the candidate
    /// (`reasons::RECEIVER_TYPE_MATCH`). Ranked above `SameClassOrSuper`:
    /// the amendment measured it as the larger single contributor (35.4%
    /// vs 10.0% of bound calls).
    ReceiverType = 6,
    QualifiedName = 7,
    Exact = 8,
}

impl Confidence {
    /// Derives the single ordered `Confidence` value for a `reasons`
    /// bitmask, by checking flags from STRONGEST to weakest and returning
    /// the first one that matches. This is a pure function: the same
    /// `reasons` value always yields the same `Confidence`. This is the
    /// function `Candidate::new` (in `super::csr`) calls to compute
    /// `confidence` from `reasons` -- never the other way around.
    pub fn derive(reasons_bits: u16) -> Confidence {
        if reasons_bits & reasons::UNIQUE_NAME_IN_REPO != 0 {
            Confidence::Exact
        } else if reasons_bits & reasons::QUALIFIED_NAME != 0 {
            Confidence::QualifiedName
        } else if reasons_bits & reasons::RECEIVER_TYPE_MATCH != 0 {
            Confidence::ReceiverType
        } else if reasons_bits & reasons::SAME_CLASS_OR_SUPER != 0 {
            Confidence::SameClassOrSuper
        } else if reasons_bits & reasons::INHERITANCE_FAMILY != 0 {
            Confidence::High
        } else if reasons_bits & reasons::SAME_PACKAGE != 0 {
            Confidence::SamePackage
        } else if reasons_bits
            & (reasons::IMPORTED | reasons::STATIC_IMPORT | reasons::WILDCARD_IMPORT)
            != 0
        {
            Confidence::Imported
        } else if reasons_bits & reasons::SAME_FILE != 0 {
            Confidence::SameFile
        } else {
            Confidence::NameOnly
        }
    }

    /// Decodes a `u8` already stored in the CSR arena (as
    /// `Candidate.confidence`, AC5) back into a `Confidence`. Every byte
    /// value that reaches this in practice is one `derive` itself produced
    /// at `Candidate::new` time -- this is a read-back of an
    /// already-consistent pairing, not a way to construct a fresh one. Any
    /// other byte indicates arena corruption -- fail loud rather than
    /// silently mapping to a wrong variant (Rule 13, anti-silent-failure).
    pub fn from_u8(value: u8) -> Confidence {
        match value {
            0 => Confidence::NameOnly,
            1 => Confidence::SameFile,
            2 => Confidence::Imported,
            3 => Confidence::SamePackage,
            4 => Confidence::High,
            5 => Confidence::SameClassOrSuper,
            6 => Confidence::ReceiverType,
            7 => Confidence::QualifiedName,
            8 => Confidence::Exact,
            other => panic!("corrupt Confidence byte in CSR arena: {other}"),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn no_reasons_derives_name_only() {
        assert_eq!(Confidence::derive(0), Confidence::NameOnly);
    }

    #[test]
    fn same_file_alone_derives_same_file() {
        assert_eq!(Confidence::derive(reasons::SAME_FILE), Confidence::SameFile);
    }

    #[test]
    fn each_import_variant_alone_derives_imported() {
        assert_eq!(Confidence::derive(reasons::IMPORTED), Confidence::Imported);
        assert_eq!(Confidence::derive(reasons::STATIC_IMPORT), Confidence::Imported);
        assert_eq!(Confidence::derive(reasons::WILDCARD_IMPORT), Confidence::Imported);
    }

    #[test]
    fn same_package_alone_derives_same_package() {
        assert_eq!(Confidence::derive(reasons::SAME_PACKAGE), Confidence::SamePackage);
    }

    /// AC1 (Story #1793, S4): "a family match is `Confidence::High` **as a
    /// set**" -- `INHERITANCE_FAMILY` evidence alone derives `High`, ranked
    /// strictly between `SamePackage` and the two SINGLE-TARGET-precision
    /// levels Story #1806 added (a family match is stronger evidence than
    /// mere same-package proximity, but weaker than a proven single
    /// target).
    #[test]
    fn inheritance_family_alone_derives_high() {
        assert_eq!(Confidence::derive(reasons::INHERITANCE_FAMILY), Confidence::High);
        assert!(Confidence::SamePackage < Confidence::High);
        assert!(Confidence::High < Confidence::SameClassOrSuper);
    }

    #[test]
    fn qualified_name_alone_derives_qualified_name() {
        assert_eq!(Confidence::derive(reasons::QUALIFIED_NAME), Confidence::QualifiedName);
    }

    /// AC1 (Story #1806, S2b): `RECEIVER_TYPE_MATCH` evidence alone
    /// derives `ReceiverType`, ranked strictly between `SameClassOrSuper`
    /// and `QualifiedName`.
    #[test]
    fn receiver_type_match_alone_derives_receiver_type() {
        assert_eq!(Confidence::derive(reasons::RECEIVER_TYPE_MATCH), Confidence::ReceiverType);
        assert!(Confidence::SameClassOrSuper < Confidence::ReceiverType);
        assert!(Confidence::ReceiverType < Confidence::QualifiedName);
    }

    /// AC3 (Story #1806, S2b): `SAME_CLASS_OR_SUPER` evidence alone
    /// derives `SameClassOrSuper`, ranked strictly between `High` and
    /// `ReceiverType`.
    #[test]
    fn same_class_or_super_alone_derives_same_class_or_super() {
        assert_eq!(Confidence::derive(reasons::SAME_CLASS_OR_SUPER), Confidence::SameClassOrSuper);
        assert!(Confidence::High < Confidence::SameClassOrSuper);
        assert!(Confidence::SameClassOrSuper < Confidence::ReceiverType);
    }

    #[test]
    fn unique_name_in_repo_alone_derives_exact() {
        assert_eq!(Confidence::derive(reasons::UNIQUE_NAME_IN_REPO), Confidence::Exact);
    }

    /// The discriminating case: a candidate can accumulate MULTIPLE reasons
    /// at once (e.g. a call resolved via same-file evidence AND an import
    /// AND arity match). A wrong implementation that derived confidence from
    /// e.g. "the last flag checked" or "the numerically largest bit" rather
    /// than an explicit strength ordering would get this wrong -- the
    /// STRONGEST evidence present must always win, regardless of what weaker
    /// evidence also happens to be set alongside it.
    #[test]
    fn strongest_present_reason_wins_when_multiple_reasons_are_set() {
        let mixed = reasons::SAME_FILE | reasons::IMPORTED | reasons::SAME_PACKAGE;
        assert_eq!(Confidence::derive(mixed), Confidence::SamePackage);

        let mixed_with_exact = reasons::SAME_FILE
            | reasons::QUALIFIED_NAME
            | reasons::ARITY_MATCH
            | reasons::UNIQUE_NAME_IN_REPO;
        assert_eq!(Confidence::derive(mixed_with_exact), Confidence::Exact);
    }

    /// `ARITY_MATCH` and `STRING_HEURISTIC` carry no strength of their own
    /// in the ordering (AC4 only names six Confidence levels for nine
    /// reasons flags) -- alone, they must not be mistaken for any of the
    /// six named levels above `NameOnly`.
    #[test]
    fn reasons_with_no_dedicated_confidence_level_fall_back_to_name_only() {
        assert_eq!(Confidence::derive(reasons::ARITY_MATCH), Confidence::NameOnly);
        assert_eq!(Confidence::derive(reasons::STRING_HEURISTIC), Confidence::NameOnly);
    }

    /// Ordering must reflect the full strength order, strongest first:
    /// Exact, QualifiedName, ReceiverType, SameClassOrSuper, High,
    /// SamePackage, Imported, SameFile, NameOnly.
    #[test]
    fn variants_are_ordered_from_weakest_to_strongest() {
        assert!(Confidence::NameOnly < Confidence::SameFile);
        assert!(Confidence::SameFile < Confidence::Imported);
        assert!(Confidence::Imported < Confidence::SamePackage);
        assert!(Confidence::SamePackage < Confidence::High);
        assert!(Confidence::High < Confidence::SameClassOrSuper);
        assert!(Confidence::SameClassOrSuper < Confidence::ReceiverType);
        assert!(Confidence::ReceiverType < Confidence::QualifiedName);
        assert!(Confidence::QualifiedName < Confidence::Exact);
    }

    /// `from_u8` must exactly invert `derive`'s output byte for every
    /// producible value -- this is the round trip the CSR arena depends on
    /// when reading a stored `Candidate.confidence` back out.
    #[test]
    fn from_u8_round_trips_every_value_derive_can_produce() {
        for &bits in reasons::ALL_FLAGS {
            let confidence = Confidence::derive(bits);
            assert_eq!(Confidence::from_u8(confidence as u8), confidence);
        }
    }

    #[test]
    #[should_panic(expected = "corrupt Confidence byte")]
    fn from_u8_panics_on_a_value_derive_never_produces() {
        Confidence::from_u8(9);
    }
}
