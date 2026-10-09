## 1. SEMANTIC SEARCH - CIDX MANDATORY

**RULE**: For code exploration use CIDX, not grep, find, rg or the Grep tool. CIDX adds semantic matching, call graphs and git-history search that text search cannot.

**ONLY EXCEPTION**: CIDX cannot answer (not installed, or no index covers the files you need). Check with `cidx status` and `cidx scip status` first.

### Capabilities (read the skill for details)

| Capability | Use case | Command |
|------------|----------|---------|
| **Semantic** | "what does X do", concept search | `cidx query "description" --quiet` |
| **FTS** | exact identifiers and words | `cidx query "name" --fts --quiet` |
| **Regex** | grep-style substring patterns | `cidx query "pattern" --fts --regex --quiet` |
| **Temporal** | git history: when added, which commit | `cidx query "X" --time-range-all --quiet` |
| **SCIP Definition** | where a symbol is defined | `cidx scip definition SYMBOL` |
| **SCIP References** | where a symbol is used | `cidx scip references SYMBOL` |
| **SCIP Dependencies** | what a symbol uses | `cidx scip dependencies SYMBOL` |
| **SCIP Dependents** | what uses a symbol | `cidx scip dependents SYMBOL` |
| **SCIP Call Chain** | call paths A to B (max depth 3) | `cidx scip callchain FROM TO` |
| **SCIP Impact** | what a change affects | `cidx scip impact SYMBOL` |

### Decision Matrix

| Task | Instead of | Use |
|------|------------|-----|
| Find a definition | `grep -r "def func"` | `cidx scip definition func` |
| Find all usages | `grep -r "ClassName"` | `cidx scip references ClassName` |
| Search by concept | `grep -r "auth"` | `cidx query "authentication" --quiet` |
| Pattern match | `rg "def \w+_user"` | `cidx query 'def [a-z_]+_user' --fts --regex --quiet` |
| Exact identifier | `grep -rw "user_id"` | `cidx query "user_id" --fts --quiet` |

### Index Management (check first)

```bash
cidx status                  # semantic, FTS and temporal index status
cidx scip status             # SCIP status per project
```

If indexes are missing:
```bash
cidx init && cidx index --fts   # semantic + FTS (FTS and regex need --fts)
cidx index --index-commits      # git history (temporal search)
cidx scip generate              # SCIP call graphs
```

**SCIP languages**: Java, Kotlin, TypeScript/JavaScript, Python, C#, Go (each needs its indexer installed).

### Key Flags

`--limit N` (start with 5-10) | `--language X` | `--path-filter '*/pattern/*'` | `--exclude-path '*/tests/*'` | `--quiet` (always)

### Full Documentation

- `~/.claude/skills/cidx/SKILL.md` - complete reference
- `~/.claude/skills/cidx/reference/scip-intelligence.md` - call graph and dependency analysis
