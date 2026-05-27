def evaluate_node(node):
    """Detect object allocations in try blocks that have finally clauses."""
    if node.kind != "try_statement":
        return []
    has_finally = False
    for child in node.children:
        if child.kind == "finally_clause":
            has_finally = True
            break
    if not has_finally:
        return []
    body = None
    for child in node.children:
        if child.kind == "block":
            body = child
            break
    if body is None:
        return []
    findings = []
    count = 0
    for stmt in body.named_children():
        if count >= 3:
            break
        if stmt.kind == "local_variable_declaration" and stmt.has_descendant_of_kind("object_creation_expression"):
            findings.append({"pattern": "allocation-in-try", "line": stmt.start_line, "snippet": stmt.text})
        count = count + 1
    return findings
