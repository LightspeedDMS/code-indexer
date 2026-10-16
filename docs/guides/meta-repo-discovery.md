# Meta-Repo Discovery

A CIDX server keeps a catalog repository, `cidx-meta`, that holds one written description per golden repository.
Searching that catalog first tells you, or an AI agent, which repositories are worth searching; a second query then
searches only those. This guide explains how the catalog is built and kept current, and how to query it.

Audience: users and AI agents working against a CIDX server, and the operators who run it. Discovery is a server
feature: the catalog is created and maintained by the server, and no CLI command builds it.

## Contents

- [How the Catalog Is Built](#how-the-catalog-is-built)
- [Discovery Workflow](#discovery-workflow)
- [Reading Discovery Results](#reading-discovery-results)
- [Dependency Map](#dependency-map)
- [Keeping the Catalog Current](#keeping-the-catalog-current)
- [Querying the Catalog from the CLI](#querying-the-catalog-from-the-cli)
- [Troubleshooting](#troubleshooting)

## How the Catalog Is Built

1. **Bootstrap.** At startup the server creates `{golden_repos_dir}/cidx-meta`, initializes an index in it and
   registers it as a golden repository with the URL `local://cidx-meta`, then activates it globally as
   `cidx-meta-global` (`bootstrap_cidx_meta`, `src/code_indexer/server/startup/bootstrap.py`). The golden-repos
   directory is `golden-repos` under the server's data directory.
2. **One description per repository.** When a golden repository is added, the server generates a Markdown
   description of it with the Claude CLI and writes it as `cidx-meta/<alias>.md`, using the short alias (`example-repo.md`,
   not `example-repo-global.md`); the catalog is then re-indexed (`on_repo_added`,
   `src/code_indexer/global_repos/meta_description_hook.py`). The description summarizes the repository's purpose,
   languages and frameworks.
3. **No fallback text.** If the Claude CLI is not available on the server, no description is written for that
   repository; the server does not substitute a README copy or a stub.
4. **Removal.** Removing a golden repository deletes its `<alias>.md` file and re-indexes the catalog
   (`on_repo_removed`).

Layout:

```
{golden_repos_dir}/cidx-meta/
  example-repo.md            description of example-repo
  another-repo.md            description of another-repo
  dependency-map/            optional, see Dependency Map
    _index.md
    <domain>.md
  .code-indexer/             the catalog's own index
```

`cidx-meta` is refreshed like every golden repository, with `cidx index --fts` (`_index_source`,
`src/code_indexer/global_repos/refresh_scheduler.py`), so it has both a semantic and an FTS index. Discovery
questions are usually phrased as concepts, so the default `semantic` mode fits best; `fts` works for an exact word
in a description. A wildcard `repository_alias` (such as `*-global`) in `fts` or `hybrid` mode skips `cidx-meta`;
name `cidx-meta-global` explicitly to search it (`src/code_indexer/server/mcp/handlers/_utils.py`).

## Discovery Workflow

Step 1, ask the catalog which repositories match (MCP `search_code`):

```json
{"query_text": "authentication service with JWT tokens", "repository_alias": "cidx-meta-global", "limit": 5}
```

Step 2, search a repository the catalog returned. A result whose `file_path` is `example-repo.md` describes the
golden repository `example-repo`; query it by its global alias:

```json
{"query_text": "JWT token validation", "repository_alias": "example-repo-global", "limit": 10}
```

The same requests work over REST as `POST /api/query` with the same field names. To list every repository you can
query, use the MCP tool `list_global_repos`.

If the first repository does not answer the question, search the next one from step 1 rather than widening step 2
to every repository.

## Reading Discovery Results

A catalog search returns the normal `search_code` result shape. In each result:

| Field | Meaning |
|-------|---------|
| `file_path` | the description file, `<alias>.md`; append `-global` to the base name to query that repository |
| `code_snippet` | the matching part of the description |
| `similarity_score` | semantic similarity, 0.0-1.0 |
| `repository_alias`, `source_repo` | `cidx-meta-global` |

Results are filtered by access: a user only sees descriptions of repositories they are allowed to query
(`filter_cidx_meta_results`, applied in `src/code_indexer/server/mcp/handlers/search/_shared.py`).

## Dependency Map

When enabled, the server also writes a cross-repository dependency map into `cidx-meta/dependency-map/`:
`_index.md` (a domain catalog and a repository-to-domain matrix) and one `<domain>.md` file per discovered domain,
describing which repositories take part in the domain and how they depend on each other. These files are indexed
with the descriptions, so a discovery query can match them too.

MCP tools that read the map:

| Tool | Returns |
|------|---------|
| `depmap_get_repo_domains` | the domains a repository takes part in, and its role in each |
| `depmap_find_consumers` | the repositories that depend on a given repository |
| `depmap_get_domain_summary` | the summary of one domain |
| `depmap_get_cross_domain_graph` | domain-to-domain edges as JSON records |
| `depmap_get_hub_domains` | the most connected domains |
| `depmap_get_stale_domains` | domains older than a given number of days |

`trigger_dependency_analysis` (requires the `manage_golden_repos` permission) starts a full or delta analysis on
demand. How the map is computed is described in [Dependency Map Architecture](../architecture/dependency-map.md).

## Keeping the Catalog Current

Descriptions are written when a repository is added. Two runtime settings, changed in the Web UI configuration
screen, control later updates (`ClaudeIntegrationConfig`, `src/code_indexer/server/utils/config_manager.py`):

| Setting | Default | Effect |
|---------|---------|--------|
| `description_refresh_enabled` | false | periodically regenerate descriptions of repositories that changed |
| `description_refresh_interval_hours` | 24 | how often the description refresh runs |
| `dependency_map_enabled` | false | build and refresh the dependency map on a schedule |
| `dependency_map_interval_hours` | 168 | how often the dependency map refresh runs |

Both scheduled jobs run the Claude CLI on the server and consume its API usage.

## Querying the Catalog from the CLI

The CLI can query the catalog only on a machine that has the server's golden-repos directory, through the local
`--repo` option:

```bash
export CIDX_GOLDEN_REPOS_DIR=/path/to/server/data/golden-repos
cidx global list
cidx query "authentication service with JWT tokens" --repo cidx-meta-global --limit 5
cidx query "JWT token validation" --repo example-repo-global --limit 10
```

`--repo` reads aliases from `$CIDX_GOLDEN_REPOS_DIR/aliases` (default `~/.code-indexer/golden-repos`). The `cidx
global` group has `activate`, `list`, `status` and `regex-search`; none of them creates or refreshes the catalog.
From a workstation, use the MCP or REST requests above.

## Troubleshooting

| Symptom | Cause and fix |
|---------|---------------|
| a registered repository never appears in discovery results | its description was not generated: check that the Claude CLI is available on the server and look for the generation error in the server logs. With `description_refresh_enabled` on, the next refresh run generates it, because a repository with no successful run counts as changed |
| a repository appears for other users but not for you | access filtering: you lack permission to query that repository |
| an `fts` or `hybrid` search over `*-global` returns no catalog hits | wildcard expansion skips `cidx-meta` in those modes; pass `cidx-meta-global` explicitly |
| descriptions describe an old state of a repository | enable `description_refresh_enabled`; repositories whose commit changed since the last run are refreshed |
| `Repository 'cidx-meta-global' not found in global registry` from `cidx global status` | `CIDX_GOLDEN_REPOS_DIR` does not point at the server's golden-repos directory |

## Related

- [Query Guide](query.md)
- [Repository Lifecycle](../architecture/repository-lifecycle.md)
- [Dependency Map Architecture](../architecture/dependency-map.md)
- [MCP Registration](../getting-started/mcp-registration.md)
