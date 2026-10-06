# Temporal Search (Git History)

Semantic search over commits: "when was X added", "which commit changed Y", "what did a developer do in March".
Runs in local mode only.

## Model

Each commit is one document: the commit message, then every changed file's diff under `--- <path> ---`. Long
documents are split into chunks; the first (head) chunk starts with the message. Author, date and hash are stored
with each chunk. History is indexed per embedder (`voyage-context-4` by default, optionally `embed-v4.0`) and per
calendar quarter.

## Index

```bash
cidx index --index-commits                          # current branch; incremental on re-run
cidx index --index-commits --since-date 2025-01-01  # only recent commits
cidx index --index-commits --max-commits 500        # cap new commits this run
cidx index --index-commits --all-branches           # all branches
cidx index --index-commits --diff-context 3         # diff context lines, 0-50 (default 5)
```

`--index-commits` indexes history only; run `cidx index` to update the current-code index. `cidx status` lists the
temporal indexes.

## Query

```bash
cidx query "JWT token validation" --time-range-all --quiet
cidx query "JWT token validation" --time-range 2025-03-01..2025-03-31 --quiet
cidx query "database connector" --time-range-all --author "Alice" --quiet
cidx query "user lookup fix" --time-range-all --chunk-type commit_message --quiet
cidx query "JWT token" --time-range-all --temporal-embedder embed-v4.0 --quiet
```

| Flag | Behaviour |
|------|-----------|
| `--time-range-all` / `--time-range START..END` | required for a temporal query; dates `YYYY-MM-DD` |
| `--author TEXT` | author NAME contains TEXT, case-insensitive; an email matches nothing |
| `--chunk-type commit_message` | only head chunks (the ones starting with the message) |
| `--chunk-type commit_diff` | all chunks; no filtering |
| `--temporal-embedder NAME` | use another indexed embedder; no fallback if it has no index |
| `--limit N` | default 10 |

These flags are accepted but do NOT filter temporal results: `--diff-type`, `--language`, `--exclude-language`,
`--min-score`, `--exclude-path`. `--path-filter` makes a temporal query return nothing. In daemon mode `--author` is
not forwarded.

## Output

`--quiet`: `<rank>. <score> [<embedder>-<quarter>] <path>`. Without `--quiet`: commit hash and date, author name and
email, message, and the matched chunk. Read the hash from the full output, then inspect the commit with git.

## Server (MCP `search_code`)

Same parameters (`time_range`, `time_range_all`, `author`, `chunk_type`, `temporal_embedder`) plus `at_commit`
(hash or ref; results at or before that commit). A golden repository needs temporal indexing enabled
(`enable_temporal` when it is added).

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `Temporal index not available` | `cidx index --index-commits` |
| recent commits missing | re-run `cidx index --index-commits` |
| `--chunk-type requires --time-range or --time-range-all` | add a time range flag |
| author filter finds nothing | pass part of the author's name, not the email |
| file-scoped history needed | drop `--path-filter`; put the file or symbol name in the query, or use `git log -- <path>` |
