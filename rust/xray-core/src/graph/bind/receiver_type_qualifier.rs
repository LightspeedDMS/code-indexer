//! Type-qualifier hard-narrowing substrate (#1922, reworked #1931), split
//! out of `receiver.rs` (Messi Rule 6, anti-file-bloat -- that file was at
//! 980 lines, within ~20 of the project's 1000-line limit, before this
//! logic grew a further two guards). `receiver.rs` keeps the CORE AC1/AC2
//! receiver-TYPE-resolution substrate (`FileTypedNames`, `ReceiverEvidence`,
//! `resolve_receiver_type`); this module owns the NARROWER, JAVA-ONLY
//! question "is this call's receiver DEFINITELY qualified by a type
//! reference the source itself wrote" -- for a bare identifier
//! (`is_definite_type_qualifier`) or a dotted chain (`resolve_dotted_
//! qualifier_type`) -- and the whole-file safety guards that gate hard-
//! narrowing on either answer (`file_is_safe_for_type_qualifier_
//! narrowing`).

use super::families::TypeIndex;
use super::name_index::RepoNameIndex;
use super::receiver::{FileTypedNames, LocalLookup, ReceiverEvidence};
use crate::graph::extract::local_index::{DeclarationKind, ImportKind, ImportRecord};
use crate::graph::identity::SymbolId;

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
/// repo-wide guard `receiver::resolve_identifier_receiver`'s own static-
/// type-name fallback already trusts, reused rather than duplicated --
/// Rule 4), and (d) is not explicitly named by a SINGLE-MEMBER static
/// import anywhere in this file (`is_statically_imported_member`).
///
/// Deliberately NOT the same question `receiver::resolve_receiver_type`
/// answers: that function asks "what type does this identifier resolve
/// to, if any" (and stays `Advisory` even for a confirmed in-repo type
/// name, permanently, per #1910's salvage doctrine); this asks "is the
/// call STRUCTURALLY qualified by a type reference at all", independent
/// of whether that type turns out to be known in-repo or external. Both
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
/// static-imported member as a type reference, and (#1931)
/// `resolve_dotted_qualifier_type`'s identical per-segment guard.
/// `ImportKind::StaticWildcard` (`import static pkg.Util.*;`) is
/// deliberately NOT consulted HERE: it names no specific member, so
/// there is nothing to positively match `name` against without guessing
/// -- `has_static_wildcard_import` below handles that shape separately
/// and more coarsely, at the WHOLE-FILE level, rather than trying to
/// name-match against an unknown wildcard target. Bounded loop (Rule
/// 14): iterates at most `imports.len()` times, finite and fixed by this
/// file's own already-extracted import list.
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

/// The FEWEST segments a `ReceiverExpr::DottedQualifier` can carry that
/// `resolve_dotted_qualifier_type` will ever attempt to resolve, and
/// also the EXACT length the nested-type rule requires (`Outer.Inner`).
/// Structural extraction never actually produces fewer than this
/// (`build_dotted_qualifier_segments` always yields at least a base
/// identifier plus one `field` segment) -- named here so neither
/// meaning is a bare, unexplained literal at its call sites.
const MIN_DOTTED_CHAIN_SEGMENTS: usize = 2;

/// #1931: resolves a `ReceiverExpr::DottedQualifier`'s SEGMENTS
/// (`["Outer", "Inner"]`, `["com", "example", "Target"]`) to a concrete
/// in-repo type name, under the SAME safety guards #1922 established for
/// a bare type-qualified call -- see `is_definite_type_qualifier`'s own
/// doc comment for the shadowing rationale each guard exists to close.
/// Positive ONLY (never Advisory/guessed): this function either proves
/// the chain resolves to a real declared type, or returns
/// `ReceiverEvidence::None` -- there is no open-world fallback here,
/// mirroring `receiver::resolve_identifier_receiver`'s own Positive-tier
/// discipline for a genuine `TypedNameRecord` hit.
///
/// Two independent resolution rules, either one sufficient:
///
/// 1. **Nested type** (`segments.len() == MIN_DOTTED_CHAIN_SEGMENTS`,
///    e.g. `["Outer", "Inner"]`): positive when the repo records a REAL
///    nesting edge `(type_name: "Inner", top_level_type: "Outer")`
///    anywhere (`TypeIndex::is_nested_type_of`) -- i.e. some file's OWN
///    extraction genuinely saw `Inner` declared nested inside `Outer`'s
///    top-level private-access domain. This is membership in the FULL
///    set of recorded nesting pairs (never merely "the" unambiguous
///    top-level owner of the bare name `Inner`), so a coincidental
///    same-named `Inner` nested under a DIFFERENT outer elsewhere in the
///    repo can never invalidate a genuine match here, and can never
///    itself be mistaken for one either (its own pair is a different,
///    unrelated tuple).
/// 2. **Fully qualified** (any length >= `MIN_DOTTED_CHAIN_SEGMENTS`,
///    e.g. `["com", "example", "Target"]`): positive when some
///    repo-declared `Type`-kind declaration named the LAST segment has
///    its OWN file's package exactly equal to every segment BEFORE it,
///    joined by `.` -- i.e. `com.example.Target` resolves only when a
///    type literally named `Target` is declared in a file whose
///    `package` statement is exactly `com.example` (never a
///    prefix/suffix/substring match).
///
/// **EVERY segment must clear four shadowing guards (reworked twice
/// after real javac+javap counterexamples), never merely the first or
/// merely a subset:**
///
/// (1) no local/parameter binding anywhere in the file
/// (`has_any_local_binding`); (2) no known field/interface-constant name
/// anywhere in the repo (`is_known_field_name`); (3) no single-member
/// static import naming it (`is_statically_imported_member`) -- a
/// dotted chain rooted in a shadowed identifier (`helperInstance.Field.
/// m()`, `obj.Nested.m()`) must be exactly as untouched as its
/// bare-qualifier counterpart. A field and a type can legally share one
/// bare name (JLS 6.3: types and members occupy separate namespaces),
/// and per JLS 6.5.2 an ExpressionName reclassification always prefers
/// an accessible FIELD over a same-named TYPE at a `.`-qualified
/// position -- so `A.B.run()` where `A` declares BOTH a field `B` AND an
/// unrelated nested class `B` resolves, in real javac, to the FIELD's
/// `run()`, never the nested type `B`'s own `run()`. A guard limited to
/// `segments[0]` missed this: the nested-type rule alone still matched
/// `("B", "A")` and hard-narrowed away the true, field-reached target
/// (Opus F16, Codex's `a.b.C.m()`, both javac+javap-verified).
///
/// (4) no segment that resolves to a known repo TYPE may have an
/// UNRESOLVED EXTERNAL supertype ANYWHERE IN ITS ANCESTOR CHAIN
/// (`TypeIndex::is_known_type_name(segment) && TypeIndex::has_
/// unresolved_external_supertype_transitively(segment)`) -- a TRANSITIVE
/// walk, deliberately NOT the direct-only predicate `has_unresolved_
/// external_supertype` #1924's `RECEIVER_TYPE_MISMATCH` tagging uses
/// (that predicate's own four-condition soundness argument was reviewed
/// specifically against direct parents; widening it is a separate,
/// unreviewed change this fix does not make -- see `TypeIndex::has_
/// unresolved_external_supertype_transitively`'s own doc comment).
/// Guards (1)-(3) can only see a field this extractor actually indexed
/// -- but a field INHERITED from an external/unindexed superclass is
/// invisible to `is_known_field_name` no matter how many segments are
/// checked, and a DIRECT-only supertype check misses it too whenever the
/// unindexed class is a GRANDPARENT (or deeper), not the segment's own
/// immediate parent. Two Codex counterexamples:
/// - `class a extends ExternalBase {}` (`ExternalBase` declares `static
///   Holder b;` but is itself outside the analysed set) makes `a.b.C.m()`
///   read, absent this guard, as a PACKAGE PATH ("a.b") coinciding with
///   an unrelated real `a.b.C` type elsewhere -- real javac resolves it
///   through the INHERITED field `b` instead.
/// - `class Outer extends IndexedBase {}` / `class IndexedBase extends
///   ExternalBase {}` (only `ExternalBase` unindexed): `Outer`'s own
///   DIRECT parent, `IndexedBase`, IS a known repo type, so the
///   direct-only predicate alone would wrongly pass -- only walking past
///   `IndexedBase` to the GRANDPARENT `ExternalBase` reveals the real
///   risk (an inherited field named `Inner` shadowing `Outer.Inner.m()`'s
///   own nested-type decoy).
///
/// `segments` is never empty by construction (see `ReceiverExpr::
/// DottedQualifier`'s own doc comment). Defensive invariant (Rule 15):
/// `segments.len() < MIN_DOTTED_CHAIN_SEGMENTS` returns `None` outright
/// before either resolution rule runs -- structural extraction never
/// actually produces fewer segments than this, but asserting the
/// precondition explicitly is what lets `segments.split_last()` below be
/// unwrapped with a plain, evidently-true `expect` rather than a second,
/// silently-swallowing `Option` branch.
///
/// **Guard (a), the FQN rule's own ambiguity check**: a package name and
/// a real declared TYPE's bare name occupy the exact same lowercase-
/// identifier syntax space -- but per the JLS, whenever a name IS
/// accessible as a type in the current context, Java NEVER falls back to
/// reading it as a package fragment instead (a package and a type cannot
/// share a qualified name in one compilation). So if ANY segment BEFORE
/// the last (the FQN rule's own "package" prefix) is ALSO a known repo
/// TYPE name, the fully-qualified reading can never be what real javac
/// does -- bail the FQN rule outright, regardless of whether guard (4)
/// above already caught it via an unresolved supertype. Scoped to the
/// FQN rule only: the nested-type rule's OWN first segment is EXPECTED
/// to be a real type (that is literally what `is_nested_type_of` proves)
/// and this ambiguity does not apply to it.
///
/// Deliberately does NOT consult `enclosing_type`/`enclosing_method`
/// context at all: unlike `receiver::resolve_identifier_receiver`, every
/// segment is checked file-WIDE (`has_any_local_binding`) and repo-WIDE
/// (`is_known_field_name`, `is_known_type_name`, `has_unresolved_
/// external_supertype`), never against one specific method/type scope --
/// unifying with a narrower scoped lookup here would only ever miss a
/// shadow, never invent one, so this stays intentionally the wider, more
/// conservative check.
pub(crate) fn resolve_dotted_qualifier_type(
    segments: &[String],
    typed_names: &FileTypedNames,
    type_index: &TypeIndex,
    name_index: &RepoNameIndex,
    imports: &[ImportRecord],
) -> ReceiverEvidence {
    if segments.len() < MIN_DOTTED_CHAIN_SEGMENTS {
        return ReceiverEvidence::None;
    }
    if segments.iter().any(|segment| {
        typed_names.has_any_local_binding(segment)
            || type_index.is_known_field_name(segment)
            || is_statically_imported_member(segment, imports)
            || (type_index.is_known_type_name(segment)
                && type_index.has_unresolved_external_supertype_transitively(segment))
    }) {
        return ReceiverEvidence::None;
    }
    let (last, prefix_segments) = segments
        .split_last()
        .expect("segments.len() >= MIN_DOTTED_CHAIN_SEGMENTS was just checked above");
    let first = &segments[0];
    if segments.len() == MIN_DOTTED_CHAIN_SEGMENTS && type_index.is_nested_type_of(last, first) {
        return ReceiverEvidence::Positive(last.clone());
    }
    if prefix_segments
        .iter()
        .any(|segment| type_index.is_known_type_name(segment))
    {
        return ReceiverEvidence::None;
    }
    let prefix = prefix_segments.join(".");
    let resolves_as_fqn = name_index
        .lookup(last, DeclarationKind::Type)
        .iter()
        .any(|decl| decl.package.as_deref() == Some(prefix.as_str()));
    if resolves_as_fqn {
        ReceiverEvidence::Positive(last.clone())
    } else {
        ReceiverEvidence::None
    }
}

#[cfg(test)]
#[path = "receiver_type_qualifier_tests.rs"]
mod tests;
