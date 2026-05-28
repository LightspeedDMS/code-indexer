/// Rust AST whitelist validator for user-supplied evaluator source code.
///
/// Uses `syn` to parse the code and walk the AST, rejecting any forbidden
/// constructs before they reach the compiler.
use syn::visit::Visit;
use syn::{File, ItemMod};

/// A validation error with line number and human-readable message.
#[derive(Debug, Clone)]
pub struct ValidationError {
    pub line: usize,
    pub message: String,
}

impl std::fmt::Display for ValidationError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "Line {}: {}", self.line, self.message)
    }
}

/// Validates user evaluator source code against the whitelist.
///
/// Returns Ok(()) if no forbidden constructs are found.
/// Returns Err(Vec<ValidationError>) with one entry per violation.
pub fn validate_evaluator_source(source: &str) -> Result<(), Vec<ValidationError>> {
    let file: File = match syn::parse_str(source) {
        Ok(f) => f,
        Err(e) => {
            let line = e.span().start().line;
            return Err(vec![ValidationError {
                line,
                message: format!("Syntax error: {}", e),
            }]);
        }
    };

    let mut visitor = ForbiddenConstructVisitor {
        errors: Vec::new(),
    };
    visitor.visit_file(&file);

    if visitor.errors.is_empty() {
        Ok(())
    } else {
        Err(visitor.errors)
    }
}

// ---- Visitor implementation ----

struct ForbiddenConstructVisitor {
    errors: Vec<ValidationError>,
}

impl ForbiddenConstructVisitor {
    fn add_error(&mut self, line: usize, message: String) {
        self.errors.push(ValidationError { line, message });
    }

    fn span_line(span: proc_macro2::Span) -> usize {
        span.start().line
    }
}

impl<'ast> Visit<'ast> for ForbiddenConstructVisitor {
    // Reject `unsafe { ... }` blocks
    fn visit_expr_unsafe(&mut self, node: &'ast syn::ExprUnsafe) {
        let line = Self::span_line(node.unsafe_token.span);
        self.add_error(line, "`unsafe` blocks are not allowed in evaluator code".to_string());
        syn::visit::visit_expr_unsafe(self, node);
    }

    // Reject `unsafe fn ...`
    fn visit_item_fn(&mut self, node: &'ast syn::ItemFn) {
        if node.sig.unsafety.is_some() {
            let line = Self::span_line(
                node.sig
                    .unsafety
                    .as_ref()
                    .map(|u| u.span)
                    .unwrap_or_else(proc_macro2::Span::call_site),
            );
            self.add_error(line, "`unsafe` functions are not allowed in evaluator code".to_string());
        }
        syn::visit::visit_item_fn(self, node);
    }

    // Reject `use std::fs`, `use std::net`, `use std::process`, `use std::env`,
    // `use std::io` (and sub-paths)
    fn visit_use_tree(&mut self, node: &'ast syn::UseTree) {
        if let syn::UseTree::Path(path) = node {
            let ident = path.ident.to_string();
            if ident == "std" {
                check_forbidden_std_subpath(&path.tree, &mut self.errors);
            }
        }
        syn::visit::visit_use_tree(self, node);
    }

    // Reject fully-qualified std::fs::*, std::net::*, std::process::*,
    // std::env::*, std::io::* path expressions used directly without import.
    fn visit_path(&mut self, node: &'ast syn::Path) {
        if node.segments.len() >= 2 {
            let first = node.segments[0].ident.to_string();
            let second = node.segments[1].ident.to_string();
            if first == "std"
                && matches!(
                    second.as_str(),
                    "fs" | "net" | "process" | "env" | "io"
                )
            {
                let line = Self::span_line(node.segments[0].ident.span());
                self.add_error(
                    line,
                    format!(
                        "`std::{}` is not allowed in evaluator code",
                        second
                    ),
                );
            }
        }
        syn::visit::visit_path(self, node);
    }

    // Reject `static` and `static mut` declarations (shared mutable state)
    fn visit_item_static(&mut self, node: &'ast syn::ItemStatic) {
        let line = Self::span_line(node.static_token.span);
        self.add_error(
            line,
            "`static` declarations are not allowed in evaluator code".to_string(),
        );
        syn::visit::visit_item_static(self, node);
    }

    // Reject raw pointer types: *const T, *mut T
    fn visit_type_ptr(&mut self, node: &'ast syn::TypePtr) {
        let kind = if node.mutability.is_some() { "*mut" } else { "*const" };
        let line = Self::span_line(node.star_token.spans[0]);
        self.add_error(
            line,
            format!("Raw pointer type `{}` is not allowed in evaluator code", kind),
        );
        syn::visit::visit_type_ptr(self, node);
    }

    // Reject `extern` blocks
    fn visit_item_foreign_mod(&mut self, node: &'ast syn::ItemForeignMod) {
        let line = Self::span_line(node.brace_token.span.open());
        self.add_error(line, "`extern` blocks are not allowed in evaluator code".to_string());
        syn::visit::visit_item_foreign_mod(self, node);
    }

    // Reject `extern "C" fn` (free-standing)
    fn visit_signature(&mut self, node: &'ast syn::Signature) {
        if let Some(abi) = &node.abi {
            let line = Self::span_line(abi.extern_token.span);
            self.add_error(line, "`extern` ABI functions are not allowed in evaluator code".to_string());
        }
        syn::visit::visit_signature(self, node);
    }

    // Reject `mod` declarations
    fn visit_item_mod(&mut self, node: &'ast ItemMod) {
        let line = Self::span_line(node.mod_token.span);
        self.add_error(line, "`mod` declarations are not allowed in evaluator code".to_string());
        syn::visit::visit_item_mod(self, node);
    }

    // Reject forbidden macros: include!, include_str!, include_bytes!, env!, option_env!,
    // println!, eprintln!, print!, eprint! (I/O side effects)
    fn visit_macro(&mut self, node: &'ast syn::Macro) {
        if let Some(last) = node.path.segments.last() {
            let name = last.ident.to_string();
            if matches!(
                name.as_str(),
                "include"
                    | "include_str"
                    | "include_bytes"
                    | "env"
                    | "option_env"
                    | "println"
                    | "eprintln"
                    | "print"
                    | "eprint"
                    | "panic"
                    | "todo"
                    | "unimplemented"
            ) {
                let line = Self::span_line(node.path.segments.first().unwrap().ident.span());
                self.add_error(
                    line,
                    format!("`{}!` macro is not allowed in evaluator code", name),
                );
            }
        }
        syn::visit::visit_macro(self, node);
    }
}

/// Recursively checks `use std::<subpath>` for forbidden modules.
fn check_forbidden_std_subpath(tree: &syn::UseTree, errors: &mut Vec<ValidationError>) {
    match tree {
        syn::UseTree::Path(path) => {
            let name = path.ident.to_string();
            if matches!(name.as_str(), "fs" | "net" | "process" | "env" | "io") {
                let line = path.ident.span().start().line;
                errors.push(ValidationError {
                    line,
                    message: format!(
                        "`use std::{}` (or sub-path) is not allowed in evaluator code",
                        name
                    ),
                });
            } else {
                check_forbidden_std_subpath(&path.tree, errors);
            }
        }
        syn::UseTree::Group(group) => {
            for item in &group.items {
                check_forbidden_std_subpath(item, errors);
            }
        }
        syn::UseTree::Name(name) => {
            let ident = name.ident.to_string();
            if matches!(ident.as_str(), "fs" | "net" | "process" | "env" | "io") {
                let line = name.ident.span().start().line;
                errors.push(ValidationError {
                    line,
                    message: format!(
                        "`use std::{}` is not allowed in evaluator code",
                        ident
                    ),
                });
            }
        }
        syn::UseTree::Rename(rename) => {
            let ident = rename.ident.to_string();
            if matches!(ident.as_str(), "fs" | "net" | "process" | "env" | "io") {
                let line = rename.ident.span().start().line;
                errors.push(ValidationError {
                    line,
                    message: format!(
                        "`use std::{}` (renamed) is not allowed in evaluator code",
                        ident
                    ),
                });
            }
        }
        syn::UseTree::Glob(glob) => {
            let line = glob.star_token.spans[0].start().line;
            errors.push(ValidationError {
                line,
                message: "`use std::*` (glob import) is not allowed in evaluator code".to_string(),
            });
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_valid_code_passes() {
        let code = r#"
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    let mut findings = Vec::new();
    if node.kind == "try_statement" {
        findings.push(EvalFinding {
            pattern: "test".to_string(),
            line: node.start_line,
            snippet: String::new(),
        });
    }
    findings
}
"#;
        assert!(validate_evaluator_source(code).is_ok());
    }

    #[test]
    fn test_rejects_unsafe_block() {
        let code = r#"
fn foo() {
    unsafe { let x = 1; }
}
"#;
        let result = validate_evaluator_source(code);
        assert!(result.is_err());
        let errors = result.unwrap_err();
        assert!(errors.iter().any(|e| e.message.contains("unsafe")));
    }

    #[test]
    fn test_rejects_unsafe_fn() {
        let code = "unsafe fn dangerous() {}";
        let result = validate_evaluator_source(code);
        assert!(result.is_err());
        let errors = result.unwrap_err();
        assert!(errors.iter().any(|e| e.message.contains("unsafe")));
    }

    #[test]
    fn test_rejects_std_fs_import() {
        let code = "use std::fs::File;";
        let result = validate_evaluator_source(code);
        assert!(result.is_err());
        let errors = result.unwrap_err();
        assert!(errors.iter().any(|e| e.message.contains("std::fs")));
    }

    #[test]
    fn test_rejects_std_net_import() {
        let code = "use std::net::TcpStream;";
        let result = validate_evaluator_source(code);
        assert!(result.is_err());
        let errors = result.unwrap_err();
        assert!(errors.iter().any(|e| e.message.contains("std::net")));
    }

    #[test]
    fn test_rejects_std_process_import() {
        let code = "use std::process::Command;";
        let result = validate_evaluator_source(code);
        assert!(result.is_err());
        let errors = result.unwrap_err();
        assert!(errors.iter().any(|e| e.message.contains("std::process")));
    }

    #[test]
    fn test_rejects_raw_pointer_const() {
        let code = "fn foo(p: *const u8) {}";
        let result = validate_evaluator_source(code);
        assert!(result.is_err());
        let errors = result.unwrap_err();
        assert!(errors.iter().any(|e| e.message.contains("*const")));
    }

    #[test]
    fn test_rejects_raw_pointer_mut() {
        let code = "fn foo(p: *mut u8) {}";
        let result = validate_evaluator_source(code);
        assert!(result.is_err());
        let errors = result.unwrap_err();
        assert!(errors.iter().any(|e| e.message.contains("*mut")));
    }

    #[test]
    fn test_rejects_extern_block() {
        let code = r#"extern "C" { fn malloc(size: usize) -> *mut u8; }"#;
        let result = validate_evaluator_source(code);
        assert!(result.is_err());
        // Either extern block or raw pointer error
        assert!(!result.unwrap_err().is_empty());
    }

    #[test]
    fn test_rejects_mod_declaration() {
        let code = "mod secret { fn leak() {} }";
        let result = validate_evaluator_source(code);
        assert!(result.is_err());
        let errors = result.unwrap_err();
        assert!(errors.iter().any(|e| e.message.contains("`mod`")));
    }

    #[test]
    fn test_rejects_include_macro() {
        let code = r#"fn foo() { include!("evil.rs"); }"#;
        let result = validate_evaluator_source(code);
        assert!(result.is_err());
        let errors = result.unwrap_err();
        assert!(errors.iter().any(|e| e.message.contains("include")));
    }

    #[test]
    fn test_rejects_env_macro() {
        let code = r#"fn foo() -> &'static str { env!("PATH") }"#;
        let result = validate_evaluator_source(code);
        assert!(result.is_err());
        let errors = result.unwrap_err();
        assert!(errors.iter().any(|e| e.message.contains("env")));
    }

    #[test]
    fn test_rejects_option_env_macro() {
        let code = r#"fn foo() { let _ = option_env!("SECRET"); }"#;
        let result = validate_evaluator_source(code);
        assert!(result.is_err());
        let errors = result.unwrap_err();
        assert!(errors.iter().any(|e| e.message.contains("option_env")));
    }

    #[test]
    fn test_rejects_include_str_macro() {
        let code = r#"const DATA: &str = include_str!("secret.txt");"#;
        let result = validate_evaluator_source(code);
        assert!(result.is_err());
        let errors = result.unwrap_err();
        assert!(errors.iter().any(|e| e.message.contains("include_str")));
    }

    #[test]
    fn test_error_contains_line_number() {
        // unsafe on line 3
        let code = "fn foo() {\n    let x = 1;\n    unsafe { let _ = x; }\n}";
        let result = validate_evaluator_source(code);
        assert!(result.is_err());
        let errors = result.unwrap_err();
        assert!(!errors.is_empty());
        // Line number must be present and positive
        assert!(errors[0].line > 0);
        // The Display format should contain the line number
        let display = errors[0].to_string();
        assert!(display.contains("Line"));
    }

    #[test]
    fn test_multiple_violations_reported() {
        let code = "use std::fs; use std::net;";
        let result = validate_evaluator_source(code);
        assert!(result.is_err());
        let errors = result.unwrap_err();
        assert!(errors.len() >= 2);
    }

    #[test]
    fn test_allowed_std_imports_pass() {
        // std::collections, std::fmt etc. are allowed
        let code = r#"
use std::collections::HashMap;
use std::fmt;
fn foo() {}
"#;
        assert!(validate_evaluator_source(code).is_ok());
    }

    #[test]
    fn test_rejects_direct_std_fs_call() {
        let code = r#"fn f() { let _ = std::fs::read_to_string("x"); }"#;
        let result = validate_evaluator_source(code);
        assert!(result.is_err(), "direct std::fs path must be rejected");
        let errors = result.unwrap_err();
        assert!(
            errors.iter().any(|e| e.message.contains("std::fs")),
            "error must mention std::fs, got: {:?}",
            errors
        );
    }

    #[test]
    fn test_rejects_direct_std_env_call() {
        let code = r#"fn f() { let _ = std::env::var("X"); }"#;
        let result = validate_evaluator_source(code);
        assert!(result.is_err(), "direct std::env path must be rejected");
        let errors = result.unwrap_err();
        assert!(
            errors.iter().any(|e| e.message.contains("std::env")),
            "error must mention std::env, got: {:?}",
            errors
        );
    }

    #[test]
    fn test_rejects_direct_std_io() {
        let code = r#"fn f() { let _ = std::io::stdin(); }"#;
        let result = validate_evaluator_source(code);
        assert!(result.is_err(), "direct std::io path must be rejected");
        let errors = result.unwrap_err();
        assert!(
            errors.iter().any(|e| e.message.contains("std::io")),
            "error must mention std::io, got: {:?}",
            errors
        );
    }

    #[test]
    fn test_allowed_std_collections_path() {
        let code = r#"fn f() { let _: std::collections::HashMap<i32, i32> = std::collections::HashMap::new(); }"#;
        let result = validate_evaluator_source(code);
        assert!(result.is_ok(), "std::collections path must be allowed, got: {:?}", result.err());
    }

    #[test]
    fn test_rejects_println_macro() {
        let code = r#"fn f() { println!("hi"); }"#;
        let result = validate_evaluator_source(code);
        assert!(result.is_err(), "println! macro must be rejected");
        let errors = result.unwrap_err();
        assert!(
            errors.iter().any(|e| e.message.contains("println")),
            "error must mention println, got: {:?}",
            errors
        );
    }

    #[test]
    fn test_rejects_eprintln_macro() {
        let code = r#"fn f() { eprintln!("hi"); }"#;
        let result = validate_evaluator_source(code);
        assert!(result.is_err(), "eprintln! macro must be rejected");
        let errors = result.unwrap_err();
        assert!(
            errors.iter().any(|e| e.message.contains("eprintln")),
            "error must mention eprintln, got: {:?}",
            errors
        );
    }

    #[test]
    fn test_rejects_static_mut() {
        let code = "static mut X: i32 = 0; fn f() {}";
        let result = validate_evaluator_source(code);
        assert!(result.is_err(), "static mut must be rejected");
        let errors = result.unwrap_err();
        assert!(
            errors.iter().any(|e| e.message.contains("static")),
            "error must mention static, got: {:?}",
            errors
        );
    }

    #[test]
    fn test_rejects_static_non_const() {
        let code = "static X: i32 = 0; fn f() {}";
        let result = validate_evaluator_source(code);
        assert!(result.is_err(), "static declaration must be rejected");
        let errors = result.unwrap_err();
        assert!(
            errors.iter().any(|e| e.message.contains("static")),
            "error must mention static, got: {:?}",
            errors
        );
    }

    #[test]
    fn test_allows_const() {
        let code = "const X: i32 = 42; fn f() {}";
        let result = validate_evaluator_source(code);
        assert!(result.is_ok(), "const declaration must be allowed, got: {:?}", result.err());
    }
}
