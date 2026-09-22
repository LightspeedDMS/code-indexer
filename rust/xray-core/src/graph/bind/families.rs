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
    /// #1931: the FULL, un-collapsed set of `(type_name, top_level_type)`
    /// nesting edges every file's own `LocalIndex::type_nesting` recorded
    /// -- deliberately NOT the same substrate as `top_levels` above (which
    /// discards BOTH sides of an ambiguous bare name once two DIFFERENT
    /// top-level owners are seen). A dotted qualifier's own `(Outer,
    /// Inner)` pair is a genuine, independent fact regardless of whether
    /// `Inner` is ALSO nested under some unrelated `OuterB` elsewhere in
    /// the repo -- membership in this set proves exactly that pair, never
    /// "the" single unambiguous owner of a bare name. Keyed by `type_name`
    /// -> the set of every `top_level_type` it was ever seen nested under,
    /// rather than a flat `HashSet<(String, String)>` of pairs, so
    /// `is_nested_type_of` can probe with borrowed `&str` (`HashSet<
    /// String>::contains::<str>` via `Borrow`) instead of allocating two
    /// fresh `String`s per call. Sole consumer: `is_nested_type_of`, the
    /// substrate `receiver::resolve_dotted_qualifier_type`'s nested-type
    /// resolution rule uses.
    nesting_pairs: HashMap<String, HashSet<String>>,
    /// #1931 round 4 (Codex P2, perf): PRECOMPUTED answer of
    /// `has_unresolved_external_supertype_transitively` for every known
    /// repo type, filled ONCE in `build()` -- see `compute_unresolved_
    /// external_supertype_transitively_map`'s own doc for the algorithm.
    /// Absent from this map (any name not in `known_type_names`) means
    /// `false`, matching the un-memoized BFS's own behaviour for a name
    /// it can never make progress from (no recorded ancestor evidence).
    unresolved_external_supertype_transitively: HashMap<String, bool>,
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
        let mut nesting_pairs: HashMap<String, HashSet<String>> = HashMap::new();
        for file in files {
            for nesting in &file.index.type_nesting {
                known_type_names.insert(nesting.type_name.clone());
                nesting_pairs
                    .entry(nesting.type_name.clone())
                    .or_default()
                    .insert(nesting.top_level_type.clone());
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
        let mut type_index = TypeIndex {
            direct_children,
            direct_parents,
            interface_names,
            top_levels,
            incomplete_supertype_names,
            known_field_names,
            field_types,
            known_type_names,
            type_parameter_names,
            nesting_pairs,
            unresolved_external_supertype_transitively: HashMap::new(),
        };
        type_index.unresolved_external_supertype_transitively =
            type_index.compute_unresolved_external_supertype_transitively_map();
        type_index
    }

    /// True when `type_name` is a known interface anywhere in the repo.
    pub(crate) fn is_interface(&self, type_name: &str) -> bool {
        self.interface_names.contains(type_name)
    }

    /// #1931: true when the repo recorded a REAL nesting edge proving
    /// `type_name` is declared nested inside `top_level_type`'s own
    /// top-level private-access domain -- see `nesting_pairs`'s own doc
    /// comment for why this is membership in the FULL set rather than
    /// `top_level_of`'s single-unambiguous-owner lookup. Sole consumer:
    /// `receiver::resolve_dotted_qualifier_type`'s two-segment
    /// (`Outer.Inner`) resolution rule.
    pub(crate) fn is_nested_type_of(&self, type_name: &str, top_level_type: &str) -> bool {
        self.nesting_pairs
            .get(type_name)
            .is_some_and(|top_level_types| top_level_types.contains(top_level_type))
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

    /// #1924 round 2 (p13): true when `type_name` has EITHER (a)
    /// `has_incomplete_supertype_evidence` (a syntactically unresolvable
    /// clause), OR (b) at least one DIRECTLY declared supertype whose bare
    /// name is not ITSELF a repo-declared type (`is_known_type_name`) --
    /// i.e. an external/JDK supertype (`extends com.lib.Base`) this binder
    /// has no visibility into: it cannot see whether that supertype
    /// privately declares a NESTED type sharing a closed-world name
    /// (`String`/`Integer`/etc.) that would otherwise be trusted as the
    /// real JDK type. Sole consumer: `receiver_mismatch`'s caller-side
    /// guard, which must skip `RECEIVER_TYPE_MISMATCH` tagging entirely
    /// for any call site inside such a type. Direct supertypes only
    /// (`direct_parents`), not the transitive closure `supertypes_of`
    /// walks -- an external supertype anywhere in a resolvable chain is
    /// already a DIFFERENT type's own direct edge, caught when checking
    /// that type instead.
    pub(crate) fn has_unresolved_external_supertype(&self, type_name: &str) -> bool {
        if self.has_incomplete_supertype_evidence(type_name) {
            return true;
        }
        self.direct_parents
            .get(type_name)
            .is_some_and(|parents| parents.iter().any(|parent| !self.is_known_type_name(parent)))
    }

    /// #1931 rework (Codex P1, third round; memoized in round 4, Codex
    /// P2): TRANSITIVE counterpart to `has_unresolved_external_
    /// supertype` above -- true when ANY ancestor at ANY depth (not
    /// merely the DIRECT parent) is itself unresolved (incomplete
    /// supertype evidence, or a supertype whose own bare name is not a
    /// repo-declared type). The direct-only predicate above is
    /// INSUFFICIENT as `resolve_dotted_qualifier_type`'s own guard: an
    /// INDEXED parent with an EXTERNAL GRANDPARENT (`Outer extends
    /// IndexedBase extends ExternalBase`, where only `ExternalBase` sits
    /// outside the analysed set) passes the direct-only check trivially
    /// (`Outer`'s own direct parent, `IndexedBase`, IS a known repo type)
    /// while an inherited field on the unindexed GRANDPARENT stays
    /// completely invisible -- exactly the shape Codex reproduced end to
    /// end (`Outer.Inner.m()` where `ExternalBase` declares a shadowing
    /// field `Inner`, and `Outer`'s own file records no `extends`/
    /// `implements` clause at all, so the whole-file #1922 guard does not
    /// save it either).
    ///
    /// O(1) lookup into `unresolved_external_supertype_transitively`,
    /// precomputed ONCE per `build()` by `compute_unresolved_external_
    /// supertype_transitively_map` below -- see that method's doc for the
    /// algorithm. A name absent from the map (never a known repo type)
    /// answers `false`, matching what the original un-memoized BFS
    /// answered for such a name (it could never find any recorded
    /// ancestor evidence to walk).
    ///
    /// Deliberately a SEPARATE predicate from `has_unresolved_external_
    /// supertype` above, never a modification of it: that direct-only
    /// check has its own existing consumer (`receiver_mismatch`'s #1924
    /// `RECEIVER_TYPE_MISMATCH` tagging), whose own four-condition
    /// soundness argument was reviewed and proven specifically against
    /// DIRECT parents -- widening it to transitive ancestry is a
    /// DIFFERENT, unreviewed change this fix does not make. Sole
    /// consumer: `receiver_type_qualifier::resolve_dotted_qualifier_
    /// type`'s own per-segment guard.
    pub(crate) fn has_unresolved_external_supertype_transitively(&self, type_name: &str) -> bool {
        self.unresolved_external_supertype_transitively
            .get(type_name)
            .copied()
            .unwrap_or(false)
    }

    /// #1931 round 4 (Codex P2, perf): builds the map `has_unresolved_
    /// external_supertype_transitively` looks up, computing every known
    /// type's answer with ONE multi-source BFS instead of one full
    /// ancestor-walking BFS PER queried segment.
    ///
    /// Restates the original per-type ancestor walk as reachability:
    /// `has_unresolved_external_supertype_transitively(t)` was true iff
    /// SOME ancestor `a` of `t` (`t` itself, or anywhere up its `direct_
    /// parents` chain) had `has_unresolved_external_supertype(a)` true.
    /// Equivalently: `t` is reachable, via `direct_children` edges (the
    /// subtype direction), from the SEED SET of types that are
    /// directly-unresolved on their own (`has_unresolved_external_
    /// supertype`) -- badness on a supertype propagates DOWN to every
    /// type that transitively extends/implements it. This is a single
    /// multi-source BFS: seed every directly-unresolved known type as
    /// `true`, then flood that `true` outward through `direct_children`.
    ///
    /// Cycle-safe, bounded BFS (Rule 14) -- the identical termination
    /// argument `implementors_of`/`supertypes_of` already document: each
    /// type is enqueued at most once (the `result.insert` guard below
    /// returns `false`/is skipped on a repeat), so a diamond or an
    /// outright cyclic inheritance edge set both still terminate, bounded
    /// by the repo's own finite count of known type names. Every known
    /// type not reached by the flood is explicitly recorded `false` (not
    /// merely absent), so lookups never need to distinguish "known and
    /// resolved" from "never computed".
    fn compute_unresolved_external_supertype_transitively_map(&self) -> HashMap<String, bool> {
        let mut result: HashMap<String, bool> = HashMap::new();
        let mut queue: std::collections::VecDeque<String> = std::collections::VecDeque::new();
        for name in &self.known_type_names {
            if self.has_unresolved_external_supertype(name) {
                result.insert(name.clone(), true);
                queue.push_back(name.clone());
            }
        }
        while let Some(current) = queue.pop_front() {
            let Some(children) = self.direct_children.get(&current) else {
                continue;
            };
            for child in children {
                if result.get(child).copied().unwrap_or(false) {
                    continue;
                }
                result.insert(child.clone(), true);
                queue.push_back(child.clone());
            }
        }
        for name in &self.known_type_names {
            result.entry(name.clone()).or_insert(false);
        }
        result
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
#[path = "families_tests.rs"]
mod tests;

#[cfg(test)]
#[path = "families_transitive_supertype_tests.rs"]
mod transitive_supertype_tests;
