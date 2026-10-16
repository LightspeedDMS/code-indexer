# SCIP Code Intelligence

SCIP (Source Code Intelligence Protocol) indexes give CIDX compiler-accurate symbol data: where a symbol is defined,
where it is referenced, what it depends on, what depends on it, and the call paths between two symbols. Semantic
search finds code by meaning; SCIP answers exact questions about named symbols.

Audience: CLI users and AI-agent integrators. This guide covers generating the indexes, every `cidx scip`
subcommand with its current flags and limits, and the equivalent server interfaces.

## Contents

- [Supported Languages](#supported-languages)
- [Generate and Maintain Indexes](#generate-and-maintain-indexes)
- [Query Commands](#query-commands)
- [Symbol Matching and Output](#symbol-matching-and-output)
- [Depth Limits](#depth-limits)
- [Server Interfaces](#server-interfaces)
- [SCIP or Semantic Search](#scip-or-semantic-search)
- [Troubleshooting](#troubleshooting)

## Supported Languages

`cidx scip generate` discovers projects by their build files and runs the matching indexer, which must be installed
and on `PATH` (`src/code_indexer/scip/discovery.py`, `src/code_indexer/scip/indexers/`):

| Language | Build file | Indexer |
|----------|-----------|---------|
| Java | `pom.xml`, `build.gradle` | scip-java, launched through Coursier (`cs launch com.sourcegraph:scip-java_2.13:...`) |
| Kotlin | `build.gradle.kts` | scip-java (Gradle) |
| TypeScript, JavaScript | `package.json` | `scip-typescript` |
| Python | `pyproject.toml`, `setup.py`, `requirements.txt` | `scip-python` |
| C# | `*.sln`, `*.csproj` | `scip-dotnet` |
| Go | `go.mod` | `scip-go` |

When one directory has several build files, one is chosen by priority (for Python: `pyproject.toml`, then
`setup.py`, then `requirements.txt`).

## Generate and Maintain Indexes

```bash
cidx scip generate                     # discover every project and index it
cidx scip status                       # overall and per-project status
cidx scip status -v                    # include per-project errors
cidx scip rebuild backend              # regenerate named projects
cidx scip rebuild --failed             # regenerate every failed project
cidx scip rebuild --force backend      # regenerate a project that already succeeded
```

- Each project's index is a SQLite database at `.code-indexer/scip/<project path>/index.scip.db`; a project at the
  repository root writes `.code-indexer/scip/index.scip.db`. Status is kept in `.code-indexer/scip/status.json`.
- The indexer first writes a `.scip` protobuf file. `generate` converts it to `index.scip.db`, verifies the
  database against it, and deletes the `.scip` file when verification passes. Only `index.scip.db` is used by
  queries.
- `cidx scip generate --skip-verify` skips that verification and keeps the `.scip` file. While a `.scip` file is
  present next to `index.scip.db`, queries returned different results in testing (14 dependents instead of 4 for
  the same symbol); run `cidx scip generate` again after verifying so the `.scip` file is removed.
- `cidx scip generate --project PATH` is accepted but currently ignored: every discovered project is indexed. To
  regenerate one project, use `cidx scip rebuild PATH`.
- `rebuild` needs project paths or `--failed`; with neither it exits with
  `Must specify project paths or use --failed flag`. Projects must have been discovered by an earlier `generate`.
- Overall status values: `pending`, `running`, `success`, `failed`, and `limbo` (some projects succeeded, some
  failed). When a query returns nothing and the index is in `limbo`, the CLI prints a warning that results may be
  missing.

`cidx scip verify DATABASE_PATH` checks a database against its `.scip` source and exits 0 on success, 1 on failure.
Because `generate` deletes the source after verifying, `verify` only works on a database produced with
`--skip-verify`:

```bash
cidx scip generate --skip-verify
cidx scip verify .code-indexer/scip/index.scip.db
```

## Query Commands

All query commands accept `--limit N` (default 0, unlimited), `--project PATH` (only that project's index) and
`-r, --repository ALIAS` (query a repository on the server in remote mode).

| Command | Purpose | Extra flags |
|---------|---------|-------------|
| `definition SYMBOL` | where the symbol is defined | `--exact` |
| `references SYMBOL` | every place the symbol is used | `--exact` |
| `dependencies SYMBOL` | symbols this symbol uses | `--depth N` (default 1), `--exact` |
| `dependents SYMBOL` | symbols that use this symbol | `--depth N` (default 1), `--exact` |
| `impact SYMBOL` | symbols and files affected by changing it, by depth | `--depth N` (default 3), `--exclude GLOB`, `--include GLOB`, `--kind class\|function\|variable` |
| `callchain FROM TO` | call paths from one symbol to another | `--max-depth N` (default 3) |
| `context SYMBOL` | curated list of the definition and related references, scored; `--limit` caps the number of files, not symbols | `--min-score F` (default 0.0) |

Examples, run against a three-file Python project (`app/store.py` defines `UserStore`, `app/service.py` uses it,
`app/api.py` calls the service):

```bash
cidx scip definition UserStore
cidx scip definition "UserStore#find_user" --exact
cidx scip references UserStore --limit 5
cidx scip dependents UserStore --depth 2
cidx scip impact UserStore
cidx scip callchain login_handler find_user
cidx scip context AuthService --limit 5
```

```
$ cidx scip dependents UserStore
Found 2 dependent(s) for 'UserStore':

  app.service/AuthService#store. (app/service.py:5) [calls]
  app.service/__init__: (app/service.py:0) [calls]
```

## Symbol Matching and Output

- Without `--exact`, `SYMBOL` matches as a substring: `find_user` finds `UserStore#find_user()`.
- With `--exact`, `SYMBOL` must equal the symbol's name path with its SCIP suffix removed: `UserStore` or
  `UserStore#find_user`. A bare method name such as `find_user` does not match exactly.
- Each result prints the SCIP symbol (`<module>/<Class>#<member>().`) and its location as `(<path>:<line>)` or
  `(<path>:<line>:<column>)`. Line numbers are zero-based: a class on the first line of `app/store.py` prints as
  `app/store.py:0:6`.
- `dependents`/`dependencies` add the relationship (`[calls]`), `impact` adds `[depth N]`, and `context` adds
  `[def]`/`[ref]` and a relevance score.

## Depth Limits

| Command | Flag | Default | Allowed |
|---------|------|---------|---------|
| `callchain` | `--max-depth` | 3 | 1-3 |
| `impact` | `--depth` | 3 | 1-10 |
| `dependencies`, `dependents` | `--depth` | 1 | 1-10 |

Values outside the range are rejected, for example `Error: --max-depth must be between 1 and 3, got 5`. The
call-chain cap is `MAX_DEPTH_CAP = 3` (`src/code_indexer/scip/database/queries.py`) and applies to the CLI, REST and
MCP alike; the impact cap is `MAX_TRAVERSAL_DEPTH = 10` (`src/code_indexer/scip/query/composites.py`). When a chain
may be longer than 3 steps, trace it in segments: run `callchain` to an intermediate symbol, then from it.

## Server Interfaces

| Interface | Commands |
|-----------|----------|
| CLI | `generate`, `status`, `rebuild`, `verify`, and the seven query commands |
| REST | `GET /scip/{definition,references,dependencies,dependents,impact,callchain,context}`; multi-repository `POST /api/scip/multi/{definition,references,dependencies,dependents,callchain}` |
| MCP | `scip_definition`, `scip_references`, `scip_dependencies`, `scip_dependents`, `scip_impact`, `scip_callchain`, `scip_context` |
| Web UI | query page, SCIP query type: all seven query commands |

Golden repositories get SCIP indexes when the server indexes them with SCIP enabled; the CLI `generate`/`rebuild`
commands work on a local checkout.

## SCIP or Semantic Search

| Question | Use |
|----------|-----|
| "Where is `UserStore` defined, and who calls it?" | `cidx scip definition`, `cidx scip references` |
| "What breaks if I change this class?" | `cidx scip impact`, `cidx scip dependents` |
| "How does the request reach this function?" | `cidx scip callchain` |
| "Where is authentication handled?" (no symbol name) | `cidx query "user authentication"` |
| a language with no SCIP indexer | semantic or FTS search |

A common sequence: find the area with a semantic query, then navigate the symbols it returns with SCIP.

```bash
cidx query "user authentication" --limit 5 --quiet
cidx scip definition AuthService
cidx scip references AuthService
```

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `SCIP Index: Not created` in `cidx status` | run `cidx scip generate` |
| a project shows `failed` | `cidx scip status -v` for the error, install or fix the indexer, then `cidx scip rebuild --failed` |
| no results for a symbol you know exists | drop `--exact`, or pass the name path (`Class#method`) with `--exact`; check `status` for `limbo` |
| `No dependencies found ... may be a leaf node` | the symbol calls nothing indexed; try `dependents` instead |
| `--max-depth must be between 1 and 3` | trace long chains in segments |
| `Corresponding SCIP file not found` from `verify` | the `.scip` source was deleted after generation; regenerate with `--skip-verify` |
| no `.scip` files under `.code-indexer/scip/` | expected; only `index.scip.db` is kept |

## Related

- [Query Guide](query.md)
- [CLI Reference](../reference/cli/README.md)
- [Architecture Overview](../architecture/overview.md)
