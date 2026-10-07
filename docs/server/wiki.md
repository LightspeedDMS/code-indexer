# Wiki

The CIDX Server can render the Markdown files of a repository as a browsable wiki. This page is for administrators:
what the wiki serves, how to turn it on for a repository, how pages are rendered and cached, who can read them, and
the view analytics.

Code: `src/code_indexer/server/wiki/` (`routes.py`, `wiki_service.py`, `wiki_cache.py`,
`wiki_cache_invalidator.py`); the router is mounted at `/wiki` (`routers/inline_routes.py`).

## What it serves

| URL | Content |
|-----|---------|
| `/wiki/<alias>/` | Wiki of the golden repository `<alias>`: its published version, the directory the `<alias>-global` alias points to |
| `/wiki/<alias>/<path>` | One article of that wiki |
| `/wiki/u/<username>/<alias>/` | Wiki of the activated repository `<alias>` of user `<username>` (a user wiki) |
| `/wiki/u/<username>/<alias>/<path>` | One article of a user wiki |
| `.../_assets/<path>` | Images and other assets of the repository |
| `.../_search?q=<text>&mode=semantic\|fts` | Search, returned as JSON |

- **Articles** are files ending in `.md`, `.markdown` or `.txt`. An article path without an extension is served
  from the matching `.md` file. Any other extension, a missing file, or a path that resolves outside the repository
  directory returns 404.
- **Assets** are served only for `.png`, `.jpg`, `.jpeg`, `.gif`, `.svg`, `.webp`, `.ico`, `.css`, `.js`, `.woff`,
  `.woff2`, `.ttf`, `.eot` and `.pdf`, under the same containment rule.
- **Root page.** `home.md` in the repository root when it exists; otherwise an index of every `.md` file.
- **Sidebar.** Every `.md` file outside directories whose name starts with `.`, grouped by top-level directory and
  then by the front-matter `category` field (articles without one go under `Uncategorized`), sorted by title.
- **Titles** come from the front-matter `title` field, or from the file name with `-` and `_` turned into spaces.
- **Metadata panel.** The article's YAML front matter, with labels for well-known keys (`created`, `modified`,
  `category`, `author`, `tags` and others), dates shown as for example "March 15, 2024", `draft: true` shown as
  visibility `draft`, and the article's view count. Which knowledge-base fields appear, and in which order, is
  configurable (see [Configuration](#configuration)).
- **Links.** Relative links between articles are rewritten to `/wiki/...` URLs, links to `http(s)` sites open in a
  new tab, and relative image sources are rewritten to the `_assets` route.
- **Search.** The search box queries the repository's own index (for a golden repository, `<alias>-global`),
  restricted to `.md` files, at most 50 results; queries shorter than 2 characters return an empty list. `mode` is
  `semantic` (default) or `fts`.

MCP search results also point into the wiki: for a `.md` hit in a wiki-enabled golden repository, the result carries
a `wiki_url` field (`/wiki/<alias>/<path without .md>`).

## Turning the wiki on

The wiki is off for every repository until an administrator enables it. Both switches are in the administration Web
UI and require an admin session (and an open elevation window when elevation enforcement is on).

| Repository | Where | Endpoint |
|------------|-------|----------|
| Golden repository | Golden Repos, repository details, Wiki "Enabled" | `POST /admin/golden-repos/{alias}/wiki-toggle` |
| Activated repository (user wiki) | Repositories page, wiki switch of the activation | `POST /admin/activated-repos/{username}/{alias}/wiki-toggle` |

Enabling a golden repository's wiki also seeds view counts from front matter (see [Analytics](#analytics)). Disabling
a user wiki clears its render cache.

## Rendering and caching

Pages are rendered when requested, not by a background job:

1. The YAML front matter is parsed.
2. When `enable_header_block_parsing` is on, leading "Article Number:", "Title:" and "Publication Status:" lines are
   removed from the body (their values go to the metadata panel).
3. The body is rendered with markdown-it-py (CommonMark, plus tables and strikethrough), and anchor ids are added to
   headings.

Rendered output is cached in two tables, keyed by the repository (golden: `<alias>`; user wiki:
`u:<username>:<alias>`):

| Table | Content | Staleness check |
|-------|---------|-----------------|
| `wiki_cache` | One rendered article | Reused only while the file's modification time and size equal the stored ones; checked on every request, so a changed file is re-rendered |
| `wiki_sidebar_cache` | The sidebar of a repository | None on read; reused until invalidated |
| `wiki_article_views` | View counts | Not a cache (see [Analytics](#analytics)) |

The tables are in the server database (`cidx_server.db`) on a standalone server and in PostgreSQL in a cluster, so
every node sees the same cache.

The sidebar cache of a repository is cleared by:

- the MCP tools `create_file`, `edit_file` and `delete_file` when the file ends in `.md`, `.markdown` or `.txt`;
- the git write tools that change the working tree (`git_pull`, `git_merge`, `git_merge_abort`, `git_reset`,
  `git_clean`, `git_checkout_file`, `git_branch_switch`, `git_mark_resolved`) and `exit_write_mode`;
- the **Clear Cache** button next to a golden repository's wiki switch
  (`POST /admin/golden-repos/{alias}/wiki-refresh`);
- disabling a user wiki.

These events clear the cache entries keyed by the repository alias the tool or button acted on. A golden-repository
refresh does not clear the sidebar cache: after a refresh that adds, removes or renames articles, use Clear Cache.

## Access rules

Contract enforced by `wiki/routes.py` (`_check_wiki_access`, `_check_user_wiki_access`):

- Every wiki URL requires a signed-in user (Web UI session or bearer token). A page or asset request without one is
  redirected to `/login?redirect_to=<the requested URL>`; a `_search` request gets 401.
- **Golden repository wiki.** Readable when the repository's wiki is enabled and the user either belongs to the
  `admins` group, holds the `manage_users` permission (the `admin` role), or has the repository among the
  repositories their group grants ([Accounts and Access](auth/accounts-and-access.md#groups-and-repository-access)).
- **User wiki.** Readable when the activation's wiki is enabled, its directory exists, and the reader is either the
  owner, who must still hold grants on the golden repositories the activation was made from, or a member of the
  `admins` group. The `admin` role without `admins` membership does not open another user's wiki.
- Every refusal, including a repository that does not exist, is answered with 404, so an unauthorized user cannot
  tell which repositories exist.

## Analytics

Every view of an article, or of a root page served from `home.md`, adds one to `real_views` for that repository and
article path in `wiki_article_views`, which also keeps the first and last view time. The generated index page is not
counted. The count is shown as "Views" in the metadata
panel once it is above zero.

- **Seeding.** When a golden repository's wiki is enabled and the repository has no view records yet, a background
  thread reads each article's front-matter `views` field (a non-negative integer) and stores it as the starting
  count. It does nothing when `enable_views_seeding` is off.
- **Removal.** Removing a golden repository deletes its view records.

The MCP tool `wiki_article_analytics` returns the view statistics of a wiki-enabled golden repository
([MCP tool reference](../reference/mcp-tools/search.md#wiki_article_analytics)):

| Parameter | Default | Meaning |
|-----------|---------|---------|
| `repo_alias` | required | Golden repository alias, with or without `-global` |
| `sort_by` | `most_viewed` | `most_viewed` or `least_viewed`; ties sort by article path |
| `limit` | `20` | 1 to 500 |
| `search_query` | none | Restricts the result to articles that match this search (2 characters or more); results stay sorted by views |
| `search_mode` | `semantic` | `semantic` or `fts` |

Each article in the result has `title`, `path`, `real_views`, `first_viewed_at`, `last_viewed_at` and `wiki_url`. A
repository whose wiki is not enabled returns `success: false` with the error "Wiki is not enabled for this
repository". The tool declares the `query_repos` permission.

## Configuration

Runtime settings, Web UI Configuration, Wiki section (object `wiki_config`), read whenever a page is rendered. Full
table: [Server Settings Reference: Wiki](../reference/server-settings.md#wiki).

| Setting | Default | Effect |
|---------|---------|--------|
| `enable_header_block_parsing` | `true` | Removes the leading "Article Number", "Title" and "Publication Status" lines from the body |
| `enable_article_number` | `true` | Shows `article_number` (or `original_article`) in the metadata panel |
| `enable_publication_status` | `true` | Shows `publication_status` in the metadata panel |
| `enable_views_seeding` | `true` | Seeds view counts from front matter when the wiki is enabled, and shows the front-matter `views` field |
| `metadata_display_order` | empty | Comma-separated metadata keys shown first, in this order; the others follow alphabetically |

The defaults suit a knowledge base exported with these fields; for a plain documentation repository, turning off the
first four removes the knowledge-base specific fields.
