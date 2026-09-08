/// Rust AST whitelist validator for user-supplied evaluator source code.
///
/// Uses `syn` to parse the code and walk the AST, rejecting any forbidden
/// constructs before they reach the compiler.
use proc_macro2::TokenStream;
use syn::parse::Parser;
use syn::punctuated::Punctuated;
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
        macro_depth: 0,
    };
    visitor.visit_file(&file);

    if visitor.errors.is_empty() {
        Ok(())
    } else {
        Err(visitor.errors)
    }
}

/// AC8: the Rust-side gate for graph-mode evaluator source, mirroring
/// `validate_evaluator_source`'s existing pattern exactly. Runs the
/// IDENTICAL forbidden-construct visitor first -- validator.rs's bans
/// are UNCHANGED for graph mode; callbacks take state as PARAMETERS
/// (`&CodeGraph`, `&FactIndex`, `&LocalIndex`), so the `static`/
/// `static mut` ban still applies with full force. Then requires
/// `crate::compiler::detect_evaluator_mode` to classify the source as
/// `EvaluatorMode::Graph` -- a legacy-shaped, mixed, or empty source is
/// rejected here too, never silently accepted as "close enough".
pub fn validate_rust_graph_evaluator(source: &str) -> Result<(), Vec<ValidationError>> {
    validate_evaluator_source(source)?;
    match crate::compiler::detect_evaluator_mode(source) {
        Ok(crate::compiler::EvaluatorMode::Graph) => Ok(()),
        Ok(crate::compiler::EvaluatorMode::Legacy) => Err(vec![ValidationError {
            line: 0,
            message: "source defines evaluate_node (legacy mode) -- graph mode requires \
                      collect_facts and analyze_graph instead"
                .to_string(),
        }]),
        Err(compile_error) => Err(vec![ValidationError { line: 0, message: compile_error.message }]),
    }
}

// ---- Visitor implementation ----

struct ForbiddenConstructVisitor {
    errors: Vec<ValidationError>,
    /// R3-3 (Codex re-review, ROUND 3): tracks recursive macro-token
    /// validation nesting (visit_macro -> validate_allowed_macro_tokens
    /// -> self.visit_expr -> visit_macro again for a nested macro).
    /// ~2000 nested vec! overflowed the real call stack -- this bounds
    /// the recursion instead of relying on the subprocess boundary alone
    /// to contain a stack overflow.
    macro_depth: usize,
}

impl ForbiddenConstructVisitor {
    fn add_error(&mut self, line: usize, message: String) {
        self.errors.push(ValidationError { line, message });
    }

    fn span_line(span: proc_macro2::Span) -> usize {
        span.start().line
    }

    /// R2-1 CRITICAL (Codex re-review): recursively parse and validate an
    /// ALLOWLISTED macro's token stream against its REAL grammar, then
    /// re-run THIS SAME visitor over the resulting sub-expressions/
    /// patterns -- so a forbidden construct nested inside an allowed
    /// macro's arguments (e.g. `format!("{}", std::process::Command::
    /// new("id").spawn())`) is caught exactly as if it had been written
    /// outside the macro. `syn::Macro.tokens` is otherwise an OPAQUE,
    /// unparsed `TokenStream` that `Visit` cannot walk into on its own.
    /// Thin dispatcher -- see `validate_vec_macro_tokens`/
    /// `validate_matches_macro_tokens`/`validate_comma_separated_exprs`
    /// for the per-macro grammars.
    fn validate_allowed_macro_tokens(&mut self, name: &str, tokens: TokenStream, fallback_line: usize) {
        match name {
            "vec" => self.validate_vec_macro_tokens(tokens, fallback_line),
            "format" => self.validate_comma_separated_exprs(name, tokens, fallback_line),
            "matches" => self.validate_matches_macro_tokens(tokens, fallback_line),
            other => unreachable!(
                "validate_allowed_macro_tokens called with non-allowlisted macro name: {}",
                other
            ),
        }
    }

    /// `vec![a, b, c]` (comma-separated exprs) OR `vec![expr; count]`
    /// (array-repeat form) -- tries the repeat form first since it has a
    /// distinct `;` separator; BOTH the repeated element and the count
    /// are inspected, since either can hide a forbidden construct.
    fn validate_vec_macro_tokens(&mut self, tokens: TokenStream, fallback_line: usize) {
        let repeat_parser = |input: syn::parse::ParseStream<'_>| -> syn::Result<(syn::Expr, syn::Expr)> {
            let expr: syn::Expr = input.parse()?;
            input.parse::<syn::Token![;]>()?;
            let count: syn::Expr = input.parse()?;
            if !input.is_empty() {
                return Err(input.error("unexpected trailing tokens"));
            }
            Ok((expr, count))
        };
        if let Ok((expr, count)) = repeat_parser.parse2(tokens.clone()) {
            self.visit_expr(&expr);
            self.visit_expr(&count);
            return;
        }
        self.validate_comma_separated_exprs("vec", tokens, fallback_line);
    }

    /// `matches!(scrutinee, pattern1 | pattern2 if guard)` -- the guard
    /// clause is optional. Fails CLOSED on anything that doesn't parse
    /// under this exact grammar.
    fn validate_matches_macro_tokens(&mut self, tokens: TokenStream, fallback_line: usize) {
        let matches_parser = |input: syn::parse::ParseStream<'_>| -> syn::Result<(syn::Expr, syn::Pat, Option<syn::Expr>)> {
            let scrutinee: syn::Expr = input.parse()?;
            input.parse::<syn::Token![,]>()?;
            let pattern = syn::Pat::parse_multi_with_leading_vert(input)?;
            let guard = if input.peek(syn::Token![if]) {
                input.parse::<syn::Token![if]>()?;
                Some(input.parse::<syn::Expr>()?)
            } else {
                None
            };
            if !input.is_empty() {
                return Err(input.error("unexpected trailing tokens"));
            }
            Ok((scrutinee, pattern, guard))
        };
        match matches_parser.parse2(tokens) {
            Ok((scrutinee, pattern, guard)) => {
                self.visit_expr(&scrutinee);
                self.visit_pat(&pattern);
                if let Some(guard_expr) = guard {
                    self.visit_expr(&guard_expr);
                }
            }
            Err(_) => {
                self.add_error(
                    fallback_line,
                    "`matches!` macro arguments could not be fully inspected and are rejected (fail-closed)".to_string(),
                );
            }
        }
    }

    /// Shared token-tree validator for vec!/format!: both take a
    /// comma-separated list of expressions (format!'s leading format
    /// string is itself just a string-literal expression, harmlessly
    /// visited like any other -- it cannot inject new code, only
    /// reference already-in-scope names via `{name}` captures).
    fn validate_comma_separated_exprs(&mut self, name: &str, tokens: TokenStream, fallback_line: usize) {
        match Punctuated::<syn::Expr, syn::Token![,]>::parse_terminated.parse2(tokens) {
            Ok(exprs) => {
                for expr in exprs.iter() {
                    self.visit_expr(expr);
                }
            }
            Err(_) => {
                self.add_error(
                    fallback_line,
                    format!(
                        "`{}!` macro arguments could not be fully inspected and are rejected (fail-closed)",
                        name
                    ),
                );
            }
        }
    }
}

/// Render a `syn::Path` as a `::`-joined string for error messages (e.g.
/// `evil::vec`, `no_mangle`).
fn path_to_string(path: &syn::Path) -> String {
    path.segments
        .iter()
        .map(|s| s.ident.to_string())
        .collect::<Vec<_>>()
        .join("::")
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

    // CRITICAL (Codex follow-up review): reject ALL `macro_rules!`
    // definitions outright. A macro DEFINITION's expansion body is an
    // opaque, unparsed `proc_macro2::TokenStream` -- `syn`'s `Visit` trait
    // has no structured AST to walk into it with, so NONE of the other
    // visit_* methods in this file (visit_path, visit_expr_unsafe, etc.)
    // can ever see a forbidden construct hidden inside one. Without this,
    // a user could define `macro_rules! innocuous { () => {
    // std::process::Command::new(..).spawn(); } }`, invoke `innocuous!()`
    // under a name that passes the `visit_macro` allowlist below, and have
    // the validator see nothing wrong -- rustc expands the macro body
    // AFTER this validator runs, at which point the forbidden code is
    // real, compiled, and dlopen()'d by the server (arbitrary code
    // execution). Never attempt to parse/expand and selectively whitelist
    // macro_rules! bodies -- that reintroduces the same bypass class one
    // token pattern at a time.
    fn visit_item_macro(&mut self, node: &'ast syn::ItemMacro) {
        if node.ident.is_some() {
            let line = node
                .mac
                .path
                .segments
                .first()
                .map(|s| Self::span_line(s.ident.span()))
                .unwrap_or(0);
            self.add_error(
                line,
                "`macro_rules!` definitions are not allowed in evaluator code".to_string(),
            );
            return; // never recurse into the opaque macro_rules! body
        }
        syn::visit::visit_item_macro(self, node);
    }

    // CRITICAL (Codex follow-up review, R2-1 re-review): FAIL-CLOSED
    // ALLOWLIST, not a blocklist. A macro invocation whose name is not
    // explicitly listed here is rejected, no matter how innocuous it
    // looks -- this is what makes the `visit_item_macro` ban above
    // actually effective: a blocklist checking only well-known I/O macro
    // NAMES (println!, include!, etc.) would let a user-defined macro's
    // INVOCATION sail through under any unlisted name. `vec!`/`format!`/
    // `matches!` are the only macros this project's own shipped evaluator
    // examples (incl. the catch-rethrow seed pattern) use.
    //
    // R2-1 fixes two gaps a re-review found in the FIRST version of this
    // allowlist:
    //   1. The path match was LAST-SEGMENT-ONLY (`node.path.segments.
    //      last()`), so a QUALIFIED path ending in an allowlisted name --
    //      `evil::vec!`, `std::vec!`, `::vec!` -- sailed through. Now
    //      requires a bare, single-segment, non-leading-colon path.
    //   2. An allowlisted macro's TOKEN STREAM was never inspected --
    //      `syn::Macro.tokens` is opaque to `Visit`, so a forbidden
    //      construct hidden inside e.g. `format!("{}", std::process::
    //      Command::new("id").spawn())` was accepted. Now dispatches to
    //      `validate_allowed_macro_tokens`, which recursively parses the
    //      tokens into their real grammar and re-runs this visitor over
    //      the result, failing CLOSED on anything it cannot parse.
    fn visit_macro(&mut self, node: &'ast syn::Macro) {
        const ALLOWED_MACROS: &[&str] = &["vec", "format", "matches"];
        // R3-3 (Codex re-review, ROUND 3): ~2000 nested vec! overflowed
        // the real call stack during validation, aborting the xray-cli
        // child process. This bounds macro-nesting recursion explicitly
        // instead of relying solely on the subprocess boundary to
        // contain a stack overflow -- 32 is far beyond any realistic
        // legitimate evaluator's nesting depth.
        const MAX_MACRO_RECURSION_DEPTH: usize = 32;

        let name = node
            .path
            .segments
            .last()
            .map(|s| s.ident.to_string())
            .unwrap_or_default();
        let name_line = node
            .path
            .segments
            .first()
            .map(|s| Self::span_line(s.ident.span()))
            .unwrap_or(0);
        let is_bare_path = node.path.leading_colon.is_none() && node.path.segments.len() == 1;

        if self.macro_depth >= MAX_MACRO_RECURSION_DEPTH {
            self.add_error(
                name_line,
                format!(
                    "macro nesting exceeds the maximum allowed depth ({}) -- \
                     rejected to avoid a stack overflow during validation",
                    MAX_MACRO_RECURSION_DEPTH
                ),
            );
            return; // never recurse further -- exactly what this guards against
        }

        if !is_bare_path {
            self.add_error(
                name_line,
                format!(
                    "qualified macro invocation `{}!` is not allowed -- only a bare, \
                     unqualified vec!/format!/matches! is permitted",
                    path_to_string(&node.path)
                ),
            );
        } else if !ALLOWED_MACROS.contains(&name.as_str()) {
            self.add_error(
                name_line,
                format!(
                    "`{}!` macro is not allowed in evaluator code (only vec!/format!/matches! are permitted)",
                    name
                ),
            );
        } else {
            self.macro_depth += 1;
            self.validate_allowed_macro_tokens(&name, node.tokens.clone(), name_line);
            self.macro_depth -= 1;
        }

        syn::visit::visit_macro(self, node);
    }

    // R2-1 (Codex re-review): attributes and their token streams were
    // accepted without ANY inspection. `#[no_mangle]` can be used to
    // clobber or impersonate exported symbols in the compiled dynamic
    // library; `#![no_std]` and any other attribute carry an opaque token
    // stream this validator cannot otherwise see into. Fail-closed
    // ALLOWLIST: only `doc` (the lowered form of `///`/`//!` comments,
    // which carry no executable content) is permitted -- every other
    // attribute name is rejected outright, regardless of what its own
    // token stream contains.
    fn visit_attribute(&mut self, node: &'ast syn::Attribute) {
        if !node.path().is_ident("doc") {
            let name = path_to_string(node.path());
            let line = Self::span_line(node.pound_token.span);
            self.add_error(
                line,
                format!(
                    "`#[{name}]`/`#![{name}]` attributes are not allowed in evaluator \
                     code (only doc comments are permitted)",
                    name = name
                ),
            );
        }
        syn::visit::visit_attribute(self, node);
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

    // --- CRITICAL (Codex follow-up review): user-defined macros bypass
    // the entire evaluator security whitelist ---
    //
    // `visit_macro` previously inspected only the macro NAME at each
    // INVOCATION site against a fixed blocklist -- it never looked at
    // `macro_rules!` DEFINITION bodies, which `syn` stores as an opaque,
    // unparsed `TokenStream` its `Visit` trait cannot walk into. A user
    // could therefore define a macro whose expansion contains forbidden
    // constructs, invoke it under an innocuous name, and have the
    // validator see nothing wrong -- rustc expands macro_rules! bodies
    // AFTER validation, at which point the forbidden code is real,
    // compiled, and dlopen()'d by the server. Fixed via `visit_item_macro`
    // (rejects every macro_rules! definition outright) plus converting
    // `visit_macro` to a fail-closed allowlist.

    #[test]
    fn macro_rules_definition_smuggling_std_process_must_be_rejected() {
        let payload = r#"
macro_rules! innocuous_helper {
    () => {
        std::process::Command::new("id").spawn().ok();
    };
}
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    innocuous_helper!();
    Vec::new()
}
"#;
        assert!(
            validate_evaluator_source(payload).is_err(),
            "a macro_rules! definition must be rejected outright -- its body is opaque \
             to this validator and can smuggle any forbidden construct, including \
             std::process::Command"
        );
    }

    #[test]
    fn macro_rules_definition_smuggling_unsafe_must_be_rejected() {
        let payload = r#"
macro_rules! innocuous_helper {
    () => {
        unsafe { std::ptr::null::<u8>().read(); }
    };
}
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    innocuous_helper!();
    Vec::new()
}
"#;
        assert!(
            validate_evaluator_source(payload).is_err(),
            "a macro_rules! definition must be rejected outright, regardless of what \
             forbidden construct its body smuggles"
        );
    }

    #[test]
    fn non_whitelisted_macro_invocation_is_rejected_fail_closed() {
        let payload = r#"
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    println!("{}", node.kind);
    Vec::new()
}
"#;
        assert!(
            validate_evaluator_source(payload).is_err(),
            "a macro invocation not on the explicit allowlist must be rejected \
             fail-closed, even one as innocuous-looking as println!"
        );
    }

    #[test]
    fn allowlisted_macros_vec_and_format_are_still_accepted() {
        let payload = r#"
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    let msg = format!("kind={}", node.kind);
    vec![EvalFinding { pattern: "x".to_string(), line: node.start_line, snippet: msg }]
}
"#;
        assert!(
            validate_evaluator_source(payload).is_ok(),
            "vec!/format! are the two macros this project's own documented evaluator \
             examples use and must remain allowed"
        );
    }

    // --- R2-1 CRITICAL (Codex re-review): allowlisted macro token streams
    // were never inspected, and the path match was last-segment-only ---

    #[test]
    fn qualified_vec_macro_path_is_rejected() {
        // No `mod evil { ... }` declaration on purpose: syn only parses
        // SYNTAX, not semantics, so `evil::vec![1, 2, 3]` is syntactically
        // valid without `evil` needing to exist anywhere. Declaring the
        // module would accidentally trigger the UNRELATED pre-existing
        // `mod` ban (visit_item_mod) instead of genuinely discriminating
        // on the path-exactness check this test targets.
        let payload = r#"
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    let _v: Vec<i32> = evil::vec![1, 2, 3];
    Vec::new()
}
"#;
        let result = validate_evaluator_source(payload);
        assert!(
            result.is_err(),
            "a QUALIFIED macro path (evil::vec!) must be rejected even though \
             the last path segment matches an allowlisted name -- only a bare, \
             unqualified vec!/format!/matches! is permitted"
        );
        let errors = result.unwrap_err();
        assert!(
            !errors.iter().any(|e| e.message.contains("mod")),
            "this test must discriminate on the QUALIFIED PATH check \
             specifically, not an unrelated `mod` declaration ban; got: {:?}",
            errors
        );
    }

    #[test]
    fn forbidden_construct_hidden_inside_format_macro_tokens_is_rejected() {
        let payload = r#"
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    let msg = format!("{}", std::process::Command::new("id").spawn().unwrap().id());
    vec![EvalFinding { pattern: "x".to_string(), line: node.start_line, snippet: msg }]
}
"#;
        assert!(
            validate_evaluator_source(payload).is_err(),
            "a forbidden construct (std::process::Command) hidden inside an \
             ALLOWLISTED format! macro's argument tokens must still be \
             rejected -- the allowlist covers the macro NAME, not a license \
             to skip inspecting its contents"
        );
    }

    #[test]
    fn forbidden_construct_hidden_inside_vec_macro_tokens_is_rejected() {
        let payload = r#"
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    let _x = vec![std::process::Command::new("id").spawn().unwrap().id() as i32];
    Vec::new()
}
"#;
        assert!(
            validate_evaluator_source(payload).is_err(),
            "a forbidden construct hidden inside an ALLOWLISTED vec! macro's \
             argument tokens must still be rejected"
        );
    }

    #[test]
    fn vec_repeat_form_with_forbidden_construct_in_count_is_rejected() {
        let payload = r#"
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    let _x: Vec<i32> = vec![0; std::process::Command::new("id").spawn().unwrap().id() as usize];
    Vec::new()
}
"#;
        assert!(
            validate_evaluator_source(payload).is_err(),
            "the vec![expr; count] array-repeat form's COUNT expression must \
             also be inspected, not just the repeated element"
        );
    }

    // --- R2-2 HIGH regression fix (Codex re-review): matches! must be
    // safely supported (token-inspected), not merely re-allowlisted ---

    #[test]
    fn matches_macro_is_allowlisted_and_accepted() {
        let payload = r#"
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    let _ = matches!(node.kind.as_str(), "x" | "y" if node.start_line > 0);
    Vec::new()
}
"#;
        assert!(
            validate_evaluator_source(payload).is_ok(),
            "matches! is used by this project's own shipped seed patterns \
             (catch-rethrow) and must be allowed once its token tree is \
             safely inspected rather than special-cased"
        );
    }

    #[test]
    fn forbidden_construct_hidden_inside_matches_guard_is_rejected() {
        let payload = r#"
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    let _ = matches!(node.kind.as_str(), _ if { std::process::Command::new("id").spawn().unwrap(); true });
    Vec::new()
}
"#;
        assert!(
            validate_evaluator_source(payload).is_err(),
            "a forbidden construct hidden inside matches!'s `if` guard \
             expression must be rejected"
        );
    }

    #[test]
    fn forbidden_construct_hidden_inside_matches_scrutinee_is_rejected() {
        let payload = r#"
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    let _ = matches!(std::process::Command::new("id").spawn().unwrap().id(), 0);
    Vec::new()
}
"#;
        assert!(
            validate_evaluator_source(payload).is_err(),
            "a forbidden construct hidden inside matches!'s scrutinee \
             expression must be rejected"
        );
    }

    // --- R2-1 attribute closure (Codex re-review): #[no_mangle]/#![no_std]
    // and other attributes were accepted without inspection ---

    #[test]
    fn no_mangle_attribute_is_rejected() {
        let payload = r#"
#[no_mangle]
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    Vec::new()
}
"#;
        assert!(
            validate_evaluator_source(payload).is_err(),
            "`#[no_mangle]` must be rejected -- it can be used to clobber or \
             impersonate exported symbols in the compiled dynamic library"
        );
    }

    #[test]
    fn no_std_inner_attribute_is_rejected() {
        let payload = r#"
#![no_std]
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    Vec::new()
}
"#;
        assert!(
            validate_evaluator_source(payload).is_err(),
            "`#![no_std]` must be rejected -- attribute token streams are not \
             otherwise inspected and must not be blanket-accepted"
        );
    }

    #[test]
    fn doc_comment_attributes_are_still_accepted() {
        let payload = r#"
/// Finds try-catch-rethrow patterns.
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    Vec::new()
}
"#;
        assert!(
            validate_evaluator_source(payload).is_ok(),
            "doc comments (lowered to #[doc = \"...\"] attributes) must remain \
             allowed -- they carry no executable content"
        );
    }

    // --- R3-3 (Codex re-review, ROUND 3): recursive macro-token
    // validation has no depth bound -- ~2000 nested vec! overflowed the
    // real call stack, aborting the xray-cli child process. Test nesting
    // levels below are deliberately far below that ~2000 crash threshold
    // so these tests themselves can NEVER crash the test binary
    // regardless of whether the fix is present -- pre-fix, exceeding the
    // limit must be a normal, safe assertion failure, not a crash.
    // ---

    const TEST_NESTING_BEYOND_LIMIT: usize = 70;
    const TEST_NESTING_WITHIN_LIMIT: usize = 5;

    fn nested_vec_macro_payload(nesting: usize) -> String {
        let nested = format!("{}1{}", "vec![".repeat(nesting), "]".repeat(nesting));
        format!(
            "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {{\n    let _x = {};\n    Vec::new()\n}}\n",
            nested
        )
    }

    #[test]
    fn deeply_nested_macros_beyond_the_recursion_limit_are_rejected() {
        let payload = nested_vec_macro_payload(TEST_NESTING_BEYOND_LIMIT);
        let result = validate_evaluator_source(&payload);
        assert!(
            result.is_err(),
            "macro nesting beyond the recursion depth limit must be rejected \
             with a clear error, never silently accepted or left to overflow \
             the real call stack"
        );
        let errors = result.unwrap_err();
        assert!(
            errors.iter().any(|e| e.message.to_lowercase().contains("depth")
                || e.message.to_lowercase().contains("recursion")
                || e.message.to_lowercase().contains("nest")),
            "the rejection message should explain it's a nesting/recursion \
             depth limit, not an unrelated error; got: {:?}",
            errors
        );
    }

    #[test]
    fn modestly_nested_macros_within_the_limit_are_still_accepted() {
        let payload = nested_vec_macro_payload(TEST_NESTING_WITHIN_LIMIT);
        assert!(
            validate_evaluator_source(&payload).is_ok(),
            "modest, realistic macro nesting (well within the depth limit) \
             must remain accepted"
        );
    }

    // Bug #1815: the depth-70 test above proves "70 is rejected", not "the
    // limit is exactly 32" -- raising MAX_MACRO_RECURSION_DEPTH to 100
    // would leave it green while silently changing the security-relevant
    // ceiling. This test PINS the intended VALUE: the two literals below
    // (32, 33) are deliberately HARDCODED, matching
    // `MAX_MACRO_RECURSION_DEPTH` in `visit_macro` above, rather than
    // referencing that constant by name -- referencing it would make this
    // test drift together with any future change and defeat its entire
    // purpose as a pinning test.
    #[test]
    fn macro_recursion_depth_boundary_pins_the_intended_limit_value() {
        let at_limit = nested_vec_macro_payload(32);
        assert!(
            validate_evaluator_source(&at_limit).is_ok(),
            "macro nesting at exactly the intended limit (32) must be accepted"
        );

        let one_over_limit = nested_vec_macro_payload(33);
        assert!(
            validate_evaluator_source(&one_over_limit).is_err(),
            "macro nesting one level past the intended limit (33) must be rejected"
        );
    }

    // --- AC8: validate_rust_graph_evaluator (Rust-side gate for graph mode) ---

    /// AC8: "validate_rust_graph_evaluator mirroring the existing
    /// validator patterns. validator.rs's existing bans are UNCHANGED —
    /// callbacks take state as PARAMETERS, so the static mut ban stands."
    /// Four cases: valid graph source (Ok); graph source with a forbidden
    /// construct, e.g. `unsafe` (Err, same ban as legacy); legacy-shaped
    /// source with `evaluate_node` instead of graph callbacks (Err, wrong
    /// mode); source with neither family (Err).
    #[test]
    fn validate_rust_graph_evaluator_accepts_valid_graph_source_and_rejects_forbidden_constructs_and_wrong_mode() {
        let valid_graph = r#"
fn collect_facts(node: &OwnedNode, file: &str, index: &LocalIndex) -> Vec<UserFact> {
    Vec::new()
}
fn analyze_graph(g: &CodeGraph, facts: &FactIndex) -> GraphResult {
    GraphResult::default()
}
"#;
        assert!(validate_rust_graph_evaluator(valid_graph).is_ok(), "valid graph-mode source must pass");

        let graph_with_unsafe = r#"
fn collect_facts(node: &OwnedNode, file: &str, index: &LocalIndex) -> Vec<UserFact> {
    unsafe { Vec::new() }
}
fn analyze_graph(g: &CodeGraph, facts: &FactIndex) -> GraphResult {
    GraphResult::default()
}
"#;
        let result = validate_rust_graph_evaluator(graph_with_unsafe);
        assert!(result.is_err(), "graph-mode source with `unsafe` must still be rejected -- bans are unchanged");
        assert!(result.unwrap_err().iter().any(|e| e.message.contains("unsafe")));

        let legacy_only = "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { Vec::new() }";
        assert!(
            validate_rust_graph_evaluator(legacy_only).is_err(),
            "a legacy-shaped evaluator must not be accepted as graph mode"
        );

        let neither = "fn helper() -> i32 { 42 }";
        assert!(validate_rust_graph_evaluator(neither).is_err(), "source with no recognized callback family must be rejected");
    }

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
