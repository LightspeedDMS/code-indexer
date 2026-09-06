use std::sync::Arc;

/// OwnedNode — heap-allocated, Clone-able copy of a tree-sitter Node.
///
/// tree-sitter Node objects borrow from the owning Tree and cannot cross
/// thread boundaries. OwnedNode owns all its data, so it can be moved freely
/// across threads for parallel scanning.
///
/// The `source` field stores the full file source as an Arc<str> shared by
/// every node in the same file. Each node uses (start_byte, end_byte) to
/// slice into that shared buffer via the `text()` method. This eliminates
/// the O(N) per-node String allocations that previously hammered the
/// allocator under rayon parallelism across thousands of files.
#[derive(Debug, Clone)]
pub struct OwnedNode {
    pub kind: String,
    pub start_line: usize, // 1-based
    pub start_byte: usize,
    pub end_byte: usize,
    pub children: Vec<OwnedNode>,
    pub is_named: bool,
    /// Shared source text for the whole file. All nodes in the same file
    /// hold a clone of this Arc (cheap atomic increment). Not public:
    /// callers use the `text()` method to get their slice.
    pub source: Arc<str>,
}

/// Bug #1795, site #3: per-node bookkeeping for `build_recursive`'s
/// explicit-stack (heap) tree-sitter-Node-to-OwnedNode build. One `Frame` is
/// pushed per tree-sitter node visited: its direct children are materialized
/// once up front (the same `node.children(&mut cursor)` technique the old
/// recursive version used, just no longer inside a recursive call), and
/// `next_child`/`built_children` track how far conversion of THIS node's
/// children has progressed.
struct BuildFrame<'tree> {
    ts_node: tree_sitter::Node<'tree>,
    ts_children: Vec<tree_sitter::Node<'tree>>,
    next_child: usize,
    built_children: Vec<OwnedNode>,
}

fn make_build_frame(ts_node: tree_sitter::Node<'_>) -> BuildFrame<'_> {
    let mut cursor = ts_node.walk();
    let ts_children: Vec<tree_sitter::Node<'_>> = ts_node.children(&mut cursor).collect();
    BuildFrame { ts_node, ts_children, next_child: 0, built_children: Vec::new() }
}

fn finish_build_frame(frame: BuildFrame<'_>, shared_source: &Arc<str>) -> OwnedNode {
    OwnedNode {
        kind: frame.ts_node.kind().to_string(),
        start_line: frame.ts_node.start_position().row + 1,
        start_byte: frame.ts_node.start_byte(),
        end_byte: frame.ts_node.end_byte(),
        children: frame.built_children,
        is_named: frame.ts_node.is_named(),
        source: Arc::clone(shared_source),
    }
}

impl OwnedNode {
    /// Returns the source text for this node's byte span.
    ///
    /// Returns an empty string if the byte range is out of bounds (which
    /// should never happen for a well-formed tree-sitter parse).
    pub fn text(&self) -> &str {
        self.source.get(self.start_byte..self.end_byte).unwrap_or("")
    }

    /// Returns direct children where is_named == true (filters anonymous
    /// punctuation tokens such as "{", ";", "(" etc.).
    pub fn named_children(&self) -> Vec<&OwnedNode> {
        self.children.iter().filter(|c| c.is_named).collect()
    }

    /// Returns the first direct child whose kind matches `kind`, or None.
    pub fn child_by_kind(&self, kind: &str) -> Option<&OwnedNode> {
        self.children.iter().find(|c| c.kind == kind)
    }

    /// Returns true if any descendant (at any depth) has the given kind.
    ///
    /// Bug #1795: explicit-stack (heap) iteration instead of recursion. A
    /// Rust stack overflow aborts the WHOLE process and cannot be caught
    /// (not even by `catch_unwind`), so a recursive walk here meant one
    /// pathologically nested file could SIGABRT the entire `xray-cli`
    /// process. Moving the stack to the heap removes the depth cliff
    /// outright, with no arbitrary depth cap. Traversal order does not
    /// matter here (boolean existence check with early return), so children
    /// are pushed in their natural order.
    pub fn has_descendant_of_kind(&self, kind: &str) -> bool {
        let mut stack: Vec<&OwnedNode> = self.children.iter().collect();
        // Bounded: each iteration pops one node from `stack` and pushes its
        // (finite) children; the total number of pushes across the whole
        // walk equals the tree's finite node count, so this terminates.
        while let Some(node) = stack.pop() {
            if node.kind == kind {
                return true;
            }
            stack.extend(node.children.iter());
        }
        false
    }

    /// Returns all descendant nodes (at any depth) whose kind matches `kind`,
    /// in the SAME pre-order depth-first order the old recursive
    /// implementation produced (each child visited before its own
    /// descendants, children in original order) — callers depend on this
    /// order, so it must be preserved exactly, not merely produce the same
    /// set of nodes.
    ///
    /// Bug #1795: explicit-stack (heap) iteration instead of recursion, same
    /// rationale as `has_descendant_of_kind` above. Order is preserved by
    /// pushing each node's children in REVERSE onto the stack: since `pop()`
    /// takes from the end, the first child pushed is the first one visited,
    /// and its own children are pushed (and fully drained) before the walk
    /// ever reaches its next sibling — exactly reproducing recursive
    /// pre-order DFS.
    pub fn descendants_of_kind(&self, kind: &str) -> Vec<&OwnedNode> {
        let mut results = Vec::new();
        let mut stack: Vec<&OwnedNode> = self.children.iter().rev().collect();
        // Bounded: same argument as has_descendant_of_kind above -- total
        // pushes across the walk equal the tree's finite node count.
        while let Some(node) = stack.pop() {
            if node.kind == kind {
                results.push(node);
            }
            stack.extend(node.children.iter().rev());
        }
        results
    }

    /// Recursively builds an OwnedNode tree from a tree-sitter Node.
    ///
    /// `source` is the raw file bytes. The bytes are converted to an Arc<str>
    /// ONCE at the top level and then cloned (cheap Arc reference count
    /// increment) for every node in the tree — eliminating the O(N)
    /// per-node String copies from the previous implementation.
    ///
    /// `start_line` is 1-based (tree-sitter rows are 0-based, so we add 1).
    ///
    /// Bug #1794: when `source` is not valid UTF-8, the resulting Arc<str>
    /// MUST stay exactly `source.len()` bytes long. tree-sitter's node
    /// offsets (start_byte/end_byte) are computed against these RAW bytes,
    /// and `text()` slices `self.source` directly with those same offsets —
    /// any length divergence between the raw bytes and this shared string
    /// desynchronizes every offset past the divergence point, making
    /// `text()` return a wrong, shifted slice (or "" once the shifted range
    /// goes out of bounds). See `sanitize_invalid_utf8_preserving_length`.
    pub fn build_from_ts_node(node: tree_sitter::Node, source: &[u8]) -> OwnedNode {
        // Convert raw bytes to Arc<str> ONCE for the whole file.
        let shared_source: Arc<str> = match std::str::from_utf8(source) {
            Ok(s) => Arc::from(s),
            Err(_) => Arc::from(Self::sanitize_invalid_utf8_preserving_length(source)),
        };
        Self::build_recursive(node, &shared_source)
    }

    /// Converts raw bytes containing invalid UTF-8 into a valid UTF-8
    /// `String` whose byte length is IDENTICAL to `bytes.len()`.
    ///
    /// `String::from_utf8_lossy` is NOT length-preserving: it replaces each
    /// ill-formed byte subsequence (1-3 raw bytes, per the Unicode
    /// replacement-character substitution algorithm) with exactly one
    /// U+FFFD codepoint, which is ALWAYS 3 bytes in UTF-8. Any ill-formed
    /// subsequence shorter than 3 raw bytes makes the lossy string LONGER
    /// than the raw source — this is the root cause of Bug #1794: every
    /// tree-sitter byte offset past that point is now stale.
    ///
    /// This function instead performs a strict 1-byte-in/1-byte-out
    /// substitution: every byte belonging to an ill-formed subsequence is
    /// individually replaced with one ASCII `?` (0x3F) byte. Valid UTF-8
    /// runs are copied through unchanged (byte-for-byte identical to the
    /// `Ok` fast path in `build_from_ts_node`). The result therefore always
    /// satisfies `result.len() == bytes.len()`, so every tree-sitter offset
    /// computed against the raw bytes remains valid against this string.
    ///
    /// This keeps a file with stray invalid bytes fully usable (every node
    /// NOT overlapping an invalid byte gets byte-exact correct text) rather
    /// than dropping it outright — at the cost of a single visible `?`
    /// placeholder for the handful of bytes that cannot be represented as
    /// valid UTF-8 at all.
    fn sanitize_invalid_utf8_preserving_length(bytes: &[u8]) -> String {
        let mut out: Vec<u8> = Vec::with_capacity(bytes.len());
        let mut remaining = bytes;
        // Bounded: every iteration below consumes at least one byte from
        // `remaining` (either via the Ok branch, which consumes all of it,
        // or via `invalid_len >= 1` in the Err branch), and `remaining`
        // starts at `bytes.len()` bytes — so this loop runs at most
        // `bytes.len()` times.
        while !remaining.is_empty() {
            match std::str::from_utf8(remaining) {
                Ok(valid) => {
                    out.extend_from_slice(valid.as_bytes());
                    break;
                }
                Err(e) => {
                    let valid_up_to = e.valid_up_to();
                    out.extend_from_slice(&remaining[..valid_up_to]);

                    let available = remaining.len() - valid_up_to;
                    // error_len() is None only when the tail of `remaining`
                    // is a truncated (incomplete) valid prefix — i.e. we
                    // hit the true end of the buffer mid-sequence. In that
                    // case every one of the `available` trailing bytes is
                    // "invalid" for our purposes and gets sanitized.
                    let invalid_len = e.error_len().unwrap_or(available).clamp(1, available);
                    out.extend(std::iter::repeat_n(b'?', invalid_len));

                    remaining = &remaining[valid_up_to + invalid_len..];
                }
            }
        }
        debug_assert_eq!(out.len(), bytes.len(), "sanitization must preserve byte length");
        String::from_utf8(out).expect("sanitized buffer is valid UTF-8 by construction")
    }

    /// Explicit-stack (heap) rewrite of the tree-sitter-Node-to-OwnedNode
    /// build (Bug #1795, site #3 -- was recursive; deep nesting SIGABRT'd
    /// the process). See `BuildFrame`/`make_build_frame`/`finish_build_frame`
    /// above for the per-node bookkeeping this walk drives.
    fn build_recursive(node: tree_sitter::Node, shared_source: &Arc<str>) -> OwnedNode {
        let mut stack = vec![make_build_frame(node)];
        // Bounded: each iteration either descends into one not-yet-visited
        // child (bounded by that frame's finite `ts_children.len()`) or pops
        // one fully-processed frame -- both monotonic over the tree's finite
        // node count, so this terminates.
        loop {
            let descend_into = {
                let top = stack.last_mut().expect("root frame is never popped early");
                if top.next_child < top.ts_children.len() {
                    let child = top.ts_children[top.next_child];
                    top.next_child += 1;
                    Some(child)
                } else {
                    None
                }
            };
            if let Some(child) = descend_into {
                stack.push(make_build_frame(child));
                continue;
            }
            let frame = stack.pop().expect("just checked non-empty above");
            let built = finish_build_frame(frame, shared_source);
            match stack.last_mut() {
                Some(parent) => parent.built_children.push(built),
                None => return built,
            }
        }
    }

    /// Test-only constructor: builds a self-contained leaf node where the
    /// text IS the full source (start_byte=0, end_byte=text.len()).
    #[cfg(test)]
    pub fn new_leaf_for_test(
        kind: &str,
        text: &str,
        start_line: usize,
        is_named: bool,
    ) -> OwnedNode {
        let source: Arc<str> = Arc::from(text);
        OwnedNode {
            kind: kind.to_string(),
            start_line,
            start_byte: 0,
            end_byte: text.len(),
            children: vec![],
            is_named,
            source,
        }
    }

    /// Test-only constructor: builds an interior node whose text() returns
    /// `text` (start_byte=0, end_byte=text.len()).
    #[cfg(test)]
    pub fn new_node_for_test(
        kind: &str,
        text: &str,
        start_line: usize,
        start_byte: usize,
        end_byte: usize,
        children: Vec<OwnedNode>,
        is_named: bool,
    ) -> OwnedNode {
        let source: Arc<str> = Arc::from(text);
        OwnedNode {
            kind: kind.to_string(),
            start_line,
            start_byte,
            end_byte,
            children,
            is_named,
            source,
        }
    }
}

/// Bug #1795, site #4: `OwnedNode` has no manual `Drop`, so the compiler
/// derives one for `children: Vec<OwnedNode>`. A derived Drop recurses one
/// stack frame per tree level exactly like the (now-fixed) traversal
/// methods above -- and a stack overflow during drop aborts the process
/// exactly the same way a stack overflow during traversal does.
///
/// This manual impl flattens the whole subtree into one explicit heap-based
/// worklist instead: `self.children` is taken out of `self` up front, then
/// each popped node's OWN `children` are drained into the same worklist
/// before that node is allowed to drop naturally. By the time any given
/// node's implicit drop actually runs, its `children` Vec is already empty
/// (drained), so that implicit drop is O(1) -- it can never recurse into a
/// child, because there are no children left to recurse into.
impl Drop for OwnedNode {
    fn drop(&mut self) {
        let mut stack: Vec<OwnedNode> = std::mem::take(&mut self.children);
        // Bounded: every iteration pops exactly one node from `stack` and
        // drains its (finite) children back into `stack`; the total number
        // of nodes ever pushed equals the subtree's finite node count, so
        // this terminates.
        while let Some(mut node) = stack.pop() {
            stack.append(&mut node.children);
            // `node` falls out of scope here with an empty `children` Vec,
            // so its own (recursive, unavoidable-to-remove-entirely-since
            // it's compiler-generated) Drop::drop call does zero work.
        }
    }
}

/// S0a regression floor for the OwnedNode primitives (story #1789, AC5).
///
/// One test per primitive, so a regression names the primitive that broke
/// instead of failing one omnibus assertion. These live in-crate rather than
/// in `tests/s0a_regression.rs` because `new_leaf_for_test` /
/// `new_node_for_test` are `#[cfg(test)]` and therefore not linked into the
/// library that integration tests see — in-crate is the only place the
/// provided constructors are reachable.
#[cfg(test)]
mod s0a_primitive_tests {
    use super::*;

    /// Root with: two named `match` leaves at the top level, one anonymous
    /// punctuation leaf, and a `nested` interior node holding a third `match`.
    fn sample_tree() -> OwnedNode {
        let first = OwnedNode::new_leaf_for_test("match", "first", 1, true);
        let punctuation = OwnedNode::new_leaf_for_test("{", "{", 1, false);
        let inner = OwnedNode::new_leaf_for_test("match", "inner", 3, true);
        let nested =
            OwnedNode::new_node_for_test("nested", "", 2, 0, 0, vec![inner], true);
        let last = OwnedNode::new_leaf_for_test("match", "last", 4, true);
        OwnedNode::new_node_for_test(
            "root",
            "",
            1,
            0,
            0,
            vec![first, punctuation, nested, last],
            true,
        )
    }

    // ---- primitive: text() ----

    #[test]
    fn text_returns_the_nodes_own_byte_slice() {
        assert_eq!(OwnedNode::new_leaf_for_test("id", "abc", 1, true).text(), "abc");
    }

    #[test]
    fn text_handles_multibyte_source_without_panicking() {
        assert_eq!(OwnedNode::new_leaf_for_test("s", "café", 1, true).text(), "café");
        assert_eq!(OwnedNode::new_leaf_for_test("s", "🙂", 1, true).text(), "🙂");
    }

    #[test]
    fn text_returns_empty_for_out_of_bounds_byte_range() {
        // Defined degradation, not a panic: a byte range past the end of the
        // shared source yields "" rather than slicing out of bounds.
        let mut node = OwnedNode::new_leaf_for_test("bad", "abc", 1, true);
        node.start_byte = 99;
        node.end_byte = 100;
        assert_eq!(node.text(), "");
    }

    #[test]
    fn text_returns_empty_when_range_splits_a_utf8_codepoint() {
        // "🙂" is 4 bytes; 0..1 lands mid-codepoint. str::get returns None
        // there, so text() degrades to "" instead of panicking the way a
        // direct `&source[0..1]` slice would.
        let mut node = OwnedNode::new_leaf_for_test("emoji", "🙂", 1, true);
        node.start_byte = 0;
        node.end_byte = 1;
        assert_eq!(node.text(), "");
    }

    #[test]
    fn text_of_an_empty_span_is_empty() {
        let node = OwnedNode::new_node_for_test("root", "", 1, 0, 0, vec![], true);
        assert_eq!(node.text(), "");
    }

    // ---- primitive: named_children() ----

    #[test]
    fn named_children_filters_out_anonymous_tokens() {
        let root = sample_tree();
        let kinds: Vec<&str> = root.named_children().iter().map(|c| c.kind.as_str()).collect();
        assert_eq!(kinds, vec!["match", "nested", "match"]);
    }

    #[test]
    fn named_children_is_direct_children_only_not_descendants() {
        // The `nested` node's own `match` child must NOT appear at the root.
        assert_eq!(sample_tree().named_children().len(), 3);
    }

    #[test]
    fn named_children_is_empty_for_a_leaf() {
        assert!(OwnedNode::new_leaf_for_test("id", "x", 1, true)
            .named_children()
            .is_empty());
    }

    // ---- primitive: child_by_kind() ----

    #[test]
    fn child_by_kind_returns_the_first_matching_direct_child() {
        assert_eq!(sample_tree().child_by_kind("match").unwrap().text(), "first");
    }

    #[test]
    fn child_by_kind_returns_none_for_an_absent_kind() {
        assert!(sample_tree().child_by_kind("does_not_exist").is_none());
    }

    #[test]
    fn child_by_kind_does_not_search_descendants() {
        // A kind that exists ONLY below depth 1 must not be found.
        let deep = OwnedNode::new_node_for_test(
            "root",
            "",
            1,
            0,
            0,
            vec![OwnedNode::new_node_for_test(
                "wrapper",
                "",
                1,
                0,
                0,
                vec![OwnedNode::new_leaf_for_test("buried", "x", 2, true)],
                true,
            )],
            true,
        );
        assert!(deep.child_by_kind("buried").is_none());
    }

    #[test]
    fn child_by_kind_matches_anonymous_children_too() {
        // Unlike named_children(), child_by_kind() does not filter on is_named.
        assert_eq!(sample_tree().child_by_kind("{").unwrap().kind, "{");
    }

    // ---- primitive: has_descendant_of_kind() ----

    #[test]
    fn has_descendant_of_kind_finds_a_direct_child() {
        assert!(sample_tree().has_descendant_of_kind("nested"));
    }

    #[test]
    fn has_descendant_of_kind_finds_a_deeper_descendant() {
        assert!(sample_tree().has_descendant_of_kind("match"));
    }

    #[test]
    fn has_descendant_of_kind_is_false_for_an_absent_kind() {
        assert!(!sample_tree().has_descendant_of_kind("does_not_exist"));
    }

    #[test]
    fn has_descendant_of_kind_excludes_the_node_itself() {
        // "descendant" means strictly below: a root of kind "root" must not
        // report itself as a descendant of kind "root".
        assert!(!sample_tree().has_descendant_of_kind("root"));
    }

    // ---- primitive: descendants_of_kind() ----

    #[test]
    fn descendants_of_kind_returns_every_match_in_dfs_pre_order() {
        let root = sample_tree();
        let texts: Vec<&str> = root
            .descendants_of_kind("match")
            .iter()
            .map(|n| n.text())
            .collect();
        assert_eq!(texts, vec!["first", "inner", "last"]);
    }

    #[test]
    fn descendants_of_kind_is_empty_for_an_absent_kind() {
        assert!(sample_tree().descendants_of_kind("does_not_exist").is_empty());
    }

    #[test]
    fn descendants_of_kind_excludes_the_node_itself() {
        assert!(sample_tree().descendants_of_kind("root").is_empty());
    }

    // ---- primitive: public fields ----

    #[test]
    fn public_fields_carry_the_values_they_were_built_with() {
        let node = OwnedNode::new_node_for_test(
            "method_declaration",
            "void f() {}",
            7,
            3,
            9,
            vec![OwnedNode::new_leaf_for_test("identifier", "f", 7, true)],
            true,
        );
        assert_eq!(node.kind, "method_declaration");
        assert_eq!(node.start_line, 7);
        assert_eq!(node.start_byte, 3);
        assert_eq!(node.end_byte, 9);
        assert_eq!(node.children.len(), 1);
        assert!(node.is_named);
        // start_byte/end_byte and text() must agree.
        assert_eq!(node.text(), &"void f() {}"[3..9]);
    }

    #[test]
    fn is_named_is_false_for_anonymous_tokens() {
        assert!(!OwnedNode::new_leaf_for_test("{", "{", 1, false).is_named);
    }

    // ---- deep nesting: stack safety ----

    /// Builds a `depth`-level chain iteratively (an inside-out loop), so the
    /// BUILDER never recurses — otherwise this helper, not the primitive under
    /// test, would be what blows the stack.
    fn deep_chain(depth: usize) -> OwnedNode {
        let mut node = OwnedNode::new_leaf_for_test("needle", "needle", depth + 1, true);
        for level in (0..depth).rev() {
            node = OwnedNode::new_node_for_test("wrapper", "", level + 1, 0, 0, vec![node], true);
        }
        node
    }

    /// Bug #1795: depth well past the empirically measured 8,000-12,000
    /// SIGABRT cliff (issue #1795), to prove real headroom -- not just a
    /// bare pass-by-a-hair. Traversal is iterative (explicit-stack) as of
    /// the #1795 fix; this pins that it survives at production-scale depth
    /// on a default test-thread stack, and also (via
    /// `dropping_a_deep_tree_does_not_abort` below) that the derived `Drop`
    /// glue over `children: Vec<OwnedNode>` no longer recurses either.
    const DEEP_NESTING_DEPTH: usize = 50_000;

    #[test]
    fn has_descendant_of_kind_survives_deep_nesting() {
        let root = deep_chain(DEEP_NESTING_DEPTH);
        assert!(root.has_descendant_of_kind("needle"));
        assert!(!root.has_descendant_of_kind("absent"));
    }

    #[test]
    fn descendants_of_kind_survives_deep_nesting() {
        let root = deep_chain(DEEP_NESTING_DEPTH);
        let found = root.descendants_of_kind("needle");
        assert_eq!(found.len(), 1);
        assert_eq!(found[0].start_line, DEEP_NESTING_DEPTH + 1);
        assert_eq!(root.descendants_of_kind("wrapper").len(), DEEP_NESTING_DEPTH - 1);
    }

    /// Bug #1795, site #4: there is no manual `impl Drop` for `OwnedNode` --
    /// the compiler derives one for `children: Vec<OwnedNode>`, and a
    /// derived Drop recurses one stack frame per tree level exactly like the
    /// traversal methods do. This test's only assertion is that reaching the
    /// end of the function (i.e. the drop of `root` completing) does not
    /// abort the process.
    #[test]
    fn dropping_a_deep_tree_does_not_abort() {
        let root = deep_chain(DEEP_NESTING_DEPTH);
        drop(root);
    }
}

/// Direct property tests for `sanitize_invalid_utf8_preserving_length`
/// (Bug #1794 review, MINOR 1: "the sanitizer has no direct tests").
///
/// The two existing integration tests in
/// `tests/bug_1794_non_utf8_text_offset.rs` only ever exercise a single
/// stray `0xFF` byte, through the public `scanner::parse_file` path. These
/// tests call the private sanitizer directly (possible from a child module
/// via `use super::*`, since Rust visibility allows private items to be
/// seen by descendant modules) with the full range of adversarial byte
/// patterns the function's own doc comment claims to handle, and assert the
/// three properties the whole fix depends on: length preservation, output
/// UTF-8 validity, and byte-exact preservation of valid runs.
///
/// The `debug_assert_eq!` at the end of the function under test (line
/// ~158) is compiled OUT of `--release` builds, and this project ships
/// `--release` with LTO — so length preservation has ZERO runtime
/// verification in the shipping configuration unless these tests cover it.
/// These tests are the intended substitute: guaranteed-by-construction
/// logic plus thorough adversarial coverage, rather than a runtime check on
/// a hot path (see the module-level reasoning recorded in the bug report).
#[cfg(test)]
mod bug_1794_sanitizer_property_tests {
    use super::*;

    /// Runs the sanitizer and asserts the two invariants that must hold for
    /// EVERY input, regardless of pattern: length preservation (the
    /// property every tree-sitter byte offset in the file depends on) and
    /// UTF-8 validity of the result.
    fn sanitize_and_check_invariants(input: &[u8]) -> String {
        let result = OwnedNode::sanitize_invalid_utf8_preserving_length(input);
        assert_eq!(
            result.len(),
            input.len(),
            "length must be preserved for input {input:?}"
        );
        assert!(
            std::str::from_utf8(result.as_bytes()).is_ok(),
            "sanitized output must be valid UTF-8 for input {input:?}"
        );
        result
    }

    /// Table-driven: (case name, input bytes, expected sanitized output
    /// bytes). Expected outputs were derived by hand-tracing the documented
    /// algorithm (`valid_up_to` / `error_len` / `clamp`) against each input
    /// — not copied from, or inferred by running, the implementation under
    /// test — so a table match is real evidence the algorithm behaves as
    /// documented, not a tautology.
    fn table_cases() -> Vec<(&'static str, &'static [u8], &'static [u8])> {
        vec![
            ("empty_buffer", b"", b""),
            ("pure_ascii_fast_path", b"hello world 123", b"hello world 123"),
            (
                "valid_multibyte_no_invalid_bytes",
                "héllo wörld 日本語 🙂".as_bytes(),
                "héllo wörld 日本語 🙂".as_bytes(),
            ),
            // Truncated 3-byte euro-sign lead (0xE2 0x82 of 0xE2 0x82 0xAC)
            // cut off at EOF: error_len() returns None (incomplete valid
            // prefix), so both trailing bytes are sanitized individually.
            // This is the MOST IMPORTANT case per the review: truncated
            // files are exactly what a non-compiling corpus contains.
            ("truncated_multibyte_sequence_at_eof", b"abc\xE2\x82", b"abc??"),
            // 0xC0 0x80 is the overlong encoding of NUL. 0xC0/0xC1 are
            // never valid UTF-8 lead bytes (width 0), so each byte is its
            // own 1-byte invalid run: 0xC0 alone, then the now-lone
            // continuation byte 0x80.
            ("overlong_encoding", b"\xC0\x80", b"??"),
            ("lone_continuation_byte", b"\x80", b"?"),
            (
                "invalid_byte_followed_by_valid_multibyte_sequence",
                b"\xFF\xE2\x82\xAC",
                "?€".as_bytes(),
            ),
            ("multiple_separate_invalid_runs", b"ab\xFFcd\x80ef", b"ab?cd?ef"),
            ("all_invalid_buffer", b"\xFF\xFE\xFD\xFC", b"????"),
        ]
    }

    #[test]
    fn sanitize_matches_expected_output_for_each_adversarial_pattern() {
        for (name, input, expected) in table_cases() {
            let result = sanitize_and_check_invariants(input);
            assert_eq!(
                result.as_bytes(),
                expected,
                "case '{name}': sanitized output did not match the expected byte-exact result"
            );
        }
    }

    /// Every byte in 0xF5..=0xFF is an invalid UTF-8 lead byte under RFC
    /// 3629 (codepoints are capped at U+10FFFF, so no lead byte above 0xF4
    /// can start a valid sequence). Each must sanitize to exactly one '?'
    /// byte — never expanded, never dropped, never merged with a neighbor.
    #[test]
    fn every_invalid_start_byte_is_sanitized_one_for_one() {
        for byte in 0xF5u8..=0xFF {
            let input = [byte];
            let result = sanitize_and_check_invariants(&input);
            assert_eq!(
                result.as_bytes(),
                b"?",
                "invalid start byte {byte:#04X} must sanitize to a single '?'"
            );
        }
    }
}
