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
    QualifiedName = 4,
    Exact = 5,
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
            4 => Confidence::QualifiedName,
            5 => Confidence::Exact,
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

    #[test]
    fn qualified_name_alone_derives_qualified_name() {
        assert_eq!(Confidence::derive(reasons::QUALIFIED_NAME), Confidence::QualifiedName);
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

    /// Ordering must reflect AC4's stated strength order:
    /// Exact > QualifiedName > SamePackage > Imported > SameFile > NameOnly.
    #[test]
    fn variants_are_ordered_from_weakest_to_strongest() {
        assert!(Confidence::NameOnly < Confidence::SameFile);
        assert!(Confidence::SameFile < Confidence::Imported);
        assert!(Confidence::Imported < Confidence::SamePackage);
        assert!(Confidence::SamePackage < Confidence::QualifiedName);
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
        Confidence::from_u8(6);
    }
}
