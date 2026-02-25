---
name: wiki_article_analytics
category: search
required_permission: query_repos
tl_dr: Query wiki article view analytics with sort, filter, and search capabilities.
inputSchema:
  type: object
  properties:
    repo_alias:
      type: string
      description: Repository alias (e.g., sf-kb-wiki-global). Must be a wiki-enabled golden repo.
    sort_by:
      type: string
      enum: [most_viewed, least_viewed]
      default: most_viewed
      description: Sort order for results by view count. most_viewed=descending, least_viewed=ascending. Tie-breaking is alphabetical by article path.
    limit:
      type: integer
      default: 20
      minimum: 1
      maximum: 500
      description: Maximum number of articles to return.
    search_query:
      type: string
      description: Optional search query to filter articles before applying analytics sort. Uses CIDX semantic or FTS index. Only articles matching the search are included; results are still sorted by view count, not relevance.
    search_mode:
      type: string
      enum: [semantic, fts]
      default: semantic
      description: Search mode when search_query is provided. semantic=conceptual matching, fts=exact text matching.
  required: [repo_alias]
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: Whether the analytics query succeeded
    articles:
      type: array
      description: List of articles with view analytics (present when success=True)
      items:
        type: object
        properties:
          title:
            type: string
            description: Humanized article title derived from filename
          path:
            type: string
            description: Relative file path within the repository
          real_views:
            type: integer
            description: Total view count for the article
          first_viewed_at:
            type: string
            description: ISO timestamp of first recorded view
          last_viewed_at:
            type: string
            description: ISO timestamp of most recent view
          wiki_url:
            type: string
            description: URL path to the wiki article
    total_count:
      type: integer
      description: Number of articles returned
    repo_alias:
      type: string
      description: Repository alias that was queried
    sort_by:
      type: string
      description: Sort order used (most_viewed or least_viewed)
    wiki_enabled:
      type: boolean
      description: Whether wiki is enabled for the repository (always True on success)
    error:
      type: string
      description: Error message (present when success=False)
  required:
  - success
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
