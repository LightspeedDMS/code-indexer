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
    /// AC2: true when this method's LAST formal parameter is
    /// variable-arity (`Foo... x`). A varargs method accepts any call
    /// arg_count >= `param_count - 1`, which the AC4 Level-1 arity
    /// narrowing's plain equality check would otherwise wrongly exclude.
    pub is_varargs: bool,
}

/// AC2: "imports (ordinary, static, wildcard)".
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ImportKind {
    Ordinary,
    Static,
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
    /// Any other argument shape (bare identifier, field access, further
    /// method call, ...): carries no discriminating evidence at this
    /// position, so it is always treated as consistent with any declared
    /// parameter type during shape narrowing.
    Other,
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
}

/// AC2: "type references".
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TypeReferenceRecord {
    pub type_name: String,
    pub line: usize,
}

/// AC2: "construction sites".
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ConstructionSite {
    pub type_name: String,
    pub line: usize,
}

/// AC1 (Story #1793, S4): links one method's `symbol` to the bare name of
/// its immediately enclosing type. Deliberately a SEPARATE record (never a
/// new field on `Declaration`, which every `DeclarationKind` shares) --
/// only methods need this, and adding it here avoids touching the many
/// existing `Declaration { .. }` construction sites across the crate.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MethodOwnerRecord {
    pub method_symbol: SymbolId,
    pub enclosing_type: String,
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
    /// AC1 (Story #1793, S4): one record per method declaration in this
    /// file whose immediately enclosing type is known (top-level methods
    /// with no enclosing type produce no record here).
    pub method_owners: Vec<MethodOwnerRecord>,
    /// AC1: bare names of every type declared as an INTERFACE in this
    /// file (`interface_declaration`, not `class`/`enum`/`record`) --
    /// the substrate the family binder's `is_interface` check reads.
    pub interface_names: Vec<String>,
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
        assert!(index.method_owners.is_empty());
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

    #[test]
    fn method_owner_record_links_a_method_symbol_to_its_declaring_type_by_name() {
        let owner = MethodOwnerRecord { method_symbol: make_symbol_id(1, 0), enclosing_type: "Foo".to_string() };
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
        };
        assert_eq!(site.arg_shapes.len(), 2);
        assert_eq!(site.arg_shapes[1], ArgShape::Cast("Foo".to_string()));
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
        });
        assert_eq!(index.declaration_named("Foo").unwrap().name, "Foo");
    }

    #[test]
    fn declaration_named_returns_none_for_an_absent_name() {
        let index = LocalIndex::new();
        assert!(index.declaration_named("DoesNotExist").is_none());
    }
}
