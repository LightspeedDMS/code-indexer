//! `budget_bind.rs`'s relocated unit tests, first half (Messi Rule 6,
//! anti-file-bloat; mirrors `resolve.rs`'s own established `mod tests;`/
//! `mod tests_family;` multi-file test split). This half covers the
//! `widen_method_signature`/varargs-formatting tests; see
//! `budget_bind_tests_ac6.rs` for the AC6 budget-ladder tests.
//! `method_decl`/`invocation`/`file` are `pub(super)` so that sibling
//! module can reach them via `super::tests::{...}`, exactly like
//! `resolve_tests.rs`/`resolve_tests_family.rs` already do.

use super::*;
use crate::graph::extract::local_index::{
    Declaration, DeclarationKind, InvocationSite, LocalIndex, MethodOwnerRecord,
};
use crate::graph::extract::LanguageExtractor;
use crate::graph::identity::{make_symbol_id, SymbolId};

pub(super) fn method_decl(
    name: &str,
    file_id: u32,
    local: u32,
    param_count: Option<usize>,
) -> Declaration {
    Declaration {
        kind: DeclarationKind::Method,
        name: name.to_string(),
        line: 1,
        symbol: make_symbol_id(file_id, local),
        param_count,
        param_types: Vec::new(),
        is_varargs: false,
        vararg_index: None,
    }
}

pub(super) fn invocation(name: &str, arg_count: Option<usize>) -> InvocationSite {
    InvocationSite {
        callee_name: name.to_string(),
        line: 10,
        arg_count,
        arg_shapes: Vec::new(),
        receiver: crate::graph::extract::local_index::ReceiverExpr::None,
        enclosing_type: None,
        enclosing_method: None,
    }
}

pub(super) fn file(file_id: u32, language: &str, index: LocalIndex) -> FileForBind {
    FileForBind {
        file_id,
        language: language.to_string(),
        index,
    }
}

/// Bug #1904: a single-file fixture with one Method declaration
/// carrying real `param_types` and (optionally) a `MethodOwnerRecord`
/// linking it to its declaring type -- mirrors what a real extractor
/// (Java or Kotlin, #1908) actually populates at extraction time, but
/// stays independent of BOTH concrete extractors: this exercises
/// `budget_bind`'s own language-agnostic widening logic, never one
/// language's extraction code. `index.signatures` is seeded with the
/// OLD arity-only text every real extractor still inserts (java.rs/
/// kotlin.rs `format!("{name}({param_count} params)")`) -- the text
/// `widen_method_signature` must REPLACE for a Method declaration,
/// never merely copy through.
fn single_method_fixture(
    name: &str,
    owner: Option<&str>,
    param_count: Option<usize>,
    param_types: Vec<String>,
) -> (Vec<FileForBind>, SymbolId) {
    let symbol = make_symbol_id(1, 1);
    let mut index = LocalIndex::new();
    index.declarations.push(Declaration {
        kind: DeclarationKind::Method,
        name: name.to_string(),
        line: 5,
        symbol,
        param_count,
        param_types,
        is_varargs: false,
        vararg_index: None,
    });
    index
        .signatures
        .insert(symbol, format!("{name}({} params)", param_count.unwrap_or(0)));
    if let Some(owner) = owner {
        index.method_owners.push(MethodOwnerRecord {
            method_symbol: symbol,
            enclosing_type: owner.to_string(),
        });
    }
    (vec![file(1, "java", index)], symbol)
}

/// Bug #1904 discriminating RED/GREEN: a Method's cached signature
/// must carry its declaring type and real parameter types, never just
/// arity -- this is what lets the shipped signature-matching template
/// (`docs/xray-templates/callers-of-symbols-matching-signature-text.rs`)
/// match on a real declaring-class name instead of matching nothing.
#[test]
fn method_declaration_with_real_param_types_and_a_known_owner_produces_the_widened_signature() {
    let (files, symbol) =
        single_method_fixture("parse", Some("TimeUtil"), Some(1), vec!["XMLGregorianCalendar".to_string()]);
    let graph = bind_with_budget(files, &IndexBudget::unlimited());
    let dense = graph.dense_id_for(symbol).expect("method must be interned");
    assert_eq!(graph.signature_for(dense), Some("TimeUtil.parse(XMLGregorianCalendar)"));
}

/// Bug #1904: with no `MethodOwnerRecord` at all (the extractor could
/// not determine the enclosing type), the plain `name(Types)` form is
/// used -- never a fabricated declaring type.
#[test]
fn method_declaration_with_no_known_owner_omits_the_prefix_but_still_lists_real_param_types() {
    let (files, symbol) = single_method_fixture("parse", None, Some(1), vec!["String".to_string()]);
    let graph = bind_with_budget(files, &IndexBudget::unlimited());
    let dense = graph.dense_id_for(symbol).expect("method must be interned");
    assert_eq!(graph.signature_for(dense), Some("parse(String)"));
}

/// Bug #1904: `param_count` says 2 parameters but only 1 type was
/// successfully read -- presenting that ONE type as if it were the
/// complete list would be fabrication (Rule 10). Must fall back to
/// the arity-only `"N params"` form, still prefixed with the known
/// owner -- under-reporting is the safe direction, never a guessed
/// partial param list.
#[test]
fn method_declaration_with_incomplete_param_types_falls_back_to_the_arity_only_form_without_fabricating() {
    let (files, symbol) = single_method_fixture("connect", Some("Client"), Some(2), vec!["String".to_string()]);
    let graph = bind_with_budget(files, &IndexBudget::unlimited());
    let dense = graph.dense_id_for(symbol).expect("method must be interned");
    assert_eq!(graph.signature_for(dense), Some("Client.connect(2 params)"));
}

/// Bug #1904: `MethodOwnerRecord`'s own doc comment says a constructor
/// gets an owner record too (constructors use `DeclarationKind::
/// Method` so invocation sites resolve by the ordinary method path) --
/// widening must behave sensibly for one, not just an ordinary method.
#[test]
fn constructor_declaration_widens_using_its_owner_exactly_like_an_ordinary_method() {
    let (files, symbol) = single_method_fixture("OrderService", Some("OrderService"), Some(0), Vec::new());
    let graph = bind_with_budget(files, &IndexBudget::unlimited());
    let dense = graph.dense_id_for(symbol).expect("constructor must be interned");
    assert_eq!(graph.signature_for(dense), Some("OrderService.OrderService()"));
}

/// Bug #1929 item 1: a Java varargs last parameter (`char... chars`)
/// must render with the REAL Java ellipsis spelling `char...`, never
/// a bare `char` -- the shipped signature-matching recipe (substring
/// match on `signature_for`) cannot otherwise tell a genuine one-arg
/// overload apart from a varargs method, exactly the collision
/// reported live against a real-world `Reader.consumeToAny`.
#[test]
fn varargs_last_parameter_renders_with_the_real_java_ellipsis_spelling() {
    let (files, symbol) =
        single_varargs_method_fixture("consumeToAny", Some("Reader"), vec!["char".to_string()], "java");
    let graph = bind_with_budget(files, &IndexBudget::unlimited());
    let dense = graph.dense_id_for(symbol).expect("method must be interned");
    assert_eq!(graph.signature_for(dense), Some("Reader.consumeToAny(char...)"));
}

/// Bug #1929 rework (coordinator P2, proven by mutation): the two tests
/// immediately above exercise ONLY `format_param_types`/`widen_method_
/// signature` against a HAND-BUILT `Declaration` from `single_varargs_
/// method_fixture`, which sets `vararg_index` itself -- they never call
/// the real `JavaExtractor`, so they cannot catch a regression in
/// `java_methods::extract_method_declaration`'s own `vararg_index`
/// computation (`param_types.len() - 1`, JLS 8.4.1). Confirmed live: a
/// scratch-copy RED proof that neutralized `java_methods.rs`'s
/// `vararg_index` to unconditional `None` left every existing vararg
/// test (including both above) still GREEN. This test closes that gap
/// by driving the real end-to-end pipeline (real `JavaExtractor`, real
/// `bind_with_budget`) over actual Java source with a leading ordinary
/// parameter before the varargs one, exactly like the Kotlin extraction
/// tests below already do for Kotlin.
#[test]
fn java_vararg_last_parameter_extracted_from_real_source_renders_the_ellipsis_and_records_the_real_position(
) {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("Api.java");
    std::fs::write(
        &path,
        "class Api {\n    void log(String prefix, int... nums) {}\n}\n",
    )
    .unwrap();
    let root = crate::scanner::parse_file(&path).unwrap();
    let index = crate::graph::extract::java::JavaExtractor.extract(&root, 1);
    let declaration = index.declaration_named("log").expect("log must be declared").clone();
    assert!(declaration.is_varargs, "the real extractor must mark this method varargs");
    assert_eq!(
        declaration.vararg_index,
        Some(1),
        "the real extractor must record the vararg's actual position (index 1, after the \
         leading `prefix` parameter), never a fabricated or defaulted value"
    );

    let symbol = declaration.symbol;
    let files = vec![super::FileForBind { file_id: 1, language: "java".to_string(), index }];
    let graph = bind_with_budget(files, &IndexBudget::unlimited());
    let dense = graph.dense_id_for(symbol).expect("method must be interned");
    assert_eq!(graph.signature_for(dense), Some("Api.log(String, int...)"));
}

/// Bug #1929 item 1: in JAVA specifically, only the LAST parameter is
/// ever variadic (JLS 8.4.1) -- a leading ordinary parameter must
/// keep its plain spelling, never also gain an ellipsis. (Kotlin has
/// NO such restriction -- see the rework item 1 test immediately
/// below, which pins that a Kotlin `vararg` can sit at any position.)
#[test]
fn varargs_last_parameter_with_a_leading_ordinary_parameter_still_ellipsizes_only_the_last() {
    let (files, symbol) = single_varargs_method_fixture(
        "format",
        Some("Formatter"),
        vec!["String".to_string(), "Object".to_string()],
        "java",
    );
    let graph = bind_with_budget(files, &IndexBudget::unlimited());
    let dense = graph.dense_id_for(symbol).expect("method must be interned");
    assert_eq!(graph.signature_for(dense), Some("Formatter.format(String, Object...)"));
}

/// Bug #1929 rework item 1 (P2, reviewer-found): Kotlin allows a
/// `vararg` parameter at ANY position (later parameters are then
/// passed by NAME) -- unlike Java, where JLS 8.4.1 guarantees it is
/// always the LAST parameter. `Declaration::is_varargs` is a bare
/// bool with no position, so a naive `split_last()`-based
/// implementation puts the marker on the WRONG (actually-last, not
/// actually-variadic) parameter for a case like this one. Exercises
/// the REAL extraction-to-bind pipeline (real `KotlinExtractor`, real
/// `bind_with_budget`) -- the real position now comes straight from
/// `Declaration::vararg_index` (rework item 2, closes #1939), which
/// `KotlinExtractor` populates directly at extraction time; no marker
/// or cached-signature parsing is involved.
#[test]
fn kotlin_vararg_in_a_non_last_position_renders_its_marker_at_the_correct_parameter() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("Api.kt");
    std::fs::write(&path, "class Api {\n    fun mid(vararg xs: Int, tail: String) {}\n}\n").unwrap();
    let root = crate::scanner::parse_file(&path).unwrap();
    let index = crate::graph::extract::kotlin::KotlinExtractor.extract(&root, 1);
    let symbol = index.declaration_named("mid").expect("mid must be declared").symbol;

    let files = vec![super::FileForBind { file_id: 1, language: "kt".to_string(), index }];
    let graph = bind_with_budget(files, &IndexBudget::unlimited());
    let dense = graph.dense_id_for(symbol).expect("method must be interned");
    assert_eq!(
        graph.signature_for(dense),
        Some("Api.mid(vararg Int, String)"),
        "the vararg marker must land on `xs` (position 0), never unconditionally on the \
         textually-last parameter `tail`"
    );
}

/// Bug #1929 rework item 3 (Codex P3): a `vararg` in the MIDDLE of
/// three parameters (never the first, never the last) must still land
/// its marker on the REAL middle position -- the strongest possible
/// discriminator against any implementation that special-cases "first"
/// or "last" instead of tracking the actual position, exactly the class
/// of bug the non-last-position test above already caught once.
/// Exercises the real extraction-to-bind pipeline end to end (real
/// `KotlinExtractor`, real `bind_with_budget`), never a hand-built
/// `Declaration` fixture.
#[test]
fn kotlin_vararg_in_the_middle_of_three_parameters_renders_its_marker_at_the_correct_parameter() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("Api.kt");
    std::fs::write(
        &path,
        "class Api {\n    fun m(a: String, vararg b: Int, c: Boolean) {}\n}\n",
    )
    .unwrap();
    let root = crate::scanner::parse_file(&path).unwrap();
    let index = crate::graph::extract::kotlin::KotlinExtractor.extract(&root, 1);
    let symbol = index.declaration_named("m").expect("m must be declared").symbol;

    let files = vec![super::FileForBind { file_id: 1, language: "kt".to_string(), index }];
    let graph = bind_with_budget(files, &IndexBudget::unlimited());
    let dense = graph.dense_id_for(symbol).expect("method must be interned");
    assert_eq!(
        graph.signature_for(dense),
        Some("Api.m(String, vararg Int, Boolean)"),
        "the vararg marker must land on `b` (the middle parameter), never on the first \
         (`a`) or last (`c`) parameter"
    );
}

/// Bug #1929 rework item 3 (Codex P3): a Kotlin method with NO `vararg`
/// parameter at all must extract `Declaration::is_varargs == false` and
/// `vararg_index == None` -- never a fabricated position -- and render
/// its plain, unmarked parameter list. Exercises the real
/// extraction-to-bind pipeline end to end, complementing the vararg-
/// position tests above by pinning the "nothing to mark" baseline case.
#[test]
fn kotlin_method_with_no_vararg_parameter_renders_a_plain_parameter_list() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("Api.kt");
    std::fs::write(&path, "class Api {\n    fun plain(a: String, b: Int) {}\n}\n").unwrap();
    let root = crate::scanner::parse_file(&path).unwrap();
    let index = crate::graph::extract::kotlin::KotlinExtractor.extract(&root, 1);
    let declaration = index.declaration_named("plain").expect("plain must be declared").clone();
    assert!(!declaration.is_varargs, "a plain method must never be marked varargs");
    assert_eq!(
        declaration.vararg_index, None,
        "a non-varargs declaration must never carry a fabricated vararg position"
    );

    let symbol = declaration.symbol;
    let files = vec![super::FileForBind { file_id: 1, language: "kt".to_string(), index }];
    let graph = bind_with_budget(files, &IndexBudget::unlimited());
    let dense = graph.dense_id_for(symbol).expect("method must be interned");
    assert_eq!(graph.signature_for(dense), Some("Api.plain(String, Int)"));
}

/// Bug #1929 item 1: Kotlin spells a variadic parameter with the
/// leading `vararg` keyword (`vararg xs: Int`), never a trailing
/// ellipsis -- rendering the Java spelling for a Kotlin declaration
/// would itself be a fabricated, non-real-source spelling.
#[test]
fn varargs_last_parameter_renders_with_the_real_kotlin_vararg_spelling() {
    let (files, symbol) =
        single_varargs_method_fixture("logAll", None, vec!["Int".to_string()], "kt");
    let graph = bind_with_budget(files, &IndexBudget::unlimited());
    let dense = graph.dense_id_for(symbol).expect("method must be interned");
    assert_eq!(graph.signature_for(dense), Some("logAll(vararg Int)"));
}

/// Bug #1929 item 1 fixture helper: mirrors `single_method_fixture`
/// but sets `is_varargs: true` AND `vararg_index: Some(last position)`
/// on the sole declaration -- this fixture always represents the
/// varargs parameter sitting LAST (the only position Java allows, per
/// JLS 8.4.1), and lets the caller pick the file's language extension
/// (`"java"` vs `"kt"`), since the real spelling differs per language
/// (see the three tests above). `param_count` is always
/// `param_types.len()` -- the "complete list" branch
/// `widen_method_signature` needs to reach the varargs-aware
/// formatting path at all.
fn single_varargs_method_fixture(
    name: &str,
    owner: Option<&str>,
    param_types: Vec<String>,
    language: &str,
) -> (Vec<FileForBind>, SymbolId) {
    let symbol = make_symbol_id(1, 1);
    let mut index = LocalIndex::new();
    let param_count = param_types.len();
    index.declarations.push(Declaration {
        kind: DeclarationKind::Method,
        name: name.to_string(),
        line: 5,
        symbol,
        param_count: Some(param_count),
        param_types,
        is_varargs: true,
        vararg_index: Some(param_count.saturating_sub(1)),
    });
    index
        .signatures
        .insert(symbol, format!("{name}({param_count} params)"));
    if let Some(owner) = owner {
        index.method_owners.push(MethodOwnerRecord {
            method_symbol: symbol,
            enclosing_type: owner.to_string(),
        });
    }
    (vec![file(1, language, index)], symbol)
}

/// Bug #1904 constraint: the widening must alter Method signature
/// CONTENT only -- every other `DeclarationKind` keeps its existing
/// cached signature byte-for-byte, since `widen_method_signature`
/// returns `None` for them and the caller falls back to the original
/// cached string unchanged.
#[test]
fn non_method_declarations_keep_their_existing_cached_signature_unchanged() {
    let symbol = make_symbol_id(1, 0);
    let mut index = LocalIndex::new();
    index.declarations.push(Declaration {
        kind: DeclarationKind::Type,
        name: "OrderService".to_string(),
        line: 1,
        symbol,
        param_count: None,
        param_types: Vec::new(),
        is_varargs: false,
        vararg_index: None,
    });
    index.signatures.insert(symbol, "class OrderService".to_string());
    let files = vec![file(1, "java", index)];
    let graph = bind_with_budget(files, &IndexBudget::unlimited());
    let dense = graph.dense_id_for(symbol).expect("type must be interned");
    assert_eq!(graph.signature_for(dense), Some("class OrderService"));
}
