---
name: cidx
description: Code search and intelligence using CIDX. Use when searching codebases, finding implementations, tracing call graphs, analyzing dependencies, or searching git history. Preferred over grep/find for all code exploration.
---

# CIDX - Semantic Code Search and Intelligence

CIDX (Code Indexer) reference for AI coding assistants. Detailed guides are in `reference/`.

## INDEX MANAGEMENT - CHECK FIRST

Each search mode needs its own index; without it the query fails or returns nothing.

```bash
cidx status                  # semantic, FTS and temporal index status
cidx scip status             # SCIP status: success / failed / pending / limbo (partial)
cidx scip status -v          # include per-project errors
```

Create or update indexes:

```bash
cidx init                    # create .code-indexer/ (once)
cidx index --fts             # semantic + FTS index (incremental on re-run)
cidx index --clear --fts     # full rebuild
cidx index --index-commits   # git history for temporal search (history only)
cidx scip generate           # SCIP indexes for every discovered project
cidx scip rebuild --failed   # retry failed SCIP projects
```

SCIP project markers: Java `pom.xml`/`build.gradle`, Kotlin `build.gradle.kts`, TypeScript/JavaScript
`package.json`, Python `pyproject.toml`/`setup.py`/`requirements.txt`, C# `*.sln`/`*.csproj`, Go `go.mod`. Each
language's SCIP indexer must be installed.

## CHOOSE THE MODE

| Query | Mode | Example |
|-------|------|---------|
| concept, behaviour, question | semantic (default) | `cidx query "user authentication flow" --quiet` |
| exact identifier or word | FTS | `cidx query "authenticate_user" --fts --quiet` |
| pattern, grep replacement | regex | `cidx query 'def [a-z_]+_user' --fts --regex --quiet` |
| when / which commit | temporal | `cidx query "JWT validation" --time-range-all --quiet` |
| definition, usages, callers | SCIP | `cidx scip references UserService` |

Never pass prose to `--fts`: `cidx query "how does login work" --fts` matches only documents containing every one
of those words. Prose goes to semantic search.

## KEY FLAGS

`--limit N` (default 10; start with 5-10 to save context) | `--language python` | `--exclude-language js` |
`--path-filter '*/src/*'` | `--exclude-path '*/tests/*'` | `--min-score 0.6` (semantic only) |
`--accuracy fast|balanced|high` | `--quiet` (always)

Example: `cidx query "authentication" --language python --exclude-path '*/tests/*' --limit 5 --quiet`

## FTS AND REGEX

- `--fts`: word matching; identifiers are split at `_` and lowercased. `--fuzzy` (edit distance 1) or
  `--edit-distance 0-3` tolerates typos per word. `--snippet-lines 0-50` (default 5).
- `--fts --regex`: grep-like substring match over each file, case-insensitive unless `--case-sensitive`.
  Whitespace and punctuation work (`'find_user\(username\)'`). Use `[A-Za-z0-9_]` instead of `\w`; `\w` exceeds
  the regex size limit and degrades to token matching. `.` stops at newlines; `\s`/`\n` cross them.
  Incompatible with `--semantic`, `--fuzzy` and `--edit-distance`.
- `--fts --semantic`: hybrid; prints the FTS list, then the semantic list.

## TEMPORAL (GIT HISTORY)

Requires `cidx index --index-commits`. Flags: `--time-range-all` | `--time-range YYYY-MM-DD..YYYY-MM-DD` |
`--author NAME` (name substring, not email) | `--chunk-type commit_message`. `--language` and `--diff-type` do not
filter temporal results; `--path-filter` makes them empty. Details: reference/temporal-search.md.

## SCIP (CALL GRAPH AND DEPENDENCIES)

```bash
cidx scip definition SYMBOL          # where defined (substring match; --exact for Class#method)
cidx scip references SYMBOL          # where used
cidx scip dependencies SYMBOL        # what it uses (--depth 1-10)
cidx scip dependents SYMBOL          # what uses it (--depth 1-10)
cidx scip impact SYMBOL              # what a change affects (--depth 1-10, default 3)
cidx scip callchain FROM TO          # call paths (--max-depth 1-3, default 3)
cidx scip context SYMBOL             # definition plus related references, scored (--limit caps files)
```

Common options: `--limit N` (default 0 = unlimited) | `--project PATH`. Details: reference/scip-intelligence.md.

## REFERENCE DOCUMENTATION

- reference/semantic-search.md - semantic search flags and patterns
- reference/fts-search.md - full-text, regex and fuzzy search
- reference/temporal-search.md - git history search
- reference/scip-intelligence.md - SCIP call graph and dependency analysis
