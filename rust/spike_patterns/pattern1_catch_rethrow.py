def evaluate_node(node):
    """Detect catch clauses that just rethrow the caught exception."""
    findings = []
    for catch_node in node.descendants_of_kind("catch_clause"):
        param = None
        for child in catch_node.children:
            if child.kind == "catch_formal_parameter":
                param = child
                break
        if param is None:
            continue
        param_name = None
        for child in param.children:
            if child.kind == "identifier":
                param_name = child.text
        if param_name is None:
            continue
        body = None
        for child in catch_node.children:
            if child.kind == "block":
                body = child
                break
        if body is None:
            continue
        named = [c for c in body.children if c.is_named]
        if len(named) != 1:
            continue
        stmt = named[0]
        if stmt.kind != "throw_statement":
            continue
        exprs = [c for c in stmt.children if c.is_named]
        if len(exprs) == 0:
            continue
        expr = exprs[0]
        if expr.kind == "identifier" and expr.text == param_name:
            findings.append({"pattern": "catch-rethrow", "line": stmt.start_line, "snippet": stmt.text})
    return findings
