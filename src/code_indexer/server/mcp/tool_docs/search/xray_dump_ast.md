---
name: xray_dump_ast
category: search
required_permission: query_repos
tl_dr: Synchronously dump the complete tree-sitter AST for a single file — no job polling required.
inputSchema:
  type: object
  properties:
    repository_alias:
      type: string
      description: 'Global repository alias, e.g. "myrepo-global". Use list_global_repos to see available repositories.'
    file_path:
      type: string
      description: 'Relative path to the file inside the repository (e.g. "src/main/App.java"). Must not contain path traversal sequences (../).'
    max_nodes:
      type: integer
      description: 'Maximum number of AST nodes to include in the output tree. When the cap is hit a {"type": "...truncated"} sentinel appears in the children list. Range: 1..2000. Default: 500.'
      minimum: 1
      maximum: 2000
      default: 500
  required:
    - repository_alias
    - file_path
outputSchema:
  type: object
  properties:
    ast_tree:
      type: object
      description: 'BFS-serialised AST tree rooted at the parse root of the file.'
    file_path:
      type: string
      description: 'Relative path of the file that was parsed.'
    language:
      type: string
      description: 'tree-sitter language name detected for this file (e.g. "python", "java").'
    error:
      type: string
      description: 'Error code when the request is rejected.'
    message:
      type: string
      description: 'Human-readable description of the error.'
---

Synchronously dump the complete tree-sitter AST for a single file. Returns the full parse tree inline — no background job and no polling required.

Use this tool to understand the AST shape tree-sitter produces for a specific file before writing an evaluator_code expression for xray_search or xray_explore.

## Parameters

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| repository_alias | str | yes | -- | Global repository alias, e.g. "myrepo-global". Use list_global_repos to see available repositories. |
| file_path | str | yes | -- | Relative path to the file inside the repository (e.g. "src/main/App.java"). Path traversal sequences (../) are rejected. |
| max_nodes | int | no | 500 | Maximum number of AST nodes to include in the output. When the cap is hit a {"type": "...truncated"} sentinel appears in the children list. Range: 1..2000. |

## Response Shape

On success:

```json
{
  "ast_tree": {
    "type": "module",
    "start_byte": 0,
    "end_byte": 128,
    "start_point": [0, 0],
    "end_point": [5, 0],
    "text_preview": "class Foo:\n    def bar(self)...",
    "child_count": 1,
    "children": [
      {
        "type": "class_definition",
        "start_byte": 0,
        "end_byte": 128,
        "start_point": [0, 0],
        "end_point": [4, 20],
        "text_preview": "class Foo:\n    def bar(self)...",
        "child_count": 3,
        "children": [
          {"type": "...truncated"}
        ]
      }
    ]
  },
  "file_path": "src/code_indexer/server/app.py",
  "language": "python"
}
```

Fields per node:
- type: tree-sitter node type string (e.g. "method_invocation", "identifier")
- start_byte / end_byte: byte offsets into the source file
- start_point / end_point: [row, col] line/column positions (0-indexed)
- text_preview: first 80 characters of the node's source text (UTF-8)
- child_count: total number of direct children in the full tree (may exceed children list size when truncated)
- children: serialised child nodes; contains {"type": "...truncated"} when max_nodes cap is hit

## Error Codes

| Error Code | Meaning |
|------------|---------|
| auth_required | User is not authenticated or lacks query_repos permission. |
| repository_not_found | The repository_alias does not exist or is not accessible. |
| invalid_file_path | file_path is empty or otherwise malformed. |
| path_traversal_rejected | file_path contains ../ sequences that would escape the repository root. |
| file_not_found | The file does not exist inside the repository. |
| unsupported_language | The file extension has no registered tree-sitter grammar. Supported extensions correspond to: java, kotlin, go, python, typescript, javascript, bash, csharp, html, css (terraform when tree_sitter_hcl is installed). |
| xray_extras_not_installed | The xray extras package (tree-sitter-languages) is not installed on this server. Install via: pip install code-indexer[xray]. |

## Supported Languages

The 10 mandatory languages: java, kotlin, go, python, typescript, javascript, bash, csharp, html, css. Terraform is the optional 11th when tree_sitter_hcl is importable.

## Examples

**Dump AST for a Python file:**
```json
{
  "repository_alias": "backend-global",
  "file_path": "src/code_indexer/server/app.py"
}
```

**Dump AST with custom node cap:**
```json
{
  "repository_alias": "backend-global",
  "file_path": "src/main/java/com/example/Service.java",
  "max_nodes": 200
}
```

## Related

- See `xray_explore` for a two-phase search that enriches every match with an AST tree.
- See `xray_search` for the production two-phase AST-aware search without AST serialisation overhead.
