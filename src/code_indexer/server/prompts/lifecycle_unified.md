Analyze this repository and emit a single JSON object containing both a description and lifecycle metadata.

**Tools available to you:**

Built-in tools:
- `Read` - inspect any file in this repo
- `Bash` - run read-only commands. Start with `git branch -a` to enumerate branches, then explore CI/CD config.
- `Glob` - find files by pattern (e.g., `.github/workflows/*.yml`)
- `Grep` - search file contents

MCP tools (registered globally, always available):
- `cidx-local` MCP server: semantic search, full-text search, and SCIP code intelligence across ALL repos on this CIDX server.

**What to produce:**

1. **description** (1-500 chars): A concise, factual summary of what this repository does — its purpose, domain, and primary capabilities. Avoid marketing language.

2. **lifecycle** — six required fields describing the CI/CD and build ecosystem:
   - `ci_system`: the CI system in use (e.g., "github-actions", "gitlab-ci", "jenkins", "circleci", "none")
   - `deployment_target`: what the built artifact deploys to (e.g., "kubernetes", "npm-registry", "pypi", "docker-hub", "none")
   - `language_ecosystem`: primary language and package manager (e.g., "python/poetry", "typescript/npm", "rust/cargo")
   - `build_system`: the build tool (e.g., "poetry", "cargo", "make", "gradle", "bazel")
   - `testing_framework`: the test runner (e.g., "pytest", "jest", "go-test", "none")
   - `confidence`: exactly one of `high`, `medium`, or `low` (see rules below)

**Primary signals to inspect:**

1. Branch topology: `git branch -a` — reason about naming (main/master, staging, develop, release/*, env/*)
2. CI/CD artifacts: `.github/workflows/*.{yml,yaml}`, `.gitlab-ci.yml`, `.circleci/config.yml`, `Jenkinsfile`, `azure-pipelines.yml`
3. Build manifests: `pyproject.toml`, `package.json`, `Cargo.toml`, `build.gradle`, `pom.xml`, `Makefile`
4. Infrastructure-as-code: `terraform/*.tf`, `pulumi/*`, `cdk/*`, `k8s/*.yaml`, `helm/**/Chart.yaml`

**Confidence rules:**

- `high` — multiple independent signals corroborate all six lifecycle fields with no conflicts
- `medium` — at least one clear signal per field but some fields rely on inference or have minor conflicts
- `low` — limited signals; one or more fields are guesses based on convention with no corroborating artifact

Do NOT use `unknown` — use `low` when evidence is weak. `confidence: unknown` is explicitly rejected by the caller.

**Anti-hallucination rules (strict):**

- NEVER invent values not supported by evidence. If a field cannot be determined, use `"none"`.
- Do not reference files you have not read.
- Trust code/config evidence over documentation claims when they conflict.
- If you cannot determine a value, use `"none"` rather than guessing.

**Exploration scope bound:**

- Inspect up to 20 files via Read. Use Glob/Grep to narrow before reading additional files.
- Do not spend more than approximately 2 minutes exploring. Prefer completing with `confidence: low` over running out of the 180-second timeout budget.

**Output contract (MANDATORY):**

Respond with a SINGLE JSON object. No preamble. No explanation outside the JSON. No markdown code fences. No trailing text. The caller uses `json.loads` on the entire stdout.

The JSON object MUST match this schema exactly:

```
{
  "description": "<1-500 char string, non-empty>",
  "lifecycle": {
    "ci_system": "<non-empty string>",
    "deployment_target": "<non-empty string>",
    "language_ecosystem": "<non-empty string>",
    "build_system": "<non-empty string>",
    "testing_framework": "<non-empty string>",
    "confidence": "<exactly one of: high | medium | low>"
  }
}
```

Example of a valid response (copy the structure, not the values):

{"description": "A Python service for semantic code search using VoyageAI embeddings and HNSW vector indexing.", "lifecycle": {"ci_system": "github-actions", "deployment_target": "kubernetes", "language_ecosystem": "python/poetry", "build_system": "poetry", "testing_framework": "pytest", "confidence": "high"}}
