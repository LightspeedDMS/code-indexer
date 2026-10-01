//! `LocalIndex` -- the per-file extraction output populated by a
//! `LanguageExtractor` (Story #1787, S2, AC2).
//!
//! Every field here holds OWNED data (`String`, not `&str`; no
//! `&OwnedNode`/`OwnedNode` at all): a `LocalIndex` must outlive the parsed
//! tree it was extracted from (see `crate::graph::fused`, which drops the
//! tree once extraction and `collect_facts` both complete) and must be
//! cacheable by content hash (`crate::graph::identity::per_file_cache_key`)
//! independently of any tree.

use crate::graph::identity::SymbolId;
use std::collections::HashMap;

/// AC2: "declarations (types, methods, fields, constants, packages)".
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DeclarationKind {
    Type,
    Method,
    Field,
    Constant,
    Package,
}

/// Story #1835: a declaration's ACCESS visibility, captured from the real
/// Java modifier keywords the extractor already walks past (see
/// `java.rs`'s `visibility_of_modifiers`) -- never inferred from anything
/// else. Stored SEPARATELY from `Declaration` (see `LocalIndex::visibilities`
/// below), mirroring the existing `signatures`/`MethodOwnerRecord` pattern
/// of keeping optional, symbol-keyed side-data off the shared `Declaration`
/// struct every `DeclarationKind` uses.
///
/// `Unknown` is the ONLY variant produced when no explicit `public`/
/// `protected`/`private` keyword is present on a declaration's modifiers
/// (or the declaration has no `modifiers` node at all, e.g. a package
/// declaration). This is a deliberate policy choice (AC1): Java's
/// no-explicit-modifier default is REAL package-private access for an
/// ordinary class member, but it means something entirely different for
/// an interface method/field or an annotation-type element, which are
/// implicitly `public` despite carrying no explicit modifier keyword. This
/// extractor does not track "is the enclosing type an interface" context,
/// so it cannot safely tell those two cases apart -- silently defaulting
/// absent modifiers to a restricted visibility would misclassify a public
/// interface method (exactly jsoup's `Connection`/`Response` API shape,
/// Bug #1833) as dead-code-eligible. `Unknown` costs some real
/// package-private detections but can never manufacture a false "dead"
/// verdict, which is the only property this predicate promises. A future
/// language extractor that tracks richer context MAY safely add a real
/// `PackagePrivate` variant; until then `Unknown` is required for every
/// absent/ambiguous case (AC1).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Visibility {
    Public,
    Protected,
    Private,
    /// No explicit modifier evidence was available, or the extractor does
    /// not (yet) understand this declaration shape well enough to judge
    /// visibility. NEVER treated as restricted -- see the type doc above.
    Unknown,
}

impl Visibility {
    /// True ONLY for `Private`: the sole visibility this extractor can
    /// PROVE cannot be invoked from outside the repository. `Protected`
    /// is reachable via subclassing from another package/module and
    /// `Public` is reachable from anywhere, so both count as externally
    /// visible here; `Unknown` carries no evidence either way and must
    /// stay conservative. This is the single predicate
    /// `CodeGraph::is_definitely_dead_code` (Story #1835) consults.
    pub fn is_provably_not_externally_visible(self) -> bool {
        matches!(self, Visibility::Private)
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Declaration {
    pub kind: DeclarationKind,
    pub name: String,
    pub line: usize,
    pub symbol: SymbolId,
    /// The declared parameter count, for a `Method` declaration only
    /// (`Some`); `None` for every other `DeclarationKind`. Captured
    /// structurally (not just baked into the `signatures` string) so
    /// AC4's Level-1 "+arity" binder narrowing can compare it against a
    /// call site's `InvocationSite::arg_count` without re-parsing text.
    pub param_count: Option<usize>,
    /// AC2 (Story #1793, S4): the declared, simple TYPE name of each
    /// formal parameter in order (e.g. `["String", "int"]`), for a
    /// `Method` declaration; empty for every other `DeclarationKind` and
    /// for a method whose parameter types could not be read. This is
    /// LOCAL SYNTACTIC evidence only (a bare/generic-stripped type name,
    /// never a resolved/qualified type) -- used for candidate-set
    /// REDUCTION beyond arity, never exact overload resolution.
    pub param_types: Vec<String>,
    /// AC2: true when this method has a variable-arity formal parameter
    /// (`Foo... x` in Java, always the LAST parameter per JLS 8.4.1; `
    /// vararg x: Foo` in Kotlin, legal at ANY position -- see
    /// `vararg_index` below for exactly where). A varargs method accepts
    /// any call arg_count >= `param_count - 1`, which the AC4 Level-1
    /// arity narrowing's plain equality check would otherwise wrongly
    /// exclude.
    pub is_varargs: bool,
    /// Bug #1929 rework (Codex P2, closes #1939): the REAL index of the
    /// variadic parameter within `param_types`, when `is_varargs` is
    /// true -- `None` when `is_varargs` is false, or when the extractor
    /// could not determine the exact position (never fabricated).
    /// Needed because Kotlin's `vararg` parameter can sit at ANY
    /// position, unlike Java's always-last rule -- `signature_for`'s
    /// varargs rendering (`budget_bind::format_param_types`) uses this
    /// directly instead of assuming the last `param_types` entry.
    pub vararg_index: Option<usize>,
}

/// AC2: "imports (ordinary, static, wildcard)".
///
/// Issue #1915: `Static` is a SINGLE-MEMBER static import (`import static
/// pkg.Util.helper;`) and `Wildcard` is an ORDINARY, non-static
/// package-level wildcard (`import pkg.*;`) -- neither is the right kind
/// for a STATIC-ON-DEMAND import (`import static pkg.Util.*;`, importing
/// every static member of `Util`), which needs its own variant. Before
/// this fix, `extract_imports` (`java.rs`) tested `is_wildcard` before
/// `is_static` and classified a static-on-demand import as plain
/// `Wildcard` -- `import_reasons` (`resolve.rs`) then compared its raw
/// `path` (which still carries the declaring-CLASS segment, e.g.
/// `"pkg.Util"`) against a candidate's PACKAGE (`"pkg"`), which never
/// matches, so the import earned ZERO reason bits at all: not `WILDCARD_
/// IMPORT` (wrong substrate: an ordinary wildcard import's `path` is a
/// bare package, not `package.Class`) and not `STATIC_IMPORT` either
/// (the `Static` arm's own name-only heuristic never ran, since the
/// import was misclassified as `Wildcard`). Combined with a decoy
/// same-named declaration elsewhere in the repo, this could silently
/// destroy the real call edge outright.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ImportKind {
    Ordinary,
    Static,
    /// Issue #1915: `import static pkg.Util.*;` -- every static member of
    /// `Util` is imported. `ImportRecord::path` for this kind is the
    /// DECLARING CLASS's own dotted path (`"pkg.Util"`), never a bare
    /// package -- see `import_reasons`'s own doc comment for how this is
    /// resolved to a `STATIC_IMPORT` reason bit.
    StaticWildcard,
    Wildcard,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ImportRecord {
    pub kind: ImportKind,
    pub path: String,
    pub line: usize,
}

/// AC2: "inheritance and interface implementation".
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum InheritanceKind {
    Extends,
    Implements,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct InheritanceRecord {
    pub kind: InheritanceKind,
    pub subtype_name: String,
    pub supertype_name: String,
    pub line: usize,
}

/// AC2: "annotations".
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AnnotationRecord {
    pub name: String,
    pub target_name: String,
    pub line: usize,
}

/// AC2 (Story #1793, S4): a coarse, per-argument SHAPE category read off
/// LOCAL syntax at a call site -- never a resolved/inferred type (no
/// generics, no type inference beyond local syntactic evidence, per the
/// story's Non-Goals). Used purely for candidate-set REDUCTION: matching
/// this against a candidate's `Declaration::param_types` narrows an
/// overloaded call site's candidates, it never proves exact resolution.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ArgShape {
    StringLiteral,
    NumericLiteral,
    BooleanLiteral,
    NullLiteral,
    /// An explicit cast `(Foo) x` -- carries the cast's simple type name.
    Cast(String),
    /// An inline `new Foo(...)` passed directly as an argument -- carries
    /// the constructed type's simple name.
    Constructor(String),
    /// A lambda expression (`x -> ...`) argument. Captured as required
    /// evidence (AC2: "lambda ... shapes") but deliberately NOT used for
    /// narrowing in this slice -- matching it against a declared
    /// functional-interface parameter type requires type inference beyond
    /// local syntactic evidence, an explicit Non-Goal.
    Lambda,
    /// A method reference (`Foo::bar`) argument -- same scope note as
    /// `Lambda` above.
    MethodReference,
    /// Bug #1923 (reworked to TAG-ONLY): a bare identifier argument
    /// (`foo` in `bar(foo)`) -- carries the identifier's own text. Its
    /// declared TYPE is NOT known at extraction time (that requires the
    /// per-file typed-name substrate `receiver::FileTypedNames` builds
    /// at BIND time, from records scattered across the whole file); the
    /// BINDER resolves it there, restricted to POSITIVE evidence only
    /// (never the open-world Advisory fallback, which is a guess) --
    /// see `receiver::resolve_argument_identifier_type`. That resolved
    /// type is consumed ONLY to decide whether `OVERLOAD_ARG_TYPE_MATCH`
    /// is TAGGED on a candidate -- it NEVER removes a candidate from the
    /// pool, mirroring `apply_receiver_type_narrowing`'s own permanently
    /// tag-only contract for the analogous receiver-type case.
    Identifier(String),
    /// Bug #1923: `this` used DIRECTLY as a call argument (not as a
    /// receiver) -- e.g. `Selector.select(cssQuery, this)`. Resolved at
    /// bind time to the call's own enclosing type -- the same
    /// definitional, always-POSITIVE evidence
    /// `receiver::resolve_receiver_type` already assigns
    /// `ReceiverExpr::None`/`SelfOrSuper` (never a guess, so this never
    /// goes through `resolve_argument_identifier_type`'s lookup at all;
    /// it is looked up directly from the call site's own `enclosing_type`).
    SelfReference,
    /// Any other argument shape (field access, further method call,
    /// lambda-captured expression, ...): carries no discriminating
    /// evidence at this position, so it is always treated as consistent
    /// with any declared parameter type during shape narrowing.
    Other,
}

/// AC1 (Story #1806, S2b): the receiver expression of a method invocation,
/// captured structurally at extraction time -- from the SAME single AST
/// walk (no second parse) -- so the binder can resolve `receiver.method(...)`
/// by the receiver's declared type without ever touching the AST again.
/// Scope is deliberately bounded to what a single file's own syntax can
/// tell you (Non-Goal: no build, no classpath, no generics/full type
/// inference) -- see `super::java`'s extraction functions for exactly what
/// each variant is derived from.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub enum ReceiverExpr {
    /// A bare, unqualified call (`bareCall()`) -- no explicit receiver at
    /// all. AC3: resolved against the enclosing class and its supertypes.
    #[default]
    None,
    /// An explicit `this.foo()` -- same resolution path as `None` (AC3),
    /// captured as a distinct variant purely for observability (never
    /// conflated with a genuinely bare call). `super.foo()` is NOT this
    /// variant -- see `Super` below.
    SelfOrSuper,
    /// D3: an explicit `super.foo()` (or a `super::foo` method reference).
    /// Resolved ONLY against the enclosing type's transitive supertypes,
    /// NEVER against the enclosing type itself: `SelfOrSuper` originally
    /// conflated `this` and `super`, resolving both against
    /// `enclosing_type` -- for `super`, that produced a false self-loop
    /// whenever the enclosing type declared its own same-named override.
    /// An external/unresolvable superclass must produce no candidate at
    /// all rather than silently falling back to the enclosing type.
    Super,
    /// A simple identifier receiver (`obj.foo()`): a local variable,
    /// field, or parameter name -- resolved at bind time against the
    /// file's own declared-type substrate (`LocalIndex.typed_names`).
    Identifier(String),
    /// AC2: a CHAINED call -- the receiver is itself a method invocation,
    /// e.g. `auth.realm().requireX()`'s outer call (`requireX`) has
    /// `Chained { method_name: "realm", receiver: Box::new(Identifier("auth")) }`.
    /// Resolved at bind time by first resolving `receiver`'s type, then
    /// following THAT type's `method_name` declared return type.
    Chained {
        method_name: String,
        receiver: Box<ReceiverExpr>,
    },
    /// Any other receiver shape (array access, parenthesized expression,
    /// a receiver chain deeper than this extractor's bounded cap, ...)
    /// this slice does not attempt to type -- never fabricated evidence
    /// (Rule 2, anti-fallback).
    Other,
    /// #1931: a DOTTED qualifier chain (`Outer.Inner`, `com.example.
    /// Target`) captured as ordered, bare-name SEGMENTS, structurally, at
    /// extraction time -- e.g. `["Outer", "Inner"]` for `Outer.Inner.m()`,
    /// `["com", "example", "Target"]` for `com.example.Target.m()`. Built
    /// only from a real `field_access` chain whose innermost base is a
    /// plain `identifier` (never `this`/`super`, and never anything this
    /// extractor cannot structurally walk) -- see
    /// `super::java_receiver::build_receiver_expr`'s own doc comment for
    /// the exact grammar shapes handled and the bounded-depth cap shared
    /// with `Chained`. `segments` is never empty by construction and its
    /// LAST element is the qualifier's own final identifier segment (the
    /// immediate receiver of the call).
    ///
    /// Whether this positively resolves to an in-repo type (a nested type
    /// or a fully-qualified package+type) is a BIND-TIME question this
    /// extractor has no repo-wide knowledge to answer -- see
    /// `bind::receiver::resolve_dotted_qualifier_type`, the sole
    /// resolver. A chain that turns out to be an ordinary field access
    /// (`obj.field.m()`, `Outer.FIELD.m()`) is captured identically at
    /// extraction time; it is the BINDER's positive-resolution guards
    /// (never this variant's mere presence) that keep such a chain
    /// exactly as tag-only as it was before this fix.
    DottedQualifier(Vec<String>),
}

/// AC2: "invocation sites".
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct InvocationSite {
    pub callee_name: String,
    pub line: usize,
    /// The real number of arguments passed at this call site: `Some(0)`
    /// for a genuine no-argument call, `None` only if the parsed node had
    /// no `argument_list` child at all (e.g. malformed/incomplete source
    /// under parse-error recovery) -- never fabricated as `Some(0)` in
    /// that case. See `Declaration::param_count` -- the AC4 binder
    /// compares the two structurally.
    pub arg_count: Option<usize>,
    /// AC2: one `ArgShape` per actual call argument, in order. Always the
    /// same length as `arg_count` when `arg_count.is_some()`; empty when
    /// `arg_count` is `None` (no `argument_list` child at all).
    pub arg_shapes: Vec<ArgShape>,
    /// AC1/AC2/AC3 (Story #1806, S2b): this call's receiver expression --
    /// `ReceiverExpr::None` for a genuinely bare/unqualified call.
    pub receiver: ReceiverExpr,
    /// AC3: the bare name of the type immediately enclosing this call
    /// site (threaded through extraction's own stack walk -- see
    /// `super::java::WalkContext`), or `None` for a call outside any type
    /// (never a guessed value).
    pub enclosing_type: Option<String>,
    /// AC1: the symbol of the method immediately enclosing this call site
    /// (same threading as `enclosing_type`), or `None` when the call sits
    /// outside any method body (e.g. a field initializer) -- local
    /// variable/parameter typed-name lookups are scoped to this symbol.
    pub enclosing_method: Option<SymbolId>,
}

/// AC2: "type references".
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TypeReferenceRecord {
    pub type_name: String,
    pub line: usize,
    /// Issue #1930 (rework, item 2): the symbol of the method immediately
    /// enclosing this type reference, threaded through the same
    /// `WalkContext`/`ctx.enclosing_method` stack `InvocationSite::
    /// enclosing_method` already uses -- `None` when the reference sits
    /// outside any method body (a field initializer, or a type mention at
    /// class level, e.g. a supertype/implements clause). `bind::resolve::
    /// enclosing_symbol_for_site` prefers this over the nearest-
    /// preceding-declaration line heuristic for `PendingReference::from`
    /// attribution, exactly like it already does for `InvocationSite`.
    pub enclosing_method: Option<SymbolId>,
}

/// AC2: "construction sites".
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ConstructionSite {
    pub type_name: String,
    pub line: usize,
    /// Issue #1930 (rework, item 2): same field, same rationale, as
    /// `TypeReferenceRecord::enclosing_method` immediately above -- a
    /// construction site's OWN sibling `InvocationSite` (pushed at the
    /// same extraction site, see `extract_construction`/
    /// `push_constructor_reference`) already carries this value, so this
    /// is never a fresh lookup, only a second field fed the same value.
    pub enclosing_method: Option<SymbolId>,
}

/// AC1 (Story #1793, S4): links one method-shaped declaration's `symbol` to
/// the bare name of its immediately enclosing type. This includes Java
/// constructors, whose declarations use `DeclarationKind::Method` so the
/// existing invocation binder can resolve constructor call sites. Deliberately
/// a SEPARATE record (never a new field on `Declaration`, which every
/// `DeclarationKind` shares) -- only methods need this, and adding it here
/// avoids touching the many existing `Declaration { .. }` construction sites
/// across the crate.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MethodOwnerRecord {
    pub method_symbol: SymbolId,
    pub enclosing_type: String,
}

/// Java private-access domain for one declared type. `type_name` remains a
/// bare name because the extractor's existing owner/inheritance substrate is
/// bare-name based; a consumer that collapses these records down to "THE
/// single unambiguous top-level owner of this bare name" (e.g. `TypeIndex::
/// top_level_of`) must treat an ambiguous mapping as unknown, never as
/// grounds to remove an edge. #1931's `TypeIndex::is_nested_type_of` is a
/// DIFFERENT kind of consumer and this warning does not apply to it the same
/// way: it never collapses these records at all, keeping the FULL,
/// un-narrowed multiset of every `(type_name, top_level_type)` pair ever
/// recorded and answering only "was THIS SPECIFIC pair ever recorded" --
/// unambiguous by construction, since a specific tuple's membership is never
/// itself an ambiguous fact even when the bare name `type_name` maps to
/// several different `top_level_type`s across the repo.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TypeNestingRecord {
    pub type_name: String,
    pub top_level_type: String,
}

/// AC2 (Story #1806, S2b): links one method's `symbol` to its declared
/// return type's bare, generic-stripped name. Deliberately a SEPARATE
/// record (mirrors `MethodOwnerRecord`'s own rationale immediately above)
/// so the many existing `Declaration { .. }` construction sites across the
/// crate need no change. Absent (no record) for constructors (which have
/// no return type) and for any method whose return type could not be
/// determined -- never a fabricated `"void"` guess.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MethodReturnTypeRecord {
    pub method_symbol: SymbolId,
    pub return_type: String,
}

/// AC1 (Story #1806, S2b): distinguishes a FIELD's scope (visible
/// throughout its enclosing TYPE, to every method of that type) from a
/// LOCAL VARIABLE's or PARAMETER's scope (visible only within one
/// enclosing METHOD) -- the two lookup keys `TypedNameRecord` needs at
/// bind time, per ordinary Java scoping rules (a local/parameter shadows
/// a field of the same name).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum NameScope {
    Field { enclosing_type: String },
    Local { enclosing_method: SymbolId },
}

/// AC1 (Story #1806, S2b): one local variable's, field's, or parameter's
/// declared TYPE name (bare, generic-stripped -- the same local-syntactic
/// evidence every other AC2 field in this module already uses), captured
/// in the SAME single AST walk as everything else `LocalIndex` holds. This
/// is the substrate the binder resolves a `receiver.method(...)` call's
/// receiver type against -- see `super::super::bind::receiver`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TypedNameRecord {
    pub name: String,
    pub declared_type: String,
    pub scope: NameScope,
}

/// The complete per-file extraction output. Built exactly once per file, in
/// its ENTIRETY, before `collect_facts` (see `crate::graph::user_facts`)
/// ever runs -- see `crate::graph::fused` for the sequencing guarantee.
#[derive(Debug, Default)]
pub struct LocalIndex {
    pub declarations: Vec<Declaration>,
    pub imports: Vec<ImportRecord>,
    pub inheritance: Vec<InheritanceRecord>,
    pub annotations: Vec<AnnotationRecord>,
    pub invocations: Vec<InvocationSite>,
    pub type_references: Vec<TypeReferenceRecord>,
    pub constructions: Vec<ConstructionSite>,
    /// AC2: "a short cached signature line per symbol".
    pub signatures: HashMap<SymbolId, String>,
    /// Story #1835 (AC1/AC2): one `Visibility` per declared symbol that has
    /// modifier evidence, populated alongside `signatures` at the SAME
    /// four extraction sites in `java.rs`. A symbol absent from this map
    /// (rather than defaulting it to `Visibility::Unknown` explicitly at
    /// insertion time) is read back as `Unknown` by every consumer -- see
    /// `crate::graph::bind::budget_bind::intern_declarations_and_attach_signatures`
    /// and `CodeGraph::visibility_for`.
    pub visibilities: HashMap<SymbolId, Visibility>,
    /// AC1 (Story #1793, S4): one record per method-shaped declaration in
    /// this file whose immediately enclosing type is known (top-level
    /// declarations with no enclosing type produce no record here).
    pub method_owners: Vec<MethodOwnerRecord>,
    /// One record for every Java type declaration, preserving the top-level
    /// private-access domain for nested types.
    pub type_nesting: Vec<TypeNestingRecord>,
    /// AC1: bare names of every type declared as an INTERFACE in this
    /// file (`interface_declaration`, not `class`/`enum`/`record`) --
    /// the substrate the family binder's `is_interface` check reads.
    pub interface_names: Vec<String>,
    /// AC2 (Story #1806, S2b): one record per method declaration in this
    /// file whose declared return type is known.
    pub method_return_types: Vec<MethodReturnTypeRecord>,
    /// AC1 (Story #1806, S2b): one record per local variable, field, or
    /// parameter in this file whose declared type is known.
    pub typed_names: Vec<TypedNameRecord>,
    /// #1924/#1925: `(enclosing_method_symbol, name)` for every genuine
    /// formal METHOD or
    /// CONSTRUCTOR PARAMETER in this file -- NEVER a block-scoped local
    /// variable, a field, or a record component (`push_parameter_typed_
    /// names`, `java_methods.rs`, is the sole population site: the ONE
    /// place this extractor turns a real `formal_parameters` node into a
    /// `TypedNameRecord`). `typed_names`'s own `NameScope::Local {
    /// enclosing_method }` cannot tell a parameter apart from an ordinary
    /// local variable declared later in the SAME method -- both share the
    /// identical `(enclosing_method, name)` lookup key, which is exactly
    /// the #1919 "locals are keyed per METHOD, not per BLOCK" gap: a
    /// block-scoped local that happens to shadow a field (or an outer
    /// parameter) pollutes that key for the WHOLE method, not just its own
    /// block. `RECEIVER_TYPE_MISMATCH` tagging (`bind::receiver_mismatch`)
    /// needs a signal strictly NARROWER than "some Local-scoped binding
    /// resolved this name" to stay sound; this is that signal. Sole
    /// consumer: `receiver::FileTypedNames::is_parameter_binding`.
    pub parameter_typed_names: Vec<(SymbolId, String)>,
    /// #1924 (p12): `(enclosing_method, name)` pairs for every PARAMETER
    /// (from `parameter_typed_names` above) whose declared type was
    /// written with an EXPLICIT QUALIFIER other than `java.lang` --
    /// `com.lib.String s`, never `String s` or `java.lang.String s`. This
    /// extractor's type model only ever records a declared type's bare
    /// simple name (`"String"` for all three of those examples), so a
    /// qualified reference to a genuinely different, unproven external
    /// type is otherwise indistinguishable from the real `java.lang`
    /// type. `RECEIVER_TYPE_MISMATCH` tagging must never trust the
    /// closed-world assumption for a name recorded here: the qualified
    /// type's real nature (final or not, its true package or not) is
    /// invisible to this binder. Sole consumer: `receiver::FileTypedNames
    /// ::is_disqualified_by_type_qualifier`.
    pub qualified_non_java_lang_parameter_types: Vec<(SymbolId, String)>,
    /// N1 (#1873/#1875 second-review rework): bare names of every type
    /// declared in this file whose recorded superclass/`implements` type-list
    /// evidence is known to be INCOMPLETE -- a `superclass` node existed
    /// syntactically but its type could not be resolved to a name, or a
    /// `type_list` entry (an `implements`/`extends_interfaces` clause) could
    /// not be resolved. `TypeIndex::has_incomplete_supertype_evidence`
    /// (bind/families.rs) is the sole consumer: `apply_super_class_narrowing`
    /// (bind/narrowing.rs) must skip narrowing entirely for such a type,
    /// never trusting a `supertypes_of` result that might be missing the
    /// real, unparseable supertype. Never removed once added -- a type can
    /// appear here even when some OTHER supertype edge for it WAS recorded
    /// (the two are independent facts).
    pub incomplete_supertypes: Vec<String>,
    /// P1-A (#1898 code review round 2, epic #1906): bare names of every
    /// GENERIC TYPE PARAMETER declared anywhere in this file -- the `T` in
    /// `class Box<T> {}`, `<T extends Svc> void run(T t) {}`, or a
    /// constructor's own `<T>`. `TypeIndex::is_known_type_parameter_name`
    /// (bind/families.rs) is the sole consumer: `receiver::
    /// resolve_receiver_type` must reject a declared-type STRING that
    /// names a type parameter rather than a real class/interface -- a
    /// receiver typed `T` (from a formal parameter `T t`) is not a
    /// concrete type this binder can narrow against, and treating it as
    /// one fabricates a hard receiver-type filter on a name that never
    /// denotes an actual declaration anywhere in the repo. Population is
    /// REPO-WIDE by aggregation in `TypeIndex::build` (a type parameter is
    /// syntactically local to its own class/method, but the blocking use
    /// here is deliberately conservative: any name EVER used as a type
    /// parameter anywhere is never trusted as a receiver type, which can
    /// only ever make narrowing MORE conservative, never fabricate a
    /// wrong hard filter).
    pub type_parameter_names: Vec<String>,
    /// #1922: bare names of EVERY local variable, parameter, or pattern
    /// binding declared ANYWHERE in this file, regardless of whether it
    /// has an enclosing method -- a lambda parameter in a field
    /// initializer, an enum constant's argument list, or a switch-
    /// expression pattern in a field initializer are all still genuine
    /// Java local bindings, but `typed_names`'s `NameScope::Local`
    /// requires a real enclosing METHOD `SymbolId`, which none of those
    /// contexts ever sets, so those bindings are absent from
    /// `typed_names` entirely (not merely mis-scoped). Sole consumer:
    /// `receiver::FileTypedNames::has_any_local_binding`'s flat,
    /// context-independent existence check. Deliberately name-only:
    /// never a declared type, never a scope, never a resolution of which
    /// specific declaration shadows which at a given point (#1919 does
    /// not apply -- this is existence, not visibility).
    pub all_local_binding_names: Vec<String>,
    /// #1922: true when this file's tree-sitter tree carried a syntax
    /// error (`fused::process_parsed_file`'s own `has_syntax_error`
    /// parameter, tree-sitter's native `Node::has_error()` computed once
    /// at parse time -- `false` for every `LocalIndex` built any other
    /// way, e.g. `LocalIndex::new()`/`Default` in a unit test). A node
    /// inside an ERROR subtree is silently absent from every extraction
    /// pass in this module (never visited, never recorded) -- so a
    /// binding this binder's hard-narrowing guards depend on can be
    /// invisible for a reason that has nothing to do with which context
    /// it was declared in (`all_local_binding_names`'s own fix for a
    /// binding with no enclosing method does not help here: the binding
    /// was never parsed into a node at all).
    /// Sole consumer: `receiver::file_is_safe_for_type_qualifier_
    /// narrowing`, which disables hard-narrowing for the WHOLE file when
    /// this is `true`, never re-deriving it via a second AST walk.
    pub has_syntax_error: bool,
    /// Bug #1926 (epic #1906): `(constructor_symbol, owning_type_symbol)`
    /// for every declaration extracted from a Java `constructor_
    /// declaration` node (both a constructor and an ordinary method still
    /// share `DeclarationKind::Method` -- see `MethodOwnerRecord`'s own doc
    /// comment for why). `owning_type_symbol` is `WalkContext.enclosing_
    /// type_symbol` at extraction time -- the owning TYPE's own interned
    /// symbol, deliberately NOT its bare name: two distinct nested classes
    /// can share a bare name (e.g. two different `Inner` types under two
    /// different outer classes), and `MethodOwnerRecord.enclosing_type`
    /// (a `String`) cannot tell them apart, which would wrongly merge
    /// their constructor counts. `None` only when extraction could not
    /// determine an enclosing type's own symbol (a malformed declaration
    /// or a synthetic anonymous/enum-constant body, which Java forbids
    /// from declaring an explicit constructor anyway). Populated ONLY by
    /// `java_methods.rs`; every other extractor leaves this empty by
    /// default, exactly like `interface_names`/`type_parameter_names`
    /// above. Sole consumer: `java_methods::mark_lone_private_no_arg_
    /// constructors`, which needs "is this declaration a constructor, and
    /// which type unambiguously owns it" to find the standard `private
    /// Foo() {}` non-instantiability idiom -- a fact `Declaration` itself
    /// cannot answer (it only records `Method` vs. `Type`/`Field`/... and
    /// never a Method's own more specific shape).
    pub constructor_owners: Vec<(SymbolId, Option<SymbolId>)>,
    /// Bug #1926: the FINAL, per-file set of constructor symbols
    /// `mark_lone_private_no_arg_constructors` determined qualify for the
    /// standard Java non-instantiable-utility-class idiom (a class's ONLY
    /// constructor, no-arg, `Private`). This is a SEPARATE carried fact,
    /// deliberately never a rewrite of the constructor's own recorded
    /// `Visibility` (which stays truthfully `Private` -- `visibility_of()`
    /// is documented as "declared visibility", and the SAME value also
    /// feeds `bind::narrowing::apply_private_visibility_filter`'s
    /// candidate admission, so silently widening it would leak into an
    /// unrelated concern). Threaded through `CodeGraphBuilder`/`CodeGraph`
    /// (`add_non_instantiable_constructor`/`is_non_instantiable_
    /// constructor`) exactly like `visibilities`/`kinds` already are, and
    /// consulted directly by `CodeGraph::is_definitely_dead_code`
    /// alongside (never instead of) the ordinary visibility check.
    pub non_instantiable_constructors: Vec<SymbolId>,
    /// Bug #1926: `(method_symbol, owning_type_symbol)` for every Java
    /// `method_declaration` (mirrors `constructor_owners` exactly, but for
    /// ordinary methods rather than constructors). Sole consumer:
    /// `java_methods::resolve_method_source_edges`, which needs "which
    /// type unambiguously owns this method" to resolve a `@MethodSource`
    /// reference against the annotated method's OWN owning type only,
    /// never a bare-name guess that could collide across two distinct
    /// same-named types.
    pub method_owner_symbols: Vec<(SymbolId, Option<SymbolId>)>,
    /// Bug #1926 (final round): one request per `@MethodSource` annotation
    /// found during the main walk, recorded here rather than resolved
    /// immediately -- resolution needs the WHOLE file's declarations
    /// (a sibling method declared later in the same class), which are not
    /// all known yet mid-walk. `target_names` is already fully normalized
    /// at extraction time (the JUnit5 same-name default, or the explicit
    /// string-literal argument(s) with `Class#method` self-qualification
    /// already applied) -- resolution only needs to look each name up
    /// against `owner_type_symbol`'s own declared zero-arg methods.
    pub method_source_requests: Vec<MethodSourceRequest>,
    /// Bug #1926 (final round): the FINAL set of symbols `resolve_method_
    /// source_edges` proved are referenced via a `@MethodSource` string --
    /// each one resolved directly against its own owning type's declared
    /// methods, NEVER through the generic name-based binder (whose
    /// same-class-or-super narrowing is permanently soft/tag-only and
    /// would otherwise let a same-named, same-arity method in an outer
    /// class, a sibling nested class, or another file in the same package
    /// fabricate a false edge). Consumed directly by `bind::budget_bind`,
    /// which marks each one referenced via `CodeGraphBuilder::mark_
    /// referenced` -- bypassing `resolve_reference`/`RepoNameIndex`
    /// entirely for these specific, already-resolved targets. `mark_
    /// referenced` only sets the AC6 referenced-bit (suppresses the
    /// target's OWN `is_definitely_dead_code` verdict); it never adds a
    /// `Reference`/`Candidate` to the CSR arena, so this reflection-
    /// invoked reference is invisible to `callers_of`/`callees_of`/
    /// reachability queries -- never a real, walkable graph edge.
    pub method_source_edges: Vec<SymbolId>,
    /// Issue #1930: one record per SYNTHETIC `enclosing_method` symbol
    /// one of the two extractors allocates for local-binding resolution
    /// ONLY, without ever pushing a matching `Declaration` for it -- a
    /// static/instance initializer or record compact constructor body
    /// (`java.rs`'s `"block" if ctx.enclosing_method.is_none()` arm), a
    /// Kotlin getter/setter/`init` block (`kotlin.rs`'s `"getter" |
    /// "setter" | "anonymous_initializer"` arm), or a malformed/nameless
    /// declaration's own parse-recovery symbol (`extract_method_
    /// declaration`/`extract_function_declaration`/`extract_secondary_
    /// constructor`, which allocate a symbol BEFORE their name lookup can
    /// fail). `bind::resolve::enclosing_symbol_for_site` attributes a
    /// call/construction/type reference made in such a scope DIRECTLY to
    /// `SyntheticScopeRecord::enclosing_type_symbol` when it is known --
    /// no search over `declarations` at all, so nothing else CAN win. A
    /// NEARBY-declaration line heuristic, even one anchored at the
    /// scope's own start line, still reaches past the scope's true
    /// boundary into an EARLIER nested type's method or a PREVIOUS
    /// sibling initializer's own anonymous class -- e.g. `static class
    /// Inner { void im() {} } static { afterInnerClass(); }` wrongly
    /// credited `afterInnerClass()` to `Inner.im()`, the nearest
    /// declaration by line, which has nothing to do with the static
    /// block at all. The line heuristic
    /// survives ONLY as the documented fallback `enclosing_type_symbol`'s
    /// own doc comment names -- one narrow, currently Java-only case
    /// where the enclosing type symbol is genuinely unknown.
    pub synthetic_scopes: Vec<SyntheticScopeRecord>,
}

/// Issue #1930 (rework, items 1/3): see `LocalIndex::synthetic_scopes`'s
/// own doc comment for the full rationale.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct SyntheticScopeRecord {
    pub symbol: SymbolId,
    pub start_line: usize,
    /// The symbol of the TYPE lexically enclosing this synthetic scope --
    /// `ctx.enclosing_type_symbol` at the moment the scope's synthetic
    /// symbol was allocated. Almost always `Some`: every Java static/
    /// instance initializer and record compact constructor lives inside a
    /// real, non-anonymous type, and Kotlin's `dispatch_type_declaration`
    /// sets this for every type INCLUDING an `object_literal` (unlike
    /// Java, which never gives an anonymous class body its own symbol).
    /// `None` in exactly one reachable Java case: an initializer block
    /// written directly inside an ANONYMOUS class's own body (`new
    /// Runnable() { { instanceInit(); } public void run() {} }`) --
    /// `anonymous_body_context` (java.rs) deliberately gives such a body
    /// no type symbol of its own (no real `Declaration` exists for it
    /// either). `enclosing_symbol_for_site` falls back to the ordinary
    /// nearest-preceding-declaration line heuristic, evaluated at
    /// `start_line`, ONLY in that one case -- ever attributing a call
    /// there to a non-existent enclosing type is not an option, and no
    /// other synthetic scope in either extractor can reach this fallback.
    pub enclosing_type_symbol: Option<SymbolId>,
}

/// Bug #1926 (final round): one `@MethodSource` annotation's not-yet-
/// resolved request -- see `LocalIndex::method_source_requests`'s own doc
/// comment for why resolution is deferred to an end-of-file postprocess.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MethodSourceRequest {
    /// The annotated test method's own symbol -- excluded as a possible
    /// resolution target (Bug #1926 final round, self-exclusion): JUnit5's
    /// same-name default always names a DIFFERENT method (a zero-arg
    /// factory can never share a real Java method signature with a
    /// parameterized test method of the same name), so a request whose
    /// only same-name, zero-arg candidate is the annotated method itself
    /// must resolve to nothing, never a self-edge that would hide a
    /// genuinely dead annotated method.
    pub from_method: SymbolId,
    /// The annotated method's own immediately enclosing type -- resolution
    /// is scoped to exactly this type's OWN declared methods, never a
    /// superclass (unmodelled, conservative) or any other class.
    pub owner_type_symbol: Option<SymbolId>,
    /// Every already-normalized target name this request names (the
    /// default name, or one name per resolved explicit string literal --
    /// empty when every explicit argument was unresolvable, e.g. every
    /// string named a different class).
    pub target_names: Vec<String>,
}

impl LocalIndex {
    pub fn new() -> Self {
        Self::default()
    }

    /// Looks up a declaration by its bare name. Returns the FIRST match in
    /// extraction order if the file declares more than one symbol with the
    /// same bare name (e.g. an overloaded method) -- this is a heuristic
    /// substrate lookup, not the AC4 binder (out of scope for this slice).
    pub fn declaration_named(&self, name: &str) -> Option<&Declaration> {
        self.declarations.iter().find(|d| d.name == name)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::graph::identity::make_symbol_id;

    /// Story #1835 AC1/AC3 (RED against unmodified code -- `Visibility`
    /// does not exist yet, so this fails to compile): the whole dead-code
    /// capability this story restores hinges on exactly one predicate --
    /// "is this visibility PROVABLY not callable from outside the
    /// repository". Only `Private` can answer yes. `Public`/`Protected`
    /// are externally visible by definition; `Unknown` means the
    /// extractor could not determine an explicit modifier at all (see
    /// `java.rs`'s `visibility_of_modifiers`) and must never be silently
    /// treated as restricted (AC1's explicit requirement).
    #[test]
    fn visibility_private_is_provably_not_externally_visible_but_others_are_not() {
        assert!(Visibility::Private.is_provably_not_externally_visible());
        assert!(!Visibility::Public.is_provably_not_externally_visible());
        assert!(!Visibility::Protected.is_provably_not_externally_visible());
        assert!(
            !Visibility::Unknown.is_provably_not_externally_visible(),
            "unknown visibility must never be treated as provably restricted"
        );
    }

    #[test]
    fn new_local_index_is_empty() {
        let index = LocalIndex::new();
        assert!(index.declarations.is_empty());
        assert!(index.imports.is_empty());
        assert!(index.inheritance.is_empty());
        assert!(index.annotations.is_empty());
        assert!(index.invocations.is_empty());
        assert!(index.type_references.is_empty());
        assert!(index.constructions.is_empty());
        assert!(index.signatures.is_empty());
        assert!(index.visibilities.is_empty());
        assert!(index.method_owners.is_empty());
        assert!(index.method_return_types.is_empty());
        assert!(index.typed_names.is_empty());
        assert!(index.incomplete_supertypes.is_empty());
        assert!(index.constructor_owners.is_empty());
        assert!(index.non_instantiable_constructors.is_empty());
        assert!(index.method_owner_symbols.is_empty());
        assert!(index.method_source_requests.is_empty());
        assert!(index.method_source_edges.is_empty());
    }

    /// AC1 (Story #1793, S4): a `Declaration` for a method carries the
    /// declaring type's name via a SEPARATE `MethodOwnerRecord` (joined by
    /// `symbol`, never a new field on the shared `Declaration` struct
    /// every `DeclarationKind` uses) so the family binder can ask "which
    /// type declared this method" without touching the many existing
    /// `Declaration { .. }` call sites across the crate.
    /// AC1 (Story #1793, S4): the family binder needs to know which
    /// declared types are INTERFACES (a call resolving to an interface
    /// method is what triggers family expansion) -- `LocalIndex` tracks
    /// this as a plain list of bare interface names, defaulting empty.
    #[test]
    fn local_index_carries_interface_names_defaulting_empty() {
        let index = LocalIndex::new();
        assert!(index.interface_names.is_empty());
    }

    /// P1-A (#1898 code review round 2, epic #1906): `type_parameter_names`
    /// defaults empty exactly like every other extraction-output field.
    #[test]
    fn local_index_carries_type_parameter_names_defaulting_empty() {
        let index = LocalIndex::new();
        assert!(index.type_parameter_names.is_empty());
    }

    #[test]
    fn method_owner_record_links_a_method_symbol_to_its_declaring_type_by_name() {
        let owner = MethodOwnerRecord {
            method_symbol: make_symbol_id(1, 0),
            enclosing_type: "Foo".to_string(),
        };
        assert_eq!(owner.enclosing_type, "Foo");
        assert_eq!(owner.method_symbol, make_symbol_id(1, 0));
    }

    /// AC2 (Story #1793, S4): a `Declaration` for a method carries its
    /// declared parameter TYPE names (beyond the existing `param_count`)
    /// and whether its last parameter is variable-arity -- both default to
    /// "no evidence" (empty/false) for every non-method `DeclarationKind`.
    #[test]
    fn declaration_carries_param_types_and_varargs_flag() {
        let decl = Declaration {
            kind: DeclarationKind::Method,
            name: "save".to_string(),
            line: 1,
            symbol: make_symbol_id(1, 0),
            param_count: Some(1),
            param_types: vec!["String".to_string()],
            is_varargs: false,
            vararg_index: None,
        };
        assert_eq!(decl.param_types, vec!["String".to_string()]);
        assert!(!decl.is_varargs);
    }

    /// AC2: an `InvocationSite` carries a per-position `ArgShape` --
    /// literal category, explicit cast, constructor name, or lambda/
    /// method-reference shape -- alongside the existing `arg_count`.
    #[test]
    fn invocation_site_carries_per_position_arg_shapes() {
        let site = InvocationSite {
            callee_name: "save".to_string(),
            line: 10,
            arg_count: Some(2),
            arg_shapes: vec![ArgShape::StringLiteral, ArgShape::Cast("Foo".to_string())],
            receiver: ReceiverExpr::None,
            enclosing_type: None,
            enclosing_method: None,
        };
        assert_eq!(site.arg_shapes.len(), 2);
        assert_eq!(site.arg_shapes[1], ArgShape::Cast("Foo".to_string()));
    }

    /// AC1: a bare/unqualified call defaults to `ReceiverExpr::None` and,
    /// when extraction found no enclosing type/method for it (e.g. a
    /// hand-built fixture), both context fields stay `None` -- never a
    /// guessed value.
    #[test]
    fn invocation_site_defaults_to_no_receiver_and_no_enclosing_context() {
        let site = InvocationSite {
            callee_name: "bareCall".to_string(),
            line: 1,
            arg_count: Some(0),
            arg_shapes: Vec::new(),
            receiver: ReceiverExpr::default(),
            enclosing_type: None,
            enclosing_method: None,
        };
        assert_eq!(site.receiver, ReceiverExpr::None);
        assert_eq!(site.enclosing_type, None);
        assert_eq!(site.enclosing_method, None);
    }

    /// AC2: `auth.realm().requireX()`'s receiver (as seen from `requireX`)
    /// is a CHAINED expression wrapping the resolved receiver of the
    /// inner `realm()` call -- the exact nesting shape
    /// `super::java::build_receiver_expr` must produce, proven here purely
    /// as a data-structure invariant (construct it, read it back).
    #[test]
    fn receiver_expr_chained_wraps_its_inner_receiver() {
        let receiver = ReceiverExpr::Chained {
            method_name: "realm".to_string(),
            receiver: Box::new(ReceiverExpr::Identifier("auth".to_string())),
        };
        match receiver {
            ReceiverExpr::Chained {
                method_name,
                receiver,
            } => {
                assert_eq!(method_name, "realm");
                assert_eq!(*receiver, ReceiverExpr::Identifier("auth".to_string()));
            }
            other => panic!("expected Chained, got {other:?}"),
        }
    }

    #[test]
    fn declaration_named_finds_a_present_declaration() {
        let mut index = LocalIndex::new();
        index.declarations.push(Declaration {
            kind: DeclarationKind::Type,
            name: "Foo".to_string(),
            line: 1,
            symbol: make_symbol_id(1, 0),
            param_count: None,
            param_types: Vec::new(),
            is_varargs: false,
            vararg_index: None,
        });
        assert_eq!(index.declaration_named("Foo").unwrap().name, "Foo");
    }

    #[test]
    fn declaration_named_returns_none_for_an_absent_name() {
        let index = LocalIndex::new();
        assert!(index.declaration_named("DoesNotExist").is_none());
    }

    /// AC1: `TypedNameRecord` carries a FIELD's scope (keyed by enclosing
    /// TYPE name) distinctly from a LOCAL/PARAMETER's scope (keyed by
    /// enclosing METHOD symbol) -- the two lookup keys bind-time
    /// resolution needs (see `super::super::bind::receiver`).
    #[test]
    fn typed_name_record_carries_field_and_local_scopes() {
        let field = TypedNameRecord {
            name: "count".to_string(),
            declared_type: "int".to_string(),
            scope: NameScope::Field {
                enclosing_type: "Counter".to_string(),
            },
        };
        assert_eq!(
            field.scope,
            NameScope::Field {
                enclosing_type: "Counter".to_string()
            }
        );

        let local = TypedNameRecord {
            name: "s".to_string(),
            declared_type: "String".to_string(),
            scope: NameScope::Local {
                enclosing_method: make_symbol_id(1, 0),
            },
        };
        assert_eq!(
            local.scope,
            NameScope::Local {
                enclosing_method: make_symbol_id(1, 0)
            }
        );
    }

    /// AC2: `MethodReturnTypeRecord` links a method's symbol to its
    /// declared return type's bare name, mirroring `MethodOwnerRecord`'s
    /// own construction/assertion shape exactly.
    #[test]
    fn method_return_type_record_links_a_method_symbol_to_its_declared_return_type() {
        let record = MethodReturnTypeRecord {
            method_symbol: make_symbol_id(1, 0),
            return_type: "Foo".to_string(),
        };
        assert_eq!(record.return_type, "Foo");
        assert_eq!(record.method_symbol, make_symbol_id(1, 0));
    }
}
