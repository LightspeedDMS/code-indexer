def evaluate_node(node):
    """Detect object allocations in try blocks that have finally clauses."""
    findings = []
    max_stmts_to_scan = 3
    for try_node in node.descendants_of_kind("try_statement"):
        has_finally = False
        for child in try_node.children:
            if child.kind == "finally_clause":
                has_finally = True
                break
        if not has_finally:
            continue
        body = None
        for child in try_node.children:
            if child.kind == "block":
                body = child
                break
        if body is None:
            continue
        count = 0
        for stmt in body.named_children():
            if count >= max_stmts_to_scan:
                break
            if stmt.kind == "local_variable_declaration" and stmt.has_descendant_of_kind("object_creation_expression"):
                findings.append({"pattern": "allocation-in-try", "line": stmt.start_line, "snippet": stmt.text})
            count = count + 1
    return findings
