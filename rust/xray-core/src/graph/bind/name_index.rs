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
            for decl in &file.index.declarations {
                by_name.entry(decl.name.clone()).or_default().push(DeclInfo {
                    symbol: decl.symbol,
                    file_id: file.file_id,
                    package: package.clone(),
                    kind: decl.kind,
                    param_count: decl.param_count,
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
}
