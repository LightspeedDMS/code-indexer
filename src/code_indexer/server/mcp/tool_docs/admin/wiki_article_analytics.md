---
name: wiki_article_analytics
category: admin
required_permission: query_repos
tl_dr: 'Query wiki article view analytics for a wiki-enabled repository.


  Returns a list of articles sorted by view count (most_viewed by default), with optional
  search filtering via CIDX semantic or FTS indexes.


  USE CASES:

  - Identify the most popular wiki articles for a repository

  - Find least-visited articles that may need promotion or removal

  - Search for articles matching a topic and see which are most read


  PARAMETERS:

  - repo_alias: The repository alias (e.g., sf-kb-wiki-global).'
---

Query wiki article view analytics for a wiki-enabled repository.

Returns a list of articles sorted by view count (most_viewed by default), with optional search filtering via CIDX semantic or FTS indexes.

USE CASES:
- Identify the most popular wiki articles for a repository
- Find least-visited articles that may need promotion or removal
- Search for articles matching a topic and see which are most read

PARAMETERS:
- repo_alias: The repository alias (e.g., sf-kb-wiki-global). Must be a golden repo with wiki enabled.
- sort_by: most_viewed (default, descending by real_views) or least_viewed (ascending). Ties broken alphabetically by path.
- limit: Cap on results returned. Default 20, max 500.
- search_query: Optional CIDX query to filter articles. Results still sorted by view count, not relevance.
- search_mode: semantic (default) or fts when search_query is provided.

RESPONSE FIELDS per article:
- title: Humanized title derived from filename (e.g., "getting-started.md" -> "Getting Started")
- path: Relative file path within the repository (e.g., "Customer/getting-started.md")
- real_views: Integer view count
- first_viewed_at: ISO timestamp of first view
- last_viewed_at: ISO timestamp of most recent view
- wiki_url: URL to the wiki article (e.g., /wiki/sf-kb-wiki/Customer/getting-started)

ERROR CASES:
- "Wiki is not enabled for this repository" if repo_alias does not point to a wiki-enabled golden repo

EXAMPLE: wiki_article_analytics(repo_alias='sf-kb-wiki-global', sort_by='most_viewed', limit=10)
