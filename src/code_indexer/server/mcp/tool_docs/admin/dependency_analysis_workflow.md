---
name: dependency_analysis_workflow
category: admin
required_permission: query_repos
tl_dr: 'Use this guide to decide when to reach for semantic `search_code` and when
  to switch

  to the structured `depmap_*` tools.'
---

Use this guide to decide when to reach for semantic `search_code` and when to switch
to the structured `depmap_*` tools. Both are valid; they answer different kinds of
questions. Mixing them up wastes tokens and yields incomplete results.

The CIDX dependency map (cidx-meta) is a curated set of domain markdown files plus
`_domains.json`. Each domain file lists participating repositories, their roles,
and cross-domain connections with explicit dependency types. The `depmap_*` tools
read this structured data directly. Semantic search reads the prose inside those
same files as a vector index. Neither is a substitute for the other.

## Phase 1 — Semantic search for discovery

Use `search_code` against the `cidx-meta` repository when the question is
conceptual or exploratory and you do not yet know the exact entity names.

Good phase-1 questions:

- "Where is authentication handled across our repos?"
- "Which domains deal with background job orchestration?"
- "What does the payment pipeline look like at a high level?"

Phase-1 strengths:

- Tolerant of fuzzy language, synonyms, and questions phrased as prose
- Surfaces prose context and design narrative, not just graph edges
- Best when you need a human-readable paragraph to orient yourself

Phase-1 limitations:

- Results are ranked by similarity, not exhaustive. A repository that genuinely
  depends on your target may not appear in the top N results.
- No guarantee of recall. You cannot answer "list ALL consumers" with semantic
  search because low-ranked matches are silently omitted.
- No structured fields. You get markdown snippets, not `{repo, domain, role}`
  records you can iterate over.

Rule of thumb: use semantic search to form a hypothesis. Use `depmap_*` to
verify it exhaustively.

## Phase 2 — depmap_* tools for exhaustive, deterministic analysis

Switch to `depmap_*` when you need every answer, not just the top matches, or
when you need structured fields to drive further tool calls.

The five tools and the questions they answer:

- `depmap_find_consumers(repo_name)` — Who depends on repo X?
  Example question: "I am about to change `auth-service`. Who will break?"
  Returns every `{domain, consuming_repo, dependency_type, evidence}` record
  where `consuming_repo` is the repository that depends on the target. One
  entry per matching Incoming-Dependencies row: rows with the same
  (domain, consuming_repo) but different evidence strings produce multiple
  entries, so callers counting unique consumers must deduplicate by
  (domain, consuming_repo).

- `depmap_get_repo_domains(repo_name)` — Which domains does repo X participate
  in, and in what role?
  Example question: "What responsibilities does `order-service` carry?"
  Returns a list of `{domain_name, role}` so you can reason about its place in
  the architecture before making changes.

- `depmap_get_domain_summary(domain_name)` — What is this domain and who is in
  it?
  Example question: "Tell me about the `checkout` domain in one call."
  Returns name, description, participating repos with roles, and outgoing
  cross-domain connection counts.

- `depmap_get_stale_domains(days_threshold)` — Which domains need re-analysis?
  Example question: "We have not touched the dep map in a sprint. What is
  overdue?"
  Returns domains sorted by `days_stale` descending, so the most neglected
  appear first. Use `days_threshold=0` for a full freshness inventory.

- `depmap_get_cross_domain_graph()` — What does the full directed graph look
  like?
  Example question: "Draw the domain-level dependency graph for a design
  review."
  Returns aggregated `{source_domain, target_domain, dependency_count,
  types[]}` edges with bidirectional consistency checking (the `types` field is
  a sorted list of the distinct dependency-type labels for that edge).
  `types` is always non-empty: edges whose contributing rows all have a
  blank Type column are omitted from the result and emit an anomaly
  instead, so clients do not need to guard against empty `types` arrays.

Phase-2 strengths:

- Exhaustive: every edge, every member, every stale entry. No top-N cutoff.
- Deterministic: the same inputs against the same dep map always return the
  same structured output.
- Structured: fields you can feed directly into further tool calls (e.g. pipe
  `depmap_find_consumers` output into `search_code` against each consumer repo).
- Diagnostic: every call returns `anomalies[]` so you can spot dep-map drift
  without a separate audit step.

## The `anomalies[]` contract — do not misinterpret it

Every `depmap_*` response includes an `anomalies[]` field. It is part of the
normal contract, not an error channel.

Distinguish three states carefully:

1. `success: true, anomalies: []`
   Clean pass. Every file parsed, every edge consistent, every required field
   present. This is what a fully repaired dep map looks like.

2. `success: true, anomalies: [...]`
   Partial results. The tool ran and returned real data, but some per-file
   parsing failures or consistency checks failed. Each entry in `anomalies[]`
   names the offending file and explains the problem (missing frontmatter key,
   unparseable date, edge claimed one way but not the other, etc.). The data
   you got back is still usable; the anomalies describe gaps.

3. `success: false`
   The tool could not run at all. Typically this means `dep_map_path` is not
   configured, the directory does not exist on disk, or an input parameter
   failed validation. Lists are empty, `error` carries a human-readable
   message. This is a real failure.

State 2 is the interesting one. During a dep-map repair cycle (a domain was
recently renamed, a repository was added and its edges have not been backfilled
yet, a `last_analyzed` date was misformatted) it is expected and healthy to see
`success: true` with a non-empty `anomalies[]`. Do not treat state 2 as a bug or
as grounds to retry. Surface the anomalies to the user, keep the real data, and
decide whether the repair cycle needs to finish before you trust the partial
result for a high-stakes change.

## Worked example — "I want to change repo X. Am I safe?"

Suppose you own `order-service` and plan a breaking change to its public API.
The question is whether anyone else in the fleet depends on you.

Step 1. Orient with semantic search.

Call `search_code` against `cidx-meta-global` with a natural-language query like
"order service responsibilities and consumers". Read the top few markdown
snippets. You now have a rough mental model of which domains `order-service`
participates in. Do not stop here — semantic search does not guarantee recall,
and in a fleet of 50 repos you cannot afford to miss a consumer.

Step 2. Enumerate your domains deterministically.

Call `depmap_get_repo_domains("order-service")`. The response lists every
domain `order-service` participates in and your role in each. Confirm the
roles match your expectation. If a domain is missing, the dep map is stale
and Step 4 will be incomplete — surface this and ask for a dep-map refresh
before promising safety.

Step 3. Enumerate your consumers exhaustively.

Call `depmap_find_consumers("order-service")`. You now have the full list of
`{domain, consuming_repo, dependency_type, evidence}` records. Count the
distinct `consuming_repo` values — that is your blast radius at the repository
level.

Step 4. Inspect the anomalies field.

If `anomalies[]` is non-empty, read every entry. An anomaly that mentions a
domain you care about means the map is partially broken for your specific
question. In that case, the consumer list in Step 3 may under-report. Decide
whether to proceed or wait for a repair cycle.

Step 5. Drill down with semantic search per consumer.

For each consumer repo returned in Step 3, run `search_code` against that repo
with a narrow query about the specific API you are changing. This is the
phase-1 tool doing what it does best: finding the specific call sites inside a
known code base.

Concrete handoff pattern (pseudo-code):

    unique_consumers = {
        (c.domain, c.consuming_repo) for c in result.consumers
    }
    for domain, consumer in unique_consumers:
        search_code(
            repository_alias=f"{consumer}-global",
            query_text="callers of <specific API you are changing>",
            limit=20,
        )

Two caveats. First, remember to deduplicate consumers by
(domain, consuming_repo) — `depmap_find_consumers` emits one entry per
matching Incoming-Dependencies row, so the same (domain, consuming_repo)
pair may appear multiple times when evidence strings differ. Second, a
consumer repo may not be indexed as a global repo in this CIDX instance;
if `search_code` returns a repo-not-found error, record the consumer in a
manual-review list and skip rather than aborting the whole sweep.

The key insight: phase 2 told you `which` repos are at risk (complete, no
misses). Phase 1 told you `where` inside each of those repos the risk lives
(prose-aware, fuzzy-tolerant). Neither tool alone answers "am I safe". Together
they do.

### See also

- `depmap/depmap_find_consumers` — exhaustive consumer enumeration for a given
  repository
- `depmap/depmap_get_repo_domains` — domain membership and role for a given
  repository
- `depmap/depmap_get_domain_summary` — structured summary of a single domain
- `depmap/depmap_get_stale_domains` — freshness audit across all domains
- `depmap/depmap_get_cross_domain_graph` — aggregated directed domain graph
