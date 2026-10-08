# SCIP Intelligence - Call Graph and Dependency Analysis

Compiler-accurate symbol data: definitions, references, dependencies, dependents, impact and call chains. Use it
when you know (or can guess) a symbol name. For concept search use `cidx query`.

## Indexes

```bash
cidx scip status                 # success / failed / pending / limbo (some projects failed)
cidx scip status -v              # per-project errors
cidx scip generate               # discover projects by build file and index them (all of them)
cidx scip rebuild --failed       # retry failed projects
cidx scip rebuild --force backend   # regenerate one project (generate --project is ignored)
```

Indexes live at `.code-indexer/scip/<project>/index.scip.db` (`.code-indexer/scip/index.scip.db` for a root
project). The intermediate `.scip` file is deleted after `generate` verifies the database, so missing `.scip` files
are normal. `cidx scip verify DB` only works after `cidx scip generate --skip-verify`; re-run `cidx scip generate`
afterwards, because a leftover `.scip` file changes query results. `rebuild` needs project paths or `--failed`.

Languages and build files: Java (`pom.xml`, `build.gradle`), Kotlin (`build.gradle.kts`), TypeScript/JavaScript
(`package.json`), Python (`pyproject.toml`, `setup.py`, `requirements.txt`), C# (`*.sln`, `*.csproj`), Go
(`go.mod`). The language's SCIP indexer must be installed.

## Query Commands

| Command | Answers | Flags beyond `--limit`, `--project`, `-r/--repository` |
|---------|---------|-------------------------------------------------------|
| `definition SYMBOL` | where is it defined | `--exact` |
| `references SYMBOL` | where is it used | `--exact` |
| `dependencies SYMBOL` | what does it use | `--depth 1-10` (default 1), `--exact` |
| `dependents SYMBOL` | what uses it | `--depth 1-10` (default 1), `--exact` |
| `impact SYMBOL` | what does a change affect | `--depth 1-10` (default 3), `--include GLOB`, `--exclude GLOB`, `--kind class\|function\|variable` |
| `callchain FROM TO` | how does FROM reach TO | `--max-depth 1-3` (default 3) |
| `context SYMBOL` | definition and related references, scored; `--limit` caps FILES, not symbols | `--min-score F` |

`--limit` defaults to 0 (unlimited): pass `--limit 10`-`20` to save context. `-r ALIAS` queries a server repository
in remote mode.

```bash
cidx scip definition UserStore
cidx scip definition "UserStore#find_user" --exact
cidx scip references UserStore --limit 10
cidx scip dependents UserStore --depth 2
cidx scip impact UserStore
cidx scip callchain login_handler find_user
cidx scip context AuthService --limit 5
```

**Call-chain depth is capped at 3** (`--max-depth 5` fails with `--max-depth must be between 1 and 3`). For longer
paths, chain two queries through an intermediate symbol, or walk `dependents` upward from the target.

## Matching and Output

- Default matching is substring: `find_user` finds `UserStore#find_user()`.
- `--exact` matches the name path without the SCIP suffix: `UserStore`, `UserStore#find_user`. A bare method name
  does not match exactly.
- Output: `<module>/<Class>#<member>(). (<path>:<line>[:<column>])`. Line numbers are zero-based (add 1 before
  opening the file). `dependents`/`dependencies` append `[calls]`; `impact` prefixes `[depth N]`; `context` prefixes
  `[def]`/`[ref]` and appends a score.

## Workflows

```bash
# Refactor a class
cidx scip definition LegacyAuth
cidx scip references LegacyAuth --limit 20
cidx scip impact LegacyAuth --depth 3 --exclude '*/tests/*'

# Trace a request path
cidx scip callchain handle_request save_record
cidx scip dependents save_record --depth 2

# Find code by concept, then navigate it
cidx query "user authentication" --limit 5 --quiet
cidx scip references AuthService
```

## Troubleshooting

| Problem | Fix |
|---------|-----|
| no results for a known symbol | drop `--exact`, or use `Class#method` with `--exact`; check `cidx scip status` for `failed`/`limbo` |
| `No dependencies found ... leaf node` | nothing indexed is called from it; try `dependents` or `references` |
| `--depth must be between 1 and 10` / `--max-depth must be between 1 and 3` | stay within the limits above |
| `No .scip.db files found for project` | wrong `--project` path; see `cidx scip status` |
| `Corresponding SCIP file not found` from `verify` | regenerate with `--skip-verify` before verifying |
