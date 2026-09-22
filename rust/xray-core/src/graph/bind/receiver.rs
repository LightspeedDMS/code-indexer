//! Receiver-type resolution substrate for AC1 (receiver-type narrowing)
//! and AC2 (return-type chaining) -- Story #1806, S2b. Bind-time
//! counterpart to `crate::graph::extract::java_receiver`'s extraction-time
//! `ReceiverExpr` construction: this module resolves an ALREADY-BUILT
//! `ReceiverExpr` into a concrete declared type name, using only
//! declared-type evidence already captured in `LocalIndex.typed_names`
//! (AC1, same file as the call) and the repo-wide `RepoNameIndex`/
//! `TypeIndex` (AC2, a chained call's intermediate method may be declared
//! in a DIFFERENT file than the call site -- return types, like every
//! other repo-wide declaration fact this binder uses, are indexed
//! globally). Scope stays bounded to declared-type evidence exactly as
//! the rest of this story: no build, no classpath, no generics/full type
//! inference. Called for real by `super::resolve_all_references`.

use super::families::TypeIndex;
use super::name_index::RepoNameIndex;
use crate::graph::extract::local_index::{
    DeclarationKind, ImportKind, ImportRecord, NameScope, ReceiverExpr, TypedNameRecord,
};
use crate::graph::identity::SymbolId;
use std::collections::HashMap;

/// Per-file lookup substrate for AC1's "declared types in the same file"
/// scope: a local variable's/parameter's declared type (keyed by its
/// enclosing METHOD symbol -- see `NameScope::Local`) and a field's
/// declared type (keyed by its enclosing TYPE name -- `NameScope::Field`).
/// Built once per file, mirroring `super::scope::FileScope`'s own
/// per-file construction pattern.
/// #1910 prerequisite 1 (round4-findings.md finding 1): a `(enclosing_
/// method, name)` key's aggregated evidence -- either every
/// `TypedNameRecord` seen for that key agreed on ONE declared type
/// (`Known`), or at least two disagreed (`Ambiguous`, e.g. two same-named
/// locals in SIBLING blocks of the same method -- Java scopes by BLOCK,
/// which this per-method key structurally cannot distinguish). Ambiguity
/// is STICKY: once two records disagree, the key stays `Ambiguous`
/// forever, even if a third record happens to repeat an earlier value --
/// the genuine disagreement already proves this key is unsafe to trust
/// (same "sticky ambiguity" doctrine `families::TypeIndex::field_types`
/// already uses for its own cross-file field-type conflicts).
#[derive(Debug, Clone, PartialEq, Eq)]
enum LocalTypeEvidence {
    Known(String),
    Ambiguous,
}

/// #1910 round 6 (review rejection of the first #1910 attempt): the
/// three-way OUTCOME of a `FileTypedNames::lookup` call. The pre-round-6
/// version of `lookup` returned a plain `Option<String>`, collapsing
/// `Ambiguous` down to `None` internally -- indistinguishable, by the time
/// it reached `resolve_identifier_receiver`, from a genuine "no evidence
/// at all" miss. Both then fell through to the SAME open-world fallback
/// chain (`unambiguous_field_type`, then the static-type-name substrate),
/// which could resolve `Positive` on a coincidental class-name collision
/// and hard-delete a real candidate -- round 6's own finding 1, reproduced
/// end-to-end by `bug_1910_round6_narrowing_regressions.rs`'s sibling-block
/// test. `Ambiguous` is WORSE evidence than a miss (it proves this exact
/// name/scope pair is UNSAFE to trust, not merely undocumented), so it
/// must be a DISTINCT, terminal outcome the caller can act on directly.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum LocalLookup {
    /// A single local/parameter binding (or, absent one, a single field
    /// binding) unambiguously agrees on `declared_type`.
    Found(String),
    /// This exact `(enclosing_method, name)` key saw two or more DIFFERENT
    /// declared types across sibling blocks -- genuinely unsafe to trust
    /// either way. The caller must treat this as a terminal "no evidence",
    /// never falling through to an open-world fallback (round 6, finding
    /// 1's remediation).
    Ambiguous,
    /// No local/parameter or field evidence at all for this name/scope --
    /// a genuine, decidable absence, safe for the caller to treat as
    /// "no local binding" and consult a broader fallback substrate.
    Missing,
}

pub(crate) struct FileTypedNames {
    locals: HashMap<(SymbolId, String), LocalTypeEvidence>,
    fields: HashMap<(String, String), String>,
    /// #1922: a FLAT, context-independent existence set -- see
    /// `has_any_local_binding`'s own doc comment for why `locals` alone
    /// (keyed by `(enclosing_method, name)`) is not, by itself, a
    /// sufficient substrate for that check. Seeded in `build` from every
    /// `NameScope::Local` record's own name (the exact same names
    /// `locals`'s keys already carry, just without the `enclosing_method`
    /// half) and extended in `with_all_local_binding_names` with names
    /// that have NO enclosing method at all -- `has_any_local_binding` is
    /// then a single O(1) lookup against this ONE set, never a linear
    /// scan.
    all_local_binding_names: std::collections::HashSet<String>,
    /// #1924/#1925: exactly the `(enclosing_method, name)` pairs
    /// `LocalIndex::parameter_typed_names` recorded -- see that field's
    /// own doc comment for why this is STRICTLY NARROWER than `locals`'s
    /// own keys (which also include ordinary local variables,
    /// indistinguishable from a parameter by key alone under the #1919
    /// per-method-not-per-block conflation). Sole consumer: `is_parameter_
    /// binding`.
    parameters: std::collections::HashSet<(SymbolId, String)>,
    /// #1924 (p12): exactly the `(enclosing_method, name)` pairs
    /// `LocalIndex::qualified_non_java_lang_parameter_types` recorded --
    /// see that field's own doc comment. Sole consumer: `is_disqualified_
    /// by_type_qualifier`.
    qualified_non_java_lang_parameters: std::collections::HashSet<(SymbolId, String)>,
}

impl FileTypedNames {
    /// Bounded loop: iterates once per already-extracted `TypedNameRecord`
    /// (finite, fixed by the file's own record count, Rule 14). Every
    /// `NameScope::Local` record's name is ALSO inserted into `all_local_
    /// binding_names` here -- the exact same names `locals`'s own keys
    /// carry, seeded once at build time rather than re-scanned per
    /// `has_any_local_binding` call. `with_all_local_binding_names` below
    /// EXTENDS this same set (never replaces it), so the two insertion
    /// points together cover the identical name set `has_any_local_
    /// binding`'s pre-O(1) implementation checked -- only ever a superset
    /// grows here, never a name removed, so narrowing can only become
    /// MORE conservative than before, never less.
    pub(crate) fn build(typed_names: &[TypedNameRecord]) -> Self {
        let mut locals: HashMap<(SymbolId, String), LocalTypeEvidence> = HashMap::new();
        let mut fields = HashMap::new();
        let mut all_local_binding_names: std::collections::HashSet<String> =
            std::collections::HashSet::new();
        for record in typed_names {
            match &record.scope {
                NameScope::Local { enclosing_method } => {
                    all_local_binding_names.insert(record.name.clone());
                    let key = (*enclosing_method, record.name.clone());
                    match locals.get(&key) {
                        None => {
                            locals.insert(key, LocalTypeEvidence::Known(record.declared_type.clone()));
                        }
                        Some(LocalTypeEvidence::Known(existing))
                            if existing == &record.declared_type => {}
                        Some(LocalTypeEvidence::Known(_)) => {
                            locals.insert(key, LocalTypeEvidence::Ambiguous);
                        }
                        Some(LocalTypeEvidence::Ambiguous) => {}
                    }
                }
                NameScope::Field { enclosing_type } => {
                    fields.insert(
                        (enclosing_type.clone(), record.name.clone()),
                        record.declared_type.clone(),
                    );
                }
            }
        }
        FileTypedNames {
            locals,
            fields,
            all_local_binding_names,
            parameters: std::collections::HashSet::new(),
            qualified_non_java_lang_parameters: std::collections::HashSet::new(),
        }
    }

    /// #1924/#1925: populates `parameters` from `LocalIndex::parameter_
    /// typed_names` -- a separate builder step, not a second `build`
    /// parameter, mirroring `with_all_local_binding_names`'s own
    /// established pattern (so `build`'s existing unit-test call sites,
    /// none of which exercise parameter-vs-local discrimination, stay
    /// unchanged).
    pub(crate) fn with_parameter_bindings(mut self, bindings: &[(SymbolId, String)]) -> Self {
        self.parameters.extend(bindings.iter().cloned());
        self
    }

    /// #1924 (p12): populates `qualified_non_java_lang_parameters` from
    /// `LocalIndex::qualified_non_java_lang_parameter_types`, the same
    /// separate-builder-step pattern as `with_parameter_bindings` above.
    pub(crate) fn with_qualified_non_java_lang_parameters(mut self, bindings: &[(SymbolId, String)]) -> Self {
        self.qualified_non_java_lang_parameters.extend(bindings.iter().cloned());
        self
    }

    /// #1922: EXTENDS the flat, context-independent local-binding-name
    /// set `build` already seeded with `java_receiver::collect_all_
    /// local_binding_names`'s own extraction (`LocalIndex::all_local_
    /// binding_names`, computed once during extraction) -- see
    /// `has_any_local_binding`'s own doc comment for why `build`'s
    /// `NameScope::Local`-keyed substrate cannot represent a binding
    /// declared outside any method body at all. A separate builder step,
    /// not a second `build` parameter, so `build`'s existing unit-test
    /// call sites (none of which exercise a binding declared outside any
    /// method body) stay unchanged.
    pub(crate) fn with_all_local_binding_names(mut self, names: &[String]) -> Self {
        self.all_local_binding_names.extend(names.iter().cloned());
        self
    }

    /// Looks up `name`'s declared type, preferring a LOCAL/PARAMETER
    /// binding within `enclosing_method` (most specific -- mirrors
    /// ordinary Java scoping, where a local/parameter shadows a
    /// same-named field), falling back to a FIELD binding on
    /// `enclosing_type`. `LocalLookup::Missing` when neither substrate has
    /// evidence; `LocalLookup::Ambiguous` (#1910 round 6, finding 1) when
    /// the local/parameter evidence for this exact `(enclosing_method,
    /// name)` key disagrees across sibling blocks -- a DISTINCT, terminal
    /// outcome from `Missing`, never collapsed into it, so the caller
    /// (`resolve_identifier_receiver`) can refuse to fall through to an
    /// open-world fallback for it.
    pub(crate) fn lookup(
        &self,
        enclosing_method: Option<SymbolId>,
        enclosing_type: Option<&str>,
        name: &str,
    ) -> LocalLookup {
        if let Some(enclosing_method) = enclosing_method {
            match self.locals.get(&(enclosing_method, name.to_string())) {
                Some(LocalTypeEvidence::Known(declared_type)) => {
                    return LocalLookup::Found(declared_type.clone())
                }
                Some(LocalTypeEvidence::Ambiguous) => return LocalLookup::Ambiguous,
                None => {}
            }
        }
        let Some(enclosing_type) = enclosing_type else {
            return LocalLookup::Missing;
        };
        match self
            .fields
            .get(&(enclosing_type.to_string(), name.to_string()))
        {
            Some(declared_type) => LocalLookup::Found(declared_type.clone()),
            None => LocalLookup::Missing,
        }
    }

    /// #1922: true when `name` is recorded as a LOCAL/PARAMETER binding
    /// ANYWHERE in this file -- deliberately WIDER than `lookup`'s own
    /// exact `(enclosing_method, name)` key match, in TWO independent
    /// ways. First, `lookup`'s `Missing` result is not always a genuine
    /// absence -- a captured local in an anonymous/local class is looked
    /// up under the WRONG (inner) enclosing-method key and produces a
    /// FALSE `Missing` for a REAL local declared in an outer, lexically-
    /// enclosing method (e.g. `final Target Helper = ...;` in an outer
    /// method, captured and read as `Helper.helper()` inside an anonymous
    /// `Runnable`'s body) -- `build`'s own seeding of `all_local_binding_
    /// names` from every `NameScope::Local` record still covers this
    /// case. Second, a binding declared OUTSIDE any method body at all (a
    /// lambda parameter in a field initializer, an enum constant's
    /// argument list, or a switch-expression pattern in a field
    /// initializer) has NO enclosing method `SymbolId` to key `self.
    /// locals` by in the first place -- `with_all_local_binding_names`'s
    /// own extension (`java_receiver::collect_all_local_binding_names`'s
    /// flat, context-independent extraction) is what still catches those.
    /// This is NOT scope resolution (no block/branch reasoning, no
    /// shadowing rules, #1919 does not apply) -- a flat existence check,
    /// the exact same shape `TypeIndex::is_known_field_name`/
    /// `is_known_type_name` already use at repo scope, just at file scope
    /// for locals. Sole consumer: `is_definite_type_qualifier`, which
    /// must stay conservative -- never wrongly conclude "definitely a
    /// type" in the face of either gap. O(1): a single hash-set lookup,
    /// never a scan over `self.locals`.
    pub(crate) fn has_any_local_binding(&self, name: &str) -> bool {
        self.all_local_binding_names.contains(name)
    }

    /// #1924/#1925: true only when `(enclosing_method, name)` was recorded
    /// as a genuine formal PARAMETER -- never a block-scoped local
    /// variable, a field, or a record component. O(1): a single hash-set
    /// lookup.
    pub(crate) fn is_parameter_binding(&self, enclosing_method: SymbolId, name: &str) -> bool {
        self.parameters.contains(&(enclosing_method, name.to_string()))
    }

    /// #1924 (p12): true only when `(enclosing_method, name)`'s declared
    /// type was written with an explicit qualifier other than
    /// `java.lang`. O(1): a single hash-set lookup.
    pub(crate) fn is_disqualified_by_type_qualifier(&self, enclosing_method: SymbolId, name: &str) -> bool {
        self.qualified_non_java_lang_parameters.contains(&(enclosing_method, name.to_string()))
    }
}

/// AC2: `type_name`'s (or one of its transitive supertypes') declared
/// return type for a method named `method_name`. Honest under overloads
/// (Rule 2, anti-fallback): when MULTIPLE declarations of `method_name`
/// exist across `type_name`'s hierarchy and they DISAGREE on return type,
/// this returns `None` rather than guessing one -- a chain can only keep
/// following a return type that is unambiguously known.
fn return_type_of_method_on_type(
    method_name: &str,
    type_name: &str,
    name_index: &RepoNameIndex,
    type_index: &TypeIndex,
) -> Option<String> {
    let mut allowed = type_index.supertypes_of(type_name);
    allowed.insert(type_name.to_string());
    let mut found: Option<&str> = None;
    for decl in name_index.lookup(method_name, DeclarationKind::Method) {
        if !decl
            .enclosing_type
            .as_deref()
            .is_some_and(|t| allowed.contains(t))
        {
            continue;
        }
        let Some(return_type) = decl.return_type.as_deref() else {
            continue;
        };
        match found {
            None => found = Some(return_type),
            Some(existing) if existing == return_type => {}
            Some(_) => return None,
        }
    }
    found.map(|t| t.to_string())
}

/// P1-A (#1898 code review round 2, epic #1906): is `declared_type` a
/// PSEUDO-type -- a string that occupies the declared-type-node position
/// this extractor reads but never names a real, narrowable class/
/// interface? Three shapes: the literal `"var"` (Java 10+ local-variable
/// type inference -- this crate performs no type inference at all, so a
/// `var`-declared local's "declared type" is always the bare keyword
/// text, never the inferred concrete type); a bare GENERIC TYPE PARAMETER
/// name (`T` in `<T extends Svc> void run(T t)` -- a formal parameter
/// typed `T` reads exactly like one typed a real class, but `T` denotes
/// no declaration anywhere in the repo); and the EMPTY STRING (P1-B's own
/// "no type evidence at all" sentinel for a local binding this extractor
/// cannot type -- an untyped lambda parameter, a multi-catch parameter, an
/// unresolvable enhanced-for/resource type -- see `java_receiver::lambda_
/// param_typed_names`/`catch_parameter_typed_name`'s own doc comments;
/// the record's PRESENCE still correctly blocks the static-type-name
/// fallback below, but the empty string itself is exactly as much a
/// pseudo-type as `"var"`). Proven end-to-end (`bug_1898_round2_
/// narrowing_regressions.rs`): trusting any of the three as a receiver
/// type used to feed `apply_receiver_type_narrowing`'s then-HARD filter a
/// name matching nothing real, deleting every genuine candidate and
/// flipping a live private method's dead-code verdict to a FALSE
/// `Some(true)` -- exactly the outcome epic #1786 declared structurally
/// impossible. Post-#1898-scope-split (epic #1906, round-4 review),
/// `apply_receiver_type_narrowing` is TAG-ONLY and can no longer delete a
/// candidate on this basis at all; this guard is still kept because it
/// now protects the accuracy of the `RECEIVER_TYPE_MATCH` reason bit (and
/// the AC4 Level 5 unique-name shortcut's admission gate, which still
/// consults Positive receiver-type evidence) rather than preventing a
/// deletion -- it becomes load-bearing for deletion again once a
/// follow-up issue (named in `docs/xray-architecture.md`'s
/// candidate-admission section) restores a safe hard filter.
fn is_pseudo_type(declared_type: &str, type_index: &TypeIndex) -> bool {
    declared_type.is_empty()
        || declared_type == "var"
        || type_index.is_known_type_parameter_name(declared_type)
}

/// Evidence tier backing a resolved receiver type (Bug #1898 round 4,
/// epic #1906 -- the "invert the contract" mandate). Round 3 fed EVERY
/// resolved receiver type into `apply_receiver_type_narrowing`'s HARD
/// filter identically, regardless of how the type was obtained. Three
/// rounds of regressions (var/generic-type-parameter, six P1-B binding
/// forms, and this round's `instanceof` pattern variables and
/// initializer-block locals) all shared the SAME root shape: a coincidental
/// same-name collision between the real binding and an unrelated in-repo
/// declaration, discovered one enumerated binding form at a time. This
/// type makes the fix structural instead of another enumerated case: only
/// evidence tiered `Positive` may drive the hard filter at all.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum ReceiverEvidence {
    /// An actual `TypedNameRecord` lookup HIT (AC1 -- a real local/
    /// parameter/field declaration this file recorded), the call's own
    /// enclosing type (`this`/an implicit receiver -- definitional, never
    /// a guess), or a chained call's declared return type resolved on top
    /// of one of those (AC2 -- itself sourced from a real declaration,
    /// never fabricated). Would have safely driven
    /// `apply_receiver_type_narrowing`'s hard filter (an empty match
    /// against this evidence is genuine proof the real target is external
    /// to the repo) under the pre-#1898-scope-split contract; post-split
    /// `apply_receiver_type_narrowing` is TAG-ONLY and never deletes a
    /// candidate on any evidence tier -- this tier instead drives
    /// `RECEIVER_TYPE_MATCH` confidence and the AC4 Level 5 unique-name
    /// shortcut's admission gate, until a follow-up issue (named in
    /// `docs/xray-architecture.md`'s candidate-admission section) restores
    /// hard filtering.
    Positive(String),
    /// Resolved only via one of the two OPEN-WORLD fallback heuristics
    /// added for P1-4/P1-B (#1898 round 2) -- `TypeIndex::unambiguous_
    /// field_type` or `TypeIndex::is_known_type_name` -- when NO direct
    /// `TypedNameRecord` evidence exists at all. Both can correctly
    /// identify a receiver's type most of the time, but neither is immune
    /// to a coincidental same-name collision with an unrelated in-repo
    /// declaration the way a direct lookup hit is. Before the #1898 scope
    /// split, this tier could PREFER a matching subset of candidates when
    /// at least one genuinely matched (narrowing noise); post-split
    /// `apply_receiver_type_narrowing` is TAG-ONLY and performs no
    /// narrowing at all, matched or not. This tier must still NEVER be
    /// trusted enough to treat a ZERO match as proof of an external
    /// target -- see `apply_receiver_type_narrowing`'s own doc comment,
    /// which matters again once a follow-up issue (named in
    /// `docs/xray-architecture.md`'s candidate-admission section) restores
    /// hard filtering.
    Advisory(String),
    /// No evidence at all (unresolved receiver, a pseudo-type, or a
    /// binding form -- covered or not -- this extractor has no typed-name
    /// record for and no fallback substrate resolves either). Narrowing on
    /// this reference is skipped entirely; the pool is kept untouched.
    None,
}

impl ReceiverEvidence {
    pub(super) fn type_name(&self) -> Option<&str> {
        match self {
            ReceiverEvidence::Positive(t) | ReceiverEvidence::Advisory(t) => Some(t.as_str()),
            ReceiverEvidence::None => Option::None,
        }
    }

    pub(super) fn is_positive(&self) -> bool {
        matches!(self, ReceiverEvidence::Positive(_))
    }
}

/// #1922 (supersedes #1893): is `name` DEFINITELY a TYPE-shaped qualifier
/// -- i.e. this invocation/method-reference's receiver is a bare
/// identifier that (a) follows Java's class-naming convention (starts
/// with an uppercase letter -- the same convention-based discriminator
/// `crate::graph::extract::kotlin::starts_with_uppercase` already trusts
/// for an analogous constructor-vs-call ambiguity), (b) carries NO
/// local/parameter/field evidence anywhere THIS FILE's `typed_names`
/// substrate can see (`typed_names.lookup` returns exactly `LocalLookup::
/// Missing` -- never `Found`, which means a real local/param/field
/// shadows the type name and this is an ordinary instance receiver, and
/// never the unsafe-to-trust `Ambiguous`), (c) is not a known FIELD
/// name anywhere in the repo (`TypeIndex::is_known_field_name`, the same
/// repo-wide guard `resolve_identifier_receiver`'s own static-type-name
/// fallback already trusts, reused rather than duplicated -- Rule 4), and
/// (d) is not explicitly named by a SINGLE-MEMBER static import anywhere
/// in this file (`is_statically_imported_member`).
///
/// Deliberately NOT the same question `resolve_receiver_type` answers:
/// that function asks "what type does this identifier resolve to, if
/// any" (and stays `Advisory` even for a confirmed in-repo type name,
/// permanently, per #1910's salvage doctrine); this asks "is the call
/// STRUCTURALLY qualified by a type reference at all", independent of
/// whether that type turns out to be known in-repo or external. Both
/// combine in `narrowing::apply_type_qualifier_narrowing`.
///
/// #1919 does NOT apply: no local-variable SCOPE analysis is performed
/// here at all (no per-block/per-branch reasoning, no flow-scoping, no
/// shadowing/obscuring rules) -- only a per-file exact-key lookup this
/// binder already performs for an unrelated purpose, plus two closed,
/// facts this file already computes or is handed (`is_known_field_name`
/// repo-wide, the file's own static-import list). A lowercase qualifier
/// (`helper.m()`) fails guard (a) immediately and this function returns
/// `false`, leaving #1922's fix a no-op for it -- exactly the "keep
/// today's behaviour" contract the issue requires for variable/field-
/// shaped qualifiers. This is a CONSERVATIVE (never over-eager) check:
/// `narrowing::apply_type_qualifier_narrowing` never treats a `false`
/// result as proof the receiver is NOT a type -- it only ever hard-
/// narrows when this returns `true` AND the qualifier positively
/// resolves AND a candidate already matches it, so a false `false` here
/// costs evidence precision only, never a dropped edge.
pub(crate) fn is_definite_type_qualifier(
    name: &str,
    enclosing_type: Option<&str>,
    enclosing_method: Option<SymbolId>,
    typed_names: &FileTypedNames,
    type_index: &TypeIndex,
    imports: &[ImportRecord],
) -> bool {
    if !name.chars().next().is_some_and(|c| c.is_uppercase()) {
        return false;
    }
    // #1922: a captured local declared in an outer,
    // lexically-enclosing method is looked up under the WRONG (inner)
    // enclosing-method key by `lookup` below and reports a FALSE
    // `Missing` -- see `has_any_local_binding`'s own doc comment for the
    // full explanation. This wider, file-scoped existence check
    // MUST run first: it is what keeps `Helper.helper()` (`Helper` a
    // captured `final Target Helper = ...;` local, read inside an
    // anonymous `Runnable`) from being wrongly promoted to a type
    // qualifier just because `lookup`'s narrower key misses it.
    if typed_names.has_any_local_binding(name) {
        return false;
    }
    if typed_names.lookup(enclosing_method, enclosing_type, name) != LocalLookup::Missing {
        return false;
    }
    if type_index.is_known_field_name(name) {
        return false;
    }
    // #1922: a SINGLE-MEMBER static import (`import static
    // ext.Holder.CONSTANT;`) explicitly declares, by the import statement
    // itself, that `name` is a MEMBER (field or method) of an external
    // class -- never a type -- regardless of whether it also
    // coincidentally matches an in-repo type's bare name.
    // `is_known_field_name` cannot see this (it only indexes fields
    // declared INSIDE this repo); the import list is the substrate that
    // proves it for an external member.
    !is_statically_imported_member(name, imports)
}

/// #1922: true when `name` is imported via a SINGLE-MEMBER static
/// import (`ImportKind::Static`) anywhere in this file's own import list
/// -- e.g. `import static ext.Holder.CONSTANT;`. Sole consumer:
/// `is_definite_type_qualifier`'s guard against treating an externally
/// static-imported member as a type reference. `ImportKind::
/// StaticWildcard` (`import static pkg.Util.*;`) is deliberately NOT
/// consulted HERE: it names no specific member, so there is nothing to
/// positively match `name` against without guessing -- `has_static_
/// wildcard_import` below handles that shape separately and more
/// coarsely, at the WHOLE-FILE level, rather than trying to name-match
/// against an unknown wildcard target. Bounded loop (Rule 14): iterates
/// at most `imports.len()` times, finite and fixed by this file's own
/// already-extracted import list.
fn is_statically_imported_member(name: &str, imports: &[ImportRecord]) -> bool {
    imports.iter().any(|import| {
        import.kind == ImportKind::Static && import.path.rsplit('.').next() == Some(name)
    })
}

/// #1922: a static WILDCARD import (`import static x.Holder.*;`) can
/// bring ANY member of `Holder` -- including an uppercase FIELD -- into
/// scope without naming it. Unlike a single-member static import, there
/// is no specific name to check `is_statically_imported_member` against:
/// the import statement alone proves nothing about any PARTICULAR
/// identifier, so the only sound response is to disable hard-narrowing
/// for the WHOLE FILE whenever one is present -- see `file_is_safe_for_
/// type_qualifier_narrowing`, this function's sole consumer. Bounded
/// loop (Rule 14): iterates at most `imports.len()` times.
pub(crate) fn has_static_wildcard_import(imports: &[ImportRecord]) -> bool {
    imports
        .iter()
        .any(|import| import.kind == ImportKind::StaticWildcard)
}

/// #1922: matching a supertype's name against a repo-wide or file-wide
/// set of DECLARED type names -- by bare name, cross-file or otherwise --
/// is never sound evidence for this guard. A file can declare its own
/// unrelated type sharing the exact bare name of the call's REAL,
/// externally-qualified supertype (`Sub extends com.example.lib.Base`
/// where this file ALSO happens to declare its own unrelated `static
/// class Base {}`), or the real supertype can be reached only through a
/// sibling nested class's own child, or through an anonymous class body
/// (`new com.example.lib.Base() { ... }`), or through no import at all
/// (implicit same-package resolution) -- every one of these can make a
/// name-based "is this supertype declared somewhere I can see" check
/// pass while the REAL supertype (the one actually declaring the
/// shadowing field) stays invisible to this binder.
///
/// So this guard asks a strictly SYNTACTIC question instead of a
/// name-resolution one: does ANY type declared in this file -- including
/// a nested, local, or anonymous class -- carry ANY explicit `extends`/
/// `implements` clause at all, or unresolvable supertype evidence?
/// `LocalIndex::inheritance` records exactly one entry per such clause
/// (`java.rs`'s own extraction, including the synthetic edge
/// `anonymous_body_context` pushes for an anonymous class body), and
/// `LocalIndex::incomplete_supertypes` records a clause the extractor
/// could not resolve to a name at all -- both are already scoped to
/// types declared IN THIS FILE by construction (extraction never
/// attributes a clause to a type declared elsewhere). If either is
/// non-empty, hard-narrowing is unsafe for the WHOLE file: there is
/// SOME supertype somewhere in it that could carry an inherited field
/// shadowing a qualifier, and this binder has no way to rule that out by
/// name alone.
///
/// An enum/record with NO explicit `implements` clause passes trivially
/// (its implicit `Enum<T>`/`Record` supertype is never recorded as an
/// inheritance edge at all, since the grammar exposes no `superclass`
/// node for either -- and neither implicit supertype can ever contribute
/// an uppercase field visible at a qualifier position); one WITH an
/// explicit `implements` clause records a real edge and correctly
/// disables the guard. An ordinary static facade (`class A { static R
/// m(x) { return B.m(x); } }`, or any class with no `extends`/
/// `implements` clause at all) also passes trivially and still
/// hard-narrows.
pub(crate) fn file_has_no_supertype_evidence(
    file_index: &crate::graph::extract::local_index::LocalIndex,
) -> bool {
    file_index.inheritance.is_empty() && file_index.incomplete_supertypes.is_empty()
}

/// #1922: true when THIS FILE is safe for type-qualifier hard-narrowing
/// at all -- three guards ANDed together: no syntax error anywhere in
/// the file's tree (`LocalIndex::has_syntax_error` -- a node inside a
/// tree-sitter ERROR subtree is silently absent from EVERY extraction
/// pass, never visited and never recorded, so a binding this narrowing
/// depends on can be invisible for a reason no other guard here can see;
/// checked FIRST, an O(1) field read, never a second AST walk), no
/// static wildcard import anywhere in the file (`has_static_wildcard_
/// import`), AND no type declared in the file carries any supertype
/// evidence at all (`file_has_no_supertype_evidence`). Computed ONCE per
/// file (`mod.rs`, alongside `FileTypedNames::build`) and reused for
/// every invocation site in it -- none of the three depend on the
/// specific call site being resolved.
pub(crate) fn file_is_safe_for_type_qualifier_narrowing(
    file_index: &crate::graph::extract::local_index::LocalIndex,
    imports: &[ImportRecord],
) -> bool {
    !file_index.has_syntax_error
        && !has_static_wildcard_import(imports)
        && file_has_no_supertype_evidence(file_index)
}

/// AC1/AC2: resolves `receiver`'s declared type, given the call site's
/// own context. `enclosing_type`/`enclosing_method` are the CALL SITE's
/// own context: used both for `ReceiverExpr::None`/`SelfOrSuper` ("this
/// object's" type IS the enclosing type) and as the scope
/// `typed_names.lookup` resolves an `Identifier` receiver against.
///
/// P1-4 (#1898 code review, AC3): a STATICALLY QUALIFIED call
/// (`TimeUtil.parse(x)`) has an `Identifier("TimeUtil")` receiver just
/// like an instance receiver (`obj.foo()`) does -- but `"TimeUtil"` is a
/// CLASS NAME, never a local variable/field/parameter, so
/// `typed_names.lookup` (which only ever indexes declared-type evidence
/// for names, never type declarations themselves) has no evidence for it
/// at all. Without a fallback, that resolved to `None`, so receiver-type
/// narrowing never ran and the call fanned out to every same-named
/// method in the repo (#1898's own bug report: `TimeUtil.parse(x)`
/// reaching 165 unrelated `parse` methods). When `typed_names.lookup`
/// finds nothing, this now checks `type_index.is_known_type_name(name)`
/// -- if the identifier is itself a known in-repo type, it resolves to
/// that type directly.
///
/// Round-2 code review correction: the ORIGINAL version of this doc
/// comment claimed this fallback is "never a guess". That was FALSE in
/// two shapes proven end-to-end (`bug_1898_round2_narrowing_regressions.
/// rs`, P1-A/P1-B): (1) `typed_names.lookup` returning a declared-type
/// STRING that is itself a pseudo-type (`"var"`, a generic type
/// parameter's own bare name -- see `is_pseudo_type` above) used to be
/// trusted verbatim; (2) the `is_known_type_name` fallback below used to
/// fire whenever `typed_names.lookup` found NOTHING for `name`, which is
/// not proof `name` denotes a type -- it can just as easily be a real
/// field access this binder's per-file/exact-owner-type lookup missed
/// (an inner class reading its outer class's field; a field inherited
/// from a superclass declared in a different file), coincidentally
/// sharing its bare name with an unrelated in-repo type. Both are now
/// guarded (`is_pseudo_type` rejects the first; `!type_index.is_known_
/// field_name(name)` gates the second) -- an identifier that clears
/// BOTH guards is genuinely never a guess; one that does not clear them
/// returns `None` (missing evidence, narrowing skipped) rather than a
/// fabricated type.
///
/// Non-recursive (Rule 14): walks OUTWARD from `receiver`'s outermost
/// `Chained` wrapping down to its base (identifier/self/other),
/// collecting the chain of intermediate method names into a `Vec`, then
/// resolves the base's type and follows the chain FORWARD through
/// declared return types. The `Vec` is bounded by the same cap
/// `crate::graph::extract::java_receiver::build_receiver_expr` already
/// enforced when IT built `receiver` at extraction time -- this function
/// introduces no new unbounded loop.
/// The `ReceiverExpr::Identifier(name)` half of `resolve_receiver_base`
/// below, split out (#1898 round 4 code review, function-length budget):
/// a genuine `TypedNameRecord` lookup HIT is POSITIVE evidence; either of
/// the two open-world fallback substrates (`unambiguous_field_type`,
/// `is_known_type_name`) is ADVISORY -- see `ReceiverEvidence`'s own doc
/// comment for why. P1-A (#1898 round 2): a pseudo-type declared-type
/// string (`"var"`, a bare generic type parameter name) is missing
/// evidence, never a real type.
///
/// #1910 SALVAGE: `LocalLookup::Ambiguous` (kept, round 6's own fix) is a
/// DISTINCT, TERMINAL outcome from `Missing` -- it proves this exact
/// name/scope pair is unsafe to trust and must never fall through to
/// either open-world fallback below, unlike a genuine `Missing`. A round-7
/// attempt to also promote a per-file "declares or imports this type"
/// substrate (`FileStaticTypeNames`) to `Positive` on a `Missing` result
/// was reverted: round 7's review proved a `Missing` result is not always
/// a genuine absence even with JLS-6.3-comprehensive extraction -- a
/// captured local in an anonymous/local class is looked up under the
/// WRONG (inner) enclosing-method key and can produce a FALSE `Missing`
/// for a real local, indistinguishable from the caller's side. Nothing
/// resting on `Missing` can therefore be promoted to `Positive`; the
/// `Missing` branch stays exactly the two Advisory-tier open-world
/// fallbacks it always was.
fn resolve_identifier_receiver(
    name: &str,
    enclosing_type: Option<&str>,
    enclosing_method: Option<SymbolId>,
    typed_names: &FileTypedNames,
    type_index: &TypeIndex,
) -> ReceiverEvidence {
    match typed_names.lookup(enclosing_method, enclosing_type, name) {
        LocalLookup::Found(declared_type) if is_pseudo_type(&declared_type, type_index) => {
            ReceiverEvidence::None
        }
        LocalLookup::Found(declared_type) => ReceiverEvidence::Positive(declared_type),
        // #1910 round 6, finding 1's remediation: an AMBIGUOUS local
        // binding is WORSE evidence than a genuine miss -- it proves this
        // exact name/scope pair is unsafe to trust, so this is TERMINAL.
        // It must never fall through to either open-world fallback below;
        // doing so (the round-6 rejected version's own defect) let a
        // coincidental class-name collision resolve `Positive` and
        // hard-delete a real candidate.
        LocalLookup::Ambiguous => ReceiverEvidence::None,
        // P1-B (#1898 round 2): `typed_names.lookup` returning `Missing`
        // means THIS FILE carries no local/field/parameter evidence for
        // `name` at all -- first try the REPO-WIDE, unanimous-only
        // field-type substrate, then the static-type-name fallback
        // (gated on `name` not ALSO being a known field name anywhere in
        // the repo). Neither is trusted enough to be Positive -- see this
        // function's own doc comment for why #1910's attempt to promote
        // the static-type-name fallback to Positive was reverted.
        LocalLookup::Missing => {
            if let Some(field_type) = type_index.unambiguous_field_type(name) {
                ReceiverEvidence::Advisory(field_type.to_string())
            } else if type_index.is_known_type_name(name) && !type_index.is_known_field_name(name)
            {
                ReceiverEvidence::Advisory(name.to_string())
            } else {
                ReceiverEvidence::None
            }
        }
    }
}

/// Bug #1923 (AC2, reworked to TAG-ONLY -- see the doc comment on
/// `apply_overload_shape_narrowing` in `narrowing.rs`): resolves a
/// bare-identifier CALL ARGUMENT's declared type, consumed ONLY to
/// decide whether `OVERLOAD_ARG_TYPE_MATCH` is TAGGED -- this result
/// NEVER drives a candidate-set exclusion, mirroring the permanently-
/// tag-only contract `apply_receiver_type_narrowing` already documents
/// for the analogous receiver-type case. Reuses `resolve_identifier_receiver`'s
/// exact lookup (Rule 4, anti-duplication) rather than a second copy of
/// the same local/parameter/field substrate -- an argument identifier
/// and a receiver identifier are looked up identically (same per-file
/// `FileTypedNames`, same enclosing-method/enclosing-type scoping). The
/// result is narrowed to `ReceiverEvidence::Positive` ONLY: a genuine
/// `TypedNameRecord` hit (a real local/parameter/field declaration in
/// THIS file). `Advisory` (either open-world fallback --
/// `unambiguous_field_type`/`is_known_type_name`, both cross-file or
/// name-coincidence guesses) and `Ambiguous`/`Missing` (folded to `None`
/// by `resolve_identifier_receiver` already) all return `None` here --
/// this is about TAG ACCURACY, not about gating a deletion that no
/// longer happens: an Advisory guess must not even mislabel a candidate
/// as compatible/incompatible, since the tag is presented to callers as
/// "not provably incompatible" evidence, never a raw guess.
pub(crate) fn resolve_argument_identifier_type(
    name: &str,
    enclosing_type: Option<&str>,
    enclosing_method: Option<SymbolId>,
    typed_names: &FileTypedNames,
    type_index: &TypeIndex,
) -> Option<String> {
    match resolve_identifier_receiver(name, enclosing_type, enclosing_method, typed_names, type_index) {
        ReceiverEvidence::Positive(declared_type) => Some(declared_type),
        ReceiverEvidence::Advisory(_) | ReceiverEvidence::None => None,
    }
}

/// Resolves the BASE (non-chained) receiver -- `current`, after every
/// `Chained` wrapping has already been peeled off by the caller -- to its
/// evidence-tiered type, with no chain-following of its own. Split out of
/// `resolve_receiver_type` (#1898 round 4 code review) to stay under the
/// per-function line budget; the two functions are one continuous
/// algorithm.
fn resolve_receiver_base(
    current: &ReceiverExpr,
    enclosing_type: Option<&str>,
    enclosing_method: Option<SymbolId>,
    typed_names: &FileTypedNames,
    type_index: &TypeIndex,
) -> ReceiverEvidence {
    match current {
        // "this object's" type IS the enclosing type -- definitional,
        // never a guess, hence POSITIVE (#1898 round 4).
        ReceiverExpr::None | ReceiverExpr::SelfOrSuper => match enclosing_type {
            Some(t) => ReceiverEvidence::Positive(t.to_string()),
            None => ReceiverEvidence::None,
        },
        // D3: unlike `SelfOrSuper`, a chained call rooted in `super.foo()`
        // has no single well-defined base type here -- `TypeIndex` exposes
        // only the full transitive supertype SET, not one "the
        // superclass" name, and guessing one would risk exactly the false
        // self/wrong-ancestor binding D3 exists to eliminate.
        ReceiverExpr::Super => ReceiverEvidence::None,
        ReceiverExpr::Identifier(name) => {
            resolve_identifier_receiver(name, enclosing_type, enclosing_method, typed_names, type_index)
        }
        ReceiverExpr::Other => ReceiverEvidence::None,
        ReceiverExpr::Chained { .. } => {
            unreachable!("the caller strips every Chained layer before calling this")
        }
    }
}

/// AC1/AC2: resolves `receiver`'s declared type, given the call site's
/// own context, tagged with its evidence tier (`ReceiverEvidence` --
/// #1898 round 4). Non-recursive (Rule 14): walks OUTWARD from
/// `receiver`'s outermost `Chained` wrapping down to its base
/// (identifier/self/other), collecting the chain of intermediate method
/// names into a `Vec`, resolves the base via `resolve_receiver_base`, then
/// follows the chain FORWARD through declared return types -- a chain
/// step is itself sourced from a real declaration (never a guess), but it
/// PRESERVES rather than upgrades the base's own evidence tier (see
/// `ReceiverEvidence`'s doc comment: a chain built on a coincidentally-
/// wrong ADVISORY guess is exactly as uncertain as the guess itself). The
/// `Vec` is bounded by the same cap
/// `crate::graph::extract::java_receiver::build_receiver_expr` already
/// enforced when IT built `receiver` at extraction time -- this function
/// introduces no new unbounded loop.
pub(crate) fn resolve_receiver_type(
    receiver: &ReceiverExpr,
    enclosing_type: Option<&str>,
    enclosing_method: Option<SymbolId>,
    typed_names: &FileTypedNames,
    name_index: &RepoNameIndex,
    type_index: &TypeIndex,
) -> ReceiverEvidence {
    let mut current = receiver;
    let mut method_chain: Vec<&str> = Vec::new();
    while let ReceiverExpr::Chained {
        method_name,
        receiver: inner,
    } = current
    {
        method_chain.push(method_name.as_str());
        current = inner;
    }
    let (mut resolved_type, is_positive) = match resolve_receiver_base(
        current,
        enclosing_type,
        enclosing_method,
        typed_names,
        type_index,
    ) {
        ReceiverEvidence::Positive(t) => (t, true),
        ReceiverEvidence::Advisory(t) => (t, false),
        ReceiverEvidence::None => return ReceiverEvidence::None,
    };
    for method_name in method_chain.into_iter().rev() {
        let Some(next_type) =
            return_type_of_method_on_type(method_name, &resolved_type, name_index, type_index)
        else {
            return ReceiverEvidence::None;
        };
        // #1910 prerequisite 2 (round4-findings.md finding 2): applied to
        // EVERY chain step's resolved type, not just the base identifier
        // (`resolve_identifier_receiver` already guards that half) -- a
        // generic method's bare-type-parameter return type (`<T> T get()`)
        // is exactly as much a pseudo-type mid-chain as it is at the base,
        // and must never be promoted into a fabricated receiver type.
        if is_pseudo_type(&next_type, type_index) {
            return ReceiverEvidence::None;
        }
        resolved_type = next_type;
    }
    if is_positive {
        ReceiverEvidence::Positive(resolved_type)
    } else {
        ReceiverEvidence::Advisory(resolved_type)
    }
}

#[cfg(test)]
#[path = "receiver_tests.rs"]
mod tests;
