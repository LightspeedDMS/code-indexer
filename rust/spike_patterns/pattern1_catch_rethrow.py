def evaluate_node(node):
    """Detect catch clauses that just rethrow the caught exception."""
    if node.kind != "catch_clause":
        return []
    # Find catch parameter name
    param = None
    for child in node.children:
        if child.kind == "catch_formal_parameter":
            param = child
            break
    if param is None:
        return []
    param_name = None
    for child in param.children:
        if child.kind == "identifier":
            param_name = child.text
    if param_name is None:
        return []
    # Find body block
    body = None
    for child in node.children:
        if child.kind == "block":
            body = child
            break
    if body is None:
        return []
    # Check for single throw statement
    named = [c for c in body.children if c.is_named]
    if len(named) != 1:
        return []
    stmt = named[0]
    if stmt.kind != "throw_statement":
        return []
    exprs = [c for c in stmt.children if c.is_named]
    if len(exprs) == 0:
        return []
    expr = exprs[0]
    if expr.kind == "identifier" and expr.text == param_name:
        return [{"pattern": "catch-rethrow", "line": stmt.start_line, "snippet": stmt.text}]
    return []
