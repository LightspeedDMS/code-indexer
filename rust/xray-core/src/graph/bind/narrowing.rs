//! F5 (#1873/#1875 rework, split out of `resolve.rs` to stay under the
//! project's 1000-line-per-file limit): the individual candidate-set
//! narrowing/filtering/expansion passes AC4 Levels 1/2/4, AC1/AC3
//! receiver-type/same-class/super-class narrowing, D2 (visibility), and
//! AC1 inheritance-family expansion apply. `resolve.rs`'s `resolve_reference`
//! is the ONLY caller -- this module has no other entry point and no
//! ordering opinion of its own; the pipeline order lives in `resolve.rs`.

use super::name_index::DeclInfo;
use super::REF_KIND_INVOCATION;
use crate::graph::identity::SymbolId;
use crate::graph::reasons;

/// AC2 (Story #1793, S4): does `decl`'s declared arity accept a call
/// passing `arg_count` arguments? A varargs declaration (`Foo... x` as
/// its last formal parameter) accepts any `arg_count >= param_count - 1`
/// (the fixed leading parameters, plus zero or more trailing varargs) --
/// `saturating_sub` avoids an unsigned underflow if `param_count` were
/// ever 0 (never true for a genuine varargs method, which always has at
/// least its one varargs parameter, but this keeps the arithmetic total
/// rather than trusting that invariant). A non-varargs declaration keeps
/// the pre-existing exact-equality check.
pub(super) fn param_count_matches_arity(decl: &DeclInfo, arg_count: usize) -> bool {
    let Some(param_count) = decl.param_count else {
        return false;
    };
    if decl.is_varargs {
        arg_count >= param_count.saturating_sub(1)
    } else {
        param_count == arg_count
    }
}

/// AC4 Level 1 ("+arity"): tags every candidate whose declared
/// `param_count` matches `arg_count` with `ARITY_MATCH`, and narrows the
/// set to exactly those matches -- including down to EMPTY when nothing
/// matches.
///
/// Bug #1898 (P1 of epic #1906): the pre-fix version of this function
/// treated an empty match as "unsafe to narrow" and silently kept the
/// ENTIRE bare-name pool instead -- when the call site's real target is
/// external to the repo (e.g. a JDK method) and no in-repo declaration
/// shares its arity, that fallback fabricated an edge to a wrong-arity
/// in-repo candidate. The correct candidate set when `arg_count` is known
/// and nothing matches it is EMPTY, never the unfiltered pool. `arg_count:
/// None` (unknown arity) is the only case that still skips narrowing
/// entirely; varargs/exact-arity semantics both live in
/// `param_count_matches_arity`, which this function trusts as-is.
///
/// P2-1 (#1898 code review, Anti-Silent-Failure): `param_count_matches_
/// arity` returns `false` for a candidate whose OWN `param_count` is
/// `None` -- that is correct for deciding whether to tag `ARITY_MATCH`
/// (missing evidence is never a confirmed match), but WRONG for deciding
/// whether to DELETE the candidate: missing arity evidence is not proof
/// of a mismatch, and retaining is the same "missing/ambiguous evidence
/// retains the candidate" doctrine `apply_private_visibility_filter`
/// already documents. Latent for the Java extractor today (every Java
/// method declaration carries a real `param_count`), but Kotlin is P4 of
/// this same epic -- an extractor that omits param counts must never
/// silently zero every arity-known call as a side effect of this
/// function's hard-empty fix.
pub(super) fn apply_arity_narrowing(
    candidates: &mut Vec<(DeclInfo, u16)>,
    arg_count: Option<usize>,
) {
    let Some(arg_count) = arg_count else { return };
    let retained: Vec<usize> = candidates
        .iter()
        .enumerate()
        .filter(|(_, (d, _))| d.param_count.is_none() || param_count_matches_arity(d, arg_count))
        .map(|(i, _)| i)
        .collect();
    for &i in &retained {
        if candidates[i].0.param_count.is_some() {
            candidates[i].1 |= reasons::ARITY_MATCH;
        }
    }
    *candidates = retained
        .into_iter()
        .map(|i| candidates[i].clone())
        .collect();
}

/// AC2 (Story #1793, S4): `decl`'s declared parameter TYPE at call-site
/// argument `position`, or `None` if `decl` carries no param-type
/// evidence at all (non-Java, or an extraction gap -- never fabricated).
/// For a varargs declaration, every position from `param_types.len() - 1`
/// onward maps to the SAME (last, element) declared type -- the varargs
/// parameter itself.
fn declared_type_at(decl: &DeclInfo, position: usize) -> Option<&str> {
    if decl.param_types.is_empty() {
        return None;
    }
    let index = if decl.is_varargs {
        position.min(decl.param_types.len() - 1)
    } else {
        position
    };
    decl.param_types.get(index).map(|s| s.as_str())
}

/// AC2: is `shape` DEFINITELY incompatible with `declared_type`? Only the
/// closed, fixed set of Java primitive/String/boxed-numeric/boolean/char
/// type NAMES is used here -- this is closed-world-safe (a
/// `StringLiteral` genuinely cannot bind to `int` in Java, full stop),
/// unlike a named class/interface type (open-world: this repo's
/// heuristic inheritance index can never prove "these two named types are
/// definitely unrelated"). `Cast`/`Constructor`/`Lambda`/`MethodReference`/
/// `Other` therefore never report incompatibility here -- see
/// `apply_overload_shape_narrowing`'s named-type PREFERENCE step for how
/// those contribute positive (not exclusionary) evidence instead.
fn literal_shape_is_incompatible(
    shape: &crate::graph::extract::local_index::ArgShape,
    declared_type: &str,
) -> bool {
    use crate::graph::extract::local_index::ArgShape;
    let is_numeric = matches!(
        declared_type,
        "int"
            | "long"
            | "double"
            | "float"
            | "short"
            | "byte"
            | "Integer"
            | "Long"
            | "Double"
            | "Float"
            | "Short"
            | "Byte"
    );
    let is_boolean = matches!(declared_type, "boolean" | "Boolean");
    let is_char = matches!(declared_type, "char" | "Character");
    let is_primitive = matches!(
        declared_type,
        "int" | "long" | "double" | "float" | "short" | "byte" | "boolean" | "char"
    );
    match shape {
        ArgShape::StringLiteral => is_numeric || is_boolean || is_char,
        ArgShape::NumericLiteral => declared_type == "String" || is_boolean || is_char,
        ArgShape::BooleanLiteral => declared_type == "String" || is_numeric || is_char,
        ArgShape::NullLiteral => is_primitive,
        ArgShape::Cast(_)
        | ArgShape::Constructor(_)
        | ArgShape::Lambda
        | ArgShape::MethodReference
        | ArgShape::Other => false,
    }
}

/// AC2: true when ANY call-site argument position hits a DEFINITE
/// literal-shape mismatch against `decl`'s declared parameter type at
/// that position (positions with no declared-type evidence, or a
/// non-discriminating shape, never count).
fn candidate_has_definite_mismatch(
    decl: &DeclInfo,
    arg_shapes: &[crate::graph::extract::local_index::ArgShape],
) -> bool {
    arg_shapes.iter().enumerate().any(|(i, shape)| {
        declared_type_at(decl, i).is_some_and(|t| literal_shape_is_incompatible(shape, t))
    })
}

/// AC2: how many argument positions carry a `Cast`/`Constructor` shape
/// whose named type EXACTLY equals `decl`'s declared type at that
/// position -- positive, open-world-safe evidence (see
/// `literal_shape_is_incompatible`'s docs on why a NAME MISMATCH here is
/// never treated as exclusionary).
fn named_type_match_count(
    decl: &DeclInfo,
    arg_shapes: &[crate::graph::extract::local_index::ArgShape],
) -> usize {
    use crate::graph::extract::local_index::ArgShape;
    arg_shapes
        .iter()
        .enumerate()
        .filter(|(i, shape)| {
            let target = match shape {
                ArgShape::Cast(t) | ArgShape::Constructor(t) => Some(t.as_str()),
                _ => None,
            };
            target.is_some() && declared_type_at(decl, *i) == target
        })
        .count()
}

/// AC2 (Story #1793, S4) Level 4 "overload discrimination": candidate-set
/// REDUCTION beyond arity, never exact resolution. Two independent
/// passes, each following the SAME "narrow only if safe" pattern as
/// `apply_import_context_narrowing` (never empty the set, never a no-op
/// "narrow" to the same set already there) -- deliberately DIFFERENT from
/// `apply_arity_narrowing` since #1898: literal-shape/named-type evidence
/// is a weaker, open-world heuristic (see `literal_shape_is_incompatible`'s
/// doc comment), so an empty match here stays a genuine "inconclusive",
/// never treated as proof of an external target the way a hard arity
/// mismatch is:
/// (1) exclude candidates with a definite literal-shape mismatch;
/// (2) among survivors, prefer the highest cast/constructor named-type
/// match count. `OVERLOAD_ARG_TYPE_MATCH` is marked on every surviving
/// candidate that carried genuine `param_types` evidence to check against
/// -- never on a candidate with no such evidence at all.
pub(super) fn apply_overload_shape_narrowing(
    candidates: &mut Vec<(DeclInfo, u16)>,
    arg_shapes: &[crate::graph::extract::local_index::ArgShape],
) {
    if arg_shapes.is_empty() {
        return;
    }
    let surviving: Vec<usize> = candidates
        .iter()
        .enumerate()
        .filter(|(_, (d, _))| !candidate_has_definite_mismatch(d, arg_shapes))
        .map(|(i, _)| i)
        .collect();
    if !surviving.is_empty() && surviving.len() < candidates.len() {
        *candidates = surviving
            .into_iter()
            .map(|i| candidates[i].clone())
            .collect();
    }

    let max_score = candidates
        .iter()
        .map(|(d, _)| named_type_match_count(d, arg_shapes))
        .max()
        .unwrap_or(0);
    if max_score > 0 {
        let preferred: Vec<usize> = candidates
            .iter()
            .enumerate()
            .filter(|(_, (d, _))| named_type_match_count(d, arg_shapes) == max_score)
            .map(|(i, _)| i)
            .collect();
        if preferred.len() < candidates.len() {
            *candidates = preferred
                .into_iter()
                .map(|i| candidates[i].clone())
                .collect();
        }
    }

    for (decl, bits) in candidates.iter_mut() {
        if !decl.param_types.is_empty() {
            *bits |= reasons::OVERLOAD_ARG_TYPE_MATCH;
        }
    }
}

/// AC4 Level 2 ("+import context"): narrows to candidates reachable via
/// `SAME_FILE`/`SAME_PACKAGE`/`IMPORTED`/`STATIC_IMPORT`/`WILDCARD_IMPORT`
/// evidence, when that is a genuine, non-degenerate narrowing (at least
/// one reachable candidate, and not all of them already were).
pub(super) fn apply_import_context_narrowing(candidates: &mut Vec<(DeclInfo, u16)>) {
    const CONTEXT_MASK: u16 = reasons::SAME_FILE
        | reasons::SAME_PACKAGE
        | reasons::IMPORTED
        | reasons::STATIC_IMPORT
        | reasons::WILDCARD_IMPORT;
    let reachable: Vec<usize> = candidates
        .iter()
        .enumerate()
        .filter(|(_, (_, bits))| bits & CONTEXT_MASK != 0)
        .map(|(i, _)| i)
        .collect();
    if reachable.is_empty() || reachable.len() == candidates.len() {
        return;
    }
    *candidates = reachable
        .into_iter()
        .map(|i| candidates[i].clone())
        .collect();
}

/// F6 (#1873/#1875 rework, LOW): shared match-and-collect block every
/// enclosing-type-based narrowing pass below (receiver-type, same-class-
/// or-super, super-class) applied as its own copy -- indices of
/// `candidates` whose `enclosing_type` is in `allowed`. Callers own the
/// bit-tagging and whether/how to apply the narrowing (soft-skip-on-empty
/// vs. hard-always-replace); this only does the filtering.
fn indices_matching_enclosing_type(
    candidates: &[(DeclInfo, u16)],
    allowed: &std::collections::HashSet<String>,
) -> Vec<usize> {
    candidates
        .iter()
        .enumerate()
        .filter(|(_, (d, _))| {
            d.enclosing_type
                .as_deref()
                .is_some_and(|t| allowed.contains(t))
        })
        .map(|(i, _)| i)
        .collect()
}

/// AC1 (Story #1806, S2b -- FINDING 3's missing narrowing): TAGS
/// candidates whose `enclosing_type` matches the resolved RECEIVER type
/// (`receiver_type`, computed by the caller via
/// `super::receiver::resolve_receiver_type` from the call's own
/// `ReceiverExpr`) or one of that type's transitive supertypes -- e.g.
/// `obj.doSomething()` where `obj`'s declared type is `Foo` tags
/// declarations of `doSomething` on `Foo` or an ancestor of `Foo` with
/// `RECEIVER_TYPE_MATCH`. `None` means the receiver's type could not be
/// resolved (unknown variable, unsupported receiver shape, ambiguous
/// chained return type) -- never a guessed tag.
///
/// #1898 SCOPE SPLIT (epic #1906, round-4 review, `.analysis/
/// 1898-review-rounds/round4-findings.md`): this function is TAG-ONLY --
/// it may NEVER remove a candidate from the set, on an empty match or a
/// non-empty one, under Positive evidence or Advisory. Rounds 1-4 each
/// tried a hard-empty variant of this filter (round 1: unconditional;
/// round 4: gated on `receiver::ReceiverEvidence::is_positive`) and each
/// was rejected for the SAME root shape: deleting a candidate on receiver-
/// type evidence that turned out not to be closed-world. Round 4's own
/// review (findings 1-2) proved `Positive` itself is not closed-world --
/// `receiver::FileTypedNames` keys locals by `(enclosing_method, name)`
/// while Java scopes by BLOCK, so two same-named locals in different
/// blocks of the SAME method collide, last-write-wins, and a hard filter
/// keyed on the wrong one deletes a genuinely live candidate (round4-
/// findings.md findings 1, 3, 4 are all "matching non-empty -> WRONG
/// subset", a path NEITHER evidence tier ever guarded). Four straight
/// rounds of enumerating one more binding form or evidence tier could not
/// converge on a safe hard-empty contract, so hard receiver-type
/// narrowing is deferred whole-cloth to a follow-up issue (named in
/// `docs/xray-architecture.md`'s candidate-admission section) that can
/// design a genuinely closed-world evidence substrate from scratch,
/// rather than patching this one again. Until then, receiver-type
/// evidence contributes only the `RECEIVER_TYPE_MATCH` reason bit (and
/// hence `Confidence::ReceiverType`) -- real signal for a caller that
/// wants it, never a candidate-set exclusion.
///
/// `try_unique_name_shortcut` (`resolve.rs`) is a SEPARATE, narrower
/// mechanism this change does not touch: it still declines to admit a
/// sole candidate whose enclosing type does not match a POSITIVE receiver
/// type. That is an ADMISSION decision (whether to take a shortcut that
/// bypasses the rest of the pipeline), not a DELETION of an already-built
/// candidate set -- declining the shortcut simply falls through to the
/// (now tag-only) full pipeline, which keeps the pool. The two are
/// orthogonal: this function governs the general N-candidate case, the
/// shortcut governs only the N == 1 case before this function ever runs.
///
/// PRESERVE (#1882/#1883): still skips tagging entirely when
/// `receiver_type`'s OWN supertype evidence is recorded incomplete
/// (`type_index.has_incomplete_supertype_evidence`, same substrate
/// `apply_super_class_narrowing` already consults for this exact reason)
/// -- `supertypes_of` may be missing the real supertype a genuine match
/// lives on, so an incomplete `allowed` set would under-tag (never
/// over-tag, since tagging never removes anything either way) rather than
/// report confidently on partial evidence.
pub(super) fn apply_receiver_type_narrowing(
    candidates: &mut [(DeclInfo, u16)],
    receiver_type: Option<&str>,
    type_index: &super::families::TypeIndex,
) {
    let Some(receiver_type) = receiver_type else {
        return;
    };
    if type_index.has_incomplete_supertype_evidence(receiver_type) {
        return;
    }
    let mut allowed = type_index.supertypes_of(receiver_type);
    allowed.insert(receiver_type.to_string());
    let matching = indices_matching_enclosing_type(candidates, &allowed);
    for &i in &matching {
        candidates[i].1 |= reasons::RECEIVER_TYPE_MATCH;
    }
    // TAG-ONLY (#1898 scope split, epic #1906): deliberately no narrowing
    // step here at all, empty match or not -- see this function's own doc
    // comment above.
}

/// AC3 (Story #1806, S2b): "unqualified calls resolve against the
/// enclosing class and its supertypes first". `same_class_context` is
/// `Some(enclosing_type)` ONLY when the caller (`super::resolve_site`)
/// determined this reference is an unqualified/`this`/`super` call whose
/// enclosing type is known -- `None` for a qualified call (a receiver-type
/// match is a DIFFERENT evidence path, AC1) or a reference kind AC3 does
/// not apply to (type references/constructions have no "calling class").
///
/// Bug #1898 (P1 of epic #1906) -- KEPT SOFT, deliberately DIFFERENT from
/// `apply_arity_narrowing` (the only sibling pass still hard-empty after
/// the #1898 scope split -- `apply_receiver_type_narrowing` is TAG-ONLY,
/// see its own doc comment): this pass's
/// `allowed` set (`{enclosing_type} U supertypes_of(enclosing_type)`)
/// covers only DIRECT inheritance evidence. Java's real unqualified-call
/// scope is strictly larger than that in two ways this substrate does
/// not model at all:
///
/// 1. **Lexically enclosing types.** An inner/anonymous/static-nested/
///    local class's bare call can resolve against ANY lexically enclosing
///    type (not just its own declared supertypes) -- e.g. an inner class
///    calling its outer class's `private` method. `enclosing_type` here is
///    the caller's OWN immediate type (`"Inner"`), which has no
///    inheritance relationship whatsoever to the outer type (`"Outer"`)
///    that legitimately owns the target.
/// 2. **Static imports.** `import static util.Util.helper;` then a bare
///    `helper()` call resolves against `Util`, which likewise has no
///    inheritance relationship to the caller's own enclosing type.
///
/// An empty `matching` set here is therefore NOT reliable evidence of an
/// external target the way it is for arity (a call's argument count is
/// exhaustive) -- it can just as easily mean "the real target lives in a
/// lexically enclosing type or arrived via a static import, neither of
/// which `allowed` can see". A/B-probed end-to-end on real javac-valid
/// source (inner/anonymous/static-nested/local class calling an outer
/// `private` method; a static-imported in-repo method): hard-narrowing
/// here flips `is_definitely_dead_code` from `Some(false)` to a FALSE
/// `Some(true)` on a live private target -- the exact outcome epic #1786
/// declared structurally impossible -- while the arity hard-narrowing
/// (kept, see its own doc) produces no such regression when isolated.
/// Widening `allowed` to
/// the caller's full lexical nest (via `TypeIndex::top_level_of`/
/// `type_nesting`, the same substrate `apply_private_visibility_filter`
/// already uses) plus exempting `reasons::STATIC_IMPORT` candidates is the
/// real long-term fix and is intentionally deferred to a follow-up issue
/// (named in `docs/xray-architecture.md`'s candidate-admission section)
/// -- this pass keeps the pre-#1898 soft "no match -> keep the pool"
/// fallback rather than risk a live->dead regression for a narrower win.
///
/// Known consequence, recorded rather than silently accepted: #1885
/// (`super.m()` with an implicit `Object` superclass) does NOT close under
/// this change -- `apply_super_class_narrowing` (below) still early-returns
/// on `allowed.is_empty()` for exactly that reason, unchanged by #1898.
///
/// PRESERVE (#1882/#1883): identical guard to
/// `apply_receiver_type_narrowing`'s -- when `enclosing_type`'s own
/// supertype evidence is recorded incomplete, narrowing is skipped
/// entirely and the pool is kept, since `supertypes_of` may be missing
/// the real supertype the target actually lives on.
pub(super) fn apply_same_class_or_super_narrowing(
    candidates: &mut Vec<(DeclInfo, u16)>,
    same_class_context: Option<&str>,
    type_index: &super::families::TypeIndex,
) {
    let Some(enclosing_type) = same_class_context else {
        return;
    };
    if type_index.has_incomplete_supertype_evidence(enclosing_type) {
        return;
    }
    let mut allowed = type_index.supertypes_of(enclosing_type);
    allowed.insert(enclosing_type.to_string());
    let matching = indices_matching_enclosing_type(candidates, &allowed);
    if matching.is_empty() {
        return;
    }
    for &i in &matching {
        candidates[i].1 |= reasons::SAME_CLASS_OR_SUPER;
    }
    if matching.len() < candidates.len() {
        *candidates = matching
            .into_iter()
            .map(|i| candidates[i].clone())
            .collect();
    }
}

/// D3: a genuine `super.foo()`/`super::foo` reference. `super_class_context`
/// is `Some(enclosing_type)` ONLY when the caller determined this reference
/// is a real `super` call (`ReceiverExpr::Super`) -- never set alongside
/// `same_class_context`, which stays reserved for `this`/bare calls.
///
/// Unlike `apply_same_class_or_super_narrowing`, this is a HARD filter when
/// there IS recorded superclass evidence: it deliberately does NOT skip
/// narrowing just because nothing matches. `enclosing_type` itself is never
/// added to `allowed` (that is exactly the bug this fixes -- the old
/// `SelfOrSuper` conflation resolved `super.foo()` against the enclosing
/// type, producing a false self-loop whenever the enclosing type declared
/// its own same-named override). When the enclosing type's real superclass
/// is external to the graph (no declaration anywhere in `allowed` matches),
/// this clears the candidate set entirely -- the call resolves to nothing
/// rather than falling back to an unrelated same-named candidate elsewhere
/// in the repo, which would be exactly as wrong as the self-loop: Java's
/// `super.foo()` can only ever target the actual superclass chain.
///
/// #1873/#1875 rework, F1 (HIGH, REGRESSION fix): when `allowed` itself is
/// EMPTY -- `enclosing_type` has no recorded supertype at all -- there is
/// no real evidence to narrow on, only an extraction gap (every genuine
/// `super.x()` call has SOME real superclass; an empty set here can never
/// mean "provably has no supertype", only "we never recorded one," e.g. an
/// implicit `extends Object`, or a qualified/anonymous/enum-constant
/// supertype this binder does not yet track). Narrowing on empty evidence
/// used to unconditionally wipe the WHOLE candidate set regardless of size,
/// which could delete an unrelated but genuinely reachable same-named
/// candidate the pre-fix `SelfOrSuper` narrowing would have left alone
/// (that softer pass always skips narrowing on an empty match). Falling
/// back to "leave every candidate referenced" here reproduces that
/// conservative behaviour and keeps the graph's under-report-never
/// contract intact.
pub(super) fn apply_super_class_narrowing(
    candidates: &mut Vec<(DeclInfo, u16)>,
    super_class_context: Option<&str>,
    type_index: &super::families::TypeIndex,
) {
    let Some(enclosing_type) = super_class_context else {
        return;
    };
    // N1 (#1873/#1875 second-review rework, MEDIUM regression fix): a
    // superclass/`implements` clause the extractor could not resolve for
    // `enclosing_type` means its supertype evidence is INCOMPLETE -- some
    // OTHER, correctly-resolved edge for the same type can still make
    // `supertypes_of` non-empty (e.g. a real `implements Marker` alongside
    // an unresolvable `extends`), so checking only `allowed.is_empty()`
    // below is not enough: narrowing must be skipped here, unconditionally,
    // whenever ANY supertype evidence for this type is known to be
    // incomplete, never just when the whole set happens to be empty.
    if type_index.has_incomplete_supertype_evidence(enclosing_type) {
        return;
    }
    let allowed = type_index.supertypes_of(enclosing_type);
    if allowed.is_empty() {
        return;
    }
    let matching = indices_matching_enclosing_type(candidates, &allowed);
    for &i in &matching {
        candidates[i].1 |= reasons::SAME_CLASS_OR_SUPER;
    }
    *candidates = matching
        .into_iter()
        .map(|i| candidates[i].clone())
        .collect();
}

/// D2: discard only candidates Java makes impossible: an explicitly private
/// method declared under a KNOWN different top-level type from the caller.
/// Any missing/ambiguous ownership evidence retains the candidate, preserving
/// the graph's under-reporting contract.
pub(super) fn apply_private_visibility_filter(
    candidates: &mut Vec<(DeclInfo, u16)>,
    caller_top_level: Option<&str>,
    type_index: &super::families::TypeIndex,
) {
    let Some(caller_top_level) = caller_top_level else {
        return;
    };
    candidates.retain(|(decl, _)| {
        if !decl.visibility.is_provably_not_externally_visible() {
            return true;
        }
        let Some(owner) = decl.enclosing_type.as_deref() else {
            return true;
        };
        let Some(declaration_top_level) = type_index.top_level_of(owner) else {
            return true;
        };
        declaration_top_level == caller_top_level
    });
}

/// AC1 (Story #1793, S4) Level 3 "inheritance families": a call resolving
/// to an interface method binds to the FAMILY of implementations, never
/// silently collapsed to one -- this EXPANDS the (possibly already
/// narrowed) candidate set, it never removes anything. Only
/// `REF_KIND_INVOCATION` is in scope (type references/constructions have
/// no "override" concept). `full_pool` is the ORIGINAL, un-narrowed
/// same-named pool: an implementor's own override may have already been
/// narrowed away by import-context evidence (the exact scenario this
/// exists to fix), so `overrides_of` must search the full pool, never the
/// already-narrowed `candidates`. Deduplicated by symbol so a candidate
/// already present (e.g. still surviving narrowing) is never added twice.
///
/// Memory-safety amendment: `overrides_of` itself hard-caps each
/// `interface_name`'s expansion at `families::MAX_FAMILY_SIZE` (see its
/// doc comment for the real 21.8GB-RSS incident this fixes). When ANY
/// interface processed for this reference was truncated, every surviving
/// `INHERITANCE_FAMILY` candidate on this reference (not just the ones
/// added by the truncated interface) is additionally marked
/// `reasons::FAMILY_TRUNCATED` -- the family this candidate set represents
/// is known-INCOMPLETE, and that must be visible on the result rather than
/// silently dropped.
pub(super) fn apply_inheritance_family_expansion(
    candidates: &mut Vec<(DeclInfo, u16)>,
    ref_kind: u8,
    full_pool: &[DeclInfo],
    type_index: &super::families::TypeIndex,
) {
    if ref_kind != REF_KIND_INVOCATION {
        return;
    }
    let mut existing_symbols: std::collections::HashSet<SymbolId> =
        candidates.iter().map(|(d, _)| d.symbol).collect();
    let interface_names: Vec<String> = candidates
        .iter()
        .filter_map(|(d, _)| d.enclosing_type.as_deref())
        .filter(|t| type_index.is_interface(t))
        .map(|t| t.to_string())
        .collect();
    let mut any_truncated = false;
    for interface_name in interface_names {
        let (overrides, truncated) = type_index.overrides_of(&interface_name, full_pool);
        any_truncated |= truncated;
        for over in overrides {
            if existing_symbols.insert(over.symbol) {
                candidates.push((over.clone(), reasons::INHERITANCE_FAMILY));
            }
        }
    }
    if any_truncated {
        for (_, bits) in candidates.iter_mut() {
            if *bits & reasons::INHERITANCE_FAMILY != 0 {
                *bits |= reasons::FAMILY_TRUNCATED;
            }
        }
    }
}
