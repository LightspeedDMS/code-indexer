---
name: Targeted scope discipline — never rewrite working UI
description: When user asks for a targeted improvement (pagination, one endpoint, one behavior), scope is strictly that. Do NOT rewrite surrounding UI, drop columns, remove buttons, change styling, or destroy working features. Preserve the existing template/layout exactly and minimally patch only the specific behavior requested.
type: feedback
originSessionId: 9e630745-c08e-4b95-9508-a414edde1350
---
When the user asks for a targeted improvement like "improve pagination," "fix the /enrich endpoint," or "change how search works," the scope is STRICTLY that feature. Do not:

- Rewrite the surrounding template from scratch
- Drop existing table columns (Description, Last Commit, Visibility, etc.)
- Remove existing action buttons (per-row Add, Hide/Unhide, batch-create modal)
- Change the color scheme or CSS classes
- Add new meaningless columns (like empty "Namespace")
- Replace htmx-driven row actions with a stripped-down vanilla JS table

**Why**: This happened on Story #754. User asked for client-side pagination (fetch-all + paginate in browser). Agents rewrote the entire `auto_discovery.html` template, destroying: Description column, Last Commit column (hash+author+date), Last Activity column, Visibility badge column, per-row Add button (form POST to /admin/golden-repos/add), per-row Hide/Unhide button (hx-post to /admin/api/discovery/hide), and the batch-create modal — all of which had nothing to do with pagination. User was furious and had to explicitly order a revert.

**How to apply**: When touching an existing working UI to change one behavior:

1. Read the existing template/page first and understand every column, button, and interaction.
2. Identify the MINIMAL change surface needed for the requested behavior.
3. Preserve HTML structure, CSS classes, and all existing elements that are unrelated to the change.
4. If pagination is the ask: swap the data source (one `/all` fetch instead of per-page) and slice client-side, but keep the exact same `<thead>` columns, `<tbody>` row template, action buttons, modals, and styles.
5. If unsure whether something is in scope, ASK before removing/rewriting it.

**Rule of thumb**: If the user's request mentions one concept (pagination), your diff should touch code related to that concept only. If the diff removes a column or button, that's a signal you've overreached.
