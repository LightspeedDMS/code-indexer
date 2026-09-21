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
    DeclarationKind, NameScope, ReceiverExpr, TypedNameRecord,
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
}

impl FileTypedNames {
    /// Bounded loop: iterates once per already-extracted `TypedNameRecord`
    /// (finite, fixed by the file's own record count, Rule 14).
    pub(crate) fn build(typed_names: &[TypedNameRecord]) -> Self {
        let mut locals: HashMap<(SymbolId, String), LocalTypeEvidence> = HashMap::new();
        let mut fields = HashMap::new();
        for record in typed_names {
            match &record.scope {
                NameScope::Local { enclosing_method } => {
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
        FileTypedNames { locals, fields }
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
mod tests {
    use super::*;
    use crate::graph::bind::FileForBind;
    use crate::graph::extract::local_index::{
        Declaration, DeclarationKind as DK, LocalIndex, MethodOwnerRecord, MethodReturnTypeRecord,
    };
    use crate::graph::identity::make_symbol_id;

    /// #1910 prerequisite 1 (round4-findings.md finding 1): `FileTypedNames`
    /// keys locals by `(enclosing_method, name)`, but Java scopes a local
    /// by BLOCK. Two legal same-named locals in SIBLING blocks of the SAME
    /// method are never simultaneously in scope under real javac, but this
    /// per-method key cannot tell them apart -- last-write-wins would let
    /// an ARBITRARY one of the two declared types answer a lookup for
    /// EITHER call site, exactly the failure that let a live private
    /// method be reported `is_definitely_dead_code() == Some(true)` in
    /// round 4. The safe fix (named explicitly in the issue as an
    /// acceptable minimal alternative to full block-scoping): once a
    /// `(method, name)` key has seen two DIFFERENT declared types, it is
    /// permanently ambiguous and `lookup` must return `LocalLookup::
    /// Ambiguous` -- a DISTINCT, TERMINAL outcome from a genuine `Missing`
    /// lookup (round 6, finding 1's remediation; see `LocalLookup`'s own
    /// doc comment) -- rather than guess.
    #[test]
    fn file_typed_names_refuses_to_pick_a_type_when_sibling_blocks_declare_the_same_name_with_different_types(
    ) {
        let method_symbol = make_symbol_id(1, 0);
        let records = vec![
            TypedNameRecord {
                name: "x".to_string(),
                declared_type: "Foo".to_string(),
                scope: NameScope::Local {
                    enclosing_method: method_symbol,
                },
            },
            TypedNameRecord {
                name: "x".to_string(),
                declared_type: "Bar".to_string(),
                scope: NameScope::Local {
                    enclosing_method: method_symbol,
                },
            },
        ];
        let typed_names = FileTypedNames::build(&records);
        assert_eq!(
            typed_names.lookup(Some(method_symbol), None, "x"),
            LocalLookup::Ambiguous,
            "two sibling-block locals sharing a bare name but disagreeing on declared type \
             must resolve to the DISTINCT, terminal Ambiguous outcome -- never a guessed type \
             AND never collapsed into an ordinary Missing lookup (#1910 round 6, finding 1)"
        );
    }

    /// Companion to the ambiguity test above: the SAME name repeating with
    /// the SAME declared type (e.g. two independent `for (String s : ...)`
    /// loops in sibling blocks of one method -- extremely common, legal
    /// Java) must NOT be treated as ambiguous -- there is no genuine
    /// disagreement to be conservative about.
    #[test]
    fn file_typed_names_still_resolves_when_sibling_blocks_repeat_the_same_name_and_type() {
        let method_symbol = make_symbol_id(1, 0);
        let records = vec![
            TypedNameRecord {
                name: "x".to_string(),
                declared_type: "Foo".to_string(),
                scope: NameScope::Local {
                    enclosing_method: method_symbol,
                },
            },
            TypedNameRecord {
                name: "x".to_string(),
                declared_type: "Foo".to_string(),
                scope: NameScope::Local {
                    enclosing_method: method_symbol,
                },
            },
        ];
        let typed_names = FileTypedNames::build(&records);
        assert_eq!(
            typed_names.lookup(Some(method_symbol), None, "x"),
            LocalLookup::Found("Foo".to_string())
        );
    }

    /// AC1: a local/parameter binding must be preferred over a
    /// same-named field -- the discriminating case ordinary Java scoping
    /// requires (shadowing).
    #[test]
    fn file_typed_names_prefers_a_local_binding_over_a_field_of_the_same_name() {
        let method_symbol = make_symbol_id(1, 0);
        let records = vec![
            TypedNameRecord {
                name: "x".to_string(),
                declared_type: "Local".to_string(),
                scope: NameScope::Local {
                    enclosing_method: method_symbol,
                },
            },
            TypedNameRecord {
                name: "x".to_string(),
                declared_type: "Field".to_string(),
                scope: NameScope::Field {
                    enclosing_type: "Owner".to_string(),
                },
            },
        ];
        let typed_names = FileTypedNames::build(&records);
        assert_eq!(
            typed_names.lookup(Some(method_symbol), Some("Owner"), "x"),
            LocalLookup::Found("Local".to_string())
        );
        // Without a matching local (different method), falls back to the field.
        assert_eq!(
            typed_names.lookup(Some(make_symbol_id(1, 99)), Some("Owner"), "x"),
            LocalLookup::Found("Field".to_string())
        );
        assert_eq!(
            typed_names.lookup(None, None, "neverDeclared"),
            LocalLookup::Missing
        );
    }

    fn method_decl(name: &str, file_id: u32, local: u32) -> Declaration {
        Declaration {
            kind: DK::Method,
            name: name.to_string(),
            line: 1,
            symbol: make_symbol_id(file_id, local),
            param_count: Some(0),
            param_types: Vec::new(),
            is_varargs: false,
        }
    }

    /// AC1: `obj.doSomething()` where `obj` is a locally-declared `Foo`
    /// resolves to `"Foo"`.
    #[test]
    fn resolve_receiver_type_resolves_a_simple_identifier_via_typed_names() {
        let method_symbol = make_symbol_id(1, 0);
        let typed_names = FileTypedNames::build(&[TypedNameRecord {
            name: "obj".to_string(),
            declared_type: "Foo".to_string(),
            scope: NameScope::Local {
                enclosing_method: method_symbol,
            },
        }]);
        let name_index = RepoNameIndex::build(&[]);
        let type_index = TypeIndex::build(&[]);

        let resolved = resolve_receiver_type(
            &ReceiverExpr::Identifier("obj".to_string()),
            Some("Caller"),
            Some(method_symbol),
            &typed_names,
            &name_index,
            &type_index,
        );
        assert_eq!(resolved, ReceiverEvidence::Positive("Foo".to_string()));
    }

    /// AC2: THE central discriminating case named in the story --
    /// `auth.realm().requireX()`'s receiver (as seen from `requireX`) is
    /// `Chained { method_name: "realm", receiver: Identifier("auth") }`.
    /// `auth`'s declared type is `Auth`; `Auth.realm()` declares return
    /// type `Realm` -- the resolved receiver type must be `"Realm"`,
    /// proving the chain was followed, not just the base.
    #[test]
    fn resolve_receiver_type_follows_a_chained_call_through_a_declared_return_type() {
        const AUTH_FILE_ID: u32 = 5;
        let mut auth_file = LocalIndex::new();
        auth_file
            .declarations
            .push(method_decl("realm", AUTH_FILE_ID, 0));
        auth_file.method_owners.push(MethodOwnerRecord {
            method_symbol: make_symbol_id(AUTH_FILE_ID, 0),
            enclosing_type: "Auth".to_string(),
        });
        auth_file.method_return_types.push(MethodReturnTypeRecord {
            method_symbol: make_symbol_id(AUTH_FILE_ID, 0),
            return_type: "Realm".to_string(),
        });
        let files = vec![FileForBind {
            file_id: AUTH_FILE_ID,
            language: "java".to_string(),
            index: auth_file,
        }];
        let name_index = RepoNameIndex::build(&files);
        let type_index = TypeIndex::build(&files);

        let method_symbol = make_symbol_id(9, 0);
        let typed_names = FileTypedNames::build(&[TypedNameRecord {
            name: "auth".to_string(),
            declared_type: "Auth".to_string(),
            scope: NameScope::Local {
                enclosing_method: method_symbol,
            },
        }]);

        let receiver = ReceiverExpr::Chained {
            method_name: "realm".to_string(),
            receiver: Box::new(ReceiverExpr::Identifier("auth".to_string())),
        };
        let resolved = resolve_receiver_type(
            &receiver,
            Some("Caller"),
            Some(method_symbol),
            &typed_names,
            &name_index,
            &type_index,
        );
        assert_eq!(resolved, ReceiverEvidence::Positive("Realm".to_string()));
    }

    /// P1-4 (#1898 code review, AC3): `TimeUtil.parse("a")`'s receiver is
    /// `ReceiverExpr::Identifier("TimeUtil")` -- a CLASS NAME, not a
    /// local/field/parameter, so `typed_names.lookup` has no evidence for
    /// it at all. Before this fix, that meant `resolve_receiver_type`
    /// returned `None`, so no receiver-type narrowing ever ran and the
    /// call fanned out to every same-named method in the repo (#1898's
    /// own bug report). `TimeUtil` IS a known in-repo type (recorded via
    /// `type_nesting`), so the receiver must resolve to `"TimeUtil"`
    /// itself.
    ///
    /// #1910 SALVAGE: a round 6/7 attempt promoted this fallback to
    /// `Positive` for a type declared/imported by the SAME calling file,
    /// but round 7's review proved even that narrower substrate rests on
    /// `FileTypedNames::lookup`'s `Missing` result, which is not always a
    /// genuine absence (the captured-local scope-key problem) -- so this
    /// fallback stays `Advisory` permanently, exactly as it shipped
    /// before any #1910 attempt.
    #[test]
    fn resolve_receiver_type_resolves_a_static_type_identifier_via_known_type_names() {
        let files = vec![FileForBind {
            file_id: 20,
            language: "java".to_string(),
            index: {
                let mut index = LocalIndex::new();
                index.type_nesting.push(
                    crate::graph::extract::local_index::TypeNestingRecord {
                        type_name: "TimeUtil".to_string(),
                        top_level_type: "TimeUtil".to_string(),
                    },
                );
                index
            },
        }];
        let name_index = RepoNameIndex::build(&files);
        let type_index = TypeIndex::build(&files);
        let typed_names = FileTypedNames::build(&[]);

        let resolved = resolve_receiver_type(
            &ReceiverExpr::Identifier("TimeUtil".to_string()),
            Some("Caller"),
            None,
            &typed_names,
            &name_index,
            &type_index,
        );
        assert_eq!(
            resolved,
            ReceiverEvidence::Advisory("TimeUtil".to_string()),
            "a bare identifier resolved only via the is_known_type_name fallback (no direct \
             typed-name evidence) must be tagged Advisory, never Positive"
        );
    }

    /// Negative control: an identifier that is neither a typed
    /// local/field/parameter NOR a known in-repo type name must still
    /// resolve to `None` -- never a guessed type (Rule 2, anti-fallback).
    #[test]
    fn resolve_receiver_type_returns_none_for_an_identifier_with_no_evidence_at_all() {
        let name_index = RepoNameIndex::build(&[]);
        let type_index = TypeIndex::build(&[]);
        let typed_names = FileTypedNames::build(&[]);

        let resolved = resolve_receiver_type(
            &ReceiverExpr::Identifier("neverDeclaredAnywhere".to_string()),
            Some("Caller"),
            None,
            &typed_names,
            &name_index,
            &type_index,
        );
        assert_eq!(resolved, ReceiverEvidence::None);
    }

    /// #1910 prerequisite 2 (round4-findings.md finding 2): `is_pseudo_type`
    /// was applied only to the BASE identifier's declared-type string, not
    /// to a chain step's own resolved return type. `auth.realm()`'s
    /// receiver chain here is `Chained { method_name: "realm", receiver:
    /// Identifier("auth") }`; `auth`'s declared type is the real, positive
    /// `"Auth"`, but `Auth.realm()`'s declared return type is a bare
    /// GENERIC TYPE PARAMETER name (`"T"`, e.g. `<T> T realm()`) -- not a
    /// real class this binder can narrow against. Before this fix, the
    /// chain-following loop trusted `next_type` verbatim, promoting `"T"`
    /// straight into `Positive("T")`; that fabricated type could then
    /// coincidentally collide with an unrelated in-repo class literally
    /// named `T` and hard-delete a real candidate. The fix: every chain
    /// step's resolved type must clear the SAME `is_pseudo_type` guard the
    /// base identifier already does.
    #[test]
    fn resolve_receiver_type_rejects_a_pseudo_type_returned_by_an_intermediate_chain_step() {
        const AUTH_FILE_ID: u32 = 5;
        let mut auth_file = LocalIndex::new();
        auth_file
            .declarations
            .push(method_decl("realm", AUTH_FILE_ID, 0));
        auth_file.method_owners.push(MethodOwnerRecord {
            method_symbol: make_symbol_id(AUTH_FILE_ID, 0),
            enclosing_type: "Auth".to_string(),
        });
        auth_file.method_return_types.push(MethodReturnTypeRecord {
            method_symbol: make_symbol_id(AUTH_FILE_ID, 0),
            return_type: "T".to_string(),
        });
        auth_file.type_parameter_names.push("T".to_string());
        let files = vec![FileForBind {
            file_id: AUTH_FILE_ID,
            language: "java".to_string(),
            index: auth_file,
        }];
        let name_index = RepoNameIndex::build(&files);
        let type_index = TypeIndex::build(&files);

        let method_symbol = make_symbol_id(9, 0);
        let typed_names = FileTypedNames::build(&[TypedNameRecord {
            name: "auth".to_string(),
            declared_type: "Auth".to_string(),
            scope: NameScope::Local {
                enclosing_method: method_symbol,
            },
        }]);

        let receiver = ReceiverExpr::Chained {
            method_name: "realm".to_string(),
            receiver: Box::new(ReceiverExpr::Identifier("auth".to_string())),
        };
        let resolved = resolve_receiver_type(
            &receiver,
            Some("Caller"),
            Some(method_symbol),
            &typed_names,
            &name_index,
            &type_index,
        );
        assert_eq!(
            resolved,
            ReceiverEvidence::None,
            "a chain step whose declared return type is a bare generic type parameter name \
             must never resolve to that pseudo-type, even though the chain's BASE was Positive"
        );
    }

    /// P1-A (#1898 code review round 2, epic #1906): `var x = new Svc();`
    /// records `x`'s declared type as the LITERAL STRING `"var"` (the
    /// extractor performs no type inference -- see `java_type_names::
    /// resolve_type_node_base_name`). `"var"` is a reserved Java keyword,
    /// never a legal class name, so it must never drive `apply_receiver_
    /// type_narrowing`'s hard filter -- `resolve_receiver_type` must
    /// return `None` (missing evidence, narrowing skipped) rather than
    /// this pseudo-type string.
    #[test]
    fn resolve_receiver_type_rejects_the_var_pseudo_type_and_returns_none() {
        let method_symbol = make_symbol_id(1, 0);
        let typed_names = FileTypedNames::build(&[TypedNameRecord {
            name: "svc".to_string(),
            declared_type: "var".to_string(),
            scope: NameScope::Local {
                enclosing_method: method_symbol,
            },
        }]);
        let name_index = RepoNameIndex::build(&[]);
        let type_index = TypeIndex::build(&[]);

        let resolved = resolve_receiver_type(
            &ReceiverExpr::Identifier("svc".to_string()),
            Some("Caller"),
            Some(method_symbol),
            &typed_names,
            &name_index,
            &type_index,
        );
        assert_eq!(
            resolved,
            ReceiverEvidence::None,
            "a var-typed receiver must never resolve to the literal string \"var\""
        );
    }

    /// P1-A: `<T extends Svc> void run(T t) { t.ping(); }` records `t`'s
    /// declared type as `"T"` -- a generic TYPE PARAMETER name, never a
    /// concrete class this binder can narrow against. `TypeIndex` knows
    /// `"T"` was declared as a type parameter (via `type_parameter_names`,
    /// populated from the method's own `<T extends Svc>` clause), so
    /// `resolve_receiver_type` must reject it the same way it rejects
    /// `"var"`.
    #[test]
    fn resolve_receiver_type_rejects_a_generic_type_parameter_name_and_returns_none() {
        let method_symbol = make_symbol_id(1, 0);
        let typed_names = FileTypedNames::build(&[TypedNameRecord {
            name: "t".to_string(),
            declared_type: "T".to_string(),
            scope: NameScope::Local {
                enclosing_method: method_symbol,
            },
        }]);
        let mut index = LocalIndex::new();
        index.type_parameter_names.push("T".to_string());
        let files = vec![FileForBind {
            file_id: 1,
            language: "java".to_string(),
            index,
        }];
        let name_index = RepoNameIndex::build(&files);
        let type_index = TypeIndex::build(&files);

        let resolved = resolve_receiver_type(
            &ReceiverExpr::Identifier("t".to_string()),
            Some("Caller"),
            Some(method_symbol),
            &typed_names,
            &name_index,
            &type_index,
        );
        assert_eq!(
            resolved,
            ReceiverEvidence::None,
            "a generic-type-parameter-typed receiver must never resolve to the type \
             parameter's own bare name"
        );
    }

    /// P1-B (#1898 code review round 2, epic #1906): an untyped lambda
    /// parameter (`(x) -> x.foo()`, no explicit type annotation) records
    /// an EMPTY-STRING declared-type sentinel (`java_receiver::lambda_
    /// param_typed_names`'s own doc comment) -- the name IS a genuine
    /// local binding (so `typed_names.lookup` correctly returns `Some`,
    /// never falling through to the coincidental-type-name fallback), but
    /// the empty string itself is exactly as much a pseudo-type as `"var"`
    /// and must never drive a hard receiver-type filter either.
    #[test]
    fn resolve_receiver_type_rejects_the_empty_string_sentinel_for_an_untyped_local_binding_and_returns_none(
    ) {
        let method_symbol = make_symbol_id(1, 0);
        let typed_names = FileTypedNames::build(&[TypedNameRecord {
            name: "x".to_string(),
            declared_type: String::new(),
            scope: NameScope::Local {
                enclosing_method: method_symbol,
            },
        }]);
        let name_index = RepoNameIndex::build(&[]);
        let type_index = TypeIndex::build(&[]);

        let resolved = resolve_receiver_type(
            &ReceiverExpr::Identifier("x".to_string()),
            Some("Caller"),
            Some(method_symbol),
            &typed_names,
            &name_index,
            &type_index,
        );
        assert_eq!(
            resolved,
            ReceiverEvidence::None,
            "an untyped local binding's empty-string sentinel must never resolve to a \
             (fabricated, empty-named) receiver type"
        );
    }

    /// P1-B (#1898 code review round 2, epic #1906): an identifier with NO
    /// typed-name evidence in THIS file (e.g. an inner class reading its
    /// outer class's field, or an inherited field declared in a different
    /// file -- `typed_names.lookup` misses both) must resolve via the
    /// REPO-WIDE `unambiguous_field_type` substrate to the field's REAL
    /// declared type -- never fall back to `is_known_type_name` and guess
    /// the coincidentally same-named type instead.
    #[test]
    fn resolve_receiver_type_resolves_a_type_name_fallback_to_the_unambiguous_field_type_when_the_identifier_is_also_a_known_field_name(
    ) {
        let mut type_decl_file = LocalIndex::new();
        type_decl_file.type_nesting.push(
            crate::graph::extract::local_index::TypeNestingRecord {
                type_name: "handle".to_string(),
                top_level_type: "handle".to_string(),
            },
        );
        let mut field_decl_file = LocalIndex::new();
        field_decl_file.typed_names.push(TypedNameRecord {
            name: "handle".to_string(),
            declared_type: "Caller".to_string(),
            scope: NameScope::Field {
                enclosing_type: "Outer".to_string(),
            },
        });
        let files = vec![
            FileForBind {
                file_id: 1,
                language: "java".to_string(),
                index: type_decl_file,
            },
            FileForBind {
                file_id: 2,
                language: "java".to_string(),
                index: field_decl_file,
            },
        ];
        let name_index = RepoNameIndex::build(&files);
        let type_index = TypeIndex::build(&files);
        // This file's OWN typed_names carries no evidence for "handle" at
        // all -- mirroring the real gap (the field lives on "Outer" in a
        // DIFFERENT file/scope than the caller's own).
        let typed_names = FileTypedNames::build(&[]);

        let resolved = resolve_receiver_type(
            &ReceiverExpr::Identifier("handle".to_string()),
            Some("Inner"),
            None,
            &typed_names,
            &name_index,
            &type_index,
        );
        assert_eq!(
            resolved,
            ReceiverEvidence::Advisory("Caller".to_string()),
            "the field's own unambiguous declared type must win, never the coincidentally \
             same-named type -- but only as ADVISORY evidence, never Positive, since it came \
             from the unambiguous_field_type fallback rather than a direct typed-name hit"
        );
    }

    /// P1-B: when the SAME bare name is used as a field with genuinely
    /// CONFLICTING declared types elsewhere in the repo (an unrelated
    /// field, not the same one), `unambiguous_field_type` has no answer
    /// and the static-type-name fallback must ALSO stay blocked (`name`
    /// is still a known field name) -- `None`, never a guess either way.
    #[test]
    fn resolve_receiver_type_returns_none_when_the_identifier_is_a_field_name_with_conflicting_declared_types(
    ) {
        let mut field_decl_file_a = LocalIndex::new();
        field_decl_file_a.typed_names.push(TypedNameRecord {
            name: "handle".to_string(),
            declared_type: "Caller".to_string(),
            scope: NameScope::Field {
                enclosing_type: "Outer".to_string(),
            },
        });
        let mut field_decl_file_b = LocalIndex::new();
        field_decl_file_b.typed_names.push(TypedNameRecord {
            name: "handle".to_string(),
            declared_type: "SomethingElse".to_string(),
            scope: NameScope::Field {
                enclosing_type: "Unrelated".to_string(),
            },
        });
        let files = vec![
            FileForBind {
                file_id: 1,
                language: "java".to_string(),
                index: field_decl_file_a,
            },
            FileForBind {
                file_id: 2,
                language: "java".to_string(),
                index: field_decl_file_b,
            },
        ];
        let name_index = RepoNameIndex::build(&files);
        let type_index = TypeIndex::build(&files);
        let typed_names = FileTypedNames::build(&[]);

        let resolved = resolve_receiver_type(
            &ReceiverExpr::Identifier("handle".to_string()),
            Some("Inner"),
            None,
            &typed_names,
            &name_index,
            &type_index,
        );
        assert_eq!(
            resolved,
            ReceiverEvidence::None,
            "conflicting declared types for the same field bare name must never resolve to \
             either guessed type"
        );
    }

    /// #1910 round 6, finding 1's remediation, at the `resolve_receiver_
    /// type` level: an AMBIGUOUS local binding (two sibling-block locals
    /// with different declared types) must resolve `ReceiverEvidence::
    /// None` even when a coincidentally same-named class IS a known
    /// in-repo type (so the `is_known_type_name` Advisory fallback would
    /// otherwise fire) -- proving `Ambiguous` never falls through to
    /// either open-world fallback.
    #[test]
    fn ambiguous_local_binding_never_falls_through_to_the_static_type_name_fallback() {
        let method_symbol = make_symbol_id(1, 0);
        let typed_names = FileTypedNames::build(&[
            TypedNameRecord {
                name: "handle".to_string(),
                declared_type: "Target".to_string(),
                scope: NameScope::Local {
                    enclosing_method: method_symbol,
                },
            },
            TypedNameRecord {
                name: "handle".to_string(),
                declared_type: "String".to_string(),
                scope: NameScope::Local {
                    enclosing_method: method_symbol,
                },
            },
        ]);
        let mut index = LocalIndex::new();
        index.type_nesting.push(
            crate::graph::extract::local_index::TypeNestingRecord {
                type_name: "handle".to_string(),
                top_level_type: "handle".to_string(),
            },
        );
        let files = vec![FileForBind {
            file_id: 1,
            language: "java".to_string(),
            index,
        }];
        let name_index = RepoNameIndex::build(&files);
        let type_index = TypeIndex::build(&files);
        // The coincidental class "handle" is a known in-repo type name
        // (via `type_nesting`) -- the strongest possible case for the
        // `is_known_type_name` Advisory fallback to misfire on.

        let resolved = resolve_receiver_type(
            &ReceiverExpr::Identifier("handle".to_string()),
            Some("Target"),
            Some(method_symbol),
            &typed_names,
            &name_index,
            &type_index,
        );
        assert_eq!(
            resolved,
            ReceiverEvidence::None,
            "an AMBIGUOUS local binding must never fall through to the static-type-name \
             fallback, even though the coincidentally same-named class 'handle' is a known \
             in-repo type"
        );
    }
}
