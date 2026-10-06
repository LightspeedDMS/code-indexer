# Code Indexer (`cidx`)

Semantic code search for your repositories: find code by what it does, not only by the words it contains.

[![CI/CD](https://img.shields.io/github/actions/workflow/status/LightspeedDMS/code-indexer/main.yml?branch=master&label=CI%2FCD)](https://github.com/LightspeedDMS/code-indexer/actions/workflows/main.yml) [![Release](https://img.shields.io/github/v/release/LightspeedDMS/code-indexer?sort=semver&color=blue)](https://github.com/LightspeedDMS/code-indexer/releases) [![Python](https://img.shields.io/badge/python-3.9%20%7C%203.10%20%7C%203.11%20%7C%203.12-blue)](https://www.python.org/) [![License: MIT](https://img.shields.io/github/license/LightspeedDMS/code-indexer?color=green)](LICENSE)

## What it is

CIDX indexes a git repository into a local vector index (VoyageAI or Cohere embeddings, HNSW) and a full-text
index (Tantivy), and answers natural-language, keyword and regex queries over it. It can also index git history,
build SCIP code-intelligence indexes, and run structural AST searches. Everything lives in the project's
`.code-indexer/` directory; nothing runs in a container.

It is for:

- developers who want to search their own code by meaning from the command line;
- AI coding assistants, through instructions installed by `cidx teach-ai` or through the server's MCP endpoint;
- teams, through the CIDX server: shared, centrally indexed repositories with REST, MCP and a Web UI.

## Quickstart

Requires Python 3.9-3.12, a C/C++ compiler (for the hnswlib build) and a VoyageAI API key. Details:
[Installation](docs/getting-started/installation.md).

```bash
pipx install git+https://github.com/LightspeedDMS/code-indexer.git@master
export VOYAGE_API_KEY="your-voyage-key"

cd /path/to/your/git/repository
cidx init
cidx index --fts
cidx query "where are requests rejected when the limit is exceeded" --limit 5
cidx query "authenticate_user" --fts            # exact text
cidx query "token bucket" --fts --semantic      # hybrid: keyword and meaning
```

Each semantic result shows the score, file and line range, then the matching code; add `--quiet` for a compact
listing.

## Capabilities

- Semantic, full-text, regex and hybrid search with language, path and score filters: [Query guide](docs/guides/query.md)
- Git history search with time ranges: [Temporal search](docs/guides/temporal-search.md)
- Definitions, references, call chains and impact analysis (SCIP): [SCIP guide](docs/guides/scip.md)
- Structural AST search with user-written evaluators (X-Ray): [X-Ray cookbook](docs/guides/xray-cookbook.md)
- A background daemon with in-memory indexes and watch mode: [Operating modes](docs/getting-started/operating-modes.md#daemon-mode)
- Instructions and skills for local AI assistants: [teach-ai](docs/getting-started/teach-ai.md)
- Embedding providers (VoyageAI, Cohere) and project settings: [Configuration](docs/getting-started/configuration.md)
- Multi-user server with golden repositories, REST API, Web UI, SSO and MFA: [Server deployment](docs/server/deployment.md)
- MCP endpoint for AI assistants: [MCP registration](docs/getting-started/mcp-registration.md)
- Cross-repository dependency map: [Meta-repo discovery](docs/guides/meta-repo-discovery.md)
- Searchable Langfuse traces: [Langfuse trace sync](docs/server/langfuse-trace-sync.md)
- Multi-node cluster on PostgreSQL: [Cluster setup](docs/server/cluster-setup.md)

All documentation, by audience: [docs/README.md](docs/README.md). Design: [Architecture overview](docs/architecture/overview.md).

## Project

- Contributing: [CONTRIBUTING.md](CONTRIBUTING.md) and the [Code of Conduct](CODE_OF_CONDUCT.md)
- Security reports: privately, as described in [SECURITY.md](SECURITY.md); not in public issues
- Bugs and feature requests: [GitHub Issues](https://github.com/LightspeedDMS/code-indexer/issues)
- Questions: [GitHub Discussions](https://github.com/LightspeedDMS/code-indexer/discussions)
- Release history: [CHANGELOG.md](CHANGELOG.md)
- License: [MIT](LICENSE)
