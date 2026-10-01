//! S0a live-path regression floor (story #1789).
//!
//! These tests intentionally use xray-core's public scanner/compiler/dynlib
//! APIs.  They are not parser-only smoke tests: every evaluator test compiles
//! a real shared object with rustc, dlopens it, and invokes it.
//!
//! Nodes are built exclusively through `scanner::parse_file` — the real
//! production path.  Nothing here constructs an `OwnedNode` by struct literal;
//! the `source` field is documented as off-contract ("callers use the `text()`
//! method"), and the synthetic-node primitives are covered in-crate in
//! `owned_node.rs`'s `s0a_primitive_tests`, where the provided
//! `new_leaf_for_test` / `new_node_for_test` constructors are reachable
//! (they are `#[cfg(test)]`, so integration tests cannot see them).

use std::io::Write;
use std::path::PathBuf;
use tempfile::TempDir;
use xray_core::compiler;
use xray_core::dynlib::DynlibEvaluator;
use xray_core::owned_node::OwnedNode;
use xray_core::scanner::{self, Evaluator};

fn write_file(dir: &TempDir, name: &str, bytes: &[u8]) -> PathBuf {
    let path = dir.path().join(name);
    std::fs::File::create(&path).unwrap().write_all(bytes).unwrap();
    path
}

fn kinds(node: &OwnedNode, out: &mut Vec<String>) {
    out.push(node.kind.clone());
    for child in &node.children {
        kinds(child, out);
    }
}

fn has_error(node: &OwnedNode) -> bool {
    node.kind == "ERROR" || node.children.iter().any(has_error)
}

// ---------------------------------------------------------------------------
// AC3 — per-language parse round-trips
// ---------------------------------------------------------------------------

/// Callable node kinds the engine's evaluator model works in terms of. The
/// non-callable grammars assert the ABSENCE of all of these rather than
/// pretending they share a function model.
const CALLABLE_KINDS: &[&str] = &["function_definition", "method_declaration", "call_expression"];

/// The exact names here are part of the engine/evaluator contract.
#[test]
fn all_supported_grammars_round_trip_with_engine_kinds() {
    // (extension, source, kinds that MUST be present, kinds that MUST be absent)
    let cases: &[(&str, &str, &[&str], &[&str])] = &[
        ("java", "import x.Y; class A { void f() { g(); } }", &["method_declaration", "method_invocation", "import_declaration"], &[]),
        ("kt", "import x.Y\nclass A { fun f() { g() } }", &["function_declaration", "call_expression", "import"], &[]),
        ("go", "package p\nimport \"fmt\"\nfunc f() { fmt.Println(1) }", &["function_declaration", "call_expression", "import_declaration"], &[]),
        ("py", "import os\ndef f():\n    os.getcwd()\n", &["function_definition", "call", "import_statement"], &[]),
        ("ts", "import {x} from 'm'; function f(): void { x(); }", &["function_declaration", "call_expression", "import_statement"], &[]),
        ("js", "import x from 'm'; function f() { x(); }", &["function_declaration", "call_expression", "import_statement"], &[]),
        ("sh", "f() { echo ok; }\nf", &["function_definition", "command"], &[]),
        ("cs", "using System; class A { void F() { M(); } }", &["method_declaration", "invocation_expression", "using_directive"], &[]),
        ("groovy", "import x.Y\nclass A { void f() { println(\"x\") } }", &["method_declaration", "method_invocation", "import_declaration"], &[]),
        ("c", "#include <x>\nint f() { return g(); }", &["function_definition", "call_expression"], &[]),
        ("cpp", "#include <x>\nint f() { return g(); }", &["function_definition", "call_expression"], &[]),
        // ---- markup / config / query grammars ----
        // These have no callable semantics in the engine's model. Each asserts
        // the useful syntax it DOES have plus the absence of every callable
        // kind, so "graceful degradation" is pinned rather than assumed.
        ("html", "<html><body><div id=\"x\">Hi</div></body></html>", &["element", "start_tag", "attribute"], CALLABLE_KINDS),
        ("css", ".card { color: red; }", &["rule_set", "class_selector", "declaration"], CALLABLE_KINDS),
        ("yaml", "root:\n  value: &anchor text\n", &["block_mapping_pair", "anchor"], CALLABLE_KINDS),
        ("xml", "<root><item id=\"1\"/></root>", &["element", "STag", "Attribute"], CALLABLE_KINDS),
        // sql and hcl belong to the same non-callable family as the four
        // above: the engine has no function/method model for either, so they
        // get the identical absence assertions instead of an empty list.
        ("hcl", "resource \"x\" \"y\" { name = \"v\" }", &["block", "attribute"], CALLABLE_KINDS),
        ("sql", "SELECT a FROM t WHERE a = 1;", &["select", "from"], CALLABLE_KINDS),
    ];
    for (ext, source, required, absent) in cases {
        let dir = TempDir::new().unwrap();
        let path = write_file(&dir, &format!("sample.{ext}"), source.as_bytes());
        let root = scanner::parse_file(&path).unwrap_or_else(|| panic!("{ext} did not parse"));
        let mut all = Vec::new();
        kinds(&root, &mut all);
        assert!(!has_error(&root), "{ext} produced an ERROR node; got {all:?}");
        for expected in *required {
            assert!(all.iter().any(|k| k == expected), "{ext} missing {expected}; got {all:?}");
        }
        for forbidden in *absent {
            assert!(
                !all.iter().any(|k| k == forbidden),
                "{ext} unexpectedly has callable kind {forbidden}"
            );
        }
    }
}

// ---------------------------------------------------------------------------
// AC5 — OwnedNode built by the real parser
// ---------------------------------------------------------------------------

#[test]
fn owned_node_build_preserves_bom_crlf_offsets_and_lines() {
    let dir = TempDir::new().unwrap();
    let source = b"\xEF\xBB\xBFclass A {\r\n  void f() {}\r\n}\r\n";
    let path = write_file(&dir, "B.java", source);
    let root = scanner::parse_file(&path).unwrap();
    assert_eq!(root.start_line, 1);
    let method = root.descendants_of_kind("method_declaration")[0];
    assert_eq!(method.start_line, 2);
    assert_eq!(&source[method.start_byte..method.end_byte], method.text().as_bytes());
}

// ---------------------------------------------------------------------------
// AC4 — compile → dlopen → execute, one test per preamble primitive
// ---------------------------------------------------------------------------

/// Java fixture reused by the primitive tests. Chosen because it exercises
/// every kind the primitives are asked about: imports, a class, a method
/// declaration, and a nested method invocation.
const JAVA_FIXTURE: &str = "import x.Y;\nclass A {\n  void f() { g(); }\n}\n";

/// Parses JAVA_FIXTURE through the production path and returns its root.
/// The TempDir is returned too so the file outlives the node.
fn java_root() -> (TempDir, OwnedNode) {
    let dir = TempDir::new().unwrap();
    let path = write_file(&dir, "A.java", JAVA_FIXTURE.as_bytes());
    let root = scanner::parse_file(&path).expect("java fixture must parse");
    (dir, root)
}

/// Compiles `body` as the whole of `evaluate_node`, loads the resulting .so.
/// The TempDir owning the .so is returned so it outlives the evaluator.
fn compile_and_load(body: &str) -> (TempDir, DynlibEvaluator) {
    let code = format!("fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {{\n{body}\n}}\n");
    let dir = TempDir::new().unwrap();
    let compiled = compiler::compile_evaluator(&code, dir.path())
        .unwrap_or_else(|e| panic!("evaluator must compile, got: {e}"));
    let evaluator = DynlibEvaluator::load(&compiled.so_path).expect("compiled .so must load");
    (dir, evaluator)
}

/// Runs an evaluator whose body produces exactly one finding and returns its
/// `pattern` field — the channel these per-primitive tests report through.
fn single_pattern(body: &str, node: &OwnedNode) -> String {
    let (_dir, evaluator) = compile_and_load(body);
    let findings = evaluator.evaluate_node(node);
    assert_eq!(findings.len(), 1, "evaluator must emit exactly one finding");
    findings[0].pattern.clone()
}

#[test]
fn preamble_primitive_text_matches_host_side_text() {
    let (_dir, root) = java_root();
    let observed = single_pattern(
        r#"    vec![EvalFinding { pattern: node.text().to_string(), line: node.start_line, snippet: String::new() }]"#,
        &root,
    );
    // The preamble's OwnedNode and the crate's must agree, not merely "work".
    assert_eq!(observed, root.text());
    assert_eq!(observed, JAVA_FIXTURE);
}

#[test]
fn preamble_primitive_named_children_matches_host_side_named_children() {
    let (_dir, root) = java_root();
    let observed = single_pattern(
        r#"    vec![EvalFinding { pattern: node.named_children().len().to_string(), line: 1, snippet: String::new() }]"#,
        &root,
    );
    assert_eq!(observed, root.named_children().len().to_string());
    // Discriminating: the root really does have named children to count.
    assert!(!root.named_children().is_empty());
}

#[test]
fn preamble_primitive_child_by_kind_finds_a_direct_child() {
    let (_dir, root) = java_root();
    let observed = single_pattern(
        r#"    let found = node.child_by_kind("class_declaration");
    let pattern = match found { Some(c) => c.kind.clone(), None => "NONE".to_string() };
    vec![EvalFinding { pattern, line: 1, snippet: String::new() }]"#,
        &root,
    );
    assert_eq!(observed, "class_declaration");
}

#[test]
fn preamble_primitive_child_by_kind_returns_none_for_a_deeper_kind() {
    let (_dir, root) = java_root();
    // method_declaration exists, but nested inside the class body — never as a
    // DIRECT child of the root. Pins that child_by_kind is depth-1 only.
    let observed = single_pattern(
        r#"    let found = node.child_by_kind("method_declaration");
    let pattern = match found { Some(c) => c.kind.clone(), None => "NONE".to_string() };
    vec![EvalFinding { pattern, line: 1, snippet: String::new() }]"#,
        &root,
    );
    assert_eq!(observed, "NONE");
    assert!(root.has_descendant_of_kind("method_declaration"));
}

#[test]
fn preamble_primitive_has_descendant_of_kind_matches_host_side() {
    let (_dir, root) = java_root();
    let observed = single_pattern(
        r#"    let present = node.has_descendant_of_kind("method_invocation");
    let absent = node.has_descendant_of_kind("no_such_kind");
    vec![EvalFinding { pattern: format!("{}/{}", present, absent), line: 1, snippet: String::new() }]"#,
        &root,
    );
    assert_eq!(observed, "true/false");
    assert!(root.has_descendant_of_kind("method_invocation"));
    assert!(!root.has_descendant_of_kind("no_such_kind"));
}

#[test]
fn preamble_primitive_descendants_of_kind_matches_host_side_count() {
    let (_dir, root) = java_root();
    let observed = single_pattern(
        r#"    vec![EvalFinding { pattern: node.descendants_of_kind("method_invocation").len().to_string(), line: 1, snippet: String::new() }]"#,
        &root,
    );
    assert_eq!(observed, root.descendants_of_kind("method_invocation").len().to_string());
    assert_eq!(observed, "1");
}

#[test]
fn preamble_primitive_public_fields_match_host_side_fields() {
    let (_dir, root) = java_root();
    let observed = single_pattern(
        r#"    let pattern = format!("{}|{}|{}|{}|{}|{}", node.kind, node.start_line, node.start_byte, node.end_byte, node.is_named, node.children.len());
    vec![EvalFinding { pattern, line: node.start_line, snippet: String::new() }]"#,
        &root,
    );
    let expected = format!(
        "{}|{}|{}|{}|{}|{}",
        root.kind, root.start_line, root.start_byte, root.end_byte, root.is_named, root.children.len()
    );
    assert_eq!(observed, expected);
}

#[test]
fn preamble_primitive_truncate_snippet_respects_char_boundaries() {
    let (_dir, root) = java_root();
    // Three cases in one evaluator: no truncation, ASCII truncation, and a
    // truncation whose byte limit lands inside a multibyte char.
    let observed = single_pattern(
        r#"    let short = truncate_snippet("abc", 8);
    let ascii = truncate_snippet("abcdefghijkl", 4);
    let multibyte = truncate_snippet("hello \u{1F642} world", 8);
    vec![EvalFinding { pattern: format!("{}|{}|{}", short, ascii, multibyte), line: 1, snippet: String::new() }]"#,
        &root,
    );
    assert_eq!(observed, "abc|abcd...|hello ...");
}

#[test]
fn preamble_primitive_truncate_snippet_collapses_whitespace() {
    let (_dir, root) = java_root();
    let observed = single_pattern(
        r#"    vec![EvalFinding { pattern: truncate_snippet("  a\n\t b   c  ", 64), line: 1, snippet: String::new() }]"#,
        &root,
    );
    assert_eq!(observed, "a b c");
}

#[test]
fn preamble_primitive_debug_log_round_trips_through_the_dylib() {
    let (_dir, root) = java_root();
    let (_so_dir, evaluator) = compile_and_load(
        r#"    debug_log("first");
    debug_log("second");
    Vec::new()"#,
    );
    evaluator.evaluate_node(&root);
    assert_eq!(evaluator.drain_debug_log(), vec!["first", "second"]);
    // Draining is destructive: a second drain must be empty.
    assert!(evaluator.drain_debug_log().is_empty());
}

/// Retained combined test: proves the primitives compose in one evaluator.
/// The per-primitive tests above are what identify WHICH primitive broke;
/// this one catches interaction regressions the isolated tests would miss.
#[test]
fn real_compile_load_execute_exercises_every_preamble_primitive() {
    let (_dir, root) = java_root();
    let (_so_dir, evaluator) = compile_and_load(
        r#"    let named = node.named_children();
    let _ = node.kind.clone(); let _ = node.start_line; let _ = node.start_byte;
    let _ = node.end_byte; let _ = node.is_named; let _ = node.children.len();
    let _ = node.text(); let _ = node.child_by_kind("method_declaration");
    let _ = node.has_descendant_of_kind("method_invocation");
    let _ = node.descendants_of_kind("method_invocation");
    debug_log("primitive-evaluator");
    let snippet = truncate_snippet("hello \u{1F642} world", 8);
    vec![EvalFinding { pattern: named.len().to_string(), line: node.start_line, snippet }]"#,
    );
    let findings = evaluator.evaluate_node(&root);
    assert_eq!(findings.len(), 1);
    assert_eq!(findings[0].pattern, root.named_children().len().to_string());
    assert_eq!(findings[0].snippet, "hello ...");
    assert_eq!(evaluator.drain_debug_log(), vec!["primitive-evaluator"]);
}

// ---------------------------------------------------------------------------
// AC4 — debug_log boundary pinning
//
// These pin the CURRENT silent-drop semantics of compiler.rs's `debug_log`
// (max 100 messages, max 10240 cumulative bytes). The epic replaces this
// behaviour, so the existing contract must be executable first or the
// replacement cannot be verified as a deliberate change.
// ---------------------------------------------------------------------------

/// Byte ceiling enforced by the preamble's `debug_log`.
const DEBUG_LOG_MAX_BYTES: usize = 10240;
/// Message-count ceiling enforced by the preamble's `debug_log`.
const DEBUG_LOG_MAX_MESSAGES: usize = 100;

#[test]
fn debug_log_silently_drops_messages_past_the_count_ceiling() {
    let (_dir, root) = java_root();
    // 150 one-to-three byte messages: far under the byte ceiling, so the
    // COUNT ceiling is the only thing that can bound the result.
    let (_so_dir, evaluator) = compile_and_load(
        r#"    for i in 0..150 { debug_log(&format!("{}", i)); }
    Vec::new()"#,
    );
    evaluator.evaluate_node(&root);
    let messages = evaluator.drain_debug_log();
    assert_eq!(messages.len(), DEBUG_LOG_MAX_MESSAGES);
    // The first N are kept and the overflow is dropped — not a ring buffer.
    assert_eq!(messages[0], "0");
    assert_eq!(messages[DEBUG_LOG_MAX_MESSAGES - 1], "99");
}

#[test]
fn debug_log_silently_drops_messages_past_the_byte_ceiling() {
    let (_dir, root) = java_root();
    // 20 messages of 600 bytes = 12000 bytes, under the 100-message ceiling
    // so only the BYTE ceiling can bound it. 17 * 600 = 10200 <= 10240;
    // an 18th would reach 10800 and is refused.
    let (_so_dir, evaluator) = compile_and_load(
        r#"    for _ in 0..20 { debug_log(&"x".repeat(600)); }
    Vec::new()"#,
    );
    evaluator.evaluate_node(&root);
    let messages = evaluator.drain_debug_log();
    assert_eq!(messages.len(), DEBUG_LOG_MAX_BYTES / 600);
    let total: usize = messages.iter().map(|m| m.len()).sum();
    assert!(total <= DEBUG_LOG_MAX_BYTES, "buffer exceeded its byte ceiling: {total}");
}

#[test]
fn debug_log_byte_ceiling_does_not_short_circuit_later_smaller_messages() {
    let (_dir, root) = java_root();
    // Current semantics: the byte check refuses the individual message and
    // keeps going — it does NOT stop accepting. So a message too big to fit
    // is skipped while a later, smaller one that still fits IS stored.
    // 10000 fits (total 10000); +500 would be 10500 and is refused;
    // +100 is 10100 and is accepted.
    let (_so_dir, evaluator) = compile_and_load(
        r#"    debug_log(&"a".repeat(10000));
    debug_log(&"b".repeat(500));
    debug_log(&"c".repeat(100));
    Vec::new()"#,
    );
    evaluator.evaluate_node(&root);
    let messages = evaluator.drain_debug_log();
    assert_eq!(messages.len(), 2, "expected the 10000 and the 100, got {:?}", messages.iter().map(|m| m.len()).collect::<Vec<_>>());
    assert_eq!(messages[0].len(), 10000);
    assert_eq!(messages[1].len(), 100);
}

#[test]
fn debug_log_accepts_a_message_landing_exactly_on_the_byte_ceiling() {
    let (_dir, root) = java_root();
    // The check is `total + len <= 10240`, so exactly 10240 is accepted and
    // 10241 is refused outright even into an empty buffer.
    let (_so_dir, evaluator) = compile_and_load(
        r#"    debug_log(&"a".repeat(10240));
    Vec::new()"#,
    );
    evaluator.evaluate_node(&root);
    let messages = evaluator.drain_debug_log();
    assert_eq!(messages.len(), 1);
    assert_eq!(messages[0].len(), DEBUG_LOG_MAX_BYTES);
}

#[test]
fn debug_log_drops_a_single_message_larger_than_the_whole_ceiling() {
    let (_dir, root) = java_root();
    let (_so_dir, evaluator) = compile_and_load(
        r#"    debug_log(&"a".repeat(10241));
    debug_log("small");
    Vec::new()"#,
    );
    evaluator.evaluate_node(&root);
    // The oversized message is dropped; the small one still lands.
    assert_eq!(evaluator.drain_debug_log(), vec!["small"]);
}

// ---------------------------------------------------------------------------
// AC6 — edge matrix
// ---------------------------------------------------------------------------

#[test]
fn scanner_classifies_read_failures_separately_from_valid_empty_files() {
    let dir = TempDir::new().unwrap();
    let empty = write_file(&dir, "empty.py", b"");
    let missing = dir.path().join("deleted.py");
    let evaluators: Vec<Box<dyn Evaluator>> = vec![];
    let result = scanner::scan_files_parallel(&[empty, missing], &evaluators);
    assert_eq!(result.files_parsed, 1);
    assert_eq!(result.files_errored, 1);
}

#[test]
fn zero_byte_file_parses_to_an_empty_root_not_an_error() {
    let dir = TempDir::new().unwrap();
    let path = write_file(&dir, "empty.py", b"");
    let root = scanner::parse_file(&path).expect("a zero-byte file is valid input, not a failure");
    assert_eq!(root.kind, "module");
    assert!(root.children.is_empty());
    assert_eq!(root.start_byte, 0);
    assert_eq!(root.end_byte, 0);
    assert_eq!(root.text(), "");
    assert!(!has_error(&root));
}

#[test]
fn comments_only_file_parses_with_no_declarations() {
    let dir = TempDir::new().unwrap();
    let source = "# leading note\n# another note\n\n# trailing note\n";
    let path = write_file(&dir, "notes.py", source.as_bytes());
    let root = scanner::parse_file(&path).expect("a comments-only file must parse");
    assert!(!has_error(&root), "comments-only file must not produce ERROR nodes");
    assert_eq!(root.descendants_of_kind("comment").len(), 3);
    // Nothing callable — the evaluator model must see an empty program.
    for kind in CALLABLE_KINDS {
        assert!(!root.has_descendant_of_kind(kind), "unexpected {kind} in a comments-only file");
    }
}

#[test]
fn non_utf8_bytes_parse_without_panicking_and_text_stays_safe() {
    let dir = TempDir::new().unwrap();
    // 0xFF / 0xFE are not valid UTF-8 in any position.
    let source: Vec<u8> = b"x = 1\ny = \"\xFF\xFE\"\nz = 2\n".to_vec();
    let path = write_file(&dir, "invalid.py", &source);
    let root = scanner::parse_file(&path).expect("invalid UTF-8 must degrade, not fail to parse");

    // The contract is "defined behaviour, not a crash": text() on EVERY node
    // must return without panicking, even though build_from_ts_node performed
    // a lossy conversion whose byte offsets no longer line up with the raw
    // file bytes.
    fn walk_text(node: &OwnedNode, visited: &mut usize) {
        let _ = node.text();
        *visited += 1;
        for child in &node.children {
            walk_text(child, visited);
        }
    }
    let mut visited = 0usize;
    walk_text(&root, &mut visited);
    assert!(visited > 1, "expected a real tree, visited only {visited} node(s)");
}

#[test]
fn file_larger_than_ten_megabytes_parses_completely() {
    let dir = TempDir::new().unwrap();
    // Long string literals keep the byte size well over 10 MB while keeping
    // the node count (and therefore OwnedNode memory) bounded.
    const STATEMENTS: usize = 1300;
    let filler = "y".repeat(8192);
    let mut source = String::with_capacity(11 * 1024 * 1024);
    for _ in 0..STATEMENTS {
        source.push_str("s = \"");
        source.push_str(&filler);
        source.push_str("\"\n");
    }
    assert!(
        source.len() > 10 * 1024 * 1024,
        "fixture must exceed 10 MB, got {} bytes",
        source.len()
    );
    let path = write_file(&dir, "huge.py", source.as_bytes());

    let root = scanner::parse_file(&path).expect("a >10 MB file must still parse");
    assert!(!has_error(&root), ">10 MB file produced ERROR nodes");
    // Every statement survives — not a truncated prefix.
    assert_eq!(root.descendants_of_kind("expression_statement").len(), STATEMENTS);
    assert_eq!(root.end_byte, source.len());
}

#[test]
fn evaluator_written_for_java_returns_nothing_against_yaml_and_css() {
    // A Java-targeted evaluator applied to grammars with no callable model
    // must degrade to zero findings — not error, not panic, not misfire.
    let (_so_dir, evaluator) = compile_and_load(
        r#"    let mut findings = Vec::new();
    for m in node.descendants_of_kind("method_declaration") {
        findings.push(EvalFinding { pattern: "java-method".to_string(), line: m.start_line, snippet: truncate_snippet(m.text(), 40) });
    }
    if node.has_descendant_of_kind("method_invocation") {
        findings.push(EvalFinding { pattern: "java-call".to_string(), line: node.start_line, snippet: String::new() });
    }
    findings"#,
    );

    let dir = TempDir::new().unwrap();
    let yaml = write_file(&dir, "conf.yaml", b"root:\n  value: text\n  list:\n    - a\n    - b\n");
    let css = write_file(&dir, "style.css", b".card { color: red; }\n#id { margin: 0; }\n");
    let java = write_file(&dir, "A.java", JAVA_FIXTURE.as_bytes());

    // Control first: the same evaluator DOES fire on Java, so a zero result
    // below means "no match", not "evaluator broken".
    let java_root = scanner::parse_file(&java).unwrap();
    assert!(!evaluator.evaluate_node(&java_root).is_empty(), "control: evaluator must match Java");
    evaluator.drain_debug_log();

    for path in [yaml, css] {
        let root = scanner::parse_file(&path)
            .unwrap_or_else(|| panic!("{} must parse", path.display()));
        let findings = evaluator.evaluate_node(&root);
        assert!(
            findings.is_empty(),
            "Java evaluator misfired on {}: {:?}",
            path.display(),
            findings.iter().map(|f| &f.pattern).collect::<Vec<_>>()
        );
    }
}

#[test]
fn cross_language_scan_reports_every_file_as_parsed_not_errored() {
    // The same mismatch through the parallel scanner: mixed-grammar batches
    // must count as parsed, with zero findings and zero errors.
    let (_so_dir, evaluator) = compile_and_load(
        r#"    let mut findings = Vec::new();
    for m in node.descendants_of_kind("method_declaration") {
        findings.push(EvalFinding { pattern: "java-method".to_string(), line: m.start_line, snippet: String::new() });
    }
    findings"#,
    );
    let dir = TempDir::new().unwrap();
    let files = vec![
        write_file(&dir, "conf.yaml", b"root:\n  value: text\n"),
        write_file(&dir, "style.css", b".card { color: red; }\n"),
    ];
    let evaluators: Vec<Box<dyn Evaluator>> = vec![Box::new(evaluator)];
    let result = scanner::scan_files_parallel(&files, &evaluators);
    assert_eq!(result.files_parsed, 2);
    assert_eq!(result.files_errored, 0);
    assert!(result.findings.is_empty(), "unexpected findings: {:?}", result.findings.len());
}
