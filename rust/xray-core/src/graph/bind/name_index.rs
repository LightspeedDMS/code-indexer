//! `RepoNameIndex` -- the repo-wide bare-name lookup substrate AC4 Level 0
//! resolves against (Story #1787, S2). Built ONCE per `bind()` call
//! (`super::bind` calls `RepoNameIndex::build` before resolving any
//! reference) from every file's already-extracted declarations -- no AST
//! involved.

use super::scope::build_file_scope;
use super::FileForBind;
use crate::graph::extract::local_index::DeclarationKind;
use crate::graph::identity::SymbolId;
use std::collections::HashMap;

/// One repo-wide declaration, as the name index sees it: enough to
/// compute every AC4 reasons bit without re-consulting the file it came
/// from.
#[derive(Clone)]
pub(crate) struct DeclInfo {
    pub(crate) symbol: SymbolId,
    pub(crate) file_id: u32,
    pub(crate) package: Option<String>,
    pub(crate) kind: DeclarationKind,
    pub(crate) param_count: Option<usize>,
    /// AC1 (Story #1793, S4): the bare name of this method's immediately
    /// enclosing type, joined from `LocalIndex.method_owners` by `symbol`
    /// (never a new field on `Declaration` itself -- see
    /// `local_index::MethodOwnerRecord`'s own docs). `None` for a
    /// declaration with no owner record, never a guessed value.
    pub(crate) enclosing_type: Option<String>,
    /// AC2: copied straight through from `Declaration::param_types`.
    pub(crate) param_types: Vec<String>,
    /// AC2: copied straight through from `Declaration::is_varargs`.
    pub(crate) is_varargs: bool,
    /// AC2 (Story #1806, S2b): this method's declared return type,
    /// joined from `LocalIndex.method_return_types` by `symbol` (mirrors
    /// `enclosing_type`'s own join above exactly). `None` for a
    /// declaration with no return-type record (every non-method kind, a
    /// constructor, or a method whose return type could not be
    /// determined) -- never a guessed value.
    pub(crate) return_type: Option<String>,
}

/// Repo-wide bare-name index. Never mutated after `build` returns.
#[derive(Default)]
pub(crate) struct RepoNameIndex {
    by_name: HashMap<String, Vec<DeclInfo>>,
}

impl RepoNameIndex {
    pub(crate) fn build(files: &[FileForBind]) -> Self {
        let mut by_name: HashMap<String, Vec<DeclInfo>> = HashMap::new();
        for file in files {
            let package = build_file_scope(&file.index).package;
            let owners_by_symbol: HashMap<SymbolId, &str> = file
                .index
                .method_owners
                .iter()
                .map(|owner| (owner.method_symbol, owner.enclosing_type.as_str()))
                .collect();
            let return_types_by_symbol: HashMap<SymbolId, &str> = file
                .index
                .method_return_types
                .iter()
                .map(|record| (record.method_symbol, record.return_type.as_str()))
                .collect();
            for decl in &file.index.declarations {
                by_name.entry(decl.name.clone()).or_default().push(DeclInfo {
                    symbol: decl.symbol,
                    file_id: file.file_id,
                    package: package.clone(),
                    kind: decl.kind,
                    param_count: decl.param_count,
                    enclosing_type: owners_by_symbol.get(&decl.symbol).map(|t| t.to_string()),
                    param_types: decl.param_types.clone(),
                    is_varargs: decl.is_varargs,
                    return_type: return_types_by_symbol.get(&decl.symbol).map(|t| t.to_string()),
                });
            }
        }
        RepoNameIndex { by_name }
    }

    /// Every declaration named `name` whose kind is `kind`, anywhere in
    /// the repository. Empty if the repo declares no such name (or only
    /// under a different kind) -- the substrate for AC4's "definition is
    /// outside the repository" -> empty candidate set requirement.
    pub(crate) fn lookup(&self, name: &str, kind: DeclarationKind) -> Vec<&DeclInfo> {
        self.by_name
            .get(name)
            .map(|decls| decls.iter().filter(|d| d.kind == kind).collect())
            .unwrap_or_default()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::graph::extract::local_index::{Declaration, LocalIndex};
    use crate::graph::identity::make_symbol_id;

    fn method_decl(name: &str, file_id: u32) -> Declaration {
        Declaration {
            kind: DeclarationKind::Method,
            name: name.to_string(),
            line: 1,
            symbol: make_symbol_id(file_id, 0),
            param_count: None,
            param_types: Vec::new(),
            is_varargs: false,
        }
    }

    #[test]
    fn finds_declarations_by_name_and_filters_by_kind() {
        let mut index = LocalIndex::new();
        index.declarations.push(method_decl("run", 1));
        let files = vec![FileForBind { file_id: 1, language: "java".to_string(), index }];

        let name_index = RepoNameIndex::build(&files);
        assert_eq!(name_index.lookup("run", DeclarationKind::Method).len(), 1);
        assert!(name_index.lookup("run", DeclarationKind::Type).is_empty());
        assert!(name_index.lookup("doesNotExist", DeclarationKind::Method).is_empty());
    }

    /// AC1/AC2 (Story #1793, S4): `DeclInfo` must expose which TYPE
    /// declared a method (joined from `LocalIndex.method_owners` by
    /// `symbol`, since `Declaration` itself carries no such field) so the
    /// family binder can ask "is this method's declaring type an
    /// interface", and must carry `param_types`/`is_varargs` straight
    /// through from `Declaration` for overload-shape narrowing. A method
    /// with NO owner record (e.g. a bare top-level declaration, or a
    /// hand-built fixture that never populated `method_owners`) must get
    /// `enclosing_type: None`, never a fabricated guess.
    #[test]
    fn decl_info_carries_enclosing_type_from_method_owner_records_and_param_shape_from_declaration() {
        use crate::graph::extract::local_index::MethodOwnerRecord;

        let mut index = LocalIndex::new();
        index.declarations.push(Declaration {
            kind: DeclarationKind::Method,
            name: "save".to_string(),
            line: 1,
            symbol: make_symbol_id(1, 0),
            param_count: Some(1),
            param_types: vec!["String".to_string()],
            is_varargs: true,
        });
        index
            .method_owners
            .push(MethodOwnerRecord { method_symbol: make_symbol_id(1, 0), enclosing_type: "Repo".to_string() });
        // A second method with no owner record at all -- a DISTINCT symbol
        // (local index 1) from "save"'s (local index 0), so the two are
        // never accidentally aliased in `owners_by_symbol`.
        index.declarations.push(Declaration {
            kind: DeclarationKind::Method,
            name: "orphan".to_string(),
            line: 2,
            symbol: make_symbol_id(1, 1),
            param_count: None,
            param_types: Vec::new(),
            is_varargs: false,
        });

        let files = vec![FileForBind { file_id: 1, language: "java".to_string(), index }];
        let name_index = RepoNameIndex::build(&files);

        let save = &name_index.lookup("save", DeclarationKind::Method)[0];
        assert_eq!(save.enclosing_type.as_deref(), Some("Repo"));
        assert_eq!(save.param_types, vec!["String".to_string()]);
        assert!(save.is_varargs);

        let orphan = &name_index.lookup("orphan", DeclarationKind::Method)[0];
        assert_eq!(orphan.enclosing_type, None, "a method with no MethodOwnerRecord must never get a guessed type");
    }

    /// AC2 (Story #1806, S2b): `DeclInfo.return_type` is joined from
    /// `LocalIndex.method_return_types` by `symbol`, mirroring
    /// `enclosing_type`'s own join exactly. A method with NO return-type
    /// record must get `return_type: None`, never a fabricated guess.
    #[test]
    fn decl_info_carries_return_type_from_method_return_type_records() {
        use crate::graph::extract::local_index::MethodReturnTypeRecord;

        let mut index = LocalIndex::new();
        index.declarations.push(Declaration {
            kind: DeclarationKind::Method,
            name: "getFoo".to_string(),
            line: 1,
            symbol: make_symbol_id(1, 0),
            param_count: Some(0),
            param_types: Vec::new(),
            is_varargs: false,
        });
        index
            .method_return_types
            .push(MethodReturnTypeRecord { method_symbol: make_symbol_id(1, 0), return_type: "Foo".to_string() });
        index.declarations.push(Declaration {
            kind: DeclarationKind::Method,
            name: "orphanReturn".to_string(),
            line: 2,
            symbol: make_symbol_id(1, 1),
            param_count: None,
            param_types: Vec::new(),
            is_varargs: false,
        });

        let files = vec![FileForBind { file_id: 1, language: "java".to_string(), index }];
        let name_index = RepoNameIndex::build(&files);

        let get_foo = &name_index.lookup("getFoo", DeclarationKind::Method)[0];
        assert_eq!(get_foo.return_type.as_deref(), Some("Foo"));

        let orphan = &name_index.lookup("orphanReturn", DeclarationKind::Method)[0];
        assert_eq!(orphan.return_type, None, "a method with no MethodReturnTypeRecord must never get a guessed type");
    }
}
