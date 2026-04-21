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

3. **lifecycle.branching** (REQUIRED in v3 — always emit; use escape values (`null`, `"unknown"`) for fields lacking evidence):
   - `default_branch` (required within section): run `git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's|refs/remotes/origin/||'`; if empty inspect `git branch -r` for `HEAD ->` pointer.
   - `model` (required within section): infer from branch names — `release/*` branches → `"gitflow"`, direct PR-to-main with short-lived branches → `"github-flow"`, long-lived `develop`/feature branches → `"trunk-based"`, single `release-*` branch → `"release-branch"`, insufficient evidence → `"unknown"`. MUST be exactly one of: `github-flow | gitflow | trunk-based | release-branch | unknown`. Use `"unknown"` when evidence is insufficient — never invent.
   - `release_branch_pattern` (required within section): e.g. `"release/*"` or `"v*"`; use `null` if no release branches detected.
   - `protected_branches` (required within section): list of branch names from `.github/settings.yml` or CONTRIBUTING.md; use `null` if no evidence found.

4. **lifecycle.ci** (REQUIRED in v3 — always emit; use empty lists (`[]`) and `"none"` enum when no CI config exists):
   - `trigger_events` (required within section): list of trigger event strings extracted from the `on:` block of `.github/workflows/*.yml`, `.gitlab-ci.yml`, or `Jenkinsfile`. MUST be a subset of: `push | pull_request | tag | schedule | workflow_dispatch | manual`. Use empty list `[]` if no CI config found.
   - `required_checks` (required within section): list of job names that act as required status checks (from workflow files); use empty list `[]` if none found.
   - `deploy_on` (required within section): condition that gates deployment. MUST be exactly one of: `tag | merge-to-main | merge-to-release-branch | manual | none`. Infer from `startsWith(github.ref, 'refs/tags/')` → `"tag"`, branch filter on main → `"merge-to-main"`, release branch filter → `"merge-to-release-branch"`, manual approval gate → `"manual"`, no deployment job → `"none"`.
   - `environments` (required within section): list of environment names from `jobs.*.environment` keys in workflow files; use `null` if none found.

5. **lifecycle.release** (REQUIRED in v3 — always emit; use escape values (`null`, `"unknown"`, `[]`) when no version manifest exists):
   - `versioning` (required within section): version scheme. MUST be exactly one of: `semver | calver | custom | none | unknown`. dotted-triad (X.Y.Z) → `"semver"`, YYYY.MM.DD → `"calver"`, other pattern → `"custom"`, no version detected → `"none"`, cannot determine → `"unknown"`.
   - `version_source` (required within section): filename containing the version (e.g., `"pyproject.toml"`, `"package.json"`, `"Cargo.toml"`, `"__init__.py"`); use `null` if no version file found.
   - `changelog` (required within section): filename of the changelog (`"CHANGELOG.md"`, `"HISTORY.md"`, `"NEWS.md"`); use `null` if absent.
   - `auto_publish` (required within section): boolean — `true` if CI workflow unconditionally runs `twine upload` / `npm publish` / `cargo publish` / `gh release create` without a manual gate; `false` otherwise.
   - `artifact_types` (required within section): list of artifact types produced. MUST be a subset of: `wheel | sdist | docker | tarball | binary | gem | jar | nupkg | deb | rpm | other`. Use empty list `[]` if no artifacts detected.

**CRITICAL — enum escape values:**

Every enum field has a legal escape value so you never have to invent:
- `branching.model`: use `"unknown"` when evidence is insufficient
- `ci.deploy_on`: use `"none"` when no deployment job exists
- `ci.trigger_events`: use `[]` when no CI config found
- `release.versioning`: use `"unknown"` when scheme cannot be determined
- `release.artifact_types`: use `[]` when no artifacts detected
- Any list field: use `null` (for nullable fields) or `[]` (for list fields) — never omit a required-within-section field

**Primary signals to inspect:**

1. Branch topology: `git branch -a` — reason about naming (main/master, staging, develop, release/*, env/*)
2. CI/CD artifacts: `.github/workflows/*.{yml,yaml}`, `.gitlab-ci.yml`, `.circleci/config.yml`, `Jenkinsfile`, `azure-pipelines.yml`
3. Build manifests: `pyproject.toml`, `package.json`, `Cargo.toml`, `build.gradle`, `pom.xml`, `Makefile`
4. Infrastructure-as-code: `terraform/*.tf`, `pulumi/*`, `cdk/*`, `k8s/*.yaml`, `helm/**/Chart.yaml`
5. Branch protection: `.github/settings.yml`, `CONTRIBUTING.md`

**Confidence rules (updated for Schema v3):**

- `high` — multiple independent signals corroborate all six v2 lifecycle fields AND branching/ci/release sections are populated without major gaps
- `medium` — all six v2 fields identified, but several workflow fields are `"unknown"` or `null` (v3 sections partially populated)
- `low` — limited signals; one or more v2 fields are guesses based on convention with no corroborating artifact

Do NOT use `unknown` for `confidence` — use `low` when evidence is weak. `confidence: unknown` is explicitly rejected by the caller.

**Anti-hallucination rules (strict):**

- NEVER invent values not supported by evidence. If a field cannot be determined, use the designated escape value (`null`, `[]`, `"unknown"`, or `"none"` as appropriate for the field).
- Do not reference files you have not read.
- Trust code/config evidence over documentation claims when they conflict.
- Always emit all three v3 sections (`branching`, `ci`, `release`). Use the designated escape values for any field lacking evidence: `null` for nullable fields, `[]` for list fields, `"none"`/`"unknown"` for enum fields. NEVER omit a section — downstream consumers require consistent shape.

**Exploration scope bound:**

- Inspect up to 20 files via Read. Use Glob/Grep to narrow before reading additional files.
- Do not spend more than approximately 3 minutes exploring. Prefer completing with `confidence: low` and using escape values in all three sections over running out of the 240-second timeout budget.

**Output contract (MANDATORY):**

Respond with a SINGLE JSON object. No preamble. No explanation outside the JSON. No markdown code fences. No trailing text. The caller uses `json.loads` on the entire stdout.

The JSON object MUST match this schema. The six lifecycle fields are REQUIRED. All three sections (`branching`, `ci`, `release`) are REQUIRED in v3 — always emit using escape values when evidence is absent:

```
{
  "description": "<1-500 char string, non-empty>",
  "lifecycle": {
    "ci_system": "<non-empty string>",
    "deployment_target": "<non-empty string>",
    "language_ecosystem": "<non-empty string>",
    "build_system": "<non-empty string>",
    "testing_framework": "<non-empty string>",
    "confidence": "<exactly one of: high | medium | low>",

    "branching": {
      "default_branch": "<string>",
      "model": "<exactly one of: github-flow | gitflow | trunk-based | release-branch | unknown>",
      "release_branch_pattern": "<string or null>",
      "protected_branches": ["<string>", ...] or null
    },

    "ci": {
      "trigger_events": ["<subset of: push | pull_request | tag | schedule | workflow_dispatch | manual>"],
      "required_checks": ["<string>", ...],
      "deploy_on": "<exactly one of: tag | merge-to-main | merge-to-release-branch | manual | none>",
      "environments": ["<string>", ...] or null
    },

    "release": {
      "versioning": "<exactly one of: semver | calver | custom | none | unknown>",
      "version_source": "<string or null>",
      "changelog": "<string or null>",
      "auto_publish": <true | false>,
      "artifact_types": ["<subset of: wheel | sdist | docker | tarball | binary | gem | jar | nupkg | deb | rpm | other>"]
    }
  }
}
```

Example of a valid v3 response (copy the structure, not the values):

{"description": "A Python service for semantic code search using VoyageAI embeddings and HNSW vector indexing.", "lifecycle": {"ci_system": "github-actions", "deployment_target": "pypi", "language_ecosystem": "python/poetry", "build_system": "poetry", "testing_framework": "pytest", "confidence": "high", "branching": {"default_branch": "main", "model": "github-flow", "release_branch_pattern": null, "protected_branches": ["main"]}, "ci": {"trigger_events": ["push", "pull_request"], "required_checks": ["lint", "test"], "deploy_on": "tag", "environments": null}, "release": {"versioning": "semver", "version_source": "pyproject.toml", "changelog": "CHANGELOG.md", "auto_publish": true, "artifact_types": ["wheel", "sdist"]}}}
