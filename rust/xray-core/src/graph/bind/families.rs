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
    /// Bare names of every type declared as an INTERFACE anywhere in the
    /// repo.
    interface_names: HashSet<String>,
}

impl TypeIndex {
    /// Bounded loop: iterates once per `(file, inheritance edge)` pair --
    /// finite, fixed by `files`' own already-extracted record counts
    /// (Rule 14).
    pub(crate) fn build(files: &[FileForBind]) -> Self {
        let mut direct_children: HashMap<String, Vec<String>> = HashMap::new();
        let mut interface_names: HashSet<String> = HashSet::new();
        for file in files {
            for name in &file.index.interface_names {
                interface_names.insert(name.clone());
            }
            for edge in &file.index.inheritance {
                direct_children.entry(edge.supertype_name.clone()).or_default().push(edge.subtype_name.clone());
            }
        }
        TypeIndex { direct_children, interface_names }
    }

    /// True when `type_name` is a known interface anywhere in the repo.
    pub(crate) fn is_interface(&self, type_name: &str) -> bool {
        self.interface_names.contains(type_name)
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
        let mut queue: std::collections::VecDeque<String> = std::collections::VecDeque::from([type_name.to_string()]);
        let mut result = HashSet::new();
        while let Some(current) = queue.pop_front() {
            let Some(children) = self.direct_children.get(&current) else { continue };
            for child in children {
                if visited.insert(child.clone()) {
                    result.insert(child.clone());
                    queue.push_back(child.clone());
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
            if !decl.enclosing_type.as_deref().is_some_and(|t| implementors.contains(t)) {
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
    use super::*;
    use crate::graph::extract::local_index::{DeclarationKind, InheritanceKind, InheritanceRecord, LocalIndex};
    use crate::graph::identity::make_symbol_id;
    use super::super::name_index::DeclInfo;

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
        }
    }

    fn edge(subtype: &str, supertype: &str, kind: InheritanceKind) -> InheritanceRecord {
        InheritanceRecord { kind, subtype_name: subtype.to_string(), supertype_name: supertype.to_string(), line: 1 }
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
        assert!(!truncated, "a family well under the cap must never report truncation");
    }

    /// Shared fixture for the `MAX_FAMILY_SIZE` boundary tests below:
    /// `count` distinct types, each implementing interface `"Repo"` and
    /// each contributing exactly one same-named pool entry, so the pool's
    /// match count against `"Repo"` is exactly `count`.
    fn single_interface_family_fixture(count: usize) -> (TypeIndex, Vec<DeclInfo>) {
        const SHARED_FILE_ID: u32 = 1;
        let implementor_names: Vec<String> = (0..count).map(|i| format!("Impl{i}")).collect();
        let edges: Vec<InheritanceRecord> =
            implementor_names.iter().map(|name| edge(name, "Repo", InheritanceKind::Implements)).collect();
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
        assert_eq!(overrides.len(), MAX_FAMILY_SIZE, "result must be capped at exactly MAX_FAMILY_SIZE");
        assert!(truncated, "exceeding the cap must be reported, never silently swallowed");
    }

    /// Companion to the cap test: exactly `MAX_FAMILY_SIZE` matches (not
    /// one more) must NOT report truncation -- the boundary is "strictly
    /// more than the cap", not "at or above it".
    #[test]
    fn overrides_of_does_not_report_truncation_when_exactly_at_the_cap() {
        let (index, pool) = single_interface_family_fixture(MAX_FAMILY_SIZE);
        let (overrides, truncated) = index.overrides_of("Repo", &pool);
        assert_eq!(overrides.len(), MAX_FAMILY_SIZE);
        assert!(!truncated, "exactly-at-cap must not be reported as truncated");
    }

    fn file_with(file_id: u32, inheritance: Vec<InheritanceRecord>, interface_names: Vec<String>) -> FileForBind {
        let mut index = LocalIndex::new();
        index.inheritance = inheritance;
        index.interface_names = interface_names;
        FileForBind { file_id, language: "java".to_string(), index }
    }

    /// AC1: a class that `implements` an interface is a direct
    /// implementor; a class that `extends` that implementor is a
    /// TRANSITIVE implementor -- both must be found.
    #[test]
    fn implementors_of_finds_direct_and_transitive_implementors() {
        let files = vec![file_with(
            1,
            vec![edge("C", "I", InheritanceKind::Implements), edge("D", "C", InheritanceKind::Extends)],
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
        assert_eq!(implementors, ["J", "K", "C"].into_iter().map(String::from).collect());
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
            vec![edge("B", "A", InheritanceKind::Extends), edge("A", "B", InheritanceKind::Extends)],
            Vec::new(),
        )];
        let index = TypeIndex::build(&files);
        let implementors = index.implementors_of("A");
        assert_eq!(implementors, ["B"].into_iter().map(String::from).collect());
    }

    #[test]
    fn is_interface_reports_known_interfaces_and_false_for_unknown_names() {
        let files = vec![file_with(1, Vec::new(), vec!["Shape".to_string()])];
        let index = TypeIndex::build(&files);
        assert!(index.is_interface("Shape"));
        assert!(!index.is_interface("NotAnInterface"));
    }
}
