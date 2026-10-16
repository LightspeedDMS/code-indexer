# Installation

How to install the `cidx` command-line tool on a workstation, verify it, upgrade it and remove it.
Installing the multi-user server is a separate procedure: see [Server deployment](../server/deployment.md).

## Requirements

| Requirement | Detail |
|-------------|--------|
| Python | 3.9, 3.10, 3.11 or 3.12 (`requires-python = ">=3.9,<3.13"` in `pyproject.toml`). 3.13 is not supported. |
| C/C++ compiler | Needed at install time: the `hnswlib` dependency is built from source from a fork (see below). Debian/Ubuntu: `sudo apt install build-essential python3-dev`. RHEL/Fedora: `sudo dnf install gcc gcc-c++ python3-devel`. macOS: `xcode-select --install`. |
| git | Needed to install from the GitHub repository and for git-aware indexing. |
| Embedding provider key | A VoyageAI key (`VOYAGE_API_KEY`) or a Cohere key (`CO_API_KEY`). Semantic indexing and semantic queries call the provider's HTTP API. See [Configuration](configuration.md#embedding-provider-keys). |

CIDX is not published on PyPI. Every install method below installs from the GitHub repository.

## Install with pipx (recommended)

pipx puts `cidx` in its own virtual environment and on your `PATH`.

```bash
pipx install git+https://github.com/LightspeedDMS/code-indexer.git@master
cidx --version
```

`@master` is the production branch. To pin a release, use `@vX.Y.Z`, where `vX.Y.Z` is a release tag from the
[releases page](https://github.com/LightspeedDMS/code-indexer/releases).

If `cidx` is not found afterwards, run `pipx ensurepath` and open a new shell.

## Install with pip in a virtual environment

```bash
python3 -m venv ~/.venvs/cidx
~/.venvs/cidx/bin/python -m pip install --upgrade pip
~/.venvs/cidx/bin/python -m pip install "git+https://github.com/LightspeedDMS/code-indexer.git@master"
~/.venvs/cidx/bin/cidx --version
```

Activate the environment (`source ~/.venvs/cidx/bin/activate`) or call `~/.venvs/cidx/bin/cidx` directly.

## Optional extras

Dependencies are declared in `pyproject.toml` (`[project.dependencies]` and `[project.optional-dependencies]`).
The VoyageAI and Cohere embedding clients are part of the base install: both call the providers over HTTP.

| Extra | Installs | Needed for |
|-------|----------|-----------|
| `cluster` | `psycopg[binary]`, `psycopg-pool` | Running the server in cluster mode (PostgreSQL). Not needed for the CLI. |
| `cohere` | the `cohere` SDK | Nothing in CIDX imports it; Cohere embeddings work without it. |
| `dev` | pytest, mypy, ruff, pre-commit and type stubs | Contributing. See [CONTRIBUTING.md](../../CONTRIBUTING.md). |

```bash
pipx install "code-indexer[cluster] @ git+https://github.com/LightspeedDMS/code-indexer.git@master"
```

## Notable dependencies

- `hnswlib` is installed from a fork pinned to a commit in `pyproject.toml`; the PyPI release lacks the integrity
  and orphan-repair methods CIDX uses. This is why a compiler is required. Background:
  [hnswlib custom build](../server/hnswlib-custom-build.md).
- `tree-sitter` and `tree-sitter-languages` are core dependencies (used by X-Ray AST search).

## Verify the installation

Run these in a small git repository, with an embedding provider key exported:

```bash
cidx --version          # prints: code-indexer, version X.Y.Z
cidx init               # creates .code-indexer/config.json
cidx index --fts        # semantic + full-text index
cidx query "where errors are handled" --limit 3 --quiet
cidx uninstall --confirm   # remove the test index and its configuration again
```

A successful `cidx index` ends with `Indexing complete!` and a `Chunks indexed:` count greater than 0.

## Upgrade

```bash
pipx upgrade code-indexer
# or, to move to a specific release:
pipx install --force git+https://github.com/LightspeedDMS/code-indexer.git@vX.Y.Z

# pip in a virtual environment:
~/.venvs/cidx/bin/python -m pip install --upgrade "git+https://github.com/LightspeedDMS/code-indexer.git@master"
```

Release notes, including any re-index requirement, are in [CHANGELOG.md](../../CHANGELOG.md).

## Uninstall

```bash
# In each indexed project, remove the index and its configuration first:
cidx uninstall          # removes the project's .code-indexer directory (asks first; --confirm skips the prompt)

pipx uninstall code-indexer
# or delete the virtual environment: rm -rf ~/.venvs/cidx
```

`cidx init` also writes `.code-indexer-override.yaml` in the project root; delete it if you no longer need it.

## Troubleshooting

| Symptom | Cause and fix |
|---------|---------------|
| `cidx: command not found` after a pipx install | pipx's bin directory is not on `PATH`. Run `pipx ensurepath` and open a new shell. |
| `Failed building wheel for hnswlib` | No C/C++ compiler or Python headers. Install the packages listed under Requirements and retry. |
| pip refuses the package with a `Requires-Python` error | The interpreter is outside 3.9-3.12. Install with a supported interpreter, for example `pipx install --python python3.12 ...`. |
| `VOYAGE_API_KEY environment variable is required for VoyageAI.` | The key is not exported in the shell that runs `cidx`. CIDX does not read `.env` files. See [Configuration](configuration.md#embedding-provider-keys). |
| `cidx teach-ai` fails with `Failed to load awareness template` | Known limitation: the templates under `prompts/ai_instructions/` are not included in an installed package, so `teach-ai` only works from a source checkout. See [teach-ai](teach-ai.md#known-limitation-source-checkout-required). |

## Next steps

- [Configuration](configuration.md): provider keys and `.code-indexer/config.json`.
- [Operating modes](operating-modes.md): CLI, daemon and server.
- [Query guide](../guides/query.md): search options.
