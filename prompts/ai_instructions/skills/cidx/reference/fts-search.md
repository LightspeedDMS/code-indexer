# Full-Text Search (FTS) and Regex

Exact word and pattern search over the Tantivy index. Use it for identifiers, error strings, TODO markers and
grep-style patterns. For concepts use semantic search (semantic-search.md).

Requires an FTS index: `cidx index --fts` (error otherwise: `FTS index not found`). FTS runs in local mode only and
needs no API key.

## Word Search (`--fts`)

```bash
cidx query "authenticate_user" --fts --quiet
cidx query "jwt token" --fts --quiet
cidx query "authenticte" --fts --fuzzy --quiet
cidx query "token" --fts --snippet-lines 0
```

- Text is split at every non-alphanumeric character (including `_`) and lowercased: `authenticate_user` is the
  two words `authenticate` + `user`. A multi-word query needs all words in the same document.
- `--fuzzy` = edit distance 1. `--edit-distance N` sets 0-3 (default 0). Fuzzy works per word: pass the misspelled
  word alone.
- `--snippet-lines N`: context lines per match, 0-50 (default 5); 0 lists files only.
- Matching is case-insensitive; `--case-sensitive` has no effect without `--regex`.
- `--quiet` output: `<rank>. <path>:<line>:<column>`. FTS results have no score, so `--min-score` does not apply.

## Regex (`--fts --regex`)

Grep-like: the pattern matches anywhere in a file's raw content, across whitespace and punctuation.

```bash
cidx query "TODO|FIXME" --fts --regex --quiet
cidx query 'def\s+[a-z_]+_user' --fts --regex --quiet
cidx query 'find_user\(username\)' --fts --regex --quiet
cidx query "def" --fts --regex --language python --quiet
cidx query "connectDatabase" --fts --regex --case-sensitive --quiet
```

- Case-insensitive by default; `--case-sensitive` makes it exact.
- `.` does not match a newline, but `\n`, `\s` and `[\s\S]` do, so a pattern can span lines
  (`'try:\s+authenticate'`).
- Patterns are limited to 1000 automaton states. The Unicode class `\w` exceeds it: CIDX logs
  `exceeds Tantivy's verbatim-field state limit` and falls back to single-token matching, which usually finds
  nothing. Use `[A-Za-z0-9_]`, `[a-z_]`, etc.
- Cannot be combined with `--semantic`, `--fuzzy` or `--edit-distance`; requires `--fts`.

## Hybrid (`--fts --semantic`)

```bash
cidx query "user authentication" --fts --semantic --quiet --limit 5
```

Runs both searches in parallel and prints two lists: FTS results first, then semantic results. If the FTS index is
missing it runs semantic only.

## Filters With FTS

`--language` (repeatable), `--exclude-language`, `--path-filter`, `--exclude-path` and `--limit` apply.
`--file-extensions` and `--min-score` are ignored by FTS.

```bash
cidx query "user" --fts --path-filter '*/tests/*' --quiet
cidx query "user" --fts --exclude-path '*/tests/*' --quiet
```

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `FTS index not found` | `cidx index --fts` |
| prose query returns nothing | prose belongs in semantic search |
| identifier not found | search one of its words, or use `--regex` with the full identifier |
| fuzzy finds nothing | one word per fuzzy query |
| regex WARNING about the state limit | replace `\w` with an ASCII range such as `[A-Za-z0-9_]` |
| `--limit 0` returns nothing | add `--rerank-query ""` (auto-reranking truncates to the limit) or pass a number |
