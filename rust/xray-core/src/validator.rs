/// Rust AST whitelist validator for user-supplied evaluator source code.
///
/// Uses `syn` to parse the code and walk the AST, rejecting any forbidden
/// constructs before they reach the compiler.
use proc_macro2::TokenStream;
use syn::parse::Parser;
use syn::punctuated::Punctuated;
use syn::visit::Visit;
use syn::{File, ItemMod};

mod source_limits;
mod std_paths;

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
    if let Some(error) = source_limits::check_source_limits(source) {
        return Err(vec![error]);
    }
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

/// Validates graph-mode evaluator source: the same forbidden-construct rules
/// as `validate_evaluator_source`, then requires the source to classify as
/// `EvaluatorMode::Graph` (legacy-shaped, mixed or empty sources are rejected).
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
    // ^ invariant macro_depth <= MAX_MACRO_RECURSION_DEPTH; visit_macro re-enters itself through allowlisted macro arguments
    macro_depth: usize,
}

impl ForbiddenConstructVisitor {
    fn add_error(&mut self, line: usize, message: String) {
        self.errors.push(ValidationError { line, message });
    }

    fn span_line(span: proc_macro2::Span) -> usize {
        span.start().line
    }

    // ^ security allowlisted macro arguments => parsed with the macro's grammar and visited like code; unparseable => rejected
    // ^ external syn::Macro.tokens is an unparsed TokenStream; Visit never walks into it on its own
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

    // ^ invariant vec![expr; count] => both expr and count visited
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

    // ^ invariant matches!(scrutinee, pattern if guard) => scrutinee, pattern and guard visited
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
/// `local::vec`, `no_mangle`).
fn path_to_string(path: &syn::Path) -> String {
    path.segments
        .iter()
        .map(|s| s.ident.to_string())
        .collect::<Vec<_>>()
        .join("::")
}

// ^ security syn ::Verbatim node = tokens syn did not parse => rejected; no other visit_* method sees inside it
fn unparsed_error(kind: &str, tokens: &TokenStream) -> (usize, String) {
    // ^ invariant Verbatim tokens are non-empty (syn builds them from consumed input); 1 only labels the error line
    let line = tokens.clone().into_iter().next().map(|t| t.span().start().line).unwrap_or(1);
    (line, format!("unparsed {kind} syntax is not allowed in evaluator code (it could not be fully inspected)"))
}

macro_rules! reject_verbatim {
    ($lt:lifetime; $($method:ident, $ty:ident, $label:literal;)*) => {$(
        fn $method(&mut self, node: &$lt syn::$ty) {
            if let syn::$ty::Verbatim(tokens) = node {
                let (line, message) = unparsed_error($label, tokens);
                self.add_error(line, message);
            }
            syn::visit::$method(self, node);
        }
    )*};
}

impl<'ast> Visit<'ast> for ForbiddenConstructVisitor {
    reject_verbatim! { 'ast;
        visit_item, Item, "item";
        visit_impl_item, ImplItem, "impl item";
        visit_trait_item, TraitItem, "trait item";
        visit_foreign_item, ForeignItem, "foreign item";
        visit_expr, Expr, "expression";
        visit_type, Type, "type";
        visit_pat, Pat, "pattern";
        visit_type_param_bound, TypeParamBound, "type bound";
    }

    fn visit_expr_unsafe(&mut self, node: &'ast syn::ExprUnsafe) {
        let line = Self::span_line(node.unsafe_token.span);
        self.add_error(line, "`unsafe` blocks are not allowed in evaluator code".to_string());
        syn::visit::visit_expr_unsafe(self, node);
    }

    fn visit_item_impl(&mut self, node: &'ast syn::ItemImpl) {
        if let Some(unsafety) = &node.unsafety {
            let line = Self::span_line(unsafety.span);
            self.add_error(line, "`unsafe impl` blocks are not allowed in evaluator code".to_string());
        }
        syn::visit::visit_item_impl(self, node);
    }

    fn visit_item_trait(&mut self, node: &'ast syn::ItemTrait) {
        if let Some(unsafety) = &node.unsafety {
            let line = Self::span_line(unsafety.span);
            self.add_error(line, "`unsafe trait` declarations are not allowed in evaluator code".to_string());
        }
        syn::visit::visit_item_trait(self, node);
    }

    fn visit_item_use(&mut self, node: &'ast syn::ItemUse) {
        for (line, message) in std_paths::use_tree_violations(&node.tree) {
            self.add_error(line, message);
        }
        syn::visit::visit_item_use(self, node);
    }

    fn visit_item_extern_crate(&mut self, node: &'ast syn::ItemExternCrate) {
        let line = Self::span_line(node.extern_token.span);
        self.add_error(line, "`extern crate` declarations are not allowed in evaluator code".to_string());
        syn::visit::visit_item_extern_crate(self, node);
    }

    fn visit_path(&mut self, node: &'ast syn::Path) {
        let segments: Vec<String> = node.segments.iter().map(|s| std_paths::canonical(&s.ident)).collect();
        if segments.len() >= 2 && std_paths::is_std_root(&segments[0]) && !std_paths::is_permitted(&segments) {
            let line = Self::span_line(node.segments[0].ident.span());
            self.add_error(
                line,
                format!(
                    "`{}` is not a permitted standard-library path in evaluator code (permitted: {})",
                    segments.join("::"),
                    std_paths::permitted_list()
                ),
            );
        }
        syn::visit::visit_path(self, node);
    }

    fn visit_item_static(&mut self, node: &'ast syn::ItemStatic) {
        let line = Self::span_line(node.static_token.span);
        self.add_error(
            line,
            "`static` declarations are not allowed in evaluator code".to_string(),
        );
        syn::visit::visit_item_static(self, node);
    }

    fn visit_type_ptr(&mut self, node: &'ast syn::TypePtr) {
        let kind = if node.mutability.is_some() { "*mut" } else { "*const" };
        let line = Self::span_line(node.star_token.spans[0]);
        self.add_error(
            line,
            format!("Raw pointer type `{}` is not allowed in evaluator code", kind),
        );
        syn::visit::visit_type_ptr(self, node);
    }

    fn visit_item_foreign_mod(&mut self, node: &'ast syn::ItemForeignMod) {
        let line = Self::span_line(node.brace_token.span.open());
        self.add_error(line, "`extern` blocks are not allowed in evaluator code".to_string());
        syn::visit::visit_item_foreign_mod(self, node);
    }

    // ^ invariant every fn form with a Signature (free, impl, trait, foreign) => checked here
    fn visit_signature(&mut self, node: &'ast syn::Signature) {
        if let Some(unsafety) = &node.unsafety {
            let line = Self::span_line(unsafety.span);
            self.add_error(line, "`unsafe` functions are not allowed in evaluator code".to_string());
        }
        if let Some(abi) = &node.abi {
            let line = Self::span_line(abi.extern_token.span);
            self.add_error(line, "`extern` ABI functions are not allowed in evaluator code".to_string());
        }
        syn::visit::visit_signature(self, node);
    }

    fn visit_type_bare_fn(&mut self, node: &'ast syn::TypeBareFn) {
        if let Some(unsafety) = &node.unsafety {
            let line = Self::span_line(unsafety.span);
            self.add_error(line, "`unsafe` function pointer types are not allowed in evaluator code".to_string());
        }
        if let Some(abi) = &node.abi {
            let line = Self::span_line(abi.extern_token.span);
            self.add_error(line, "`extern` ABI function pointer types are not allowed in evaluator code".to_string());
        }
        syn::visit::visit_type_bare_fn(self, node);
    }

    fn visit_item_mod(&mut self, node: &'ast ItemMod) {
        let line = Self::span_line(node.mod_token.span);
        self.add_error(line, "`mod` declarations are not allowed in evaluator code".to_string());
        syn::visit::visit_item_mod(self, node);
    }

    // ^ security every macro_rules! definition => rejected; rustc expands its body after validation
    // ^ hazard selectively accepting macro_rules! bodies => code this visitor never sees reaches rustc
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
            return;
        }
        syn::visit::visit_item_macro(self, node);
    }

    // ^ security macro invocations = allowlist of bare, single-segment vec!/format!/matches!; any other name or path => rejected
    // ^ coupled ALLOWED_MACROS with rust/xray-core/tests/s0a_validator_reject_sites.rs:ALLOWED_MACROS; test:rule_tables_match_validator_source
    fn visit_macro(&mut self, node: &'ast syn::Macro) {
        const ALLOWED_MACROS: &[&str] = &["vec", "format", "matches"];
        // ^ invariant MAX_MACRO_RECURSION_DEPTH = 32; test:macro_recursion_depth_boundary_pins_the_intended_limit_value
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
                format!("macro nesting exceeds the maximum depth of {}", MAX_MACRO_RECURSION_DEPTH),
            );
            return;
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

    // ^ security attributes = allowlist of `doc` only; any other attribute (no_mangle, no_std, ...) => rejected
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

#[cfg(test)]
mod tests_constructs;

#[cfg(test)]
mod tests_source_limits;

#[cfg(test)]
mod tests_std_paths;
