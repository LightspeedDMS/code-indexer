# Configuration (CLI)

What a CLI user configures: the embedding provider key, the per-project `.code-indexer/config.json`, and the
per-project override file.

Server settings are not configured here. A CIDX server keeps only its bootstrap keys in
`~/.cidx-server/config.json`; every other server setting is a runtime setting stored in the server database and
changed in the Web UI configuration screen. See [Server deployment](../server/deployment.md).

## Embedding provider keys

Semantic indexing and semantic queries call an embedding provider. CIDX supports VoyageAI (default) and Cohere.

| Provider | Environment variable | Where the CLI reads it |
|----------|---------------------|------------------------|
| VoyageAI | `VOYAGE_API_KEY` | Environment only (`src/code_indexer/services/voyage_ai.py`). |
| Cohere | `CO_API_KEY` | `cohere.api_key` in `config.json` if set, otherwise the environment (`src/code_indexer/services/cohere_embedding.py`). A non-empty `cohere.api_key` takes precedence over `CO_API_KEY`. |

Export the key in the shell that runs `cidx`, and persist it in your shell profile:

```bash
export VOYAGE_API_KEY="your-voyage-key"
echo 'export VOYAGE_API_KEY="your-voyage-key"' >> ~/.bashrc
```

Windows PowerShell, current session: `$env:VOYAGE_API_KEY = "your-voyage-key"`.

The `cidx` CLI does not load `.env` or `.env.local` files (there is no dotenv loading in `src/`). If you keep a key in
such a file, load it into the shell yourself, for example `set -a; source .env.local; set +a`.

Only the repository's own test tooling reads these files, for contributors:

- pytest: `tests/conftest.py` imports `tests/load_env.py`, which reads `<repo-root>/.env.local` and then
  `<repo-root>/.env` (`KEY=value` or `export KEY=value` lines). A variable already set in the environment is never
  overridden, so the shell wins over `.env.local`, and `.env.local` wins over `.env`.
- `fast-automation.sh`, `server-fast-automation.sh` and `slow-automation.sh` also `source` `.env.local` and then
  `.env` from the current directory.

Never commit either file.

Missing keys fail loudly at the first embedding call:

```
VOYAGE_API_KEY environment variable is required for VoyageAI.
Cohere API key required. Set via config or CO_API_KEY env var.
```

## The project configuration file

`cidx init` creates `.code-indexer/config.json` in the project. Run it before the first `cidx index`: in a
directory without a configuration, `cidx index` stops with `project needs initialization`. Options worth knowing:

```bash
cidx init                                  # defaults: VoyageAI, voyage-code-3
cidx init --voyage-model voyage-large-2    # choose the VoyageAI model
cidx init --max-file-size 2000000          # bytes; default 1048576
cidx init --force                          # overwrite an existing config.json
cidx config --show                         # print the daemon and temporal settings
```

The file holds many keys with defaults. These are the ones users normally change:

| Key | Default | Meaning |
|-----|---------|---------|
| `embedding_provider` | `"voyage-ai"` | `"voyage-ai"` or `"cohere"`. `cidx init --embedding-provider` only offers `voyage-ai`; to use Cohere, edit this key. |
| `embedding_providers` | `null` | Optional list of providers. When `null`, the CLI uses `[embedding_provider]`. |
| `voyage_ai.model` | `"voyage-code-3"` | VoyageAI model. Known models and dimensions: `src/code_indexer/data/voyage_models.yaml`. |
| `cohere.model` | `"embed-v4.0"` | Cohere model. |
| `cohere.api_key` | `""` | Cohere key; overrides `CO_API_KEY` when non-empty. Keep keys out of version control. |
| `file_extensions` | 61 extensions (`py`, `js`, `ts`, `java`, `go`, `rs`, `md`, ...) | Extensions to index, written without the dot. |
| `exclude_dirs` | `node_modules`, `venv`, `__pycache__`, `.git`, `dist`, `build`, `target`, `.idea`, `.vscode`, `.gradle`, `bin`, `obj`, `coverage`, `.next`, `.nuxt`, `dist-*`, `.code-indexer` | Directories never indexed. |
| `indexing.max_file_size` | `1048576` | Largest file indexed, in bytes. Nested under `indexing`. |
| `voyage_ai.parallel_requests`, `cohere.parallel_requests` | `8` | Concurrent embedding requests during indexing. |

Example, switching a project to Cohere and indexing only Python and TypeScript:

```json
{
  "embedding_provider": "cohere",
  "file_extensions": ["py", "ts", "tsx"],
  "indexing": { "max_file_size": 2097152 }
}
```

Only the keys you change need editing; keep the rest of the generated file. After changing which files are
indexed or switching provider, rebuild the index:

```bash
cidx index --clear
```

Each provider and model gets its own collection under `.code-indexer/index/` (for example `voyage-code-3` or
`embed-v4.0`).

## The override file

`cidx init` also writes `.code-indexer-override.yaml` in the project root. It adjusts file selection per project
and takes precedence over `config.json` and `.gitignore`, in this order:

1. `force_exclude_patterns` (gitignore syntax, overrides everything)
2. `force_include_patterns`
3. `add_extensions`, `remove_extensions`
4. `add_exclude_dirs`, `add_include_dirs`
5. `config.json` and `.gitignore` rules

Pass `--no-override-file` to `cidx init` to skip creating it.

## Index types

| Command | Builds |
|---------|--------|
| `cidx index` | Semantic (vector) index. |
| `cidx index --fts` | Semantic index plus the full-text (Tantivy) index. |
| `cidx index --rebuild-fts-index` | Only the full-text index, from files already indexed. Use it to add full-text search to an existing index. |
| `cidx index --index-commits` | Git history (temporal) index. See [Temporal search](../guides/temporal-search.md). |
| `cidx scip generate` | SCIP code-intelligence index. See [SCIP](../guides/scip.md). |

Running `cidx index --fts` on a project whose semantic index is already current does not populate the full-text
index; use `cidx index --rebuild-fts-index` in that case.

## Daemon settings

```bash
cidx config --daemon            # enable daemon mode for this project
cidx config --no-daemon         # disable it
cidx config --daemon-ttl 30     # minutes an idle cached index stays in memory (default 10)
cidx watch --debounce 3.0       # seconds to wait before processing changes (default 2.0)
```

See [Operating modes](operating-modes.md#daemon-mode).

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `VOYAGE_API_KEY environment variable is required for VoyageAI.` | Export the key in the shell running `cidx` (see above). |
| `Cohere API key required. Set via config or CO_API_KEY env var.` | Export `CO_API_KEY` or set `cohere.api_key`. |
| `config.json` does not parse | Validate with `python3 -m json.tool .code-indexer/config.json`, or regenerate it with `cidx init --force`, or run `cidx fix-config`. |
| Files you expect are missing from results | Check `file_extensions`, `exclude_dirs`, `indexing.max_file_size` and `.code-indexer-override.yaml`, then `cidx index --clear`. |
| `cidx query --fts` returns `No matches found` on a project indexed before you added `--fts` | `cidx index --rebuild-fts-index`. |
