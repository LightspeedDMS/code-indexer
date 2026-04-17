Post-generation verification pass for dependency map and description artifacts.

You will receive a generated markdown document and verify every factual claim in it against
the actual source code in the repositories listed below. Your job is to produce a corrected
version of the document and a machine-readable evidence log.

**Inputs provided by the caller:**

- `document_content` (the document to verify):
{document_content}

- `repo_list` (repositories whose source code you must use as ground truth):
{repo_list}

- `discovery_mode` (controls whether new ADDED items are permitted):
{discovery_mode}

---

**Tools available to you:**

Built-in tools:
- `Read` — read any file in the target repositories
- `Glob` — find files by pattern (e.g., `src/**/*.py`, `**/*.ts`)
- `Grep` — search file contents by pattern

MCP tools (if registered):
- `mcp__cidx-local__search_code` — semantic/FTS search across repos
- `mcp__cidx-local__scip_dependencies` / `mcp__cidx-local__scip_dependents` — code intelligence

Use cidx-local MCP tools when direct file inspection is insufficient to locate a symbol or
verify a relationship claim.

---

**Verification procedure:**

1. Read the document provided in `document_content` above.

2. Build a checklist of every verifiable factual claim in the document. Verifiable claims
   include but are not limited to:
   - Named dependencies between components, modules, or services
   - Relationships stated as "X calls Y", "X depends on Y", "X imports Y"
   - Component or symbol names (classes, functions, modules, packages)
   - Specific file path references
   - Cardinality claims: "N components", "3 services", "two layers", etc.
   - Version or configuration assertions (e.g., "uses PostgreSQL", "requires Python 3.10+")

3. For each claim, use the available tools to verify it against the actual source code in the
   repositories listed in `repo_list`. Perform targeted lookups — do not speculatively read
   large portions of the codebase.

4. Classify each claim as exactly one of:
   - `VERIFIED` — confirmed correct by source evidence
   - `CORRECTED` — the claim exists but the detail is wrong; you have corrected it
   - `REMOVED` — the claim is false and cannot be corrected (no supporting evidence exists)
   - `ADDED` — a new claim not present in the original document (only if discovery_mode allows)

5. Evidence requirements:
   - `CORRECTED` items: MUST include at least one of (a) `file_path` + `line_range` OR
     (b) `symbol` + `definition_location`. Items lacking evidence will be discarded by the caller.
   - `ADDED` items: MUST include the same evidence. Items lacking evidence will be discarded.
   - `VERIFIED` items: evidence is optional; include it when readily available.
   - `REMOVED` items: require only the original claim text; evidence is optional.

6. Discovery mode rules:
   - If `discovery_mode` is `false`: do NOT produce any `ADDED` items. Only `VERIFIED`,
     `CORRECTED`, and `REMOVED` are permitted. Ignore any new information you find that is
     not already represented in the document.
   - If `discovery_mode` is `true`: `ADDED` items are permitted but each MUST carry source
     evidence as described above.

7. Produce the corrected document:
   - Include all `VERIFIED` content unchanged.
   - Replace content for all `CORRECTED` items with the corrected text.
   - Omit all `REMOVED` content.
   - Append or integrate `ADDED` content where appropriate (only when `discovery_mode` is `true`).
   - Preserve document structure, headings, and formatting. Do not reformat or reorganize
     sections unless a correction requires it.

---

**Output contract (MANDATORY):**

Emit a SINGLE JSON object as your entire response. No surrounding prose. No markdown code
fences. No text before or after the JSON. The caller passes your entire stdout to
`json.loads()`.

Schema:

{
  "corrected_document": "<string: the corrected markdown document>",
  "evidence": [
    {
      "claim": "<string: the original claim text from the document>",
      "disposition": "VERIFIED | CORRECTED | REMOVED | ADDED",
      "file_path": "<string or null: path relative to repo root>",
      "line_range": [<int start>, <int end>],
      "symbol": "<string or null: symbol name>",
      "definition_location": "<string or null: file:line for symbol definition>",
      "notes": "<string or null: optional short explanation>"
    }
  ],
  "counts": {
    "verified": <int>,
    "corrected": <int>,
    "removed": <int>,
    "added": <int>
  }
}

Rules for the evidence array:
- At least one of (`file_path` + `line_range`) OR (`symbol` + `definition_location`) MUST be
  populated for every `CORRECTED` and `ADDED` item.
- `line_range` must be a two-element integer array `[start, end]` (1-based, inclusive). If
  only a single line is known, use `[line, line]`.
- `VERIFIED` items may omit evidence fields (set to null).
- `REMOVED` items may omit all evidence fields (set to null).
- Every claim in the original document that you evaluated must appear in the evidence array
  exactly once.
- `ADDED` items must also appear in the evidence array with their supporting evidence.

---

**Safety instructions:**

- Do NOT invent sources. Every evidence citation must reference a real file that exists in
  the target repository and a real line range or symbol location you verified with tools.
- Do NOT truncate or summarize `corrected_document`. Preserve all content that was `VERIFIED`,
  include corrected text for `CORRECTED` items, integrate `ADDED` content when discovery_mode
  permits, and omit `REMOVED` content.
- Do NOT produce `REMOVED` dispositions for more than half the claims in the original document
  without extraordinary justification. Aggressive removal indicates over-correction and the
  caller's safety guards will reject the output.
- Do NOT shorten `corrected_document` below 50% of the original character length. Outputs
  shorter than this threshold will be rejected by the caller's safety guards.
- Do NOT restructure or reformat the document beyond what corrections require.
- If `discovery_mode` is `false`, treat ADDED items as forbidden. Silently drop any discovery
  findings rather than including them in the output.

---

**Anti-hallucination rules:**

- NEVER cite a file path you have not confirmed exists via Read, Glob, or Grep.
- NEVER cite a line range without having read the file and observed the relevant lines.
- NEVER cite a symbol definition location without having verified the symbol exists there.
- If a claim cannot be verified with available tools, classify it as `REMOVED` with a note
  explaining the absence of evidence — do NOT mark it `VERIFIED` speculatively.
- If tools return no results for a named component or dependency, this is evidence of absence.
  Classify accordingly rather than assuming the claim might be correct.

---

**Format constraints:**

- The entire response must be valid JSON parseable by `json.loads()`.
- Strings inside `corrected_document` must be properly JSON-escaped: newlines as `\n`,
  double-quotes as `\"`, backslashes as `\\`, etc.
- Do NOT wrap the JSON in markdown code fences (` ```json ... ``` `).
- Do NOT emit any text before or after the JSON object.
- `counts` must accurately reflect the number of items of each disposition in the `evidence`
  array. The caller validates these counts.
