//! `FileScope` -- one file's resolution context, derived purely from its
//! already-extracted `LocalIndex` (Story #1787, S2, AC4). No AST is
//! consulted here: `LocalIndex.declarations` already carries the file's
//! `Package` declaration (if any) and `LocalIndex.imports` already carries
//! every import record -- this module just packages that into the shape
//! `super::resolve` needs when it computes reasons for a candidate.
//! Called for real by `super::resolve_all_references` for every file
//! `bind()` processes.

use crate::graph::extract::local_index::{DeclarationKind, ImportRecord, LocalIndex};

/// One file's resolution context: its declared package (if any) and its
/// import list. Built once per file when a `bind()` call starts (full
/// rebuild, never incrementally patched).
pub(crate) struct FileScope {
    pub(crate) package: Option<String>,
    pub(crate) imports: Vec<ImportRecord>,
}

/// Builds a `FileScope` from `index`. A file's package name is read off
/// the FIRST `Package`-kind declaration in `index.declarations` -- real
/// Java source has at most one `package` statement per file, so "first"
/// and "only" coincide in practice; a file with none (the default
/// package) has `package: None`.
pub(crate) fn build_file_scope(index: &LocalIndex) -> FileScope {
    let package = index
        .declarations
        .iter()
        .find(|d| d.kind == DeclarationKind::Package)
        .map(|d| d.name.clone());
    FileScope { package, imports: index.imports.clone() }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::graph::extract::local_index::Declaration;
    use crate::graph::identity::make_symbol_id;

    fn package_declaration(name: &str) -> Declaration {
        Declaration {
            kind: DeclarationKind::Package,
            name: name.to_string(),
            line: 1,
            symbol: make_symbol_id(1, 0),
            param_count: None,
            param_types: Vec::new(),
            is_varargs: false,
        }
    }

    #[test]
    fn extracts_package_name_when_a_package_declaration_is_present() {
        let mut index = LocalIndex::new();
        index.declarations.push(package_declaration("com.example"));

        let scope = build_file_scope(&index);
        assert_eq!(scope.package.as_deref(), Some("com.example"));
    }

    #[test]
    fn has_no_package_when_the_file_declares_none() {
        let index = LocalIndex::new();
        let scope = build_file_scope(&index);
        assert_eq!(scope.package, None);
    }

    #[test]
    fn clones_every_import_record_from_the_index() {
        use crate::graph::extract::local_index::ImportKind;
        let mut index = LocalIndex::new();
        index.imports.push(ImportRecord {
            kind: ImportKind::Wildcard,
            path: "java.util".to_string(),
            line: 3,
        });

        let scope = build_file_scope(&index);
        assert_eq!(scope.imports.len(), 1);
        assert_eq!(scope.imports[0].path, "java.util");
    }
}
