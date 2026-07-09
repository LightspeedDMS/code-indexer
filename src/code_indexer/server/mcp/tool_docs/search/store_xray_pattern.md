---
name: store_xray_pattern
category: search
required_permission: query_repos
tl_dr: Store a reusable Rust xray evaluator pattern in the cidx-meta pattern library. Stored patterns can be referenced by name in xray_search and xray_explore via the pattern_name parameter.
slim_description: "Persist a hard-won xray evaluator to the pattern library so it survives session restarts, is shared with all users, and can be re-run without re-deriving. Use this whenever an evaluator took more than one iteration to get right. Patterns can declare typed parameters (usize, i64, f64, bool, str) with defaults that callers override via pattern_name in xray_search and xray_explore."
inputSchema:
  type: object
  properties:
    scope:
      type: string
      description: 'Target scope for the pattern. Use "__any__" to store a cross-repo pattern accessible from any repository alias. Use a specific repository alias (e.g. "myrepo-global") to store a repo-specific pattern that takes priority over __any__ patterns when that repo is searched.'
    pattern_yaml:
      type: string
      description: |
        YAML string defining the pattern. Required top-level fields:
          name (str): Pattern name used as filename stem and lookup key. Use kebab-case (e.g. "catch-rethrow").
          description (str): Human-readable description of what the pattern finds.
          language (str): Target language (e.g. "java", "python", "typescript").
          evaluator_code (str): Rust fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> body. Same Rust security whitelist as xray_search applies.
        Optional fields:
          tags (list[str]): Categorization labels.
          author (str): Pattern author.
          created_at (str): ISO date string.
          parameters (list): Typed parameter declarations — see Parameters section.
    overwrite:
      type: boolean
      description: 'When false (default), returns pattern_already_exists error if a pattern with the same name already exists in the given scope. Set to true to replace an existing pattern.'
      default: false
  required:
    - scope
    - pattern_yaml
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: 'true on successful store.'
    path:
      type: string
      description: 'Relative path (within cidx-meta) where the pattern YAML was written (e.g. xray-patterns/__any__/catch-rethrow.yaml).'
    error:
      type: string
      description: 'Error code when the request fails.'
    message:
      type: string
      description: 'Human-readable description of the error.'
    error_code:
      type: string
      description: 'Structured sub-code for xray_evaluator_validation_failed errors (e.g. forbidden_unsafe, forbidden_import).'
    offending_construct:
      type: string
      description: 'The specific forbidden construct that caused xray_evaluator_validation_failed.'
    offending_line:
      type: integer
      description: '1-based line number of the offending construct in evaluator_code.'
---

Store a named, reusable Rust xray evaluator pattern in the cidx-meta pattern library.

Stored patterns are YAML files written to the cidx-meta repository under `xray-patterns/{scope}/{name}.yaml`. The write is committed to git automatically so the pattern is versioned and available across server restarts.

Once stored, use the pattern by name in `xray_search` or `xray_explore` via the `pattern_name` parameter (mutually exclusive with `evaluator_code`).

## When to store a pattern

Store a pattern when any of these apply:

- You ran `xray_explore` before writing the evaluator (the AST shape wasn't obvious).
- The evaluator needed more than one compile/run cycle to get right.
- You filtered out false positives through iteration (e.g. added a `Promise.all` exclusion, tightened node-kind checks).
- The pattern encodes domain knowledge specific to this codebase.
- Another user would plausibly need the same scan.

If you close the session without storing, that work is gone.

### Post-session checklist

Before ending a session where you developed a new evaluator: if the evaluator took iteration, call `store_xray_pattern`. The pattern is git-committed to cidx-meta automatically and immediately available to all users.

## Pattern YAML Schema

```yaml
name: catch-rethrow              # required, kebab-case, used as lookup key
description: "Detects empty catch blocks that just rethrow"  # required
language: java                   # required, target language
tags: [error-handling]           # optional
author: myteam                   # optional
created_at: "2026-05-28"         # optional
evaluator_code: |                # required, Rust fn evaluate_node body
  fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
      vec![]
  }
parameters:                      # optional, typed parameter declarations
  - name: DEPTH_THRESHOLD
    type: usize
    default: 4
    description: "Minimum nesting depth to report"
```

## Parameters (Typed Constants)

Patterns can declare typed parameters that become Rust `const` declarations prepended to the evaluator code. Callers override defaults via `pattern_params` in `xray_search`/`xray_explore`.

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| name | str | yes | UPPER_SNAKE_CASE constant name (e.g. DEPTH_THRESHOLD) |
| type | str | yes | One of: usize, i64, f64, bool, str |
| default | any | yes | Default value compatible with the declared type |
| description | str | no | Human-readable description |

Parameter types map to Rust consts:
- `usize` → `const NAME: usize = value;`
- `i64` → `const NAME: i64 = value;`
- `f64` → `const NAME: f64 = value;`
- `bool` → `const NAME: bool = true/false;`
- `str` → `const NAME: &str = "value";`

## Scope Resolution

When `xray_search` or `xray_explore` resolves a `pattern_name`:
1. Looks for `cidx-meta/xray-patterns/{repo_alias}/{pattern_name}.yaml` (repo-specific, highest priority)
2. Falls back to `cidx-meta/xray-patterns/__any__/{pattern_name}.yaml` (cross-repo)

Store in `__any__` scope for patterns that apply to any codebase. Store in a repo-specific scope for patterns tuned to a particular repository.

## Error Codes

| Code | Meaning |
|------|---------|
| auth_required | Unauthenticated or missing query_repos permission |
| scope_required | scope parameter missing or empty |
| pattern_yaml_required | pattern_yaml parameter missing or empty |
| invalid_yaml | pattern_yaml cannot be parsed as YAML |
| missing_required_field | A required field (name, description, language, evaluator_code) is absent |
| xray_evaluator_validation_failed | evaluator_code fails the Rust security whitelist (see error_code, offending_construct, offending_line) |
| pattern_already_exists | Pattern with this name already exists in scope and overwrite=false |
| invalid_parameter | A parameter declaration uses an unknown field |
| invalid_parameter_type | Parameter type is not one of: usize, i64, f64, bool, str |

## Examples

**Store a simple cross-repo pattern (no parameters)**:
```json
{
  "scope": "__any__",
  "pattern_yaml": "name: find-todos\ndescription: \"Find TODO and FIXME comments\"\nlanguage: python\nevaluator_code: |\n  fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {\n      let mut findings = Vec::new();\n      for c in node.descendants_of_kind(\"comment\") {\n          if c.text.contains(\"TODO\") || c.text.contains(\"FIXME\") {\n              findings.push(EvalFinding {\n                  pattern: \"todo_fixme\".to_string(),\n                  line: c.start_line,\n                  snippet: c.text.chars().take(120).collect(),\n              });\n          }\n      }\n      findings\n  }\n"
}
```

**Store a parametrized pattern**:
```json
{
  "scope": "__any__",
  "pattern_yaml": "name: deep-nesting\ndescription: \"Finds methods with high nesting depth\"\nlanguage: java\nparameters:\n  - name: DEPTH_THRESHOLD\n    type: usize\n    default: 4\n    description: Minimum nesting depth\nevaluator_code: |\n  fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {\n      let mut findings = Vec::new();\n      for m in node.descendants_of_kind(\"method_declaration\") {\n          let depth = m.descendants_of_kind(\"if_statement\").len();\n          if depth >= DEPTH_THRESHOLD {\n              findings.push(EvalFinding {\n                  pattern: \"deep_nesting\".to_string(),\n                  line: m.start_line,\n                  snippet: format!(\"depth={}\", depth),\n              });\n          }\n      }\n      findings\n  }\n"
}
```

**Overwrite an existing pattern**:
```json
{
  "scope": "__any__",
  "pattern_yaml": "name: find-todos\n...",
  "overwrite": true
}
```

**Store a repo-specific pattern (takes priority over __any__)**:
```json
{
  "scope": "myapp-global",
  "pattern_yaml": "name: check-api-usage\ndescription: \"Check API usage patterns specific to myapp\"\nlanguage: java\nevaluator_code: |\n  fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { vec![] }\n"
}
```

## Pattern Library Best Practices

A non-trivial evaluator typically costs 3-6 tool round-trips to develop: one `xray_explore` to understand the AST shape, one or two compile failures on type errors, and one or two runs to eliminate false positives. Storing the result means that cost is paid once across all users and all future sessions. An evaluator that is not stored is effectively thrown away when the session ends.

Store any evaluator that meets the trigger heuristics above (see "When to store a pattern") rather than re-deriving it later. Benefits:

- **Reuse across sessions**: Stored patterns persist in cidx-meta (git-versioned) and survive server restarts.
- **Share across users**: All authenticated users with query_repos permission can reference stored patterns.
- **Parameterize once, override per-call**: Declare typed parameters with defaults; callers override via pattern_params without editing evaluator code.

### Discovering Stored Patterns

Use the `browse_directory` or `directory_tree` tools on the cidx-meta repository to list available patterns:

- `browse_directory('cidx-meta-global', path='xray-patterns')` -- lists all scopes
- `browse_directory('cidx-meta-global', path='xray-patterns/__any__')` -- lists cross-repo patterns
- `directory_tree('cidx-meta-global', path='xray-patterns')` -- visual tree of all patterns

To read a specific pattern's YAML (including its evaluator_code and parameters), use `get_file_content('cidx-meta-global', path='xray-patterns/__any__/catch-rethrow.yaml')`.

## Related

- See `xray_search` for using stored patterns via `pattern_name` in production searches.
- See `xray_explore` for using stored patterns via `pattern_name` during AST exploration.
- Seed patterns `catch-rethrow` and `deep-nesting` are created automatically in `__any__/` on first use.
