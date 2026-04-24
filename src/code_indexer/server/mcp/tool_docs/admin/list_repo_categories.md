---
name: list_repo_categories
category: admin
required_permission: query_repos
tl_dr: Lists all repository categories with their patterns and priorities.
---

Lists all repository categories with their patterns and priorities. Categories are used to organize repositories in the UI and filter repository listings. Each category has a regex pattern used for automatic assignment when repositories are registered.

Categories are returned in priority order (ascending), which determines their display order in the UI. Lower priority numbers appear first.

Pattern matching uses re.match() against the repository alias (anchored at start) and re.search() against the repository URL (matches anywhere in the URL). A repository is assigned to the first category whose pattern matches either the alias or the URL. This allows patterns like ".*github\\.com:backend-team/.*" to categorize repositories by their Git hosting organization, group, or team regardless of the short alias name.

USE CASES: Discover available categories for filtering repositories. Understand category patterns for troubleshooting auto-assignment. Check category priorities for display order. Create URL-based patterns to categorize repositories by Git org or team (e.g., ".*github\\.com:my-org/.*").
