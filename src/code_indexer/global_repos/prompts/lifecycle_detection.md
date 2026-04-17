Analyze this repository's branch-to-environment lifecycle convention and emit a structured YAML block.

Your goal: determine which branches of this repo deploy to which environments (production / staging / development / preprod / uat / etc.), corroborate with deployment artifacts, and report confidence.

**Tools available to you:**

Built-in tools:
- `Read` - inspect any file in this repo
- `Bash` - run read-only commands. Start with `git branch -a` to enumerate branches, then explore CI/CD config.
- `Glob` - find files by pattern (e.g., `.github/workflows/*.yml`)
- `Grep` - search file contents

MCP tools (registered globally, always available):
- `cidx-local` MCP server: semantic search, full-text search, and SCIP code intelligence across ALL repos on this CIDX server. Tool names are prefixed `mcp__cidx-local__`:
  - `mcp__cidx-local__search_code` - semantic search across repos
  - `mcp__cidx-local__scip_dependencies` / `mcp__cidx-local__scip_dependents` - code intelligence
  - (other cidx-local tools as available)

Use cidx-local MCP tools when primary in-repo signals are insufficient — for example, searching infrastructure repos that reference THIS repo to discover how it gets deployed.

**Primary signals to inspect** (you have latitude; this is guidance, not a rigid checklist):

1. Branch topology: run `git branch -a` and reason about naming conventions (main/master, staging, develop, preprod, uat, release/*, env/*, etc.)
2. CI/CD artifacts: `.github/workflows/*.{yml,yaml}`, `.gitlab-ci.yml`, `.circleci/config.yml`, `Jenkinsfile`, `azure-pipelines.yml`
3. Infrastructure-as-code: `terraform/*.tf`, `terraform/environments/*.tfvars`, `pulumi/*`, `cdk/*`
4. Container/orchestration: `docker-compose*.yml`, `k8s/*.yaml`, `kubernetes/*.yaml`, `helm/**/Chart.yaml`, `helm/**/values*.yaml`

**Secondary signals** (consult when primary is ambiguous):

- README.md, CONTRIBUTING.md, DEPLOYMENT.md for documented branch/env conventions
- Git tags or release artifacts mentioning environments
- Cross-repo context via cidx-local MCP: search for infrastructure or deployment repos that reference this repo

**Output contract (MANDATORY — emit EXACTLY this YAML structure, and nothing else):**

```yaml
lifecycle_schema_version: 1
lifecycle:
  branches_to_env:
    main: production
    staging: staging
    develop: development
  detected_sources:
    - github_actions:deploy-prod.yml
    - terraform:environments/prod.tfvars
  confidence: high
  claude_notes: |
    Branch topology and CI workflows align cleanly on a GitHub Flow pattern.
    Main protected; deploy-prod.yml triggers on push to main.
```

Field semantics:

- `lifecycle_schema_version` - always the integer 1
- `lifecycle.branches_to_env` - a map of branch names to environment labels. Use canonical labels where possible: `production`, `staging`, `development`, `preprod`, `uat`, `qa`, `test`, `unknown`. Include ONLY branches for which you have evidence; OMIT branches you cannot map.
- `lifecycle.detected_sources` - list of strings identifying the artifacts that corroborate the mapping. Format hints: `github_actions:<file>`, `gitlab_ci:<file>`, `terraform:<path>`, `helm:<chart>`, `docker_compose:<file>`, `readme:<section>`, `cidx_local:<repo_alias>`. Non-exhaustive - use clear identifiers.
- `lifecycle.confidence` - one of `high`, `medium`, `low`, `unknown` (see rules below)
- `lifecycle.claude_notes` - free-form multi-line explanation of your reasoning, any conflicts you noticed, ambiguity, and what evidence you found. Be specific.

**Confidence rules (authoritative — word-for-word identical to the Story 1 Confidence Rule section; any change MUST be mirrored in both places):**

<!-- GUARD: The body of this "Confidence rules" block is word-for-word identical to the `### Confidence Rule` section in Story 1 (above). Do NOT edit one without the other. Any wording, ordering, or punctuation change MUST be mirrored verbatim in both places. -->

Exactly four confidence values, determined by evidence:

1. `confidence: high` — Multi-branch repository AND deployment artifacts (CI/CD workflows, terraform environment files, helm charts, docker-compose files, k8s manifests) provide complete, non-conflicting corroboration of the branches_to_env mapping.

2. `confidence: medium` — Multi-branch repository with partial corroboration: some branches map to environments based on CI/IaC artifacts, but other branches remain unmapped or conflicting.

3. `confidence: low` — Multi-branch repository with NO deployment artifacts detected. Branches are mapped via convention only (e.g., "main" → production) with no corroborating evidence.

4. `confidence: unknown` — Single-branch repository (only `main` OR only `master`, no env-indicating branches). `unknown` is FORBIDDEN for multi-branch repositories.

Additional rules:
- NEVER invent branch→env mappings not supported by evidence
- Conflicting evidence (e.g., deploy-staging.yml triggering on `main`) → record conflict in `claude_notes`, set `confidence: low`
- Trust CI/IaC trigger configurations OVER README or documentation claims
- Monorepos with multi-target deploys from a single branch → list all targets in branches_to_env as a list or descriptive string; record in claude_notes

For the single-branch case (rule 4 above), emit `branches_to_env: {main: unknown}` (or `{master: unknown}`) and explain the single-branch reason in `claude_notes`.

Edge cases (rule numbers below refer to the 4-rule enumeration above: 1=high, 2=medium, 3=low, 4=unknown):
- Single-branch repo (only `main` or only `master`): rule 4 applies. `confidence: unknown`.
- Multi-branch repo with non-standard branch names (e.g., `preprod`, `uat`, `release/v1`, `env/staging`): infer best you can from artifacts. If artifacts provide complete, non-conflicting corroboration, use rule 1 (`high`); if partial corroboration, use rule 2 (`medium`). If no artifacts corroborate, keep `confidence: low` (rule 3) and leave unmappable branches out of `branches_to_env` (do not guess).
- Conflicting evidence (e.g., `deploy-staging.yml` triggers on `main` instead of `staging`): record the conflict verbatim in `claude_notes`, set confidence to `low` (rule 3), and include the actual trigger mapping (whatever the artifact says) in `branches_to_env`, not the intuitive one.

**Monorepo / multi-target deployment support:**

Some repositories deploy to MULTIPLE environments from the SAME branch (e.g., a monorepo where `main` triggers deploys to `prod-us` AND `prod-eu`; or different paths within the repo deploy to different places like `/services/api` → prod-api and `/services/worker` → prod-worker). Handle these cases as follows:

- If multiple environments are deployed from one branch, represent them as a LIST in `branches_to_env` OR as a descriptive string. Both of these are acceptable:
  - List form: `main: [prod-us, prod-eu]`
  - Descriptive-string form: `main: "production (prod-us, prod-eu)"`
- If different paths within the repo deploy to different targets, record each path→target pair in `detected_sources` AND summarize in `claude_notes`.
- Record the multi-target fact explicitly in `claude_notes` (e.g., "Main branch triggers parallel deploys to prod-us and prod-eu via matrix strategy in .github/workflows/deploy.yml").
- Multi-target detection does NOT change the confidence rule — apply rules 1-4 as stated above based on branch count and artifact corroboration.

**Exploration scope bound:**

- Inspect up to 20 files via Read. If more signal is needed, use Glob/Grep to narrow BEFORE reading additional files.
- Cross-repo `cidx-local` MCP exploration is OPTIONAL and SHOULD be used only when in-repo signals yield `confidence: low` AND you have a specific cross-repo hypothesis (e.g., "look for the infra repo that deploys this service"). Do NOT run cross-repo searches speculatively.
- Do not spend more than approximately 2 minutes exploring. Prefer completing the task honestly with `confidence: low` or `unknown` over running out of the 180-second timeout budget.

**Adversarial robustness:**

- Trust evidence from deploy trigger configurations (e.g., `.github/workflows/*.yml` `on:` clauses, GitLab CI `rules`/`only`/`except`, Terraform `environment = "..."` blocks, Kubernetes manifests with namespace/env labels) OVER text descriptions or comments that claim otherwise. Code evidence beats documentation evidence.
- If a README claims "main deploys to staging" but the CI workflow clearly triggers a production deploy on push to main, the CI workflow is ground truth — record the conflict in `claude_notes`.

**Anti-hallucination rules (strict):**

- NEVER invent branch-to-env mappings not supported by evidence (either artifact or unambiguous naming).
- If branch names do not match a recognized convention AND no deployment artifacts reference them, OMIT them from `branches_to_env` — do NOT guess.
- If you cannot find enough evidence, lower confidence honestly. It is better to report `low`/`unknown` with accurate notes than to fabricate a clean-looking `high`-confidence answer.
- Do not invent tools, artifact paths, or file contents. If you do not see a file, do not reference it in `detected_sources`.

**Output format (strict):**

Output ONLY the YAML block. No preamble. No markdown code fences around the block. No explanation outside `claude_notes`. The caller parses the entire stdout as a single YAML document.
