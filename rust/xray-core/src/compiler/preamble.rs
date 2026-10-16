//! Issue #1934: the legacy-mode evaluator PREAMBLE/EPILOGUE text,
//! extracted verbatim out of `compiler.rs` (pure move -- no behaviour
//! change, see the module doc comment on `super`). These constants are
//! STRING LITERALS compiled verbatim into every legacy-mode evaluator
//! artifact; their content must never be touched by a refactor -- only the
//! Rust-level visibility of `EPILOGUE` itself changes here (bumped from
//! private to `pub(crate)` so sibling modules in the new `compiler` tree,
//! and the split test files, can still reach it -- this has zero effect on
//! the compiled evaluator, since Rust visibility is a compile-time-only
//! concept that never appears in generated code). The graph-mode mirror
//! constants (`GRAPH_PREAMBLE_EXTRA_*`/`GRAPH_EPILOGUE`/
//! `GRAPH_REFINE_EPILOGUE`) live in the sibling `graph_preamble` module.

/// The evaluator preamble defines OwnedNode and EvalFinding so user code
/// can reference them without imports. Layout MUST match xray_core types.
///
/// `truncate_snippet` is included as a utility helper for user evaluator code
/// that needs to trim long text snippets before storing them in EvalFinding.
///
/// `XRAY_ABI_VERSION` is exported so the loader can verify the compiled .so
/// was built with a compatible type layout before calling the evaluate function.
pub(crate) const PREAMBLE: &str = r#"
/// ABI version sentinel — substituted from the single source of truth
/// (compiler::XRAY_ABI_VERSION) by assemble_evaluator_source_with_preamble
/// before every compile (Bug #1784 review MAJOR-3).
const XRAY_ABI_VERSION: u64 = __XRAY_ABI_VERSION_PLACEHOLDER__;
const XRAY_RUSTC_VERSION: &[u8] = b"__XRAY_RUSTC_VERSION_PLACEHOLDER__";

use std::sync::Arc;

#[derive(Debug, Clone)]
pub struct OwnedNode {
    pub kind: String,
    pub start_line: usize,
    pub start_byte: usize,
    pub end_byte: usize,
    pub children: Vec<OwnedNode>,
    pub is_named: bool,
    source: Arc<str>,
}

impl OwnedNode {
    pub fn text(&self) -> &str {
        self.source.get(self.start_byte..self.end_byte).unwrap_or("")
    }
    pub fn named_children(&self) -> Vec<&OwnedNode> {
        self.children.iter().filter(|c| c.is_named).collect()
    }
    pub fn child_by_kind(&self, kind: &str) -> Option<&OwnedNode> {
        self.children.iter().find(|c| c.kind == kind)
    }
    // Bug #1795: explicit-stack (heap) traversal instead of recursion, so a
    // deeply nested OwnedNode passed into a compiled evaluator cannot
    // SIGABRT the process. Mirrors owned_node.rs's fix exactly (known
    // duplication debt, tracked separately as AC16 on #1787 -- not
    // refactored here).
    pub fn has_descendant_of_kind(&self, kind: &str) -> bool {
        let mut stack: Vec<&OwnedNode> = self.children.iter().collect();
        while let Some(node) = stack.pop() {
            if node.kind == kind { return true; }
            stack.extend(node.children.iter());
        }
        false
    }
    pub fn descendants_of_kind(&self, kind: &str) -> Vec<&OwnedNode> {
        let mut results = Vec::new();
        let mut stack: Vec<&OwnedNode> = self.children.iter().rev().collect();
        while let Some(node) = stack.pop() {
            if node.kind == kind { results.push(node); }
            stack.extend(node.children.iter().rev());
        }
        results
    }
}

#[derive(Debug, Clone)]
pub struct EvalFinding {
    pub pattern: String,
    pub line: usize,
    pub snippet: String,
}

/// Utility for user evaluator code: collapse whitespace and truncate to max_len bytes.
/// Truncation always falls on a UTF-8 char boundary — never panics on multibyte chars.
/// If truncation occurs, appends "...".
fn truncate_snippet(s: &str, max_len: usize) -> String {
    let collapsed: String = s.split_whitespace().collect::<Vec<_>>().join(" ");
    if collapsed.len() <= max_len {
        collapsed
    } else {
        let boundary = collapsed
            .char_indices()
            .map(|(i, _)| i)
            .take_while(|&i| i <= max_len)
            .last()
            .unwrap_or(0);
        format!("{}...", &collapsed[..boundary])
    }
}

use std::cell::RefCell;

thread_local! {
    static DEBUG_LOG: RefCell<Vec<String>> = RefCell::new(Vec::new());
}

/// Debug logging helper for evaluator development.
/// Messages are collected in a per-thread buffer (max 100 messages, 10KB total)
/// and returned alongside findings in the JSON output as debug_messages[].
/// Calls past the limits are silently dropped.
fn debug_log(msg: &str) {
    DEBUG_LOG.with(|log| {
        let mut log = log.borrow_mut();
        if log.len() < 100 {
            let total_bytes: usize = log.iter().map(|s| s.len()).sum();
            if total_bytes + msg.len() <= 10240 {
                log.push(msg.to_string());
            }
        }
    });
}
"#;

pub(crate) const EPILOGUE: &str = r#"
#[no_mangle]
pub fn xray_evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    evaluate_node(node)
}

/// Bug #1855 (H1): the compatibility probe itself must be ABI-stable BY
/// CONSTRUCTION, not by convention. These three exports are the ONLY
/// symbols the dynlib loader calls before compatibility between the host
/// and this compiled evaluator has been established (xray_abi_version
/// first, then the rustc_version ptr/len pair) -- so they cannot rely on
/// "both sides happened to use the same rustc" the way the DATA-carrying
/// callbacks below (xray_evaluate_node above; xray_collect_facts/
/// xray_analyze_graph/xray_refine in GRAPH_EPILOGUE) legitimately can,
/// since those only ever run AFTER this probe has already proven it. Using
/// `extern "C"` pins a fixed, documented calling convention across the
/// dylib boundary instead of Rust's own unstable-across-compiler-versions
/// ABI, removing the one case where this codebase asked the plain Rust ABI
/// to prove its own precondition.
#[no_mangle]
pub extern "C" fn xray_abi_version() -> u64 {
    XRAY_ABI_VERSION
}

/// Raw pointer half of the evaluator's embedded rustc version string,
/// read by the host loader BEFORE compatibility is established (H1).
#[no_mangle]
pub extern "C" fn xray_rustc_version_ptr() -> u64 {
    XRAY_RUSTC_VERSION.as_ptr() as u64
}

/// Length half of the evaluator's embedded rustc version string,
/// read by the host loader BEFORE compatibility is established (H1).
#[no_mangle]
pub extern "C" fn xray_rustc_version_len() -> u64 {
    XRAY_RUSTC_VERSION.len() as u64
}

#[no_mangle]
pub fn xray_drain_debug_log() -> Vec<String> {
    DEBUG_LOG.with(|log| {
        let mut log = log.borrow_mut();
        std::mem::take(&mut *log)
    })
}
"#;
<<<<<<< HEAD

/// The evaluator is compiled into the SAME module as these preamble
/// constants, so every name the preamble imports is reachable from user
/// evaluator code as `crate::<name>` -- with no `std` segment anywhere in
/// the path for `validator.rs`'s forbidden-module check to see. The
/// validator rejects `std::process::Command`; it cannot reject
/// `crate::Command`, because the only thing that makes `crate::Command`
/// dangerous is what the preamble happens to import.
///
/// Today the preamble imports `Arc`, `RefCell` and `PhantomData`, all
/// harmless, so this is latent rather than live. Nothing guarded it: adding
/// one `use std::process::Command;` here opened a sandbox escape silently --
/// the full `xray-core` suite stays green with `std::process::Command` and
/// `std::fs` imported into the evaluator's module (748 passed).
///
/// These tests are that guard. They do NOT weaken the preamble; they pin it,
/// so a new import is a deliberate, reviewed act rather than an invisible
/// one.
#[cfg(test)]
mod import_allowlist_tests {
    use super::{EPILOGUE, PREAMBLE};
    use crate::compiler::graph_preamble::{
        GRAPH_EPILOGUE, GRAPH_PREAMBLE_EXTRA_1, GRAPH_PREAMBLE_EXTRA_2,
        GRAPH_PREAMBLE_EXTRA_3, GRAPH_PREAMBLE_EXTRA_4, GRAPH_PREAMBLE_EXTRA_5,
        GRAPH_PREAMBLE_EXTRA_6, GRAPH_REFINE_EPILOGUE,
    };

    /// Every constant whose text is concatenated into an evaluator's
    /// compiled source by `assemble.rs`. A new one added there must be
    /// added here too, or it is unguarded.
    const ASSEMBLED_INTO_EVERY_EVALUATOR: &[(&str, &str)] = &[
        ("PREAMBLE", PREAMBLE),
        ("EPILOGUE", EPILOGUE),
        ("GRAPH_PREAMBLE_EXTRA_1", GRAPH_PREAMBLE_EXTRA_1),
        ("GRAPH_PREAMBLE_EXTRA_2", GRAPH_PREAMBLE_EXTRA_2),
        ("GRAPH_PREAMBLE_EXTRA_3", GRAPH_PREAMBLE_EXTRA_3),
        ("GRAPH_PREAMBLE_EXTRA_4", GRAPH_PREAMBLE_EXTRA_4),
        ("GRAPH_PREAMBLE_EXTRA_5", GRAPH_PREAMBLE_EXTRA_5),
        ("GRAPH_PREAMBLE_EXTRA_6", GRAPH_PREAMBLE_EXTRA_6),
        ("GRAPH_EPILOGUE", GRAPH_EPILOGUE),
        ("GRAPH_REFINE_EPILOGUE", GRAPH_REFINE_EPILOGUE),
    ];

    /// The ONLY imports an evaluator's module may contain, as exact
    /// normalised statements.
    ///
    /// Exact statements, not path prefixes, on purpose: `use std::sync::*;`
    /// or `use std::sync::{Arc, Mutex};` would both pass a "starts with
    /// std::sync" rule while dragging in names nobody reviewed. Each entry
    /// is one reviewed binding, and the reason it is safe to expose as
    /// `crate::<name>`:
    ///
    ///   Arc         -- shared ownership of the node source text; no I/O,
    ///                  no syscalls, no interior mutability of its own.
    ///   RefCell     -- the thread-local debug_log buffer. Single-threaded
    ///                  interior mutability; worst case is a panic.
    ///   PhantomData -- a zero-sized lifetime marker on GraphHandle. Cannot
    ///                  do anything at all at runtime.
    ///
    /// Anything that performs I/O, spawns a process, loads a library,
    /// touches the filesystem, opens a socket, or reaches the environment
    /// does NOT belong here, however convenient it would be in the preamble
    /// -- the evaluator inherits it.
    const ALLOWED_IMPORTS: &[&str] = &[
        "use std::sync::Arc;",
        "use std::cell::RefCell;",
        "use std::marker::PhantomData;",
    ];

    /// Collapse runs of whitespace so formatting can never hide a statement
    /// from the allowlist.
    fn normalise(statement: &str) -> String {
        let collapsed: Vec<&str> = statement.split_whitespace().collect();
        format!("{};", collapsed.join(" "))
    }

    /// Every `use` / `pub use` / `extern crate` statement in `source`.
    ///
    /// Splits on `;` rather than on lines: statements can share a line, and
    /// a line-oriented scan would miss `use std::sync::Arc; use
    /// std::process::Command;`.
    fn import_statements(source: &str) -> Vec<String> {
        let mut found = Vec::new();
        for segment in source.split(';') {
            // A segment starts just after the previous `;`, so it carries
            // the tail of that line plus everything up to this statement.
            // Only the final line can be the statement itself.
            let mut statement = match segment.rfind('\n') {
                Some(idx) => segment[idx + 1..].trim(),
                None => segment.trim(),
            };
            // Drop leading attributes: `#[allow(unused_imports)] use std::fs`.
            while statement.starts_with("#[") {
                match statement.find(']') {
                    Some(idx) => statement = statement[idx + 1..].trim(),
                    None => break,
                }
            }
            if statement.starts_with("use ")
                || statement.starts_with("pub use ")
                || statement.starts_with("extern crate ")
            {
                found.push(normalise(statement));
            }
        }
        found
    }

    #[test]
    fn preamble_imports_stay_inside_the_allowlist() {
        for (name, source) in ASSEMBLED_INTO_EVERY_EVALUATOR {
            for statement in import_statements(source) {
                assert!(
                    ALLOWED_IMPORTS.contains(&statement.as_str()),
                    "{name} imports `{statement}`, which is not on the \
                     evaluator import allowlist.\n\n\
                     User evaluator code is compiled into the SAME module as \
                     this preamble, so anything imported here is reachable \
                     from an evaluator as `crate::<name>` -- a path with no \
                     `std` segment for validator.rs's forbidden-module check \
                     to match on. Adding an import here therefore hands every \
                     evaluator that capability, whatever the validator says.\n\n\
                     If this import really is needed AND is safe to expose to \
                     arbitrary evaluator code, add the exact statement to \
                     ALLOWED_IMPORTS in this file together with the reason it \
                     cannot be used to reach the filesystem, the network, the \
                     environment, another process or the dynamic linker. If it \
                     is not safe to expose, it does not belong in the preamble \
                     at all -- put it in xray-core's own code, which is not \
                     concatenated into the evaluator."
                );
            }
        }
    }

    #[test]
    fn every_allowlisted_import_is_actually_present() {
        let present: Vec<String> = ASSEMBLED_INTO_EVERY_EVALUATOR
            .iter()
            .flat_map(|(_, source)| import_statements(source))
            .collect();
        for allowed in ALLOWED_IMPORTS {
            assert!(
                present.iter().any(|s| s == allowed),
                "ALLOWED_IMPORTS lists `{allowed}` but no preamble constant \
                 imports it. An allowlist that outlives what it describes \
                 silently re-permits a removed import later; delete the entry \
                 along with the import."
            );
        }
    }

    #[test]
    fn the_scanner_finds_imports_in_every_shape_they_can_be_written() {
        // If this scanner missed a shape, the allowlist test above would pass
        // vacuously on exactly the import someone was trying to sneak in.
        let hostile = "\n\
            use std::process::Command;\n\
            pub use std::fs;\n\
            #[allow(unused_imports)] use std::net::TcpStream;\n\
            use std::sync::Arc; use std::env;\n\
            use std::{io, path::PathBuf};\n\
            use std::process::Command as Shell;\n\
            use std::os::unix::fs::*;\n\
            extern crate libc;\n\
            use   std::ffi::CString  ;\n";

        let found = import_statements(hostile);

        for expected in [
            "use std::process::Command;",
            "pub use std::fs;",
            "use std::net::TcpStream;",
            "use std::env;",
            "use std::{io, path::PathBuf};",
            "use std::process::Command as Shell;",
            "use std::os::unix::fs::*;",
            "extern crate libc;",
            "use std::ffi::CString;",
        ] {
            assert!(
                found.iter().any(|s| s == expected),
                "scanner missed `{expected}`; found {found:?}"
            );
        }

        let violations: Vec<&String> = found
            .iter()
            .filter(|s| !ALLOWED_IMPORTS.contains(&s.as_str()))
            .collect();
        assert_eq!(
            violations.len(),
            found.len() - 1,
            "only the `use std::sync::Arc;` in that sample is allowlisted; \
             everything else must be rejected, got {violations:?}"
        );
    }

    #[test]
    fn wildcard_and_group_forms_of_an_allowed_path_are_still_rejected() {
        // `std::sync::Arc` is allowed; `std::sync::*` is not the same thing.
        for broadened in [
            "use std::sync::*;",
            "use std::sync::{Arc, Mutex};",
            "use std::sync::Arc as _;",
            "use std::cell::*;",
        ] {
            let found = import_statements(broadened);
            assert_eq!(found.len(), 1, "scanner failed on `{broadened}`");
            assert!(
                !ALLOWED_IMPORTS.contains(&found[0].as_str()),
                "`{broadened}` widens an allowlisted import and must not pass"
            );
        }
    }
}
=======
>>>>>>> upstream/master
