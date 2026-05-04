"""XRayNode: ergonomic wrapper over tree_sitter.Node.

Wraps a raw tree_sitter.Node to provide:
- text as str (not bytes) via UTF-8 decoding
- children / named_children as list[XRayNode] (raw nodes never exposed)
- Iterable interface over children
- All positional and flag attributes forwarded from the underlying node

NOTE: This module does NOT import tree_sitter at module level.
The TYPE_CHECKING guard keeps type annotations available for IDEs
without triggering the import at CLI startup time.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Iterator, Optional

if TYPE_CHECKING:
    from tree_sitter import Node as _TsNode


class XRayNode:
    """Ergonomic wrapper over a tree_sitter.Node.

    Never exposes the raw tree_sitter.Node to callers.
    All child-returning methods return XRayNode instances.
    """

    __slots__ = ("_node",)

    def __init__(self, node: "_TsNode") -> None:
        self._node = node

    # ------------------------------------------------------------------
    # Text
    # ------------------------------------------------------------------

    @property
    def text(self) -> str:
        """Node source text as a decoded str (UTF-8, replace on error)."""
        raw = self._node.text
        if raw is None:
            return ""
        if isinstance(raw, bytes):
            return raw.decode("utf-8", errors="replace")
        # tree-sitter may return str in some versions
        return str(raw)

    # ------------------------------------------------------------------
    # Type
    # ------------------------------------------------------------------

    @property
    def type(self) -> str:
        """Grammar node type name (e.g. 'function_definition')."""
        return self._node.type  # always str in tree-sitter

    # ------------------------------------------------------------------
    # Children
    # ------------------------------------------------------------------

    @property
    def children(self) -> list["XRayNode"]:
        """All children (named and anonymous) as XRayNode instances."""
        return [XRayNode(c) for c in self._node.children]

    @property
    def named_children(self) -> list["XRayNode"]:
        """Named children only as XRayNode instances."""
        return [XRayNode(c) for c in self._node.named_children]

    @property
    def child_count(self) -> int:
        """Number of children (named + anonymous)."""
        return self._node.child_count

    @property
    def named_child_count(self) -> int:
        """Number of named children."""
        return self._node.named_child_count

    def child_by_field_name(self, name: str) -> Optional["XRayNode"]:
        """Return the child with the given field name, or None."""
        raw = self._node.child_by_field_name(name)
        if raw is None:
            return None
        return XRayNode(raw)

    def children_by_field_name(self, name: str) -> list["XRayNode"]:
        """Return all children with the given field name as XRayNode instances."""
        return [XRayNode(c) for c in self._node.children_by_field_name(name)]

    # ------------------------------------------------------------------
    # Parent / ancestry
    # ------------------------------------------------------------------

    @property
    def parent(self) -> Optional["XRayNode"]:
        """Parent node as an XRayNode, or None if this is the root."""
        raw = self._node.parent
        if raw is None:
            return None
        return XRayNode(raw)

    def is_descendant_of(self, type_name: str) -> bool:
        """Return True if any ancestor of this node has the given type.

        Walks the parent chain upward.  Returns False if no ancestor matches
        or if this is already the root node.
        """
        current = self._node.parent
        while current is not None:
            if current.type == type_name:
                return True
            current = current.parent
        return False

    def descendants_of_type(self, name: str) -> list["XRayNode"]:
        """Return all descendants whose type matches *name*, in DFS pre-order.

        Uses an explicit stack (not recursion) to keep stack depth bounded for
        deeply nested trees.  Does NOT include the node itself.
        """
        result: list["XRayNode"] = []
        stack = list(reversed(self._node.children))
        while stack:
            current = stack.pop()
            if current.type == name:
                result.append(XRayNode(current))
            stack.extend(reversed(current.children))
        return result

    def count_descendants_of_type(self, name: str) -> int:
        """Return the count of descendants whose type matches *name*.

        Faster than ``len(descendants_of_type(name))`` — uses a plain integer
        accumulator and never materializes wrapper objects.
        """
        count = 0
        stack = list(reversed(self._node.children))
        while stack:
            current = stack.pop()
            if current.type == name:
                count += 1
            stack.extend(reversed(current.children))
        return count

    def enclosing(self, type_name: str) -> Optional["XRayNode"]:
        """Walk up the parent chain (inclusive of self) and return the first
        node whose type matches *type_name*.  Returns None if not found.
        """
        current = self._node
        while current is not None:
            if current.type == type_name:
                return XRayNode(current)
            current = current.parent
        return None

    # ------------------------------------------------------------------
    # Position
    # ------------------------------------------------------------------

    @property
    def start_point(self) -> tuple[int, int]:
        """(row, column) of the first character of this node."""
        return self._node.start_point  # already a tuple

    @property
    def end_point(self) -> tuple[int, int]:
        """(row, column) one past the last character of this node."""
        return self._node.end_point

    @property
    def start_byte(self) -> int:
        """Byte offset of the first character of this node."""
        return self._node.start_byte

    @property
    def end_byte(self) -> int:
        """Byte offset one past the last character of this node."""
        return self._node.end_byte

    # ------------------------------------------------------------------
    # Flags
    # ------------------------------------------------------------------

    @property
    def is_named(self) -> bool:
        """True if this is a named node (not anonymous punctuation)."""
        return bool(self._node.is_named)

    @property
    def has_error(self) -> bool:
        """True if this node or any descendant is an ERROR node."""
        return bool(self._node.has_error)

    # ------------------------------------------------------------------
    # Iteration
    # ------------------------------------------------------------------

    def __iter__(self) -> Iterator["XRayNode"]:
        """Iterate over all children (named + anonymous)."""
        for child in self._node.children:
            yield XRayNode(child)

    # ------------------------------------------------------------------
    # Repr
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        return (
            f"XRayNode(type={self.type!r}, "
            f"start={self.start_point}, end={self.end_point})"
        )
