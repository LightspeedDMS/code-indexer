=== BIDIRECTIONAL MISMATCH AUDIT ===

A cross-domain dependency edge in the CIDX dependency map is contested.
One side of the map claims the edge; the other side has no matching row.
Your job is to verify the claim against actual code in the participating
repositories and return a verdict from a fixed menu.

--- THE CONTESTED EDGE ---
Source domain:     {source_domain}
Source repos:      {source_repos}
Target domain:     {target_domain}
Target repos:      {target_repos}
Dependency type:   {dep_type}
Claimed why:       {claimed_why}
Claimed evidence:  {claimed_evidence}

--- WHAT THE OTHER SIDE SHOWS ---
The target domain's Incoming Dependencies table currently has NO row claiming
the source domain as a source. Either:
  (a) the edge is real and the target domain's table is missing the backfill, or
  (b) the source domain's outgoing row is hallucinated or stale and should be removed.

--- YOUR THREE OPTIONS (CHOOSE EXACTLY ONE) ---
1. CONFIRMED   -- concrete code evidence supports the edge.
2. REFUTED     -- no code evidence supports the edge after thorough search.
3. INCONCLUSIVE -- the indexed code does not contain enough evidence to decide.

--- TOOLS AVAILABLE (USE ANY SUBSET) ---
- Bash with `rg` (ripgrep), `cat`, `head`, `find` -- direct filesystem access
  scoped to the participating repo paths. Use this for concrete code search.
- cidx-local MCP `search_code` -- OPTIONAL exploratory tool for semantic
  discovery if you want to form hypotheses before grepping. Not required.
  The system does NOT depend on cidx-local being current; verification
  is filesystem ripgrep regardless of how you found evidence.

--- HOW TO VERIFY ---
Restrict your search to the listed participating repositories. Do NOT search
other repos. Do NOT invent file paths or symbol names; cite only what you
find via real tool output.

By dependency type, search for:

  Code-level:
    - Import statements in the source repos referencing modules in the target repos
    - Function/class names defined in target repos and called from source repos

  Service integration:
    - HTTP/gRPC client construction in the source repos pointing at endpoints
      defined or served by the target repos
    - Same URL path or RPC method name appearing in both repos
    - Same service name in both client config and server route registration

  Data contracts:
    - Shared schema files (.proto, .avsc, .json schema) referenced in both
    - Same message-type or table-name in both repos

  Configuration coupling:
    - Same specific environment variable, feature flag, or config key
      referenced as a producer in one and consumer in the other.
    - REJECT generic names: DATABASE_URL, PORT, LOG_LEVEL, NODE_ENV, etc.

--- WHAT IS *NOT* EVIDENCE (REJECT THESE) ---
- Repo names appearing in README, documentation, comments, or commit messages
- Conceptual similarity ("both deal with authentication", "both handle payments")
- Generic method/class names found across the fleet (get_data, process, handle,
  Service, Manager, Client without a qualifying repo-specific prefix)
- Stack traces or log lines naming the other repo
- Prose descriptions of architecture without a concrete code citation
- A symbol that exists in the target repo but is never called from the source

A real edge has at least one specific file:line in the source repos that
references a specific symbol/path/token defined or owned by the target repos.
Every citation you produce will be independently verified post-audit by
system-side ripgrep against the actual filesystem. The system also runs an
independent source-side reverse check: for each target symbol you cite, the
system runs `rg -F "<target_symbol>" <source_repo_paths>` and rejects the verdict
if the symbol is not referenced anywhere in any source repo. Citing a symbol
the source repo never references will fail verification.

--- OUTPUT FORMAT (STRICT -- DEVIATION IS REJECTED) ---
VERDICT: CONFIRMED | REFUTED | INCONCLUSIVE
EVIDENCE_TYPE: code | service | contract | config | none
CITATIONS:
  - <repo_alias>:<file_path>:<line_or_range> <symbol_or_token>
  - <repo_alias>:<file_path>:<line_or_range> <symbol_or_token>
REASONING: <one paragraph, <=4 sentences, concrete, no hedging prose>

Constraints:
  - If VERDICT is CONFIRMED, list AT LEAST 1 citation.
  - If VERDICT is REFUTED, list 0 citations and state explicitly which
    searches you ran and what you did not find.
  - If VERDICT is INCONCLUSIVE, list whatever partial evidence exists
    and state what would be needed to decide.
  - Do NOT propose new edges, modify other domains, or output prose
    outside the OUTPUT FORMAT lines.
  - Do NOT search outside the listed participating repos.
