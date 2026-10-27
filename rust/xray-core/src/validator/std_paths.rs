use syn::ext::IdentExt;

// ^ security std/core/alloc path outside PERMITTED_STD_PATHS => evaluator rejected; entries give no file, network, process, thread or environment access
// ^ external RandomState::new() reads OS randomness; Box/Rc can produce raw pointers, unusable because `unsafe` is rejected
// ^ coupled PERMITTED_STD_PATHS with src/code_indexer/xray/rust_std_paths.py:PERMITTED_STD_PATHS; test:python_mirror_matches_permitted_std_paths
pub(super) const PERMITTED_STD_PATHS: &[&str] = &[
    "std::collections", "std::fmt", "std::iter", "std::cmp", "std::str", "std::string", "std::vec",
    "std::option", "std::result", "std::ops", "std::convert", "std::char", "std::num", "std::hash",
    "std::borrow", "std::default", "std::boxed", "std::rc", "std::cell", "std::array", "std::slice",
    "std::marker", "std::primitive", "std::f32", "std::f64", "std::i8", "std::i16", "std::i32", "std::i64",
    "std::i128", "std::u8", "std::u16", "std::u32", "std::u64", "std::u128", "std::isize", "std::usize",
    "std::error", "std::any",
    "core::fmt", "core::iter", "core::cmp", "core::str", "core::option", "core::result", "core::ops",
    "core::convert", "core::char", "core::num", "core::hash", "core::borrow", "core::default", "core::cell",
    "core::array", "core::slice", "core::marker", "core::primitive", "core::f32", "core::f64", "core::i8",
    "core::i16", "core::i32", "core::i64", "core::i128", "core::u8", "core::u16", "core::u32", "core::u64",
    "core::u128", "core::isize", "core::usize", "core::error", "core::any",
    "alloc::vec", "alloc::string", "alloc::collections", "alloc::boxed", "alloc::rc",
];

const STD_ROOTS: &[&str] = &["std", "core", "alloc"];

pub(super) fn canonical(ident: &proc_macro2::Ident) -> String {
    ident.unraw().to_string()
}

pub(super) fn is_std_root(name: &str) -> bool {
    STD_ROOTS.contains(&name)
}

pub(super) fn is_permitted(path: &[String]) -> bool {
    let joined = path.join("::");
    PERMITTED_STD_PATHS
        .iter()
        .any(|permitted| joined == *permitted || joined.starts_with(&format!("{permitted}::")))
}

pub(super) fn permitted_list() -> String {
    let std_modules: Vec<&str> = PERMITTED_STD_PATHS.iter().filter_map(|p| p.strip_prefix("std::")).collect();
    format!("std::{{{}}} and the same modules under core::/alloc:: where they exist", std_modules.join(", "))
}

pub(super) fn use_tree_violations(tree: &syn::UseTree) -> Vec<(usize, String)> {
    let mut violations = Vec::new();
    collect_use_violations(tree, &mut Vec::new(), &mut violations);
    violations
}

fn collect_use_violations(tree: &syn::UseTree, prefix: &mut Vec<String>, out: &mut Vec<(usize, String)>) {
    match tree {
        syn::UseTree::Path(path) => {
            prefix.push(canonical(&path.ident));
            collect_use_violations(&path.tree, prefix, out);
            prefix.pop();
        }
        syn::UseTree::Group(group) => {
            for item in &group.items {
                collect_use_violations(item, prefix, out);
            }
        }
        syn::UseTree::Name(name) => {
            check_use_leaf(&leaf_path(prefix, &name.ident), name.ident.span().start().line, false, out);
        }
        syn::UseTree::Rename(rename) => {
            let leaf = leaf_path(prefix, &rename.ident);
            let line = rename.ident.span().start().line;
            if leaf.first().is_some_and(|root| is_std_root(root)) {
                out.push((
                    line,
                    format!(
                        "`use {} as {}` renames a standard-library path, which is not allowed in evaluator code",
                        leaf.join("::"),
                        canonical(&rename.rename)
                    ),
                ));
            }
            check_use_leaf(&leaf, line, false, out);
        }
        syn::UseTree::Glob(glob) => {
            check_use_leaf(prefix, glob.star_token.spans[0].start().line, true, out);
        }
    }
}

fn leaf_path(prefix: &[String], ident: &proc_macro2::Ident) -> Vec<String> {
    let name = canonical(ident);
    let mut leaf = prefix.to_vec();
    if name != "self" {
        leaf.push(name);
    }
    leaf
}

fn check_use_leaf(leaf: &[String], line: usize, glob: bool, out: &mut Vec<(usize, String)>) {
    let std_rooted = leaf.first().is_some_and(|root| is_std_root(root));
    if !std_rooted || is_permitted(leaf) {
        return;
    }
    let shown = if glob {
        format!("`use {}::*` (glob import)", leaf.join("::"))
    } else {
        format!("`use {}`", leaf.join("::"))
    };
    out.push((
        line,
        format!(
            "{shown} is not a permitted standard-library import in evaluator code (permitted: {})",
            permitted_list()
        ),
    ));
}
