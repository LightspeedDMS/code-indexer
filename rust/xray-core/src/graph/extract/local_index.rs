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
        });
        assert_eq!(index.declaration_named("Foo").unwrap().name, "Foo");
    }

    #[test]
    fn declaration_named_returns_none_for_an_absent_name() {
        let index = LocalIndex::new();
        assert!(index.declaration_named("DoesNotExist").is_none());
    }
}
