---
name: scip_references
category: scip
required_permission: query_repos
tl_dr: Find all locations where a symbol is used (called, imported, referenced).
---

Find all locations where a symbol is used (called, imported, referenced). Returns list of file paths, line numbers, and reference kinds.

Pass simple symbol names (e.g., 'UserService'). Fuzzy match by default ('User' matches 'UserService', 'UserManager'). Use exact=true for precise match. Requires SCIP indexes (cidx scip generate).

USE FOR: Finding all usages of a symbol, understanding code coupling, impact of changes.
NOT FOR: Finding where symbol is defined (scip_definition), analyzing call chains (scip_callchain).

EXAMPLE: scip_references(symbol='authenticate') -> [{file_path, line, kind='call'}, ...]

### Reranking Parameters (Optional)

**Mental model — two-query pattern**: Use `symbol` to select which symbol's references to retrieve; use `rerank_query` (verbose natural language) to pick the best ordering from those references. These serve different purposes.

When a symbol has 100+ references across a codebase, most are import statements or test fixtures. Reranking surfaces the production instantiation, configuration, and meaningful call sites that are most relevant to your intent.

- **rerank_query** = WHAT you want ranked highest. Write a detailed sentence describing the kind of reference you care about. The cross-encoder scores each reference's `context` snippet against this description.
- **rerank_instruction** = WHAT to deprioritize. Steer the reranker away from noise. Example: "Focus on instantiation and production use, not imports or tests". Has no effect without rerank_query.

#### When to Proactively Add Reranking

Consider adding rerank_query even when the user did not ask for it explicitly:
- The symbol is widely used (>20 references likely) and the user cares about a specific usage pattern
- The result set will likely include many import statements, test fixtures, or boilerplate
- The user is trying to understand where something is actually instantiated or configured

#### When to Use Reranking

Cross-encoder reranking re-scores references against your rerank_query to surface references that best match your intent. Particularly useful when a symbol has many references but only a few are semantically relevant (e.g., finding where a service is actually instantiated vs. just imported everywhere).

Reranking for scip_references uses the `context` field of each reference — the line of code where the reference occurs. It works best when context snippets contain meaningful code, not just import lines or pass-through calls.

#### What Reranking Does Not Do

Reranking does not change the set of symbols searched or bypass SCIP index queries. However, when `rerank_query` is set, the handler automatically overfetches up to 5x the requested `limit` (capped at 200) to give the reranker a larger candidate pool, then truncates back to `limit` after reranking.

#### Returned Telemetry

When reranking is requested, the response includes query_metadata with:
- reranker_used
- reranker_provider
- rerank_time_ms

If providers are disabled, unavailable, or all rerank attempts fail, the tool falls back to the base reference ordering and reports that reranking was not used.

#### Examples

**With reranking — finding production instantiation:**
```json
{
  "symbol": "UserService",
  "rerank_query": "places where UserService is instantiated or constructed in production application code",
  "rerank_instruction": "Focus on instantiation and dependency injection, not imports or test setup",
  "limit": 20
}
```

**With reranking — finding configuration references:**
```json
{
  "symbol": "DatabaseManager",
  "rerank_query": "references that configure or initialize DatabaseManager with connection settings",
  "limit": 20
}
```

**Without reranking (intentional opt-out):**
```json
{
  "symbol": "authenticate",
  "limit": 20
}
```
Result: same references in default order, with no reranking overhead.
