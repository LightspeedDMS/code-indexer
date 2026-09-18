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

/// AC1 (Story #1806, S2b -- FINDING 3's missing narrowing): a
/// qualified call's receiver was resolved (by the caller, via
/// `super::receiver::resolve_receiver_type`) to declared type `"Foo"`,
/// which `extends Base` -- narrows to `Base.doSomething` (declared on
/// a SUPERTYPE of the receiver's declared type, not just an
/// exact-type match), excluding an unrelated `Other.doSomething` that
/// shares only the name.
#[test]
fn receiver_type_match_narrows_a_qualified_call_to_the_receivers_declared_type() {
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
        None,
        None,
        None,
        true,
    );
    assert_eq!(
        candidates.len(),
        1,
        "Other.doSomething must be excluded -- it has no relation to Foo's hierarchy"
    );
    assert_eq!(
        candidates[0].0.file_id, 30,
        "Base.doSomething must be reachable via Foo's supertype chain"
    );
    assert_ne!(candidates[0].1 & reasons::RECEIVER_TYPE_MATCH, 0);
    assert_eq!(
        Confidence::derive(candidates[0].1),
        Confidence::ReceiverType
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
