---
name: create_memory
category: admin
required_permission: repository:write
tl_dr: 'Create a persistent, evidence-backed technical memory in the shared memory
  store.


  WHEN TO CALL THIS TOOL (trigger-driven - act on these):

  - The user corrects an assumption you made about architecture, behavior, or code
  structure

  - The user explains a non-obvious gotcha, ordering constraint, or failure mode

  - The user clarifies how a config key behaves, including edge cases or defaults

  - The user describes an API contract, parameter limit, or undocumented behavior

  - You observe a verified performance characteristic (measured, not guessed)

  - You discover an architectural fact that a future agent would need to know


  Before calling, search existing memories on the topic (search_code with

  path_filter="cidx-meta/memories/*.md").'
---

Create a persistent, evidence-backed technical memory in the shared memory store.

WHEN TO CALL THIS TOOL (trigger-driven - act on these):
- The user corrects an assumption you made about architecture, behavior, or code structure
- The user explains a non-obvious gotcha, ordering constraint, or failure mode
- The user clarifies how a config key behaves, including edge cases or defaults
- The user describes an API contract, parameter limit, or undocumented behavior
- You observe a verified performance characteristic (measured, not guessed)
- You discover an architectural fact that a future agent would need to know

Before calling, search existing memories on the topic (search_code with
path_filter="cidx-meta/memories/*.md"). If a matching memory exists, call
edit_memory instead of creating a duplicate.

WHEN NOT TO CALL THIS TOOL:
- You have a hunch but have not verified it against code or commits
- The information is business context, not a technical fact
- The note is prose commentary rather than a verifiable claim
- You are summarizing a conversation rather than extracting a discrete fact

FACT-CHECK DISCIPLINE (mandatory before writing):
Every memory MUST include at least one evidence entry pointing to a specific file and
line range, or a specific commit hash. This is not optional. If you cannot name the
file and lines that prove the claim, stop, find them first, then write the memory.
A memory without evidence is speculation and will mislead future agents.

SUMMARY DISCIPLINE:
The summary field is a pointer, not an essay. Write one tight claim (aim for under 200
characters, never more than 1000). Include a file:line reference inline if it fits.
Do not restate the evidence list in prose form.

GOOD summary:
  "VoyageAI batch limit is 120,000 tokens enforced in embedded_voyage_tokenizer.py:88-102.
   Exceeding it raises BatchSizeError, not a warning."

BAD summary:
  "There are some considerations around how VoyageAI handles batching that might be
   worth knowing about in certain scenarios when you're working with embeddings."

SCOPE:
- global: fact applies across all repos; scope_target and referenced_repo MUST be null
- repo: fact is specific to one repository; provide referenced_repo alias as scope_target
- file: fact is specific to one file path; provide referenced_repo and scope_target path

BIAS TOWARD CAPTURING: sessions end, memories persist. If the user taught you something
non-obvious, capture it. Future agent sessions depend on it.

After writing, confirm the uuid and summary to the user.

EXAMPLE: {"type": "config-behavior", "scope": "global", "summary": "VoyageAI batch limit is 120,000 tokens enforced in embedded_voyage_tokenizer.py:88-102. Exceeding it raises BatchSizeError, not a warning.", "evidence": [{"file": "src/code_indexer/embedded_voyage_tokenizer.py", "lines": "88-102"}]}
