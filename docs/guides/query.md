# Query Guide

How to search an indexed repository with `cidx query`: the search modes, filters, result controls, reranking,
and the parameters the same search accepts through the server's REST API and MCP `search_code` tool.

Audience: CLI users and AI-agent integrators. Git-history search has its own guide
([Temporal Search](temporal-search.md)); symbol navigation is covered in [SCIP Code Intelligence](scip.md).

## Contents

- [Before You Query](#before-you-query)
- [Search Modes](#search-modes)
  - [Semantic Search](#semantic-search)
  - [Full-Text Search (FTS)](#full-text-search-fts)
  - [Regex Search](#regex-search)
  - [Hybrid Search](#hybrid-search)
- [Filters](#filters)
  - [Language names](#language-names)
  - [Exclusions](#exclusions)
- [Result Control](#result-control)
- [Reranking](#reranking)
- [Multi-Provider Query Strategy](#multi-provider-query-strategy)
- [Querying Other Repositories](#querying-other-repositories)
- [Query Parameter Inventory](#query-parameter-inventory)
- [Validation Rules](#validation-rules)
- [Known Limitations](#known-limitations)
- [Troubleshooting](#troubleshooting)

## Before You Query

Each search mode reads its own index. Build the ones you need inside the repository:

| Mode | Index | Build command | Location |
|------|-------|---------------|----------|
| Semantic | vector index (HNSW) | `cidx init` then `cidx index` | `.code-indexer/index/<model>/` |
| FTS, regex | Tantivy full-text index | `cidx index --fts` (builds semantic and FTS) | `.code-indexer/tantivy_index/` |
| Temporal | per-commit history index | `cidx index --index-commits` | see [Temporal Search](temporal-search.md) |

`cidx index --rebuild-fts-index` rebuilds only the FTS index from already-indexed files. `cidx status` shows which
indexes exist.

Semantic queries embed the query text, so the embedding provider's API key must be set in the environment
(`VOYAGE_API_KEY` for VoyageAI, `CO_API_KEY` for Cohere). FTS and regex queries need no API key.

## Search Modes

| Mode | Flags | Use for |
|------|-------|---------|
| Semantic (default) | none, or `--semantic` | concepts and behaviour: "user authentication logic" |
| FTS | `--fts` | exact identifiers and words: `authenticate_user` |
| Regex | `--fts --regex` | patterns: `def\s+[a-z_]+_user` |
| Hybrid | `--fts --semantic` | an identifier search and a concept search in one call |

### Semantic Search

Finds code by meaning. The query is embedded and compared with the indexed chunks; each result carries a
similarity score (0.0-1.0). By default the candidates are then reordered by a reranker using the query itself (see
[Reranking](#reranking)), so the order can differ from the score order; `--rerank-query ""` keeps similarity order.

```bash
cidx query "user authentication logic" --quiet --limit 5
cidx query "database connection" --language javascript --quiet
```

Results are filtered to the current git branch. In `--quiet` mode each result prints
`<rank>. <score> <staleness> <path>:<lines>` followed by the chunk content.

### Full-Text Search (FTS)

Matches words in the Tantivy index. The index tokenizer splits text on every non-alphanumeric character
(underscore included) and lowercases it, so `authenticate_user` is indexed as the two terms `authenticate` and
`user`. A query with several words returns only documents containing all of them.

```bash
cidx query "authenticate_user" --fts --quiet
cidx query "jwt token" --fts --quiet
cidx query "authenticte" --fts --fuzzy --quiet          # typo tolerance, edit distance 1
cidx query "token" --fts --snippet-lines 0               # list matches without context
```

- `--fuzzy` is shorthand for `--edit-distance 1`; `--edit-distance N` accepts 0-3. Fuzzy matching applies to
  each word separately, so pass the misspelled word on its own.
- `--snippet-lines N` (0-50, default 5) sets the context lines shown around each match.
- Matching is case-insensitive. See [Known Limitations](#known-limitations) for `--case-sensitive`.
- FTS results carry no similarity score; `--min-score` does not apply to them.

In `--quiet` mode each result prints `<rank>. <path>:<line>:<column>`.

### Regex Search

`--fts --regex` matches the pattern as a substring of each indexed file's raw content, like grep. It is not
limited to single tokens: whitespace, punctuation and identifiers with underscores all match.

```bash
cidx query "TODO|FIXME" --fts --regex --quiet
cidx query 'def\s+[a-z_]+_user' --fts --regex --quiet
cidx query 'find_user\(username\)' --fts --regex --quiet
cidx query "def" --fts --regex --language python --quiet
cidx query "connectDatabase" --fts --regex --case-sensitive --quiet
```

- Matching is case-insensitive unless you pass `--case-sensitive`.
- `.` does not match a newline, but `\n`, `\s` and `[\s\S]` do, so a pattern can span lines:
  `'try:\s+authenticate'` matches a `try:` followed by `authenticate` on the next line.
- Tantivy compiles the pattern to an automaton capped at 1000 states. The Unicode class `\w` exceeds the cap;
  CIDX then logs a WARNING and falls back to matching single index tokens, which usually returns nothing for
  multi-word patterns. Use an ASCII class such as `[A-Za-z0-9_]` instead of `\w`.
- `--regex` requires `--fts` and cannot be combined with `--semantic`, `--fuzzy` or `--edit-distance`.

### Hybrid Search

`--fts --semantic` runs the FTS and semantic searches in parallel.

```bash
cidx query "user authentication" --fts --semantic --quiet --limit 5
```

- The CLI prints the FTS results first and the semantic results second, as two separate lists; it does not merge
  them.
- The server merges the two lists with Reciprocal Rank Fusion when `search_mode` is `hybrid`
  (`SemanticQueryManager._merge_hybrid_results`).
- If the FTS index is missing, the CLI warns and runs the semantic search only.
- The FTS half honours only the first `--language` value.

## Filters

| Flag | Repeatable | Effect |
|------|------------|--------|
| `--language LANG` | yes (OR) | include a language by friendly name (`python`, `javascript`, ...) or extension (`py`, `js`) |
| `--exclude-language LANG` | yes | exclude a language |
| `--path-filter GLOB` | yes (OR) | include paths matching a glob |
| `--exclude-path GLOB` | yes | exclude paths matching a glob |
| `--file-extensions LIST` | no | comma-separated extensions (`py,js`, leading dot optional); intersected with `--language` |

```bash
cidx query "authentication" --path-filter "*/tests/*" --quiet
cidx query "authentication" --exclude-path "*/tests/*" --quiet --limit 3
cidx query "authentication" --exclude-language python --quiet
cidx query "authentication" --file-extensions py --language python --quiet --limit 2
cidx query "user" --fts --path-filter "*/tests/*" --quiet
```

- Globs support `*`, `**`, `?` and `[seq]`. A pattern starting with `*/` also matches at the repository root:
  `*/tests/*` matches `tests/test_login.py`.
- Run `cidx query --help` for the full list of friendly language names.
- `--file-extensions` applies to semantic search only; see [Known Limitations](#known-limitations).

### Language names

`--language` and `--exclude-language` accept a friendly name, which expands to several extensions, or a bare
extension, which matches only itself. Names are case-insensitive (`LanguageMapper` in
`src/code_indexer/services/language_mapper.py`):

| Value | Matches |
|-------|---------|
| `python` (or `PYTHON`) | `py`, `pyw`, `pyi` |
| `py` | `py` only |
| `cpp` | `cpp`, `cc`, `cxx`, `c++` |
| `shell` | `sh`, `bash` |

A value that is neither a known name nor a known extension stops the query with a suggestion:

```text
$ cidx query "authenticate user" --language pythom
Error: Unknown language: 'pythom'. Did you mean: python, toml, py, or others?
```

The mapping lives in `.code-indexer/language-mappings.yaml`. `cidx init` creates it with the defaults, and a query
creates it on first use if it is missing. Each entry maps a name to a list of extensions:

```yaml
python: [py, pyw, pyi]
pysource: [py]          # a custom name
```

- The file replaces the built-in table instead of extending it: a name you delete from the file is no longer known.
  Add custom names beside the existing entries.
- The file is read once per process. A standalone `cidx query` sees an edit on its next run; a running daemon keeps
  the table it loaded until it is restarted (`cidx stop`, then `cidx start`).

### Exclusions

- Exclusions win: a file matching any `--exclude-path` or `--exclude-language` is dropped even when it also matches
  an inclusion. Excluding an extension removes only that extension: `--language python --exclude-language py`
  still matches `.pyw` and `.pyi` files. The same language given to both (`--language python --exclude-language
  python`) is reported as a filter conflict (`Language 'python' is both included and excluded. Exclusion will
  override inclusion, resulting in no python files.`) and the query returns no python files.
- The same pattern given to both `--path-filter` and `--exclude-path` is reported as a filter conflict
  (`Path pattern '*/tests/*' is both included and excluded`) and the query returns no results.
- A pattern starting with `*/` matches at any depth, including the repository root: `*/tests/*` matches
  `tests/test_app.py` and `src/a/tests/b/c.py`. `**/tests/**` is equivalent. A pattern without a directory part,
  such as `*.min.js`, matches the file name anywhere (`PathPatternMatcher` in
  `src/code_indexer/services/path_pattern_matcher.py`).

```bash
cidx query "production code" --exclude-path "*/tests/*" --exclude-path "*_test.py" --quiet
cidx query "application logic" --exclude-path "*/node_modules/*" --exclude-path "*/vendor/*" --quiet
cidx query "database models" --language python --path-filter "*/src/*" --exclude-path "*/tests/*" --quiet
```

## Result Control

| Flag | Default | Effect |
|------|---------|--------|
| `--limit N`, `-l N` | 10 | maximum results; see [Known Limitations](#known-limitations) for `0` |
| `--min-score F` | none | drop semantic results scoring below F (0.0-1.0) |
| `--accuracy fast\|balanced\|high` | `balanced` | search accuracy profile; `high` is slower |
| `--quiet`, `-q` | off | print results only, without headers and timing |

```bash
cidx query "authentication" --min-score 0.5 --quiet
cidx query "authentication" --accuracy high --quiet --limit 1
```

## Reranking

A reranker re-orders the retrieved candidates against a second query before the result list is cut to `--limit`.

```bash
cidx query "authentication" --rerank-query "JWT token signature check" --limit 3
cidx query "authentication" --rerank-query "JWT token signature check" \
  --rerank-instruction "Prefer implementation over tests" --quiet --limit 2
cidx query "user" --fts --rerank-query "" --quiet       # disable reranking for this query
```

- `--rerank-query TEXT` sets the reranker query. `--rerank-instruction TEXT` is passed to the reranker with it and
  has no effect without a rerank query.
- When `--rerank-query` is omitted and `rerank.auto_populate_rerank_query` is true (the default), the search query
  itself is used as the rerank query. An empty string (`--rerank-query ""`) disables reranking, except for temporal
  queries (`--time-range`, `--time-range-all`), where an empty value counts as not given and auto-populate applies.
- Reranking calls Voyage or Cohere and needs `VOYAGE_API_KEY` or `CO_API_KEY`. With no key, or when every reranker
  fails, results are returned in retrieval order.
- CLI reranker settings live in a per-user file, created with defaults on first use: `$CIDX_GLOBAL_CONFIG_PATH`
  if set, else `$XDG_CONFIG_HOME/cidx/global.json`, else `~/.config/cidx/global.json`
  (`src/code_indexer/config_global.py`):

```json
{
  "rerank": {
    "auto_populate_rerank_query": true,
    "cohere_reranker_model": "rerank-v3.5",
    "overfetch_multiplier": 5,
    "voyage_reranker_model": "rerank-2.5"
  }
}
```

Rerankers are tried in a fixed order, VoyageAI then Cohere; the file may also contain a `preferred_vendor_order` key,
which does not change that order.

On the server, REST `POST /api/query` and MCP `search_code` accept `rerank_query` and `rerank_instruction`; without
`rerank_query` the server does not rerank.

## Multi-Provider Query Strategy

A server can hold embeddings of the same repository from two providers, VoyageAI (`voyage-ai`) and Cohere
(`cohere`). The MCP `search_code` tool chooses how to use them:

| `query_strategy` | Behaviour |
|------------------|-----------|
| `primary_only` | query the primary provider only |
| `failover` | query VoyageAI; on an API error or timeout (not on empty results) query Cohere |
| `parallel` | query both and fuse the two result lists with `score_fusion` |
| `specific` | query only the provider named in `preferred_provider` (`voyage-ai` or `cohere`; required) |

`score_fusion` (parallel only): `rrf` (Reciprocal Rank Fusion, default; rank-based, so it is unaffected by the two
providers' different score scales), `multiply` or `average` (normalized score arithmetic). Parallel results carry
`fusion_score` and `contributing_providers` next to the raw `similarity_score`.

When `query_strategy` is omitted, the server picks `parallel` with `rrf` if both providers are configured, the
search mode is `semantic` and no temporal parameter is set; otherwise `primary_only`
(`SemanticQueryManager`, `src/code_indexer/server/query/semantic_query_manager.py`). The resolved value is returned
as `effective_query_strategy`. REST `POST /api/query` has no `query_strategy` field and always uses this default.

The CLI does not fan out across providers. A local semantic query embeds the query with the repository's configured
`embedding_provider` (`.code-indexer/config.json`, read by `EmbeddingProviderFactory.create`); there is no fallback
to the other provider: with `VOYAGE_API_KEY` unset, a `voyage-ai` repository fails with
`VOYAGE_API_KEY environment variable is required` even when `CO_API_KEY` is set. Temporal search selects an
embedder with `--temporal-embedder` instead (see [Temporal Search](temporal-search.md)).

## Querying Other Repositories

| Flag | Mode | Effect |
|------|------|--------|
| `--repo ALIAS` | local | query a global repository by its alias (`example-repo-global`) from any directory |
| `--repos A,B` | remote only | query several server repositories in one call (`POST /api/query/multi`) |

`--repo` resolves the alias from the local golden-repos directory: `$CIDX_GOLDEN_REPOS_DIR`, or
`~/.code-indexer/golden-repos` when unset (alias files in its `aliases/` subdirectory). `cidx global list` lists the
repositories registered there. `--repo` and `--repos` are mutually exclusive.

In remote mode (`cidx init --remote`), `cidx query` runs a semantic search on the server for the linked repository.
FTS, regex, hybrid and temporal queries run in local mode only. Which options a remote query sends is listed in
[Remote CLI: Querying in remote mode](remote-cli.md#querying-in-remote-mode).

## Query Parameter Inventory

One table for all three interfaces. The CLI is `cidx query`; REST is `POST /api/query` (model `SemanticQueryRequest`,
`src/code_indexer/server/models/query.py`); MCP is the `search_code` tool
(`src/code_indexer/server/mcp/tool_docs/search/search_code.md`). "-" means the interface does not accept it.

| Parameter | CLI | REST | MCP | Default | Notes |
|-----------|-----|------|-----|---------|-------|
| query text | `QUERY` (positional) | `query_text` | `query_text` | required | REST: 1-1000 characters |
| limit | `--limit` | `limit` | `limit` | 10 | REST/MCP: 1-100 |
| min score | `--min-score` | `min_score` | `min_score` | none | MCP single-repository search applies 0.3 when omitted |
| language | `--language` (repeatable) | `language` | `language` | none | REST/MCP: one value |
| exclude language | `--exclude-language` (repeatable) | `exclude_language` | `exclude_language` | none | |
| path filter | `--path-filter` (repeatable) | `path_filter` | `path_filter` | none | |
| exclude path | `--exclude-path` (repeatable) | `exclude_path` | `exclude_path` | none | MCP: comma-separated for several |
| file extensions | `--file-extensions py,js` | `file_extensions` | `file_extensions` | none | REST/MCP: list with leading dot, `[".py"]` |
| search mode | `--fts` / `--semantic` | `search_mode` | `search_mode` | `semantic` | `semantic`, `fts`, `hybrid` |
| accuracy | `--accuracy` | `accuracy` | `accuracy` | `balanced` | `fast`, `balanced`, `high` |
| case sensitive | `--case-sensitive` | `case_sensitive` | `case_sensitive` | false | CLI also has `--case-insensitive` |
| fuzzy | `--fuzzy` | `fuzzy` | `fuzzy` | false | edit distance 1 |
| edit distance | `--edit-distance` | `edit_distance` | `edit_distance` | 0 | 0-3 |
| snippet lines | `--snippet-lines` | `snippet_lines` | `snippet_lines` | 5 | 0-50 |
| regex | `--regex` | `regex` | `regex` | false | FTS or hybrid only |
| time range | `--time-range` | `time_range` | `time_range` | none | `YYYY-MM-DD..YYYY-MM-DD` |
| all history | `--time-range-all` | `time_range_all` | `time_range_all` | false | |
| at commit | - | `at_commit` | `at_commit` | none | results at or before a commit or ref |
| diff type | `--diff-type` (repeatable) | `diff_type` | `diff_type` | none | accepted, does not filter (see temporal guide) |
| author | `--author` | `author` | `author` | none | |
| chunk type | `--chunk-type` | `chunk_type` | `chunk_type` | none | `commit_message`, `commit_diff` |
| temporal embedder | `--temporal-embedder` | `temporal_embedder` | `temporal_embedder` | active embedder | |
| rerank query | `--rerank-query` | `rerank_query` | `rerank_query` | none | CLI auto-fills it, see [Reranking](#reranking) |
| rerank instruction | `--rerank-instruction` | `rerank_instruction` | `rerank_instruction` | none | |
| repository | `--repo` / `--repos` | `repository_alias` | `repository_alias` | none | MCP also takes a list or a wildcard (`*-global`) |
| aggregation mode | - | `aggregation_mode` | `aggregation_mode` | `global` | multi-repo: `global` or `per_repo` |
| exclude repositories | - | `exclude_patterns` | `exclude_patterns` | none | regex patterns, multi-repo |
| query strategy | - | - | `query_strategy` | see above | [Multi-Provider Query Strategy](#multi-provider-query-strategy) |
| score fusion | - | - | `score_fusion` | `rrf` | parallel strategy only |
| preferred provider | - | - | `preferred_provider` | none | `specific` strategy only |
| response format | - | - | `response_format` | `flat` | multi-repo: `flat` or `grouped` |
| skip embedding cache | - | `no_embedding_cache_shortcut` | `no_embedding_cache_shortcut` | false | skip the server's query-embedding cache read |
| asynchronous | - | `async_query` | - | false | returns a job; fetch it with `GET /api/query/result/{job_id}` |
| quiet output | `--quiet` | - | - | false | |

`tests/unit/query/test_query_parameter_parity.py` pins the parameters shared by the CLI, REST and MCP. A new query
parameter goes into all three interfaces and that test, or is listed there as interface-specific.

## Validation Rules

The CLI rejects these combinations before searching (exit code 1):

| Input | Message |
|-------|---------|
| `--regex` without `--fts` | `--regex requires --fts flag` |
| `--fts --regex --semantic` | `Cannot combine --regex with --semantic` |
| `--regex` with `--fuzzy` or `--edit-distance` > 0 | `Cannot combine --regex with --fuzzy or --edit-distance` |
| `--case-sensitive --case-insensitive` with `--fts` | `Cannot use both --case-sensitive and --case-insensitive` |
| `--edit-distance` outside 0-3 with `--fts` | `--edit-distance must be between 0 and 3` |
| `--snippet-lines` outside 0-50 with `--fts` | `--snippet-lines must be between 0 and 50` |
| `--chunk-type` without a time range | `--chunk-type requires --time-range or --time-range-all` |
| `--repo` with `--repos` | `--repos and --repo are mutually exclusive` |
| `--repos` outside remote mode | `Multi-repository queries require remote mode` |

The case, edit-distance and snippet-lines checks run only in FTS and hybrid mode; a semantic query ignores those
flags. (`--regex --semantic` without `--fts` stops earlier, at `--regex requires --fts flag`.)

REST validates differently (`SemanticQueryRequest`): `regex=true` is accepted with `search_mode` `fts` or `hybrid`
(otherwise `regex=true requires search_mode to be 'fts' or 'hybrid'`), is rejected with `fuzzy=true`
(`regex=true is incompatible with fuzzy=true`), and is accepted with `edit_distance` > 0. REST also rejects a
malformed `time_range` and any `diff_type` other than `added`, `modified`, `deleted`, `renamed`, `binary`.

## Known Limitations

Observed on the current code and filed for fixing; listed so results are not misread.

- `--limit 0`: a semantic query returns no results. An FTS query returns every match only when reranking is off
  (`--rerank-query ""`); with the default auto-filled rerank query it returns nothing.
- `--file-extensions` with more than one extension (`py,js`) returns no results; one extension works. FTS and
  regex searches ignore `--file-extensions`.
- `--case-sensitive` without `--regex` does not distinguish case, because the index lowercases terms. Use
  `--fts --regex --case-sensitive` for case-sensitive matching.
- Temporal queries ignore several of the filters above; see
  [Temporal Search](temporal-search.md#filter-behaviour).

## Troubleshooting

| Symptom | Cause and fix |
|---------|---------------|
| `FTS index not found` | build it: `cidx index --fts` |
| `Temporal index not available` | build it: `cidx index --index-commits` |
| `Full-text search is only supported in local mode` | FTS, regex and hybrid need a local index |
| `Global repo alias '...' not found` | check `cidx global list` and `CIDX_GOLDEN_REPOS_DIR` |
| WARNING `exceeds Tantivy's verbatim-field state limit` | replace `\w` with an ASCII class such as `[A-Za-z0-9_]` |
| no semantic results | lower or drop `--min-score`, remove filters, confirm with `cidx status` that files are indexed |
| fuzzy query finds nothing | pass a single word: fuzzy matching is per word and needs `--fts` |

## Related

- [Temporal Search](temporal-search.md)
- [SCIP Code Intelligence](scip.md)
- [Meta-Repo Discovery](meta-repo-discovery.md)
- [CLI Reference](../reference/cli/README.md)
- [Operating Modes](../getting-started/operating-modes.md)
