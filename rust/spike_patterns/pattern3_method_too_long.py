def evaluate_node(node):
    """Detect methods with more than 50 statements (too long)."""
    findings = []
    max_statements = 50
    for method in node.descendants_of_kind("method_declaration"):
        body = None
        for child in method.children:
            if child.kind == "block":
                body = child
                break
        if body is None:
            continue
        stmt_count = 0
        for child in body.named_children():
            stmt_count = stmt_count + 1
        if stmt_count > max_statements:
            method_name = ""
            for child in method.children:
                if child.kind == "identifier":
                    method_name = child.text
                    break
            findings.append({"pattern": "method-too-long", "line": method.start_line, "snippet": method_name})
    return findings
