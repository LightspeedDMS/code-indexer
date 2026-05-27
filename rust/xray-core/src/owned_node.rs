/// OwnedNode — heap-allocated, Clone-able copy of a tree-sitter Node.
///
/// tree-sitter Node objects borrow from the owning Tree and cannot cross
/// thread boundaries. OwnedNode owns all its data, so it can be moved freely
/// across threads for parallel scanning.
#[derive(Debug, Clone)]
pub struct OwnedNode {
    pub kind: String,
    pub start_line: usize, // 1-based
    pub start_byte: usize,
    pub end_byte: usize,
    pub children: Vec<OwnedNode>,
    pub is_named: bool,
    pub text: String,
}

impl OwnedNode {
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

    /// Recursively builds an OwnedNode tree from a tree-sitter Node.
    ///
    /// `source` is the raw file bytes used to extract text slices.
    /// `start_line` is 1-based (tree-sitter rows are 0-based, so we add 1).
    pub fn build_from_ts_node(node: tree_sitter::Node, source: &[u8]) -> OwnedNode {
        let start_byte = node.start_byte();
        let end_byte = node.end_byte();
        let text = String::from_utf8_lossy(&source[start_byte..end_byte]).into_owned();
        let start_line = node.start_position().row + 1;

        let mut children = Vec::with_capacity(node.child_count());
        let mut cursor = node.walk();
        for child in node.children(&mut cursor) {
            children.push(OwnedNode::build_from_ts_node(child, source));
        }

        OwnedNode {
            kind: node.kind().to_string(),
            start_line,
            start_byte,
            end_byte,
            children,
            is_named: node.is_named(),
            text,
        }
    }
}
