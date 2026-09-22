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
/// `Other` therefore never report incompatibility here -- `Cast`/
/// `Constructor` evidence only ever feeds `OVERLOAD_ARG_TYPE_MATCH`
/// tagging via `candidate_has_named_type_mismatch`, never candidate-set
/// exclusion.
fn literal_shape_is_incompatible(
    shape: &crate::graph::extract::local_index::ArgShape,
    declared_type: &str,
) -> bool {
    use crate::graph::extract::local_index::ArgShape;
    let is_numeric = NUMERIC_TYPE_NAMES.contains(&declared_type);
    let is_boolean = BOOLEAN_TYPE_NAMES.contains(&declared_type);
    let is_char = CHAR_TYPE_NAMES.contains(&declared_type);
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
        | ArgShape::Identifier(_)
        | ArgShape::SelfReference
        | ArgShape::Lambda
        | ArgShape::MethodReference
        | ArgShape::Other => false,
    }
}

/// Bug #1923: the closed, exhaustively-enumerable set of Java value-type
/// NAMES (String, primitives, boxed wrappers) whose full compatibility
/// rules this crate CAN prove -- unlike an arbitrary class/interface name
/// (open-world). Shared by `literal_shape_is_incompatible` above (kept
/// byte-for-byte its pre-#1923 semantics -- a pure constant-reuse
/// refactor) and the named-type functions below.
const NUMERIC_TYPE_NAMES: &[&str] = &[
    "int", "long", "double", "float", "short", "byte", "Integer", "Long", "Double", "Float",
    "Short", "Byte",
];
const BOOLEAN_TYPE_NAMES: &[&str] = &["boolean", "Boolean"];
const CHAR_TYPE_NAMES: &[&str] = &["char", "Character"];

pub(super) fn is_closed_world_value_type(type_name: &str) -> bool {
    type_name == "String"
        || NUMERIC_TYPE_NAMES.contains(&type_name)
        || BOOLEAN_TYPE_NAMES.contains(&type_name)
        || CHAR_TYPE_NAMES.contains(&type_name)
}

/// Bug #1923: is closed-world `arg_type` assignable to `param_type`?
/// Exact match; `Object`/`Serializable`/`Comparable` (every value type
/// here implements both, or does once autoboxed); `Number` (numeric
/// wrappers only); `String -> CharSequence`; same-family boxing/widening
/// (every numeric pair mutually compatible -- an over-approximation of
/// real widening that can only under-report a mismatch, never fabricate
/// one); `char`/`Character` ALSO widen to any numeric target (JLS
/// 5.1.2/5.1.8 -- `char` widens directly, `Character` via unboxing then
/// widening under loose invocation context), one-way only: a numeric arg
/// does NOT implicitly narrow to `char`.
fn closed_world_value_type_compatible(arg_type: &str, param_type: &str) -> bool {
    if arg_type == param_type || matches!(param_type, "Object" | "Serializable" | "Comparable") {
        return true;
    }
    if arg_type == "String" {
        return matches!(param_type, "CharSequence" | "Constable" | "ConstantDesc");
    }
    if NUMERIC_TYPE_NAMES.contains(&arg_type) {
        return NUMERIC_TYPE_NAMES.contains(&param_type)
            || matches!(param_type, "Number" | "Constable" | "ConstantDesc");
    }
    if BOOLEAN_TYPE_NAMES.contains(&arg_type) {
        return BOOLEAN_TYPE_NAMES.contains(&param_type) || matches!(param_type, "Constable" | "ConstantDesc");
    }
    if CHAR_TYPE_NAMES.contains(&arg_type) {
        return CHAR_TYPE_NAMES.contains(&param_type)
            || NUMERIC_TYPE_NAMES.contains(&param_type)
            || matches!(param_type, "Constable" | "ConstantDesc");
    }
    false
}

/// Bug #1923: is `arg_type` (a resolved, KNOWN argument type -- from
/// `ArgShape::Identifier`/`SelfReference`/`Cast`/`Constructor`) DEFINITELY
/// incompatible with `param_type`? TAG-ONLY (see `apply_overload_shape_
/// narrowing`'s doc comment): the sole consumer never lets this remove a
/// candidate, only withhold `OVERLOAD_ARG_TYPE_MATCH` -- so an over-eager
/// "compatible" costs precision, never soundness. Covers exact match,
/// `Object`, a generic type PARAMETER `param_type` (or the element of an
/// array-of-type-parameter -- a type variable binds to anything), arrays
/// by CLOSED-WORLD element type under Java's array-covariance rules
/// (`array_element_compatible` below), varargs (`T...` accepts both `T`
/// and `T[]`), and the closed-world value-type rules above.
///
/// Deliberately does NOT consult this repo's own recorded supertype
/// chain for two named CLASS/INTERFACE types: `TypeIndex::supertypes_of`/
/// `is_known_type_name` are BARE-NAME keyed throughout this whole binder,
/// so a repo type sharing its bare name with an unrelated EXTERNAL type
/// is indistinguishable from the genuine article, and a bare-name
/// "match" between two differently-packaged types is equally
/// indistinguishable from a genuine one. It also models NO implicit JDK
/// supertype this extractor's own inheritance recording cannot see
/// (`Enum`/`Record` for an enum/record, `Collection` for `List`, etc.) --
/// extraction records only explicit `extends`/`implements` clauses. So
/// for any two named types that are neither the same name nor a
/// closed-world value type, this always returns `false` -- "when in
/// doubt, tag it" is this bare-name substrate's only sound default for
/// TAG accuracy, mirroring "unknown never counts as a mismatch" for the
/// separate, exclusion-driving literal-shape check.
/// The eight Java primitive KEYWORD names (never their boxed wrapper
/// counterparts) -- used only by `array_element_compatible` below, where
/// primitive-vs-wrapper distinction matters in a way it does not for
/// ordinary scalar compatibility (a primitive-element array is invariant;
/// a wrapper-element array is a genuine, covariant reference array).
const PRIMITIVE_KEYWORD_NAMES: &[&str] =
    &["int", "long", "double", "float", "short", "byte", "boolean", "char"];

/// Bug #1923 (P2): the CLOSED-WORLD scalar base of `type_name` after
/// stripping every `[]` array dimension (any depth), or `None` when that
/// base is not a value type this crate can reason about (an open-world
/// class/interface name, at any array depth). Gates array-covariance
/// incompatibility determination below: reasoning about array element
/// compatibility is only sound when the fully-stripped base element type
/// is a known closed-world value type.
fn closed_world_array_base(type_name: &str) -> Option<&str> {
    let base = type_name.trim_end_matches("[]");
    is_closed_world_value_type(base).then_some(base)
}

/// Bug #1923 (P2): is `arg_element` (this argument array's element type,
/// possibly itself a further array for a multi-dimensional array) assignable
/// to `param_element` (the parameter array's element type) under Java's
/// ARRAY COVARIANCE rules, which are STRICTER than ordinary scalar
/// assignability. Strips exactly ONE array dimension per recursive step,
/// comparing a multi-dimensional array level-by-level rather than
/// collapsing every dimension to the deepest scalar name at once (an
/// `int[]` element one level down from `int[][]` is itself a REFERENCE
/// type -- assignable to `Object`/`Serializable`/`Cloneable` one level up,
/// even though bare `int` is invariant). A PRIMITIVE-element array
/// (`int[]`, `char[]`, ...) is INVARIANT at its innermost level: `int[]`
/// is compatible with `int[]` alone, never `long[]`/`Integer[]`/`Object[]`
/// -- the scalar widening/boxing rules `closed_world_value_type_compatible`
/// applies do NOT extend to a primitive array's own element type. A
/// REFERENCE-element array (`String[]`, a boxed-wrapper array like
/// `Integer[]`) genuinely IS covariant and follows the ordinary
/// closed-world compatibility rules at its innermost level (`Integer[]`
/// is compatible with `Number[]`/`Object[]`).
fn array_element_compatible(arg_element: &str, param_element: &str) -> bool {
    match (arg_element.strip_suffix("[]"), param_element.strip_suffix("[]")) {
        (Some(arg_inner), Some(param_inner)) => array_element_compatible(arg_inner, param_inner),
        (Some(_), None) => matches!(param_element, "Object" | "Serializable" | "Cloneable"),
        (None, Some(_)) => false,
        (None, None) => {
            if PRIMITIVE_KEYWORD_NAMES.contains(&arg_element) || PRIMITIVE_KEYWORD_NAMES.contains(&param_element) {
                arg_element == param_element
            } else {
                closed_world_value_type_compatible(arg_element, param_element)
            }
        }
    }
}

fn named_type_is_definitely_incompatible(
    arg_type: &str,
    param_type: &str,
    is_varargs_tail: bool,
    type_index: &super::families::TypeIndex,
) -> bool {
    let param_type = if is_varargs_tail {
        param_type.trim_end_matches("...")
    } else {
        param_type
    };
    if arg_type == param_type
        || param_type == "Object"
        || type_index.is_known_type_parameter_name(param_type)
    {
        return false;
    }
    let arg_is_array = arg_type.ends_with("[]");
    if is_varargs_tail && arg_is_array {
        let arg_element = arg_type.strip_suffix("[]").unwrap_or(arg_type);
        return closed_world_array_base(arg_element).is_some()
            && !array_element_compatible(arg_element, param_type);
    }
    if param_type.ends_with("[]") {
        let param_base = param_type.trim_end_matches("[]");
        if type_index.is_known_type_parameter_name(param_base) {
            return false;
        }
        if arg_is_array {
            let param_element = param_type.strip_suffix("[]").unwrap_or(param_type);
            let arg_element = arg_type.strip_suffix("[]").unwrap_or(arg_type);
            return closed_world_array_base(arg_element).is_some()
                && !array_element_compatible(arg_element, param_element);
        }
        return true;
    }
    if arg_is_array {
        return !matches!(param_type, "Serializable" | "Cloneable");
    }
    if is_closed_world_value_type(arg_type) {
        return !closed_world_value_type_compatible(arg_type, param_type);
    }
    false
}

/// AC2: true when ANY call-site argument position hits a DEFINITE
/// literal-shape mismatch against `decl`'s declared parameter type at
/// that position (positions with no declared-type evidence, or a
/// non-discriminating shape, never count). Bug #1923 rework: reverted to
/// this EXACT pre-#1923 literal-only shape and signature -- the sole
/// driver of `apply_overload_shape_narrowing`'s candidate-set EXCLUSION,
/// byte-identical to HEAD. Named-type evidence never reaches this
/// function at all; see `candidate_has_named_type_mismatch` below for
/// its TAG-ONLY counterpart.
fn candidate_has_definite_mismatch(
    decl: &DeclInfo,
    arg_shapes: &[crate::graph::extract::local_index::ArgShape],
) -> bool {
    arg_shapes.iter().enumerate().any(|(i, shape)| {
        declared_type_at(decl, i).is_some_and(|t| literal_shape_is_incompatible(shape, t))
    })
}

/// Bug #1923 rework: true when ANY call-site argument position carries
/// resolved NAMED-TYPE evidence (`arg_known_types[i]`, only ever
/// populated for `ArgShape::Identifier`/`SelfReference` with POSITIVE
/// bind-time evidence) proven incompatible with `decl`'s declared
/// parameter type there, per `named_type_is_definitely_incompatible`.
/// TAG-ONLY: `apply_overload_shape_narrowing`'s final tagging loop is
/// its sole consumer, never the candidate-set exclusion step above.
fn candidate_has_named_type_mismatch(
    decl: &DeclInfo,
    arg_shapes: &[crate::graph::extract::local_index::ArgShape],
    arg_known_types: &[Option<String>],
    type_index: &super::families::TypeIndex,
) -> bool {
    // Bug #1923 (P2): a non-Java callee declaration records its
    // OWN language's type vocabulary (Kotlin's `Int`/`Any`/... never
    // equal Java's `int`/`Object`), which this closed-world rule has no
    // way to recognise as equivalent -- skipping entirely for a non-Java
    // callee keeps today's (pre-#1923) tagging behaviour for it, rather
    // than risking a false mismatch on every cross-language call.
    if decl.language != "java" {
        return false;
    }
    arg_shapes.iter().enumerate().any(|(i, _)| {
        let Some(declared_type) = declared_type_at(decl, i) else {
            return false;
        };
        let Some(arg_type) = arg_known_types.get(i).and_then(|t| t.as_deref()) else {
            return false;
        };
        let is_varargs_tail = decl.is_varargs && i + 1 >= decl.param_types.len();
        named_type_is_definitely_incompatible(arg_type, declared_type, is_varargs_tail, type_index)
    })
}

/// AC2 (Story #1793, S4) Level 4 "overload discrimination": candidate-set
/// REDUCTION beyond arity, never exact resolution. Follows the SAME
/// "narrow only if safe" pattern as `apply_import_context_narrowing`
/// (never empty the set, never a no-op "narrow" to the same set already
/// there) -- deliberately DIFFERENT from `apply_arity_narrowing` since
/// #1898: literal-shape evidence is a weaker, open-world heuristic (see
/// `literal_shape_is_incompatible`'s doc comment), so an empty match here
/// stays a genuine "inconclusive", never treated as proof of an external
/// target the way a hard arity mismatch is. Candidate-set EXCLUSION is
/// driven solely by a definite literal-shape mismatch.
/// `OVERLOAD_ARG_TYPE_MATCH` is marked on every surviving candidate whose
/// declared shape is not provably incompatible with the call's argument
/// evidence (Cast/Constructor named types included, TAG-ONLY -- see
/// `candidate_has_named_type_mismatch` below): it can only WITHHOLD the
/// tag from a candidate that already survived the literal-only exclusion
/// step above, never remove the candidate itself, and never re-rank
/// exact bare-name matches against each other -- a named class/interface
/// match can be a false positive across packages (`com.a.Node` and
/// `com.b.Node` both normalize to bare "Node"), so no evidence derived
/// from a bare-name comparison drives exclusion here, only tagging.
pub(super) fn apply_overload_shape_narrowing(
    candidates: &mut Vec<(DeclInfo, u16)>,
    arg_shapes: &[crate::graph::extract::local_index::ArgShape],
    arg_known_types: &[Option<String>],
    type_index: &super::families::TypeIndex,
) {
    if arg_shapes.is_empty() {
        // Bug #1923 (P2): a zero-argument call still has REAL
        // arity evidence -- a varargs-only or truly zero-parameter
        // candidate genuinely accepts it, and that must still earn
        // `OVERLOAD_ARG_TYPE_MATCH`. A genuinely zero-parameter method has
        // an empty `param_types` by construction (there are no parameters
        // to record), so requiring non-empty `param_types` here would
        // wrongly withhold the tag from the exact candidates arity already
        // proves accept this call.
        for (decl, bits) in candidates.iter_mut() {
            if param_count_matches_arity(decl, 0) {
                *bits |= reasons::OVERLOAD_ARG_TYPE_MATCH;
            }
        }
        return;
    }
    // Bug #1923 rework: candidate-set EXCLUSION is driven ONLY by the
    // literal-shape check, byte-for-byte the pre-#1923 mechanism --
    // named-type (identifier/`this`/Cast/Constructor) evidence is
    // TAG-ONLY (see the final loop below) and must NEVER remove a
    // candidate here: it is resolved via bare-name lookups this binder
    // cannot prove immune to an external shadowing collision (see
    // `named_type_is_definitely_incompatible`'s own doc comment), so
    // treating it as exclusionary risks the exact false-dead-code class
    // epic #1786 declared structurally impossible.
    let surviving: Vec<usize> = candidates
        .iter()
        .enumerate()
        .filter(|(_, (d, _))| !candidate_has_definite_mismatch(d, arg_shapes))
        .map(|(i, _)| i)
        .collect();
    if !surviving.is_empty() && surviving.len() < candidates.len() {
        *candidates = surviving.into_iter().map(|i| candidates[i].clone()).collect();
    }

    // Bug #1923: OVERLOAD_ARG_TYPE_MATCH additionally requires that no
    // NAMED-TYPE (identifier/`this`/Cast/Constructor) argument is provably
    // incompatible -- a TAG-ONLY check (see above): it can only WITHHOLD
    // the tag from a candidate that already survived the literal-only
    // exclusion step, never remove the candidate itself.
    for (decl, bits) in candidates.iter_mut() {
        if !decl.param_types.is_empty()
            && !candidate_has_named_type_mismatch(decl, arg_shapes, arg_known_types, type_index)
        {
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
/// subset", a path NEITHER evidence tier ever guarded).
///
/// **#1910 SALVAGE (issue #1910, rounds 5-7)**: a follow-up attempt tried
/// exactly the redesign this doc used to defer to -- an ambiguity-safe
/// `LocalLookup` (kept, see `receiver::LocalLookup`), per-chain-step
/// pseudo-type rejection (kept), and JLS-6.3-comprehensive local-binding
/// extraction feeding a `Positive`/`Advisory` evidence tier that would
/// hard-narrow under `Positive`. Round 6 found the first attempt itself
/// unsound (an `Ambiguous` local result fell through to the fallback
/// chain instead of staying terminal); round 7 then proved by EXECUTION
/// that `Positive` still cannot be closed-world on this substrate: any
/// substrate promoted to `Positive` on a `FileTypedNames::lookup` `Missing`
/// result is trusting "genuinely no local binding", but a captured local
/// in an anonymous/local class is looked up under the WRONG (inner)
/// enclosing-method key and can produce a FALSE `Missing` for a real
/// local -- indistinguishable, from the caller's side, from an actual
/// absence. Nothing built on top of that lookup can be proven closed-
/// world without fixing the underlying `(enclosing_method, name)` scope
/// key (see `docs/xray-architecture.md`'s candidate-admission section for
/// what a future attempt would need). Five straight rounds across two
/// issues could not converge on a safe hard-empty contract, so hard
/// receiver-type narrowing is retired for good, not merely deferred.
/// Receiver-type evidence contributes only the `RECEIVER_TYPE_MATCH`
/// reason bit (and hence `Confidence::ReceiverType`) -- real signal for a
/// caller that wants it, never a candidate-set exclusion.
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
    // TAG-ONLY, PERMANENTLY (#1898 scope split, epic #1906; reconfirmed by
    // the #1910 salvage): deliberately no narrowing step here at all,
    // empty match or not -- see this function's own doc comment above.
}

/// #1922 (supersedes #1893): a TYPE-QUALIFIED call/method-reference
/// (`Type.m(x)`, `Type::m`) is structurally different from every other
/// receiver-evidence path this module narrows on -- its qualifier is not
/// an INFERRED local-variable/field type (the substrate #1898/#1910
/// proved unsafe to hard-narrow on), it is the literal bare identifier
/// the source itself wrote as the call's qualifier. `receiver_is_type_
/// qualifier` (computed by `receiver::is_definite_type_qualifier`,
/// `mod.rs`, JAVA files only) is `true` only when that identifier (a)
/// follows Java's class-naming convention (starts uppercase), (b)
/// carries no local/parameter/field evidence ANYWHERE this binder can
/// see, and (c) is not explicitly named by a static import anywhere in
/// this file. A `helper.m()` lowercase qualifier is untouched, exactly
/// as #1922's acceptance criteria require.
///
/// **Why "no positive match" is never treated as proof of absence.**
/// This binder's own extraction is incomplete in ways that have nothing
/// to do with the call site -- an interface constant field, a Kotlin
/// companion `@JvmStatic` member attributed to a different enclosing-type
/// string than its outer class, a Kotlin top-level function's synthetic
/// `FileKt` facade name never recorded as a type, and (tracked
/// separately, deliberately NOT solved here) a genuinely external
/// qualifier that happens to share a bare method name with an unrelated
/// in-repo declaration. Every one of these can produce EITHER
/// `receiver_type: None` OR a real `receiver_type` with an empty tagged
/// subset -- treating either as "the qualifier proves the real target
/// isn't any of these" would silently drop genuine edges, or flip a live
/// private method to a false dead verdict end-to-end (`Some(true)`,
/// `callers: 0`) on real, compilable source.
///
/// **This function's rule, singular:** hard-narrow ONLY when `receiver_
/// type` POSITIVELY resolves to a declared in-repo type (never on
/// `None`) AND at least one candidate already carries `RECEIVER_TYPE_
/// MATCH` (set by `apply_receiver_type_narrowing`, which must run
/// immediately before this pass) -- in that one case alone, `retain` to
/// exactly that non-empty subset; every other combination is a no-op,
/// falling through to this file's existing permanently-tag-only
/// doctrine. There is no hard-empty path in this function at all: a
/// `None` qualifier or an empty tagged subset both leave `candidates`
/// completely untouched, so completeness (`narrowed_to_zero`/`fact_
/// graph_complete`) is never affected by this pass either.
/// `has_incomplete_supertype_evidence` needs no independent check here:
/// `apply_receiver_type_narrowing` already skips tagging ENTIRELY when
/// the resolved type's own supertype evidence is incomplete, so the
/// tagged subset is trivially empty in that case too, and this pass
/// already treats an empty subset as a no-op.
pub(super) fn apply_type_qualifier_narrowing(
    candidates: &mut Vec<(DeclInfo, u16)>,
    receiver_is_type_qualifier: bool,
    receiver_type: Option<&str>,
) {
    if !receiver_is_type_qualifier || receiver_type.is_none() {
        return;
    }
    let has_positive_match = candidates
        .iter()
        .any(|(_, bits)| bits & reasons::RECEIVER_TYPE_MATCH != 0);
    if !has_positive_match {
        return;
    }
    candidates.retain(|(_, bits)| bits & reasons::RECEIVER_TYPE_MATCH != 0);
}

/// AC3 (Story #1806, S2b): "unqualified calls resolve against the
/// enclosing class and its supertypes first". `same_class_context` is
/// `Some(enclosing_type)` ONLY when the caller (`super::resolve_site`)
/// determined this reference is an unqualified/`this`/`super` call whose
/// enclosing type is known -- `None` for a qualified call (a receiver-type
/// match is a DIFFERENT evidence path, AC1) or a reference kind AC3 does
/// not apply to (type references/constructions have no "calling class").
///
/// Bug #1898 (P1 of epic #1906) -- KEPT SOFT PERMANENTLY, deliberately
/// DIFFERENT from `apply_arity_narrowing` (the only sibling pass that
/// stays hard-empty -- `apply_receiver_type_narrowing` is ALSO tag-only,
/// see its own doc comment): this pass's `allowed` set (`{enclosing_type}
/// U supertypes_of(enclosing_type)`) covers only DIRECT inheritance
/// evidence. Java's real unqualified-call scope is strictly larger than
/// that in two ways this substrate does not model at all:
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
/// which `allowed` can see".
///
/// **#1910 SALVAGE (issue #1910, second scope item, rounds 6-7)**: two
/// follow-up attempts tried widening `allowed` to the caller's full
/// lexical nest (`TypeIndex::lexical_nest_of`, backed by a new
/// `LexicalParentRecord` substrate keyed `(file_id, bare type name)`)
/// plus exempting `reasons::STATIC_IMPORT`-tagged candidates, then
/// hard-narrowing unconditionally. Round 7's review proved BOTH halves
/// still unsound by execution: (1) the `(file_id, bare type name)` key
/// still collides WITHIN one file -- two different outer classes each
/// declaring a same-named nested class (`OuterA.Builder`/`OuterB.Builder`)
/// share the same key, and the caller-side context this pass receives
/// (`same_class_context`/`site.enclosing_type`) is ITSELF only ever a bare
/// simple name, never a qualified one, so even a properly-qualified
/// substrate could not disambiguate the two without threading a
/// qualified/unique symbol id through the entire reference-site pipeline
/// (`MethodOwnerRecord`, `site.enclosing_type`, `TypeIndex`'s own
/// family/supertype maps -- all bare-name-keyed today, a much broader
/// pre-existing limitation, not specific to the lexical-parent work
/// alone); (2) the `STATIC_IMPORT` exemption depends on `import_reasons`
/// classifying a static-on-demand import (`import static pkg.Util.*;`)
/// correctly, which it did NOT (see `resolve.rs::import_reasons`'s own
/// doc comment, issue #1915) -- a decoy same-named method elsewhere in
/// the repo, not statically imported, silently annihilated the real edge
/// with zero import bits ever set to trigger the exemption at all. Both
/// defects are independent of #1915's fix: even with the classification
/// bug fixed, defect (1) alone is enough to keep this pass unsafe to
/// hard-narrow, so this pass stays soft PERMANENTLY -- not pending a
/// follow-up, retired. Widening `allowed` to the caller's full lexical
/// nest plus exempting static-imported candidates remains a documented,
/// UNIMPLEMENTED idea for tagging precision only (`SAME_CLASS_OR_SUPER`
/// tag accuracy), never as grounds for a hard-narrow, unless a future
/// attempt first threads a qualified/unique type identity through the
/// entire reference-site pipeline (see `docs/xray-architecture.md`'s
/// candidate-admission section).
///
/// A/B-probed end-to-end on real javac-valid source (inner/anonymous/
/// static-nested/local class calling an outer `private` method; a
/// static-imported in-repo method): hard-narrowing here flips
/// `is_definitely_dead_code` from `Some(false)` to a FALSE `Some(true)`
/// on a live private target -- the exact outcome epic #1786 declared
/// structurally impossible -- while the arity hard-narrowing (kept, see
/// its own doc) produces no such regression when isolated. This pass
/// keeps the pre-#1898 soft "no match, or a non-empty wrong subset --
/// keep the pool either way" fallback rather than risk a live->dead
/// regression for a narrower win.
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
    candidates: &mut [(DeclInfo, u16)],
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
    for &i in &matching {
        candidates[i].1 |= reasons::SAME_CLASS_OR_SUPER;
    }
    // TAG-ONLY, PERMANENTLY (#1910 salvage): deliberately no narrowing
    // step here at all, empty match or a non-empty subset alike -- see
    // this function's own doc comment above.
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
