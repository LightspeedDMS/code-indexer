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
    pub fn has_descendant_of_kind(&self, kind: &str) -> bool {
        for child in &self.children {
            if child.kind == kind {
                return true;
            }
            if child.has_descendant_of_kind(kind) {
                return true;
            }
        }
        false
    }

    /// Returns all descendant nodes (at any depth) whose kind matches `kind`.
    pub fn descendants_of_kind(&self, kind: &str) -> Vec<&OwnedNode> {
        let mut results = Vec::new();
        self.collect_descendants_of_kind(kind, &mut results);
        results
    }

    fn collect_descendants_of_kind<'a>(&'a self, kind: &str, results: &mut Vec<&'a OwnedNode>) {
        for child in &self.children {
            if child.kind == kind {
                results.push(child);
            }
            child.collect_descendants_of_kind(kind, results);
        }
    }

    /// Recursively builds an OwnedNode tree from a tree-sitter Node.
    ///
    /// `source` is the raw file bytes. The bytes are converted to an Arc<str>
    /// ONCE at the top level and then cloned (cheap Arc reference count
    /// increment) for every node in the tree — eliminating the O(N)
    /// per-node String copies from the previous implementation.
    ///
    /// `start_line` is 1-based (tree-sitter rows are 0-based, so we add 1).
    pub fn build_from_ts_node(node: tree_sitter::Node, source: &[u8]) -> OwnedNode {
        // Convert raw bytes to Arc<str> ONCE for the whole file.
        let shared_source: Arc<str> = match std::str::from_utf8(source) {
            Ok(s) => Arc::from(s),
            Err(_) => Arc::from(String::from_utf8_lossy(source).as_ref()),
        };
        Self::build_recursive(node, &shared_source)
    }

    /// Internal recursive helper. Takes a reference to the shared Arc so
    /// each recursive call only increments the reference count (no copy).
    fn build_recursive(node: tree_sitter::Node, shared_source: &Arc<str>) -> OwnedNode {
        let start_byte = node.start_byte();
        let end_byte = node.end_byte();
        let start_line = node.start_position().row + 1;

        let mut children = Vec::with_capacity(node.child_count());
        let mut cursor = node.walk();
        for child in node.children(&mut cursor) {
            children.push(Self::build_recursive(child, shared_source));
        }

        OwnedNode {
            kind: node.kind().to_string(),
            start_line,
            start_byte,
            end_byte,
            children,
            is_named: node.is_named(),
            source: Arc::clone(shared_source),
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
