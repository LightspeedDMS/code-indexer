# Semantic Search

Finds code by meaning. Use it for "what does X do", "where is X implemented", "how is Y handled". For exact
identifiers use `--fts`; for patterns use `--fts --regex` (see fts-search.md).

Requires `cidx index` and the embedding provider's API key in the environment (`VOYAGE_API_KEY` or `CO_API_KEY`).
Results are limited to the current git branch.

## Basic Usage

```bash
cidx query "user authentication logic" --limit 5 --quiet
cidx query "database connection" --language javascript --quiet
```

`--quiet` output: `<rank>. <score> <staleness> <path>:<lines>` followed by the chunk content. Scores are 0.0-1.0.

## Flags

| Flag | Effect |
|------|--------|
| `--limit N` | maximum results (default 10); start with 5-10, large results consume context |
| `--language LANG` | include a language (`python`, `typescript`, or an extension such as `py`); repeatable |
| `--exclude-language LANG` | exclude a language; repeatable |
| `--path-filter GLOB` | include matching paths (`'*/src/*'`); repeatable, OR |
| `--exclude-path GLOB` | exclude matching paths (`'*/tests/*'`); repeatable; exclusions win |
| `--file-extensions EXT` | one extension (`py`); a comma list of several currently returns nothing |
| `--min-score F` | drop results below F; no default |
| `--accuracy fast\|balanced\|high` | accuracy profile (default `balanced`) |
| `--rerank-query TEXT` | rerank candidates against TEXT; `--rerank-query ""` disables reranking |
| `--rerank-instruction TEXT` | instruction for the reranker, used with a rerank query |
| `--quiet` | results only; always use it |

`*/tests/*` also matches a `tests/` directory at the repository root.

## Progressive Refinement

```bash
cidx query "authentication" --limit 5 --quiet
cidx query "authentication" --exclude-path '*/tests/*' --limit 5 --quiet
cidx query "authentication" --language python --exclude-path '*/tests/*' --limit 5 --quiet
cidx query "JWT token signature check" --language python --limit 10 --quiet
```

Add domain words to the query ("JWT token signature check" instead of "authentication") before raising `--limit`.

## Other Repositories

- `--repo ALIAS`: query a global repository (`example-repo-global`) from any directory; aliases come from
  `$CIDX_GOLDEN_REPOS_DIR` (default `~/.code-indexer/golden-repos`). List them with `cidx global list`.
- `--repos A,B`: several repositories at once, remote mode only.

## Troubleshooting

| Problem | Try |
|---------|-----|
| too many weak results | add `--language`/`--path-filter`, raise `--min-score`, make the query more specific |
| no results | drop `--min-score` and filters; check `cidx status` |
| results from tests | `--exclude-path '*/tests/*'` |
| you know the exact name | switch to `--fts` |
| `--limit 0` returns nothing | known limitation; pass an explicit limit |
