//! F5 (#1873/#1875 rework): second half of `resolve.rs`'s relocated unit
//! tests -- see `resolve_tests.rs` for the first half and the split
//! rationale. This half covers same-class/receiver-type narrowing,
//! inheritance-family expansion, the unique-name shortcut, and
//! `enclosing_symbol`.

use super::*;
use crate::graph::bind::FileForBind;

// N6 (#1873/#1875 second-review rework): `file`/`method_decl`/`package_decl`
// used to be a verbatim duplicate of `resolve_tests.rs`'s own copies; both
// are sibling `#[cfg(test)]` modules declared directly under `resolve.rs`
// (see its `mod tests;`/`mod tests_family;`), so `super::tests::*` reaches
// the canonical, now-`pub(super)` originals instead.
use super::tests::{file, method_decl, package_decl};

/// Shared fixture: `subtype_name` (file `sub_id`, referenced only by name
/// via `same_class_context`/`receiver_type` in the caller, never itself
/// declared) inherits (`kind`) from `supertype_name` (file `super_id`),
/// which declares `method_name`; an unrelated `other_type` (file
/// `other_id`) declares the SAME method name with zero relation to the
/// hierarchy. Used by both the same-class-or-super and receiver-type
/// narrowing tests below, whose only real difference is which
/// `resolve_reference` parameter carries the hierarchy link.
fn hierarchy_and_unrelated_sibling(
    method_name: &str,
    super_id: u32,
    supertype_name: &str,
    subtype_name: &str,
    other_id: u32,
    other_type: &str,
    kind: crate::graph::extract::local_index::InheritanceKind,
) -> Vec<FileForBind> {
    use crate::graph::extract::local_index::{InheritanceRecord, MethodOwnerRecord};

    let mut base_file = LocalIndex::new();
    base_file
        .declarations
        .push(method_decl(method_name, super_id, 0, Some(0)));
    base_file.method_owners.push(MethodOwnerRecord {
        method_symbol: make_symbol_id(super_id, 0),
        enclosing_type: supertype_name.to_string(),
    });
    base_file.inheritance.push(InheritanceRecord {
        kind,
        subtype_name: subtype_name.to_string(),
        supertype_name: supertype_name.to_string(),
        line: 1,
    });

    let mut other_file = LocalIndex::new();
    other_file
        .declarations
        .push(method_decl(method_name, other_id, 0, Some(0)));
    other_file.method_owners.push(MethodOwnerRecord {
        method_symbol: make_symbol_id(other_id, 0),
        enclosing_type: other_type.to_string(),
    });

    vec![
        file(super_id, "java", base_file),
        file(other_id, "java", other_file),
    ]
}

/// AC3 (Story #1806, S2b): a bare call from `Sub` (which `extends
/// Base`) to `helper()` must narrow to `Base.helper` -- reachable via
/// the caller's own supertype chain -- excluding an unrelated
/// `Other.helper` that shares only the name, with zero relation to
/// `Sub`'s type hierarchy. Neither candidate carries any import/
/// package/arity evidence, so `SAME_CLASS_OR_SUPER` must be the ONLY
/// thing doing the narrowing here.
#[test]
fn same_class_or_super_narrows_an_unqualified_call_to_the_callers_own_type_hierarchy() {
    use crate::graph::confidence::Confidence;
    use crate::graph::extract::local_index::InheritanceKind;

    let files = hierarchy_and_unrelated_sibling(
        "helper",
        20,
        "Base",
        "Sub",
        21,
        "Other",
        InheritanceKind::Extends,
    );
    let name_index = RepoNameIndex::build(&files);
    let type_index = super::super::families::TypeIndex::build(&files);
    let scope = FileScope {
        package: None,
        imports: Vec::new(),
    };

    let candidates = resolve_reference(
        "helper",
        REF_KIND_INVOCATION,
        1,
        &scope,
        Some(0),
        &[],
        &name_index,
        &type_index,
        None,
        false,
        Some("Sub"),
        None,
        None,
        true,
    );
    assert_eq!(
        candidates.len(),
        1,
        "Other.helper must be excluded -- it has no relation to Sub's hierarchy"
    );
    assert_eq!(candidates[0].0.file_id, 20);
    assert_ne!(candidates[0].1 & reasons::SAME_CLASS_OR_SUPER, 0);
    assert_eq!(
        Confidence::derive(candidates[0].1),
        Confidence::SameClassOrSuper
    );
}

/// AC1 (Story #1806, S2b -- FINDING 3's missing narrowing), RE-SCOPED by
/// the #1898 scope split (epic #1906, round-4 review): a qualified call's
/// receiver was resolved (by the caller, via `super::receiver::
/// resolve_receiver_type`) to declared type `"Foo"`, which `extends
/// Base` -- `apply_receiver_type_narrowing` TAGS `Base.doSomething`
/// (declared on a SUPERTYPE of the receiver's declared type, not just an
/// exact-type match) with `RECEIVER_TYPE_MATCH`, but no longer EXCLUDES
/// the unrelated `Other.doSomething` that shares only the name -- that
/// exclusion is exactly the "hard-narrow to a non-empty subset" path
/// round4-findings.md found unsound (Positive evidence is not
/// closed-world), so it moved whole-cloth to the receiver-type
/// hard-narrowing follow-up issue named in `docs/xray-architecture.md`'s
/// candidate-admission section. This test now asserts what the code
/// actually guarantees: `Base.doSomething` is present and tagged, and
/// `Other.doSomething` is accepted noise, not silently excluded from the
/// suite's coverage.
#[test]
fn receiver_type_match_tags_the_receivers_declared_type_but_no_longer_excludes_the_unrelated_sibling(
) {
    use crate::graph::confidence::Confidence;
    use crate::graph::extract::local_index::InheritanceKind;

    let files = hierarchy_and_unrelated_sibling(
        "doSomething",
        30,
        "Base",
        "Foo",
        31,
        "Other",
        InheritanceKind::Extends,
    );
    let name_index = RepoNameIndex::build(&files);
    let type_index = super::super::families::TypeIndex::build(&files);
    let scope = FileScope {
        package: None,
        imports: Vec::new(),
    };

    let candidates = resolve_reference(
        "doSomething",
        REF_KIND_INVOCATION,
        1,
        &scope,
        Some(0),
        &[],
        &name_index,
        &type_index,
        Some("Foo"),
        true,
        None,
        None,
        None,
        true,
    );
    assert_eq!(
        candidates.len(),
        2,
        "accepted regression (#1898 scope split): receiver-type narrowing is tag-only now, \
         so Other.doSomething is no longer excluded -- both candidates survive"
    );
    let base_candidate = candidates
        .iter()
        .find(|(d, _)| d.file_id == 30)
        .expect("Base.doSomething must still be present, reachable via Foo's supertype chain");
    assert_ne!(base_candidate.1 & reasons::RECEIVER_TYPE_MATCH, 0);
    assert_eq!(
        Confidence::derive(base_candidate.1),
        Confidence::ReceiverType
    );
    assert!(
        candidates.iter().any(|(d, _)| d.file_id == 31),
        "Other.doSomething must still be present (accepted regression, not silently dropped \
         from the suite's coverage) pending the receiver-type hard-narrowing follow-up"
    );
}

/// Shared fixture helper for the never-shrinks invariant tests below: two
/// files, each declaring one 0-arg `"helper"` method owned by its own
/// `enclosing_type` -- the minimal pool needed to observe whether
/// `apply_receiver_type_narrowing` shrinks or tags a candidate set.
fn two_helper_candidates(
    file_a_id: u32,
    enclosing_type_a: &str,
    file_b_id: u32,
    enclosing_type_b: &str,
) -> Vec<FileForBind> {
    use crate::graph::extract::local_index::MethodOwnerRecord;

    let mut file_a = LocalIndex::new();
    file_a
        .declarations
        .push(method_decl("helper", file_a_id, 0, Some(0)));
    file_a.method_owners.push(MethodOwnerRecord {
        method_symbol: make_symbol_id(file_a_id, 0),
        enclosing_type: enclosing_type_a.to_string(),
    });
    let mut file_b = LocalIndex::new();
    file_b
        .declarations
        .push(method_decl("helper", file_b_id, 0, Some(0)));
    file_b.method_owners.push(MethodOwnerRecord {
        method_symbol: make_symbol_id(file_b_id, 0),
        enclosing_type: enclosing_type_b.to_string(),
    });
    vec![
        file(file_a_id, "java", file_a),
        file(file_b_id, "java", file_b),
    ]
}

/// Resolves `"helper"` (0-arg invocation) against `files`, with
/// `receiver_type` as POSITIVE evidence -- shared by both never-shrinks
/// invariant tests below.
fn resolve_helper_against(files: &[FileForBind], receiver_type: &str) -> Vec<(DeclInfo, u16)> {
    let name_index = RepoNameIndex::build(files);
    let type_index = super::super::families::TypeIndex::build(files);
    let scope = FileScope {
        package: None,
        imports: Vec::new(),
    };
    resolve_reference(
        "helper",
        REF_KIND_INVOCATION,
        1,
        &scope,
        Some(0),
        &[],
        &name_index,
        &type_index,
        Some(receiver_type),
        true,
        None,
        None,
        None,
        true,
    )
}

/// #1898 scope split (epic #1906, round-4 review) -- the STRUCTURAL
/// invariant the split rests on, proven directly rather than only via the
/// historical AC1/AC3 fixtures above: a resolved receiver type matching
/// NOTHING in the candidate pool must leave the pool's LENGTH unchanged
/// and must tag NOTHING.
#[test]
fn apply_receiver_type_narrowing_never_shrinks_the_pool_on_a_zero_match() {
    let files = two_helper_candidates(70, "Unrelated1", 71, "Unrelated2");
    let candidates = resolve_helper_against(&files, "TargetType");
    assert_eq!(
        candidates.len(),
        2,
        "a zero match against the resolved receiver type must never shrink the pool"
    );
    assert!(
        candidates
            .iter()
            .all(|(_, bits)| bits & reasons::RECEIVER_TYPE_MATCH == 0),
        "a zero match must tag nothing"
    );
}

/// Sibling of the zero-match invariant test above: a resolved receiver
/// type matching SOME (not all) of the pool must still leave the pool's
/// LENGTH unchanged, with `RECEIVER_TYPE_MATCH` set on EXACTLY the
/// matching subset -- never used to also narrow the set, unlike every
/// pre-#1898-scope-split round of this filter.
#[test]
fn apply_receiver_type_narrowing_never_shrinks_the_pool_and_tags_only_the_matching_subset() {
    let files = two_helper_candidates(80, "MatchType", 81, "Unrelated");
    let candidates = resolve_helper_against(&files, "MatchType");
    assert_eq!(
        candidates.len(),
        2,
        "a partial match against the resolved receiver type must never shrink the pool"
    );
    let match_candidate = candidates
        .iter()
        .find(|(d, _)| d.file_id == 80)
        .expect("MatchType.helper must still be present");
    assert_ne!(
        match_candidate.1 & reasons::RECEIVER_TYPE_MATCH,
        0,
        "the matching candidate must be tagged"
    );
    let unrelated_candidate = candidates
        .iter()
        .find(|(d, _)| d.file_id == 81)
        .expect("Unrelated.helper must still be present");
    assert_eq!(
        unrelated_candidate.1 & reasons::RECEIVER_TYPE_MATCH,
        0,
        "the non-matching candidate must never be tagged"
    );
}

/// Shared fixture helper: a file declaring interface(s) `interface_names`
/// plus one `save` method owned by `enclosing_type`, in `package`. Used
/// by the `MAX_FAMILY_SIZE` cap and cyclic-hierarchy tests below.
fn interface_decl_file(
    file_id: u32,
    package: &str,
    interface_names: &[&str],
    enclosing_type: &str,
) -> LocalIndex {
    use crate::graph::extract::local_index::MethodOwnerRecord;
    use crate::graph::identity::make_symbol_id;
    let mut f = LocalIndex::new();
    f.declarations.push(package_decl(file_id, package));
    f.declarations
        .push(method_decl("save", file_id, 1, Some(0)));
    for name in interface_names {
        f.interface_names.push(name.to_string());
    }
    f.method_owners.push(MethodOwnerRecord {
        method_symbol: make_symbol_id(file_id, 1),
        enclosing_type: enclosing_type.to_string(),
    });
    f
}

/// Shared fixture helper: a file declaring type `type_name` (which
/// `implements supertype`) plus its own `save` override, in `package`.
fn implementor_file(file_id: u32, package: &str, type_name: &str, supertype: &str) -> LocalIndex {
    use crate::graph::extract::local_index::{
        InheritanceKind, InheritanceRecord, MethodOwnerRecord,
    };
    use crate::graph::identity::make_symbol_id;
    let mut f = LocalIndex::new();
    f.declarations.push(package_decl(file_id, package));
    f.declarations
        .push(method_decl("save", file_id, 1, Some(0)));
    f.method_owners.push(MethodOwnerRecord {
        method_symbol: make_symbol_id(file_id, 1),
        enclosing_type: type_name.to_string(),
    });
    f.inheritance.push(InheritanceRecord {
        kind: InheritanceKind::Implements,
        subtype_name: type_name.to_string(),
        supertype_name: supertype.to_string(),
        line: 1,
    });
    f
}

/// Shared fixture helper: resolves `"save"` (`REF_KIND_INVOCATION`,
/// `arg_count = Some(0)`) from a caller scoped to `"pkg.a"` -- the
/// package every interface-owning fixture file above declares itself
/// in, so import-context narrowing alone always collapses to the
/// interface's own candidate, forcing family expansion to do the real
/// work.
fn resolve_save_against(files: &[FileForBind]) -> Vec<(DeclInfo, u16)> {
    let name_index = RepoNameIndex::build(files);
    let type_index = super::super::families::TypeIndex::build(files);
    let scope = FileScope {
        package: Some("pkg.a".to_string()),
        imports: Vec::new(),
    };
    resolve_reference(
        "save",
        REF_KIND_INVOCATION,
        1,
        &scope,
        Some(0),
        &[],
        &name_index,
        &type_index,
        None,
        false,
        None,
        None,
        None,
        true,
    )
}

/// AC1 (Story #1793, S4): THE central discriminating case named in
/// the story -- import-context narrowing alone would collapse a call
/// resolving to an interface method down to just that ONE
/// declaration (the interface's own package matches the caller's;
/// the real implementor lives in an unrelated package with zero
/// import evidence). Family expansion must add the implementor's
/// override BACK, marked `INHERITANCE_FAMILY` and `Confidence::High`
/// -- never leaving the call silently collapsed to the interface
/// declaration alone.
#[test]
fn interface_method_expands_to_its_family_after_narrowing_would_have_collapsed_it_to_one() {
    use crate::graph::confidence::Confidence;

    let files = vec![
        file(
            10,
            "java",
            interface_decl_file(10, "pkg.a", &["Repo"], "Repo"),
        ),
        file(11, "java", implementor_file(11, "pkg.b", "Impl", "Repo")),
    ];
    let candidates = resolve_save_against(&files);
    assert_eq!(
        candidates.len(),
        2,
        "the family (interface + its real implementor) must both be present, never collapsed to one"
    );

    let impl_candidate = candidates
        .iter()
        .find(|(d, _)| d.file_id == 11)
        .expect("Impl.save must be present");
    assert_ne!(impl_candidate.1 & reasons::INHERITANCE_FAMILY, 0);
    assert_eq!(Confidence::derive(impl_candidate.1), Confidence::High);
}

/// Memory-safety amendment (real 21.8GB-RSS incident on Elasticsearch,
/// killed before it exhausted the host): family expansion through the
/// FULL `resolve_reference` pipeline must cap the family at
/// `MAX_FAMILY_SIZE` and mark every surviving family candidate
/// `reasons::FAMILY_TRUNCATED` -- never silently return a partial
/// family indistinguishable from a genuinely small one.
#[test]
fn family_expansion_caps_at_max_family_size_and_marks_family_truncated() {
    use super::super::families::MAX_FAMILY_SIZE;
    const INTERFACE_FILE_ID: u32 = 10;
    const IMPL_FILE_ID_BASE: u32 = 100;
    const IMPLEMENTOR_COUNT: usize = MAX_FAMILY_SIZE + 5;

    let mut files = vec![file(
        INTERFACE_FILE_ID,
        "java",
        interface_decl_file(INTERFACE_FILE_ID, "pkg.a", &["Repo"], "Repo"),
    )];
    for i in 0..IMPLEMENTOR_COUNT {
        let file_id = IMPL_FILE_ID_BASE + i as u32;
        let impl_type_name = format!("Impl{i}");
        let impl_file = implementor_file(file_id, &format!("pkg.impl{i}"), &impl_type_name, "Repo");
        files.push(file(file_id, "java", impl_file));
    }

    let candidates = resolve_save_against(&files);

    assert_eq!(
        candidates.len(),
        MAX_FAMILY_SIZE + 1,
        "the interface's own candidate plus a family capped at MAX_FAMILY_SIZE"
    );
    let family_candidates: Vec<_> = candidates
        .iter()
        .filter(|(_, bits)| bits & reasons::INHERITANCE_FAMILY != 0)
        .collect();
    assert_eq!(family_candidates.len(), MAX_FAMILY_SIZE);
    assert!(
        family_candidates
            .iter()
            .all(|(_, bits)| bits & reasons::FAMILY_TRUNCATED != 0),
        "every surviving family candidate must be marked FAMILY_TRUNCATED once the cap is hit"
    );
}

/// A class hierarchy can contain cycles through interfaces (malformed
/// or adversarial extraction, never valid real Java) -- family
/// expansion through the FULL `resolve_reference` pipeline must still
/// terminate, not merely the lower-level `TypeIndex::implementors_of`
/// BFS in isolation. `I` and `J` extend each other (a 2-cycle); `Impl`
/// genuinely implements `I`. This test itself fails to return (times
/// out the test run) rather than failing an assertion if
/// `apply_inheritance_family_expansion` loops forever on the cycle.
#[test]
fn family_expansion_terminates_on_a_cyclic_interface_hierarchy_through_resolve_reference() {
    use crate::graph::extract::local_index::{InheritanceKind, InheritanceRecord};
    const INTERFACE_FILE_ID: u32 = 10;
    const IMPL_FILE_ID: u32 = 11;

    let mut interface_file = interface_decl_file(INTERFACE_FILE_ID, "pkg.a", &["I", "J"], "I");
    // The adversarial 2-cycle: I extends J, J extends I.
    interface_file.inheritance.push(InheritanceRecord {
        kind: InheritanceKind::Extends,
        subtype_name: "I".to_string(),
        supertype_name: "J".to_string(),
        line: 1,
    });
    interface_file.inheritance.push(InheritanceRecord {
        kind: InheritanceKind::Extends,
        subtype_name: "J".to_string(),
        supertype_name: "I".to_string(),
        line: 1,
    });

    let files = vec![
        file(INTERFACE_FILE_ID, "java", interface_file),
        file(
            IMPL_FILE_ID,
            "java",
            implementor_file(IMPL_FILE_ID, "pkg.b", "Impl", "I"),
        ),
    ];

    let candidates = resolve_save_against(&files);

    assert_eq!(
        candidates.len(),
        2,
        "must terminate and return exactly the interface's own candidate plus Impl.save -- \
         a cyclic hierarchy must never hang or fabricate extra candidates"
    );
    assert!(candidates.iter().any(|(d, _)| d.file_id == IMPL_FILE_ID));
}

/// AC4 Level 5: a name unique across the whole repo reaches
/// `Confidence::Exact` via `UNIQUE_NAME_IN_REPO` -- but ONLY when the
/// caller confirms the index is complete.
#[test]
fn unique_name_in_repo_resolves_to_a_single_exact_confidence_candidate() {
    use crate::graph::confidence::Confidence;

    let mut index = LocalIndex::new();
    index
        .declarations
        .push(method_decl("uniqueMethod", 1, 0, None));
    let name_index = RepoNameIndex::build(&[file(1, "java", index)]);
    let scope = FileScope {
        package: None,
        imports: Vec::new(),
    };

    let candidates = resolve_reference(
        "uniqueMethod",
        REF_KIND_INVOCATION,
        1,
        &scope,
        None,
        &[],
        &name_index,
        &super::super::families::TypeIndex::build(&[]),
        None,
        false,
        None,
        None,
        None,
        true,
    );
    assert_eq!(candidates.len(), 1);
    let reasons_bits = candidates[0].1;
    assert_ne!(reasons_bits & reasons::UNIQUE_NAME_IN_REPO, 0);
    assert_eq!(Confidence::derive(reasons_bits), Confidence::Exact);
}

/// Dual-review defect D3 (Critical): `UNIQUE_NAME_IN_REPO` must NEVER
/// be claimed when the caller reports the index is PARTIAL (e.g. this
/// exact same fixture, but a sibling file elsewhere in the real repo
/// was dropped by `max_files` truncation and never made it into
/// `RepoNameIndex`). A wrong implementation that ignored
/// `index_is_complete` would pass the test right above this one and
/// still fail here -- the discriminating input is the SAME single
/// declaration, only the completeness flag differs.
#[test]
fn a_name_unique_only_in_a_partial_index_does_not_get_exact_confidence() {
    use crate::graph::confidence::Confidence;

    let mut index = LocalIndex::new();
    index
        .declarations
        .push(method_decl("uniqueMethod", 1, 0, None));
    let name_index = RepoNameIndex::build(&[file(1, "java", index)]);
    let scope = FileScope {
        package: None,
        imports: Vec::new(),
    };

    let candidates = resolve_reference(
        "uniqueMethod",
        REF_KIND_INVOCATION,
        1,
        &scope,
        None,
        &[],
        &name_index,
        &super::super::families::TypeIndex::build(&[]),
        None,
        false,
        None,
        None,
        None,
        false,
    );
    assert_eq!(
        candidates.len(),
        1,
        "the sole indexed declaration is still a candidate -- never dropped"
    );
    let reasons_bits = candidates[0].1;
    assert_eq!(
        reasons_bits & reasons::UNIQUE_NAME_IN_REPO,
        0,
        "UNIQUE_NAME_IN_REPO must not be claimed from a partial index"
    );
    assert_ne!(
        Confidence::derive(reasons_bits),
        Confidence::Exact,
        "a partial-index match must never reach Exact confidence"
    );
}

/// `enclosing_symbol` only ever receives ONE file's `LocalIndex` (each
/// file has its own, in `bind()`'s real pipeline) -- there is no
/// cross-file data for it to confuse, so the real discriminating axis
/// is LINE proximity, not file identity. A wrong implementation that
/// picked the LAST declaration regardless of line (rather than the
/// nearest one AT OR BEFORE the query line) would pass a naive test
/// but fail this one: querying line 10 (between the two declarations)
/// must still return the EARLIER one, not the later one at line 20.
#[test]
fn enclosing_symbol_picks_the_nearest_preceding_declaration_by_line() {
    let mut index = LocalIndex::new();
    let mut first = method_decl("first", 1, 0, None);
    first.line = 5;
    let mut second = method_decl("second", 1, 1, None);
    second.line = 20;
    index.declarations.push(first);
    index.declarations.push(second);

    assert_eq!(enclosing_symbol(&index, 1, 25), make_symbol_id(1, 1));
    assert_eq!(enclosing_symbol(&index, 1, 10), make_symbol_id(1, 0));
}

/// A declaration existing in the index is NOT enough to avoid the
/// sentinel fallback -- it must actually PRECEDE the query line. A
/// wrong implementation that fell back to the sentinel only on a
/// truly empty index (never checking `d.line <= line`) would wrongly
/// pick this later-only declaration instead of falling back.
#[test]
fn enclosing_symbol_falls_back_to_a_sentinel_when_nothing_precedes_the_line() {
    let mut index = LocalIndex::new();
    let mut later = method_decl("later", 3, 0, None);
    later.line = 50;
    index.declarations.push(later);

    assert_eq!(enclosing_symbol(&index, 3, 10), make_symbol_id(3, u32::MAX));
}

/// Bug #1898 (P1 of epic #1906), RE-SCOPED by the #1898 scope split
/// (epic #1906, round-4 review): a statically qualified call
/// (`TimeUtil.parse(x)`) whose receiver resolves to a KNOWN in-repo type
/// that declares (and inherits) no method by this name USED to resolve to
/// ZERO candidates under the old hard-empty contract. That contract is
/// exactly the "Positive/Advisory evidence hard-narrows an empty match"
/// path the round-4 review found unsound (findings 1-2: `Positive` is not
/// closed-world either) -- `apply_receiver_type_narrowing` is now
/// TAG-ONLY, so a zero match tags nothing and removes nothing; the pool
/// (every unrelated same-named method elsewhere in the repo) survives
/// untouched. Exclusive binding to the qualified type moves to the
/// receiver-type hard-narrowing follow-up issue named in `docs/
/// xray-architecture.md`'s candidate-admission section.
#[test]
fn receiver_type_with_no_matching_member_keeps_the_full_pool_pending_the_hard_narrowing_followup() {

    use crate::graph::extract::local_index::MethodOwnerRecord;

    let mut file_a = LocalIndex::new();
    file_a.declarations.push(method_decl("parse", 40, 0, Some(1)));
    file_a.method_owners.push(MethodOwnerRecord {
        method_symbol: make_symbol_id(40, 0),
        enclosing_type: "UnrelatedA".to_string(),
    });
    let mut file_b = LocalIndex::new();
    file_b.declarations.push(method_decl("parse", 41, 0, Some(1)));
    file_b.method_owners.push(MethodOwnerRecord {
        method_symbol: make_symbol_id(41, 0),
        enclosing_type: "UnrelatedB".to_string(),
    });
    let files = vec![file(40, "java", file_a), file(41, "java", file_b)];
    let name_index = RepoNameIndex::build(&files);
    let type_index = super::super::families::TypeIndex::build(&files);
    let scope = FileScope {
        package: None,
        imports: Vec::new(),
    };

    let candidates = resolve_reference(
        "parse",
        REF_KIND_INVOCATION,
        1,
        &scope,
        Some(1),
        &[],
        &name_index,
        &type_index,
        Some("TimeUtil"),
        true,
        None,
        None,
        None,
        true,
    );
    assert_eq!(
        candidates.len(),
        2,
        "accepted regression (#1898 scope split): receiver-type narrowing is tag-only now, \
         so TimeUtil.parse(x) keeps binding to UnrelatedA.parse/UnrelatedB.parse until the \
         hard-narrowing follow-up lands -- got {} candidate(s)",
        candidates.len()
    );
    assert!(
        candidates
            .iter()
            .all(|(_, bits)| bits & reasons::RECEIVER_TYPE_MATCH == 0),
        "neither UnrelatedA.parse nor UnrelatedB.parse matches TimeUtil (or its supertypes), \
         so neither may be tagged RECEIVER_TYPE_MATCH even though tag-only narrowing keeps \
         them both in the set"
    );
}

/// PRESERVE (#1882/#1883): when the receiver's OWN supertype evidence is
/// recorded INCOMPLETE, `apply_receiver_type_narrowing` must NOT trust
/// `supertypes_of` enough to narrow to empty -- an empty `matching` here
/// could just as easily mean "the real supertype exists but this binder
/// failed to record the edge" as "the target is genuinely external".
/// Narrowing must be skipped (full pool retained) exactly like
/// `apply_super_class_narrowing` already does for this same evidence gap
/// -- this guards against the #1898 fix introducing a NEW #1882/#1883-
/// shaped regression in a filter #1882/#1883 never originally audited.
#[test]
fn receiver_type_narrowing_skips_when_receivers_own_supertype_evidence_is_incomplete() {
    use crate::graph::extract::local_index::MethodOwnerRecord;

    let mut file_a = LocalIndex::new();
    file_a
        .declarations
        .push(method_decl("helper", 50, 0, Some(0)));
    file_a.method_owners.push(MethodOwnerRecord {
        method_symbol: make_symbol_id(50, 0),
        enclosing_type: "Other".to_string(),
    });
    file_a.incomplete_supertypes.push("Sub".to_string());
    let mut file_b = LocalIndex::new();
    file_b
        .declarations
        .push(method_decl("helper", 51, 0, Some(0)));
    file_b.method_owners.push(MethodOwnerRecord {
        method_symbol: make_symbol_id(51, 0),
        enclosing_type: "AlsoUnrelated".to_string(),
    });
    let files = vec![file(50, "java", file_a), file(51, "java", file_b)];
    let name_index = RepoNameIndex::build(&files);
    let type_index = super::super::families::TypeIndex::build(&files);
    let scope = FileScope {
        package: None,
        imports: Vec::new(),
    };

    let candidates = resolve_reference(
        "helper",
        REF_KIND_INVOCATION,
        1,
        &scope,
        Some(0),
        &[],
        &name_index,
        &type_index,
        Some("Sub"),
        true,
        None,
        None,
        None,
        true,
    );
    assert_eq!(
        candidates.len(),
        2,
        "incomplete supertype evidence must fall back to keeping the pool, \
         never narrow to empty on unproven absence"
    );
}

/// PRESERVE (#1882/#1883): the same guard for
/// `apply_same_class_or_super_narrowing` (AC3, unqualified/`this` calls).
///
/// P3-a (#1898 code review round 2, epic #1906): the ORIGINAL version of
/// this test gave `Sub` NO real inheritance edges at all -- `allowed` was
/// therefore EMPTY regardless of the incomplete-evidence guard, so
/// `apply_same_class_or_super_narrowing`'s OWN separate "`matching.is_
/// empty() -> keep the pool`" soft-skip (unrelated to the guard this test
/// names) already produced `candidates.len() == 2` on its own -- the test
/// passed identically with the guard deleted, i.e. it was VACUOUS.
/// Discriminating now: `Sub` gets a REAL recorded edge (`Sub implements
/// Marker`) alongside its incomplete marker (representing e.g. an
/// unresolvable `extends` alongside a resolved `implements`), and one
/// candidate is owned by `Marker` -- so `supertypes_of("Sub")` is
/// NON-EMPTY and produces a PARTIAL match (1 of 2 candidates). Without
/// the guard, that partial match narrows to 1; WITH it (the guard this
/// test exists to prove), narrowing is skipped entirely and both
/// candidates survive -- the only case the guard can actually change,
/// per the review's own diagnosis.
#[test]
fn same_class_or_super_narrowing_skips_when_callers_own_supertype_evidence_is_incomplete() {
    use crate::graph::extract::local_index::{InheritanceKind, InheritanceRecord, MethodOwnerRecord};

    let mut file_a = LocalIndex::new();
    file_a
        .declarations
        .push(method_decl("helper", 52, 0, Some(0)));
    file_a.method_owners.push(MethodOwnerRecord {
        method_symbol: make_symbol_id(52, 0),
        enclosing_type: "Marker".to_string(),
    });
    file_a.inheritance.push(InheritanceRecord {
        kind: InheritanceKind::Implements,
        subtype_name: "Sub".to_string(),
        supertype_name: "Marker".to_string(),
        line: 1,
    });
    file_a.incomplete_supertypes.push("Sub".to_string());
    let mut file_b = LocalIndex::new();
    file_b
        .declarations
        .push(method_decl("helper", 53, 0, Some(0)));
    file_b.method_owners.push(MethodOwnerRecord {
        method_symbol: make_symbol_id(53, 0),
        enclosing_type: "AlsoUnrelated".to_string(),
    });
    let files = vec![file(52, "java", file_a), file(53, "java", file_b)];
    let name_index = RepoNameIndex::build(&files);
    let type_index = super::super::families::TypeIndex::build(&files);
    // Sanity check on the fixture itself: without the guard, `allowed`
    // would be non-empty and produce a partial (not empty, not full)
    // match -- otherwise this test is exactly as vacuous as before.
    assert!(
        !type_index.supertypes_of("Sub").is_empty(),
        "fixture bug: Sub must have a real recorded supertype for this test to discriminate"
    );
    let scope = FileScope {
        package: None,
        imports: Vec::new(),
    };

    let candidates = resolve_reference(
        "helper",
        REF_KIND_INVOCATION,
        1,
        &scope,
        Some(0),
        &[],
        &name_index,
        &type_index,
        None,
        false,
        Some("Sub"),
        None,
        None,
        true,
    );
    assert_eq!(
        candidates.len(),
        2,
        "incomplete supertype evidence must fall back to keeping the pool, \
         never narrow to the partial match Sub's OWN (incomplete) supertypes_of would produce"
    );
}

/// Regression guard (#1882/#1883): `apply_super_class_narrowing`'s
/// existing "incomplete evidence -> keep the pool" contract (already
/// shipped by the #1873/#1875 rework, untouched by #1898) must survive
/// the #1898 fix unchanged -- over-referencing on `super(...)` remains
/// the safe, documented fallback (see #1882's own "Expected" section: the
/// call resolves under the bare name rather than emitting nothing).
/// `super_class_context.is_some()` deliberately bypasses the unique-name
/// shortcut (D3), so this exercises the real narrowing pipeline even with
/// a single-candidate pool.
#[test]
fn super_class_narrowing_still_keeps_the_pool_on_incomplete_evidence_after_the_1898_fix() {
    use crate::graph::extract::local_index::MethodOwnerRecord;

    let mut file_a = LocalIndex::new();
    file_a.declarations.push(method_decl("marker", 60, 0, Some(0)));
    file_a.method_owners.push(MethodOwnerRecord {
        method_symbol: make_symbol_id(60, 0),
        enclosing_type: "Outer.Nested".to_string(),
    });
    file_a.incomplete_supertypes.push("Holder.Nested".to_string());
    let files = vec![file(60, "java", file_a)];
    let name_index = RepoNameIndex::build(&files);
    let type_index = super::super::families::TypeIndex::build(&files);
    let scope = FileScope {
        package: None,
        imports: Vec::new(),
    };

    let candidates = resolve_reference(
        "marker",
        REF_KIND_INVOCATION,
        1,
        &scope,
        Some(0),
        &[],
        &name_index,
        &type_index,
        None,
        false,
        None,
        Some("Holder.Nested"),
        None,
        true,
    );
    assert_eq!(
        candidates.len(),
        1,
        "super(...) with incomplete supertype evidence must still resolve under the bare \
         name, never emit zero references (#1882's documented safe-over-reference contract)"
    );
}
