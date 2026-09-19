//! `TypeIndex` -- repo-wide inheritance-family substrate (Story #1793, S4,
//! AC1). Built once per `bind()` call (mirrors `RepoNameIndex::build`,
//! `super::name_index`) from every file's already-extracted
//! `LocalIndex.inheritance`/`interface_names` records -- no AST involved,
//! same as every other bind-time index in this crate.
//!
//! This module owns ONLY the pure inheritance graph (`Extends`/
//! `Implements` edges) and the interface-name set. It deliberately does
//! NOT re-derive a second (type, method-name) -> declarations index: the
//! binder already has that via `RepoNameIndex::lookup(name, Method)` --
//! every candidate there already carries `DeclInfo::enclosing_type` (see
//! `super::name_index`). `overrides_of` (added in a follow-up edit)
//! filters that EXISTING pool rather than rebuilding an equivalent join a
//! second time (Rule 4, anti-duplication).

use super::FileForBind;
use std::collections::{HashMap, HashSet};

/// Repo-wide inheritance-family index. Never mutated after `build`
/// returns.
pub(crate) struct TypeIndex {
    /// supertype name -> its DIRECT subtypes (via `Extends` or
    /// `Implements` edges recorded anywhere in the repo).
    direct_children: HashMap<String, Vec<String>>,
    /// AC1/AC3 (Story #1806, S2b): subtype name -> its DIRECT supertypes
    /// (the exact same edges as `direct_children`, indexed in the OPPOSITE
    /// direction) -- the substrate `supertypes_of` walks.
    direct_parents: HashMap<String, Vec<String>>,
    /// Bare names of every type declared as an INTERFACE anywhere in the
    /// repo.
    interface_names: HashSet<String>,
    /// Bare type name -> one unambiguous top-level private-access domain.
    /// Omitted when duplicate bare names disagree, so callers retain edges.
    top_levels: HashMap<String, String>,
    /// N1 (#1873/#1875 second-review rework): bare names of every type
    /// whose recorded superclass/`implements` evidence is known to be
    /// INCOMPLETE (aggregated from every file's own
    /// `LocalIndex::incomplete_supertypes`). `apply_super_class_narrowing`
    /// (narrowing.rs) consults this BEFORE trusting `supertypes_of` -- an
    /// incomplete set may be missing the real supertype entirely, so
    /// narrowing on it must be skipped, not just when the set is empty.
    incomplete_supertype_names: HashSet<String>,
    /// P1-4 (#1898 code review, AC3): bare names of EVERY type declared
    /// anywhere in the repo (every `type_nesting` record's `type_name`,
    /// collected independently of the `top_levels` ambiguity bookkeeping
    /// below -- an ambiguous-top-level type is still a KNOWN type name).
    /// The sole consumer is `receiver::resolve_receiver_type`'s static-
    /// type fallback: an `Identifier` receiver that is not a local
    /// variable/field/parameter but IS a known in-repo type name resolves
    /// to that type itself (e.g. `TimeUtil.parse(x)`'s receiver
    /// `"TimeUtil"`).
    known_type_names: HashSet<String>,
    /// P1-B (#1898 code review round 2, epic #1906): bare names of EVERY
    /// field declared anywhere in the repo -- aggregated from every
    /// file's OWN `typed_names` records whose `scope` is `NameScope::
    /// Field`, repo-wide (unlike `receiver::FileTypedNames`, which is
    /// deliberately per-file). Sole consumer:
    /// `receiver::resolve_receiver_type`'s static-type-name fallback must
    /// NOT fire for an identifier that is ALSO a known field name
    /// anywhere in the repo -- `FileTypedNames::lookup` only ever sees a
    /// field declared on the EXACT `enclosing_type` passed to it (never
    /// an outer lexically-enclosing type, never a superclass in another
    /// file), so a lookup MISS for a genuine field access (an inner class
    /// reading its outer class's field; an inherited field from a
    /// superclass in a different file) is a real evidence GAP, not proof
    /// the identifier denotes a type. Without this set, such a miss could
    /// misresolve the field access as a receiver TYPE whenever an
    /// unrelated type in the repo happens to share the field's bare name.
    known_field_names: HashSet<String>,
    /// P1-B (#1898 code review round 2, epic #1906): bare field name ->
    /// its UNANIMOUS declared type across every Field-scope `typed_names`
    /// record sharing that name, REPO-WIDE (a field name declared on one
    /// type in file A and another type in file B both count -- this is
    /// intentionally cross-file, unlike `receiver::FileTypedNames`).
    /// `None` when two or more such records disagree on the declared
    /// type (an unrelated field elsewhere in the repo happens to share
    /// the bare name but not the type) -- ambiguous, never guessed, same
    /// "disagree -> None" doctrine `receiver::return_type_of_method_on_
    /// type` already uses for overloaded return types. Sole consumer:
    /// `receiver::resolve_receiver_type`'s field-access fallback, which
    /// this makes an EXACT resolution (not merely "block the wrong
    /// guess") for the two P1-B forms where the field's real type is
    /// genuinely recorded somewhere in the repo but not visible to the
    /// per-file lookup: an inner class reading its outer class's field,
    /// and a field inherited from a superclass declared in a different
    /// file.
    field_types: HashMap<String, Option<String>>,
    /// P1-A (#1898 code review round 2, epic #1906): bare names of EVERY
    /// generic type parameter declared anywhere in the repo -- aggregated
    /// from every file's own `LocalIndex::type_parameter_names`. Sole
    /// consumer: `receiver::resolve_receiver_type` must reject a
    /// declared-type STRING (from a local/field/parameter's own typed-name
    /// evidence) that names a type parameter (`T` in `<T extends Svc> void
    /// run(T t)`) rather than a real class/interface -- a receiver typed
    /// `T` is not a concrete declaration this binder can narrow against.
    /// Post-#1898-scope-split, `apply_receiver_type_narrowing` is
    /// TAG-ONLY and can never fabricate an empty candidate set regardless;
    /// this check still matters for `Confidence`/reason-bit accuracy
    /// (`T` must never spuriously earn `RECEIVER_TYPE_MATCH`) and for the
    /// AC4 Level 5 unique-name shortcut's admission gate, which still
    /// consults Positive receiver-type evidence.
    type_parameter_names: HashSet<String>,
}

impl TypeIndex {
    /// Bounded loop: iterates once per `(file, inheritance edge)` pair --
    /// finite, fixed by `files`' own already-extracted record counts
    /// (Rule 14).
    pub(crate) fn build(files: &[FileForBind]) -> Self {
        let mut direct_children: HashMap<String, Vec<String>> = HashMap::new();
        let mut direct_parents: HashMap<String, Vec<String>> = HashMap::new();
        let mut interface_names: HashSet<String> = HashSet::new();
        let mut incomplete_supertype_names: HashSet<String> = HashSet::new();
        for file in files {
            for name in &file.index.interface_names {
                interface_names.insert(name.clone());
            }
            for edge in &file.index.inheritance {
                direct_children
                    .entry(edge.supertype_name.clone())
                    .or_default()
                    .push(edge.subtype_name.clone());
                direct_parents
                    .entry(edge.subtype_name.clone())
                    .or_default()
                    .push(edge.supertype_name.clone());
            }
            for name in &file.index.incomplete_supertypes {
                incomplete_supertype_names.insert(name.clone());
            }
        }
        let mut top_levels = HashMap::new();
        let mut ambiguous_top_levels = HashSet::new();
        let mut known_type_names: HashSet<String> = HashSet::new();
        for file in files {
            for nesting in &file.index.type_nesting {
                known_type_names.insert(nesting.type_name.clone());
                if ambiguous_top_levels.contains(&nesting.type_name) {
                    continue;
                }
                match top_levels.get(&nesting.type_name) {
                    Some(existing) if existing != &nesting.top_level_type => {
                        top_levels.remove(&nesting.type_name);
                        ambiguous_top_levels.insert(nesting.type_name.clone());
                    }
                    Some(_) => {}
                    None => {
                        top_levels
                            .insert(nesting.type_name.clone(), nesting.top_level_type.clone());
                    }
                }
            }
        }
        // P1-B (#1898 code review round 2): repo-wide, regardless of which
        // file declares the field or which type's scope it belongs to --
        // the sole consumer only ever asks "is this bare name a field
        // ANYWHERE", never "on which type". `field_types` is built in the
        // SAME pass: `None` means "conflicting declared types seen for
        // this name" (an unrelated field elsewhere shares the bare name
        // but not the type), `Some(t)` means every record seen so far
        // agreed on `t` -- ambiguity is sticky (once `None`, stays `None`
        // even if a later record happens to match an earlier one, since
        // by then a genuine conflict is already proven).
        let mut known_field_names: HashSet<String> = HashSet::new();
        let mut field_types: HashMap<String, Option<String>> = HashMap::new();
        for file in files {
            for typed_name in &file.index.typed_names {
                if !matches!(
                    &typed_name.scope,
                    crate::graph::extract::local_index::NameScope::Field { .. }
                ) {
                    continue;
                }
                known_field_names.insert(typed_name.name.clone());
                match field_types.get(&typed_name.name) {
                    None => {
                        field_types
                            .insert(typed_name.name.clone(), Some(typed_name.declared_type.clone()));
                    }
                    Some(Some(existing)) if existing != &typed_name.declared_type => {
                        field_types.insert(typed_name.name.clone(), None);
                    }
                    Some(_) => {}
                }
            }
        }
        // P1-A (#1898 code review round 2): repo-wide, same conservative
        // rationale as `known_field_names` above -- a name EVER used as a
        // type parameter anywhere only ever makes this MORE conservative.
        let mut type_parameter_names: HashSet<String> = HashSet::new();
        for file in files {
            for name in &file.index.type_parameter_names {
                type_parameter_names.insert(name.clone());
            }
        }
        TypeIndex {
            direct_children,
            direct_parents,
            interface_names,
            top_levels,
            incomplete_supertype_names,
            known_field_names,
            field_types,
            known_type_names,
            type_parameter_names,
        }
    }

    /// True when `type_name` is a known interface anywhere in the repo.
    pub(crate) fn is_interface(&self, type_name: &str) -> bool {
        self.interface_names.contains(type_name)
    }

    /// P1-4 (#1898 code review, AC3): true when `name` is the bare name of
    /// a type declared ANYWHERE in the repo -- see the `known_type_names`
    /// field doc for the sole consumer (static-type receiver resolution).
    pub(crate) fn is_known_type_name(&self, name: &str) -> bool {
        self.known_type_names.contains(name)
    }

    /// P1-B (#1898 code review round 2, epic #1906): true when `name` is
    /// the bare name of a FIELD declared ANYWHERE in the repo -- see the
    /// `known_field_names` field doc for the sole consumer (blocking
    /// `receiver::resolve_receiver_type`'s static-type-name fallback for
    /// an identifier that could just as easily be an out-of-scope field
    /// access this binder's per-file `FileTypedNames` missed).
    pub(crate) fn is_known_field_name(&self, name: &str) -> bool {
        self.known_field_names.contains(name)
    }

    /// P1-B (#1898 code review round 2, epic #1906): `name`'s declared
    /// type, ONLY when every Field-scope `typed_names` record repo-wide
    /// sharing that bare name agrees on it -- `None` for a name that is
    /// not a known field at all, OR one with conflicting declared types
    /// recorded (never a guessed type). See the `field_types` field doc
    /// for the sole consumer.
    pub(crate) fn unambiguous_field_type(&self, name: &str) -> Option<&str> {
        self.field_types.get(name)?.as_deref()
    }

    /// P1-A (#1898 code review round 2, epic #1906): true when `name` is
    /// the bare name of a GENERIC TYPE PARAMETER declared anywhere in the
    /// repo -- see the `type_parameter_names` field doc for the sole
    /// consumer (`receiver::resolve_receiver_type` rejecting a declared
    /// type that names a type variable, never a concrete class).
    pub(crate) fn is_known_type_parameter_name(&self, name: &str) -> bool {
        self.type_parameter_names.contains(name)
    }

    /// The Java top-level declaration sharing this type's private-access
    /// domain, if the extractor can establish it without ambiguity.
    pub(crate) fn top_level_of(&self, type_name: &str) -> Option<&str> {
        self.top_levels.get(type_name).map(String::as_str)
    }

    /// N1 (#1873/#1875 second-review rework): true when `type_name`'s
    /// recorded superclass/`implements` evidence is known to be INCOMPLETE
    /// -- a `superclass`/type-list entry existed syntactically somewhere in
    /// the repo for this type but could not be resolved to a name. The sole
    /// consumer, `apply_super_class_narrowing`, must treat this as "do not
    /// trust `supertypes_of` for this type", regardless of whether that set
    /// happens to be non-empty from some OTHER, correctly-resolved edge.
    pub(crate) fn has_incomplete_supertype_evidence(&self, type_name: &str) -> bool {
        self.incomplete_supertype_names.contains(type_name)
    }

    /// AC1 "engine query": every type that directly or transitively
    /// extends/implements `type_name`, via a genuine BFS. `visited`
    /// starts pre-seeded with `type_name` ITSELF, so a cyclic edge set
    /// that eventually points back to the root can never re-enqueue it;
    /// every other type is enqueued at most once (the `insert` guard
    /// below returns `false` on a repeat), so the total number of loop
    /// iterations is bounded by the repo's own finite count of distinct
    /// type names -- a diamond (two paths converging on the same
    /// descendant) or an outright cycle both terminate by this
    /// construction alone, never by a depth cap (Rule 14,
    /// anti-unbounded-loop).
    pub(crate) fn implementors_of(&self, type_name: &str) -> HashSet<String> {
        let mut visited: HashSet<String> = HashSet::from([type_name.to_string()]);
        let mut queue: std::collections::VecDeque<String> =
            std::collections::VecDeque::from([type_name.to_string()]);
        let mut result = HashSet::new();
        while let Some(current) = queue.pop_front() {
            let Some(children) = self.direct_children.get(&current) else {
                continue;
            };
            for child in children {
                if visited.insert(child.clone()) {
                    result.insert(child.clone());
                    queue.push_back(child.clone());
                }
            }
        }
        result
    }

    /// AC1/AC3 (Story #1806, S2b) "engine query": every type that
    /// `type_name` directly or transitively `extends`/`implements`, via a
    /// genuine BFS -- the mirror image of `implementors_of` above, walking
    /// `direct_parents` instead of `direct_children`. Same cycle-safety
    /// argument applies verbatim (`visited` pre-seeded with `type_name`
    /// itself, each other type enqueued at most once): a diamond or an
    /// outright cyclic hierarchy both terminate by construction, never by
    /// a depth cap (Rule 14, anti-unbounded-loop). No `MAX_FAMILY_SIZE`-
    /// style cap: the `visited` guard alone already bounds total work and
    /// memory by the repo's own finite count of distinct type names (the
    /// same termination argument `implementors_of` relies on), which is
    /// the honest guarantee this function makes -- it does NOT claim a
    /// type's ancestor chain is inherently small (a type CAN declare many
    /// direct supertypes, e.g. `implements A, B, C, D`, and each of those
    /// can itself have many ancestors); it claims only that the walk
    /// cannot loop or grow without bound relative to that finite count.
    pub(crate) fn supertypes_of(&self, type_name: &str) -> HashSet<String> {
        let mut visited: HashSet<String> = HashSet::from([type_name.to_string()]);
        let mut queue: std::collections::VecDeque<String> =
            std::collections::VecDeque::from([type_name.to_string()]);
        let mut result = HashSet::new();
        while let Some(current) = queue.pop_front() {
            let Some(parents) = self.direct_parents.get(&current) else {
                continue;
            };
            for parent in parents {
                if visited.insert(parent.clone()) {
                    result.insert(parent.clone());
                    queue.push_back(parent.clone());
                }
            }
        }
        result
    }

    /// AC1 "engine query": every declaration in `pool` (an existing
    /// `RepoNameIndex::lookup(method_name, Method)` result) whose
    /// `enclosing_type` is a known implementor of `interface_name` --
    /// i.e. the concrete overrides of an interface method, given the
    /// repo-wide same-named-method pool the binder already computed.
    ///
    /// Memory-safety amendment (real 21.8GB-RSS incident, 2026-09,
    /// killed before it exhausted the host): HARD CAPPED at
    /// `MAX_FAMILY_SIZE`. Without this cap, a single common interface
    /// (e.g. a plugin/listener contract with hundreds of implementors)
    /// multiplied by every call site invoking it turns family expansion
    /// into unbounded `O(references x family size)` memory growth --
    /// this growth happens INSIDE `resolve_reference`, cloned into every
    /// reference's `PendingReference.candidates`, BEFORE the `IndexBudget`
    /// ladder ever runs (that ladder only trims the FINAL CSR arena, long
    /// after this Vec has already been built -- see
    /// `admission::finish_bind`). The bool return is `true` exactly when
    /// the true match count exceeds `MAX_FAMILY_SIZE`; the caller
    /// (`resolve::apply_inheritance_family_expansion`) uses this to mark
    /// affected candidates `reasons::FAMILY_TRUNCATED` so the
    /// incompleteness is VISIBLE on the result -- collapsing the family to
    /// fewer members without saying so is exactly the confidently-wrong
    /// outcome AC1 exists to prevent.
    ///
    /// Deterministic: always keeps the first `MAX_FAMILY_SIZE` matches in
    /// `pool`'s own (extraction-stable) order, and STOPS iterating `pool`
    /// the instant the cap is hit -- bounding both memory and the CPU cost
    /// of this call to at most `MAX_FAMILY_SIZE` pushes, never the full
    /// `pool.len()` scan a filter-and-collect would otherwise always pay
    /// (Rule 14, anti-unbounded-loop: this loop terminates in at most
    /// `pool.len()` iterations, capped in practice at `MAX_FAMILY_SIZE`
    /// pushes).
    pub(crate) fn overrides_of<'a>(
        &self,
        interface_name: &str,
        pool: &'a [super::name_index::DeclInfo],
    ) -> (Vec<&'a super::name_index::DeclInfo>, bool) {
        let implementors = self.implementors_of(interface_name);
        let mut result = Vec::new();
        let mut truncated = false;
        for decl in pool {
            if !decl
                .enclosing_type
                .as_deref()
                .is_some_and(|t| implementors.contains(t))
            {
                continue;
            }
            if result.len() >= MAX_FAMILY_SIZE {
                truncated = true;
                break;
            }
            result.push(decl);
        }
        (result, truncated)
    }
}

/// Hard ceiling on how many override candidates `overrides_of` will EVER
/// return for one `(interface_name, pool)` call. A real interface rarely
/// has more than a handful of genuine implementors; this is deliberately
/// generous relative to that so it only ever engages on the pathological
/// case (a very common interface/method-name pair on a very large
/// corpus), never a normal repo. See `overrides_of`'s doc comment for the
/// full memory-safety rationale.
pub(crate) const MAX_FAMILY_SIZE: usize = 64;

#[cfg(test)]
mod tests {
    use super::super::name_index::DeclInfo;
    use super::*;
    use crate::graph::extract::local_index::{
        DeclarationKind, InheritanceKind, InheritanceRecord, LocalIndex, TypeNestingRecord,
        Visibility,
    };
    use crate::graph::identity::make_symbol_id;

    fn method_decl_info(file_id: u32, local: u32, enclosing_type: &str) -> DeclInfo {
        DeclInfo {
            symbol: make_symbol_id(file_id, local),
            file_id,
            package: None,
            kind: DeclarationKind::Method,
            param_count: Some(0),
            enclosing_type: Some(enclosing_type.to_string()),
            param_types: Vec::new(),
            is_varargs: false,
            return_type: None,
            visibility: Visibility::Unknown,
        }
    }

    fn edge(subtype: &str, supertype: &str, kind: InheritanceKind) -> InheritanceRecord {
        InheritanceRecord {
            kind,
            subtype_name: subtype.to_string(),
            supertype_name: supertype.to_string(),
            line: 1,
        }
    }

    /// AC1: `overrides_of` filters an EXISTING same-named-method pool down
    /// to declarations whose enclosing type is a known implementor --
    /// excluding both the interface's own declaration and any unrelated
    /// same-named method in a type with no inheritance relation at all.
    #[test]
    fn overrides_of_filters_the_pool_to_known_implementors_only() {
        const SHARED_FILE_ID: u32 = 1;
        // Three distinct `DeclInfo`s (distinct `local` symbol indices),
        // all named "save" by fixture convention, in three different
        // enclosing types: the interface itself, its real implementor,
        // and an unrelated type with NO inheritance relation to "Repo".
        const REPO_METHOD_LOCAL: u32 = 0;
        const IMPL_METHOD_LOCAL: u32 = 1;
        const UNRELATED_METHOD_LOCAL: u32 = 2;

        let files = vec![file_with(
            SHARED_FILE_ID,
            vec![edge("Impl", "Repo", InheritanceKind::Implements)],
            vec!["Repo".to_string()],
        )];
        let index = TypeIndex::build(&files);

        let pool = vec![
            method_decl_info(SHARED_FILE_ID, REPO_METHOD_LOCAL, "Repo"),
            method_decl_info(SHARED_FILE_ID, IMPL_METHOD_LOCAL, "Impl"),
            method_decl_info(SHARED_FILE_ID, UNRELATED_METHOD_LOCAL, "UnrelatedType"),
        ];
        let (overrides, truncated) = index.overrides_of("Repo", &pool);
        assert_eq!(overrides.len(), 1);
        assert_eq!(overrides[0].enclosing_type.as_deref(), Some("Impl"));
        assert!(
            !truncated,
            "a family well under the cap must never report truncation"
        );
    }

    /// Shared fixture for the `MAX_FAMILY_SIZE` boundary tests below:
    /// `count` distinct types, each implementing interface `"Repo"` and
    /// each contributing exactly one same-named pool entry, so the pool's
    /// match count against `"Repo"` is exactly `count`.
    fn single_interface_family_fixture(count: usize) -> (TypeIndex, Vec<DeclInfo>) {
        const SHARED_FILE_ID: u32 = 1;
        let implementor_names: Vec<String> = (0..count).map(|i| format!("Impl{i}")).collect();
        let edges: Vec<InheritanceRecord> = implementor_names
            .iter()
            .map(|name| edge(name, "Repo", InheritanceKind::Implements))
            .collect();
        let files = vec![file_with(SHARED_FILE_ID, edges, vec!["Repo".to_string()])];
        let index = TypeIndex::build(&files);
        let pool: Vec<DeclInfo> = implementor_names
            .iter()
            .enumerate()
            .map(|(i, name)| method_decl_info(SHARED_FILE_ID, i as u32, name))
            .collect();
        (index, pool)
    }

    /// Memory-safety amendment (real 21.8GB-RSS incident on Elasticsearch,
    /// 31,929 files, killed before it exhausted the host -- see the
    /// story's own remediation notes): `overrides_of` MUST hard-cap its
    /// result at `MAX_FAMILY_SIZE` and report `truncated = true` whenever
    /// the true match count exceeds it -- never silently return a
    /// truncated set indistinguishable from "the family really only has
    /// this many members" (that would be the confidently-wrong outcome
    /// AC1 exists to prevent). `MAX_FAMILY_SIZE + 5` distinct implementors
    /// is the minimal discriminating fixture: strictly more matches than
    /// the cap allows, so a wrong implementation that never capped at all
    /// (returning all `MAX_FAMILY_SIZE + 5`) fails this test just as
    /// loudly as one that capped but forgot to report `truncated`.
    #[test]
    fn overrides_of_caps_family_size_and_reports_truncation() {
        let (index, pool) = single_interface_family_fixture(MAX_FAMILY_SIZE + 5);
        let (overrides, truncated) = index.overrides_of("Repo", &pool);
        assert_eq!(
            overrides.len(),
            MAX_FAMILY_SIZE,
            "result must be capped at exactly MAX_FAMILY_SIZE"
        );
        assert!(
            truncated,
            "exceeding the cap must be reported, never silently swallowed"
        );
    }

    /// Companion to the cap test: exactly `MAX_FAMILY_SIZE` matches (not
    /// one more) must NOT report truncation -- the boundary is "strictly
    /// more than the cap", not "at or above it".
    #[test]
    fn overrides_of_does_not_report_truncation_when_exactly_at_the_cap() {
        let (index, pool) = single_interface_family_fixture(MAX_FAMILY_SIZE);
        let (overrides, truncated) = index.overrides_of("Repo", &pool);
        assert_eq!(overrides.len(), MAX_FAMILY_SIZE);
        assert!(
            !truncated,
            "exactly-at-cap must not be reported as truncated"
        );
    }

    fn file_with(
        file_id: u32,
        inheritance: Vec<InheritanceRecord>,
        interface_names: Vec<String>,
    ) -> FileForBind {
        let mut index = LocalIndex::new();
        index.inheritance = inheritance;
        index.interface_names = interface_names;
        FileForBind {
            file_id,
            language: "java".to_string(),
            index,
        }
    }

    /// AC1: a class that `implements` an interface is a direct
    /// implementor; a class that `extends` that implementor is a
    /// TRANSITIVE implementor -- both must be found.
    #[test]
    fn implementors_of_finds_direct_and_transitive_implementors() {
        let files = vec![file_with(
            1,
            vec![
                edge("C", "I", InheritanceKind::Implements),
                edge("D", "C", InheritanceKind::Extends),
            ],
            vec!["I".to_string()],
        )];
        let index = TypeIndex::build(&files);
        let implementors = index.implementors_of("I");
        assert!(implementors.contains("C"));
        assert!(implementors.contains("D"));
        assert_eq!(implementors.len(), 2);
    }

    /// AC1: a name with NO implementors anywhere in the repo returns an
    /// EMPTY set, never a fabricated guess.
    #[test]
    fn implementors_of_returns_empty_for_a_name_with_no_implementors() {
        let files = vec![file_with(1, Vec::new(), vec!["Lonely".to_string()])];
        let index = TypeIndex::build(&files);
        assert!(index.implementors_of("Lonely").is_empty());
    }

    /// AC1 + Rule 14 (anti-unbounded-loop): diamond inheritance (two
    /// sub-interfaces of `I`, both implemented by the SAME class `C`)
    /// must converge on `C` exactly once, not loop or double-count.
    #[test]
    fn implementors_of_converges_on_diamond_inheritance() {
        let files = vec![file_with(
            1,
            vec![
                edge("J", "I", InheritanceKind::Extends),
                edge("K", "I", InheritanceKind::Extends),
                edge("C", "J", InheritanceKind::Implements),
                edge("C", "K", InheritanceKind::Implements),
            ],
            vec!["I".to_string(), "J".to_string(), "K".to_string()],
        )];
        let index = TypeIndex::build(&files);
        let implementors = index.implementors_of("I");
        assert_eq!(
            implementors,
            ["J", "K", "C"].into_iter().map(String::from).collect()
        );
    }

    /// AC1 + Rule 14: a malformed/adversarial CYCLIC inheritance edge set
    /// (never valid real Java, but the binder must not trust its own
    /// heuristic extraction to always be well-formed) must still
    /// terminate -- this test itself times out (fails to return) rather
    /// than failing an assertion if the implementation loops forever.
    #[test]
    fn implementors_of_terminates_on_a_cyclic_edge_set() {
        let files = vec![file_with(
            1,
            vec![
                edge("B", "A", InheritanceKind::Extends),
                edge("A", "B", InheritanceKind::Extends),
            ],
            Vec::new(),
        )];
        let index = TypeIndex::build(&files);
        let implementors = index.implementors_of("A");
        assert_eq!(implementors, ["B"].into_iter().map(String::from).collect());
    }

    /// AC1/AC3 (Story #1806, S2b): a class that `implements` an interface
    /// has that interface as a direct supertype; a class that `extends`
    /// that implementor has the interface as a TRANSITIVE supertype --
    /// both must be found. Mirrors `implementors_of_finds_direct_and_
    /// transitive_implementors` exactly, walking the OPPOSITE direction.
    #[test]
    fn supertypes_of_finds_direct_and_transitive_supertypes() {
        let files = vec![file_with(
            1,
            vec![
                edge("C", "I", InheritanceKind::Implements),
                edge("D", "C", InheritanceKind::Extends),
            ],
            vec!["I".to_string()],
        )];
        let index = TypeIndex::build(&files);
        let supertypes = index.supertypes_of("D");
        assert!(supertypes.contains("C"));
        assert!(supertypes.contains("I"));
        assert_eq!(supertypes.len(), 2);
    }

    /// AC1/AC3: a type with NO declared supertypes anywhere in the repo
    /// returns an EMPTY set, never a fabricated guess.
    #[test]
    fn supertypes_of_returns_empty_for_a_type_with_no_declared_supertypes() {
        let files = vec![file_with(1, Vec::new(), vec!["Lonely".to_string()])];
        let index = TypeIndex::build(&files);
        assert!(index.supertypes_of("Lonely").is_empty());
    }

    /// AC1/AC3 + Rule 14: a malformed/adversarial CYCLIC inheritance edge
    /// set must still terminate -- this test itself times out (fails to
    /// return) rather than failing an assertion if the implementation
    /// loops forever.
    #[test]
    fn supertypes_of_terminates_on_a_cyclic_edge_set() {
        let files = vec![file_with(
            1,
            vec![
                edge("B", "A", InheritanceKind::Extends),
                edge("A", "B", InheritanceKind::Extends),
            ],
            Vec::new(),
        )];
        let index = TypeIndex::build(&files);
        let supertypes = index.supertypes_of("B");
        assert_eq!(supertypes, ["A"].into_iter().map(String::from).collect());
    }

    #[test]
    fn is_interface_reports_known_interfaces_and_false_for_unknown_names() {
        let files = vec![file_with(1, Vec::new(), vec!["Shape".to_string()])];
        let index = TypeIndex::build(&files);
        assert!(index.is_interface("Shape"));
        assert!(!index.is_interface("NotAnInterface"));
    }

    /// P1-4 (#1898 code review): the substrate AC3's static-type receiver
    /// resolution needs -- "is this bare identifier the name of a type
    /// declared ANYWHERE in the repo", built from the SAME `type_nesting`
    /// records `top_level_of` already reads, so no third parallel index of
    /// declared type names is introduced. Two SEPARATE files, each
    /// declaring a DIFFERENT type, prove this is a genuinely repo-wide
    /// (not single-file) query.
    #[test]
    fn is_known_type_name_reports_every_declared_type_and_false_for_unknown_names() {
        let mut index_a = LocalIndex::new();
        index_a.type_nesting.push(TypeNestingRecord {
            type_name: "TimeUtil".to_string(),
            top_level_type: "TimeUtil".to_string(),
        });
        let mut index_b = LocalIndex::new();
        index_b.type_nesting.push(TypeNestingRecord {
            type_name: "ParserA".to_string(),
            top_level_type: "ParserA".to_string(),
        });
        let files = vec![
            FileForBind {
                file_id: 1,
                language: "java".to_string(),
                index: index_a,
            },
            FileForBind {
                file_id: 2,
                language: "java".to_string(),
                index: index_b,
            },
        ];
        let type_index = TypeIndex::build(&files);
        assert!(type_index.is_known_type_name("TimeUtil"));
        assert!(type_index.is_known_type_name("ParserA"));
        assert!(!type_index.is_known_type_name("NeverDeclared"));
    }

    /// P1-B (#1898 code review round 2, epic #1906): `is_known_field_name`
    /// must see a field declared in ANY file, repo-wide -- the exact
    /// substrate an inner-class field access or an inherited field (both
    /// P1-B's own regression fixtures) needs, since the field's OWN
    /// `field_declaration` may live in a different file from the call
    /// site that reads it.
    #[test]
    fn is_known_field_name_reports_every_declared_field_and_false_for_unknown_names() {
        use crate::graph::extract::local_index::{NameScope, TypedNameRecord};

        let mut index_a = LocalIndex::new();
        index_a.typed_names.push(TypedNameRecord {
            name: "outerField".to_string(),
            declared_type: "int".to_string(),
            scope: NameScope::Field {
                enclosing_type: "Outer".to_string(),
            },
        });
        let mut index_b = LocalIndex::new();
        index_b.typed_names.push(TypedNameRecord {
            name: "count".to_string(),
            declared_type: "int".to_string(),
            scope: NameScope::Local {
                enclosing_method: make_symbol_id(2, 0),
            },
        });
        let files = vec![
            FileForBind {
                file_id: 1,
                language: "java".to_string(),
                index: index_a,
            },
            FileForBind {
                file_id: 2,
                language: "java".to_string(),
                index: index_b,
            },
        ];
        let type_index = TypeIndex::build(&files);
        assert!(type_index.is_known_field_name("outerField"));
        assert!(
            !type_index.is_known_field_name("count"),
            "a LOCAL-scoped typed name must never be reported as a known field"
        );
        assert!(!type_index.is_known_field_name("neverDeclared"));
    }

    /// P1-A (#1898 code review round 2, epic #1906): `is_known_type_
    /// parameter_name` must see a type parameter declared in ANY file,
    /// repo-wide -- mirrors `is_known_field_name`'s own repo-wide test
    /// exactly.
    #[test]
    fn is_known_type_parameter_name_reports_every_declared_type_parameter_and_false_for_unknown_names(
    ) {
        let mut index_a = LocalIndex::new();
        index_a.type_parameter_names.push("T".to_string());
        let index_b = LocalIndex::new();
        let files = vec![
            FileForBind {
                file_id: 1,
                language: "java".to_string(),
                index: index_a,
            },
            FileForBind {
                file_id: 2,
                language: "java".to_string(),
                index: index_b,
            },
        ];
        let type_index = TypeIndex::build(&files);
        assert!(type_index.is_known_type_parameter_name("T"));
        assert!(!type_index.is_known_type_parameter_name("NeverDeclared"));
    }

    /// P1-B (#1898 code review round 2, epic #1906): `unambiguous_field_
    /// type` must resolve a field declared in a DIFFERENT file from the
    /// caller (the "inherited field" P1-B shape) when every record for
    /// that bare name agrees, and return `None` when two unrelated
    /// records disagree on the type (never guessed) or the name is
    /// simply unknown.
    #[test]
    fn unambiguous_field_type_resolves_a_field_declared_in_a_different_file_and_returns_none_on_conflict(
    ) {
        use crate::graph::extract::local_index::{NameScope, TypedNameRecord};

        let field_file = |id: u32, owner: &str, name: &str, ty: &str| {
            let mut index = LocalIndex::new();
            index.typed_names.push(TypedNameRecord {
                name: name.to_string(),
                declared_type: ty.to_string(),
                scope: NameScope::Field {
                    enclosing_type: owner.to_string(),
                },
            });
            FileForBind { file_id: id, language: "java".to_string(), index }
        };
        let files = vec![
            field_file(1, "Base", "inheritedField", "Svc"),
            field_file(2, "A", "conflicting", "int"),
            field_file(3, "B", "conflicting", "String"),
        ];
        let type_index = TypeIndex::build(&files);
        assert_eq!(
            type_index.unambiguous_field_type("inheritedField"),
            Some("Svc")
        );
        assert_eq!(
            type_index.unambiguous_field_type("conflicting"),
            None,
            "two unrelated fields sharing a bare name but disagreeing on type must never \
             resolve to either guessed type"
        );
        assert_eq!(type_index.unambiguous_field_type("neverDeclared"), None);
    }
}
