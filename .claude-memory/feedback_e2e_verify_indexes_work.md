---
name: feedback_e2e_verify_indexes_work
description: E2E testing must verify indexes are ACTUALLY CREATED and RETURN RESULTS — not just that code runs without errors
type: feedback
---

When implementing multi-provider indexing or any index-related feature, E2E testing MUST verify:
1. The index collection directory actually EXISTS on disk with files in it
2. A QUERY against that index returns REAL RESULTS (not zero results)
3. Both providers return results independently (test with query_strategy=specific)

**Why:** Story #620 was "tested" but the Cohere index was never actually built in production because _append_provider_to_config wrote to the wrong path (versioned snapshot instead of base clone). The tests only verified that code ran without errors, not that indexes were actually created and functional.

**How to apply:**
- After any indexing operation, check the filesystem: does `.code-indexer/index/{collection_name}/` exist and have content?
- After indexing, run a real search query and verify results come back
- For multi-provider: verify EACH provider's collection independently
- "Tests pass" is NOT the same as "feature works" — verify the actual observable outcome
