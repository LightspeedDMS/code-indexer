def evaluate_node(node):
    """Detect methods with more than 50 statements (too long)."""
    if node.kind != "method_declaration":
        return []
    body = None
    for child in node.children:
        if child.kind == "block":
            body = child
            break
    if body is None:
        return []
    stmt_count = 0
    for child in body.named_children():
        stmt_count = stmt_count + 1
    if stmt_count > 50:
        method_name = ""
        for child in node.children:
            if child.kind == "identifier":
                method_name = child.text
                break
        return [{"pattern": "method-too-long", "line": node.start_line, "snippet": method_name}]
    return []
