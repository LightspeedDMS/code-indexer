# X-Ray Evaluator Cookbook

Practical evaluator patterns for the X-Ray AST-aware code search engine.
Each pattern includes the MCP `xray_search` parameters and a complete evaluator script.

---

## Java try-with-resources Leak Detection

Finds resource acquisitions (`getConnection`, `getSession`, `openStream`) that are
NOT wrapped in a `try-with-resources` block -- potential resource leaks.

Uses `is_in_try_resources()` (walks the AST parent chain for `resource_specification`)
and `enclosing_method_body()` (resolves the enclosing method for context).

### MCP Parameters

```json
{
  "repository_alias": "my-java-repo-global",
  "driver_regex": "getConnection|getSession|openStream",
  "search_target": "content",
  "evaluator_code": "... (see below) ..."
}
```

### Evaluator Code

```python
def get_method_name(node):
    """Walk to enclosing method and extract its name."""
    body = node.enclosing_method_body()
    if body and body.parent:
        name_node = body.parent.child_by_field_name("name")
        if name_node:
            return name_node.text
    return "unknown"

leaks = []
for pos in match_positions:
    hit_node = pos.get("ast_node")
    if hit_node is None:
        continue
    # Skip hits already inside a try-with-resources resource declaration
    if hit_node.is_in_try_resources():
        continue
    # Not wrapped -- potential leak
    leaks.append({
        "line_number": pos["line_number"],
        "method": get_method_name(hit_node),
        "reason": "resource acquired outside try-with-resources",
    })
return {"matches": leaks, "value": {"leak_count": len(leaks)}}
```

### What it detects

Source:
```java
public class UserDao {
    Connection getConn() {
        // FLAGGED: bare getConnection, no try-with-resources
        return pool.getConnection();
    }

    void safe() throws Exception {
        // NOT flagged: wrapped in try-with-resources
        try (Connection c = pool.getConnection()) {
            c.execute("SELECT 1");
        }
    }
}
```

Output: one match for line 4 (`getConn` method), zero matches inside `safe`.

---

## Kotlin .use {} Detection

Finds resource acquisitions in Kotlin code that are NOT followed by `.use { }`,
the idiomatic Kotlin resource management pattern (equivalent to Java
try-with-resources).

This pattern uses line-content heuristics rather than AST node traversal,
since Kotlin `.use {}` is a stdlib extension function (not a language construct
visible in the AST grammar).

### MCP Parameters

```json
{
  "repository_alias": "my-kotlin-repo-global",
  "driver_regex": "getConnection|openStream|createStatement",
  "search_target": "content",
  "evaluator_code": "... (see below) ..."
}
```

### Evaluator Code

```python
leaks = []
for pos in match_positions:
    line = pos.get("line_content", "")
    # Kotlin idiomatic resource management uses .use { }
    if ".use " not in line and ".use{" not in line:
        leaks.append({
            "line_number": pos["line_number"],
            "line_content": line.strip(),
            "reason": "resource acquired without .use {} block",
        })
return {"matches": leaks, "value": None}
```

### What it detects

Source:
```kotlin
fun fetchData() {
    // FLAGGED: no .use {}
    val conn = dataSource.getConnection()
    conn.close()

    // NOT flagged: idiomatic .use {} pattern
    dataSource.getConnection().use { conn ->
        conn.prepareStatement("SELECT 1").execute()
    }
}
```

Output: one match for line 3 (bare `getConnection`), zero for the `.use {}` block.

---

## Pattern Template

Use this skeleton when writing new evaluator patterns:

```python
# Phase 1 driver_regex selects candidate files; evaluator refines per-file.
# match_positions[i]["ast_node"] is the smallest named AST node at the hit.
# Return {"matches": [...], "value": <per-file-summary>}
# Return {"skip": True} to bail out early (file counts as processed, no matches).

results = []
for pos in match_positions:
    node = pos.get("ast_node")
    if node is None:
        continue
    # Your filtering logic here
    results.append({"line_number": pos["line_number"]})
return {"matches": results, "value": None}
```

### Available globals in evaluator

| Global | Type | Description |
|--------|------|-------------|
| `node` | XRayNode | File root AST node |
| `root` | XRayNode | Alias for `node` |
| `source` | str | Full file source text |
| `lang` | str | Language identifier (java, python, kotlin, ...) |
| `file_path` | str | Absolute file path |
| `match_positions` | list[dict] | Phase 1 hits with `line_number`, `byte_offset`, `ast_node`, ... |

### XRayNode helpers

| Method | Returns | Description |
|--------|---------|-------------|
| `.type` | str | Grammar node type (e.g. `method_invocation`) |
| `.text` | str | Node source text (UTF-8 decoded) |
| `.children` | list[XRayNode] | All children |
| `.named_children` | list[XRayNode] | Named children only |
| `.parent` | XRayNode or None | Parent node |
| `.child_by_field_name(name)` | XRayNode or None | Child with grammar field name |
| `.descendants_of_type(name)` | list[XRayNode] | All descendants matching type |
| `.enclosing(type_name)` | XRayNode or None | Walk parent chain for type |
| `.is_in_try_resources()` | bool | Inside Java try-with-resources |
| `.enclosing_method_body()` | XRayNode or None | Body block of enclosing method |
| `.node_at_byte_offset(off)` | XRayNode or None | Smallest named node at offset |
| `.start_byte` / `.end_byte` | int | Byte range |
| `.start_point` / `.end_point` | tuple[int,int] | (row, column) |

### Available built-ins

Evaluators cannot import any modules. The sandbox blocks all `import` and
`from ... import` statements. Use only the provided globals (listed above) and
the following eight safe built-ins:

`len`, `any`, `all`, `range`, `enumerate`, `sorted`, `min`, `max`

No other built-ins or standard library modules are available.

### Evaluator return contract

Must return `{"matches": [{"line_number": int, ...}, ...], "value": <any>}`.

Optional keys: `"file_role": str` (surfaced in `file_metadata[]`),
`"skip": True` (early bail-out, no matches contributed).
