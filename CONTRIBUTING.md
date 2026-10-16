# Contributing to CIDX

How to set up a development checkout, which branches to use, which test suites must pass, and how releases are
cut. User documentation starts at [docs/README.md](docs/README.md).

## Prerequisites

- Python 3.9 to 3.12 (`requires-python = ">=3.9,<3.13"`).
- git, and a C/C++ compiler plus Python headers (the `hnswlib` fork is built from source).
- A Rust toolchain only if you touch `rust/`: `rustup` installs the pinned version from `rust/rust-toolchain.toml`
  automatically the first time `cargo` runs inside `rust/`.
- For tests that call embedding providers: `VOYAGE_API_KEY` and, for Cohere and reranking tests, `CO_API_KEY`.

## Setup

```bash
git clone https://github.com/LightspeedDMS/code-indexer.git
cd code-indexer
git submodule update --init --recursive
python3 -m venv .venv && source .venv/bin/activate
python3 -m pip install -e ".[dev]"
pre-commit install
```

Submodules: `third_party/hnswlib` (the hnswlib fork), `test-fixtures/multimodal-mock-repo` and
`test-fixtures/scip-python-mock`. The last one is configured with an SSH URL (`git@github.com:...`), so
`--recursive` needs a GitHub SSH key; without one, initialise the other two by path
(`git submodule update --init third_party/hnswlib test-fixtures/multimodal-mock-repo`).

`fast-automation.sh` and `server-fast-automation.sh` run `pip install -e ".[dev]"` from the current checkout
before testing, so running them points your editable install at that checkout.

## Dependencies

All dependencies are declared in `pyproject.toml`, the only source of truth: `[project.dependencies]` for the base
install and `[project.optional-dependencies]` for the `cluster`, `cohere` and `dev` extras. There is no
`requirements.txt`.

```bash
python3 -m pip install -e ".[dev]"            # development
python3 -m pip install -e ".[dev,cluster]"    # also PostgreSQL drivers, for cluster-mode tests
```

CI installs `".[dev,cluster]"` plus `third_party/hnswlib` in editable mode. End-user installation is described in
[docs/getting-started/installation.md](docs/getting-started/installation.md).

## Pre-commit hooks

`.pre-commit-config.yaml` runs on every commit:

- `ruff` with `--fix --exit-non-zero-on-fix`, and `ruff-format`;
- `mypy` on `src/` only (tests are not type-checked by the hook, but `./lint.sh` and CI check them);
- trailing whitespace, end-of-file, YAML syntax, large files (over 1000 KB), merge-conflict markers, case conflicts;
- a check that `skills/` bundles are in sync with their `SKILL.md` (only when files under `skills/` change).

When a hook rewrites files, stage them again and commit again. `pre-commit run --all-files` runs every hook on the
whole tree.

## Developer Certificate of Origin (DCO)

CIDX uses the [Developer Certificate of Origin](https://developercertificate.org/) as a lightweight contributor agreement. The DCO is a per-commit attestation that you wrote the code you are submitting, or have the right to submit it under the project's MIT license. There is no CLA to sign.

### How to sign off

Every commit you contribute must include a `Signed-off-by` trailer in the commit message footer:

```
Signed-off-by: Your Real Name <your.email@example.com>
```

The easiest way is to use `git commit -s` (or `--signoff`), which appends the trailer automatically using your `user.name` and `user.email` from `git config`. To make this the default for every commit:

```bash
git config --global format.signOff true     # appends -s by default for git commit
```

### What you are attesting

By adding the `Signed-off-by` trailer you certify the statements in https://developercertificate.org/ — in summary:

- You wrote the code (or have the right to submit it).
- The contribution is licensed under the project's open-source license (MIT).
- You understand the contribution is public and will be redistributed under that license.

### Enforcement

Pull requests without DCO sign-off on every commit will be asked to re-submit with sign-offs added. To retroactively sign existing commits on a branch, use:

```bash
git rebase --signoff origin/development
```

(replace the base branch as appropriate).

## Branches

| Branch | Purpose | Direct commits |
|--------|---------|----------------|
| `development` | Active work; target of pull requests. Feature branches (`feature/*`, `bugfix/*`) start here. | yes |
| `staging` | Pre-production validation. Receives merges from `development` only. | no |
| `master` | Production. Receives merges from `staging`, and hotfixes. | hotfixes only |

Normal flow: `development` -> `staging` -> `master`. `development` is never merged directly into `master`.

Hotfix flow: branch from `master` (optionally `hotfix/*`), make only the fix, bump the HOTFIX version component,
merge to `master`, then merge `master` back into `development`. The direction is always `master` -> `development`.

Open pull requests against `development`, one change per pull request, and describe what changed and why.

## Test suites

| Command | What it runs | Required when |
|---------|-------------|---------------|
| `pytest tests/unit/<path>/test_x.py -v --tb=short` | The tests for what you are changing, plus the tests of anything your change might break. | While developing. |
| `./fast-automation.sh` | Unit tests outside `tests/unit/server/`, excluding the `slow`, `e2e`, `real_api`, `integration`, `requires_server`, `requires_containers` and `performance` markers. Per-test timeout 15 s (`PYTEST_TIMEOUT`). | Every change. |
| `./server-fast-automation.sh` | `tests/unit/server/` in 6 parallel chunks, each with its own temporary `CIDX_SERVER_DATA_DIR`. Per-test timeout 15 s. | Any change under `src/code_indexer/server/`. |
| `./rust-automation.sh` | `cargo test --workspace` and `cargo clippy --workspace --all-targets -- -D warnings` in `rust/`. | Any change under `rust/`, or to files compiled into the Rust binary (`docs/guides/xray-cookbook.md`, `docs/xray-templates/`). |
| `./slow-automation.sh [--phase 1\|2] [--timeout N] [--paths ...]` | Tests marked `@pytest.mark.slow`: phase 1 non-server, phase 2 server (isolated data dir). Per-test timeout 120 s (`--timeout` or `SLOW_PYTEST_TIMEOUT`). | When you add or change a slow test. |
| `./e2e-automation.sh [--phase N]` | End-to-end phases 1-7, no mocks: CLI standalone, CLI daemon, server in-process, CLI remote against a live server, fault-injection resiliency, PostgreSQL parity, SIEM delivery. | Before a story or epic is complete. |
| `./lint.sh` | See below. | Every change. |

Notes:

- A test that takes more than 30 s belongs in the slow lane: mark it `@pytest.mark.slow`.
- Server tests write to `~/.cidx-server/` unless `CIDX_SERVER_DATA_DIR` points elsewhere. Run them through
  `server-fast-automation.sh`, or set `CIDX_SERVER_DATA_DIR` to a temporary directory (and `CIDX_TEST_FAST_SQLITE=1`,
  as the script does) when running them by hand.
- pytest loads `<repo-root>/.env.local` and then `<repo-root>/.env` (`tests/conftest.py` imports
  `tests/load_env.py`; `KEY=value` or `export KEY=value` lines; a variable already set in the environment is never
  overridden). `fast-automation.sh`, `server-fast-automation.sh` and `slow-automation.sh` also `source` both files.
  They are gitignored and must never be committed.
- `e2e-automation.sh` reads credentials from `.e2e-automation` (copy `.e2e-automation.template`) or from the
  environment. `E2E_ADMIN_USER` and `E2E_ADMIN_PASS` are required (the script exits at once without them).
  `E2E_VOYAGE_API_KEY` falls back to `VOYAGE_API_KEY`. Phase 5 needs `CO_API_KEY` (or `E2E_COHERE_API_KEY`).
  Phase 6 needs the PostgreSQL server utilities (`initdb`, `pg_ctl`) and is skipped, loudly, without them. The
  OpenTelemetry collector check in phase 3 needs Docker and is skipped without it.
- Run the full suites once, after code review, not between review rounds.

Test layout, main directories (not an exhaustive list): `tests/unit/` (fast, no external services),
`tests/integration/`, and `tests/e2e/` (end-to-end tests grouped by phase), plus shared fixtures and
helpers such as `tests/fixtures/` and `tests/utils/`. Use real components rather than mocks wherever possible.

## Lint

`./lint.sh` checks and does not modify files. It runs:

1. `ruff check src tests`
2. `ruff format --check src tests`
3. `mypy --explicit-package-bases --check-untyped-defs src tests`
4. `scripts/check_no_direct_cp_reflink.py` (production code must go through the clone backend)
5. `scripts/check_doc_references.py`: every relative Markdown link in a tracked `.md` file, every literal
   `docs/...` path in code, scripts and docs, and every Rust `include_str!` path must point to a file that exists.
6. Generated reference check: `PYTHONPATH=src python3 -m tools.docs.<generator> --check` for `cli_reference`,
   `mcp_tools` and `error_codes`. Each generator owns one directory (`docs/reference/cli/`,
   `docs/reference/mcp-tools/`, `docs/reference/error-codes/`) and fails when it no longer matches the code.

To fix formatting and auto-fixable lint findings: `ruff format src tests` and `ruff check --fix src tests`. When
step 6 fails (you changed a CLI option, an MCP tool doc or an error code), regenerate with the same command without
`--check`, for example `PYTHONPATH=src python3 -m tools.docs.cli_reference`, and commit the regenerated files. Never
edit those generated files by hand.
`./lint.sh` must exit 0 before a change is merged.

Documentation files use plain Markdown with no emoji, and neutral sample data (`example.com`, `example-repo`,
RFC 5737 addresses). Never put credentials, hostnames or personal data in a commit, issue or document.

## What CI runs

`.github/workflows/main.yml` runs on pushes to `master`, `main`, `develop`, `development` and `staging`:

| Job | Runs |
|-----|------|
| `lint` | `./lint.sh` on Python 3.9, with `ruff==0.14.1` (the version `.pre-commit-config.yaml` pins) and `mypy==1.18.2` (the pre-commit hook pins mypy v1.13.0, so the two can disagree). |
| `test` | Three smoke-test files on Python 3.9, 3.10, 3.11 and 3.12. Not the full suite. |
| `rust` | The full Rust test suite and clippy, with the toolchain pinned to match `rust/rust-toolchain.toml`. |
| `create-tag` | On `development`, when `src/code_indexer/__init__.py` changed in the pushed commit and the jobs above passed. |
| `create-release` | On `master`, under the same conditions: builds the package and creates the GitHub release `vX.Y.Z` with it (or uploads the build to that release if it already exists). |

A green CI badge means lint, the smoke tests and the Rust suite passed. The full Python suites run locally.

## Version bumps

Versions are `MAJOR.MINOR.HOTFIX`. Normal development bumps MINOR on `development`; HOTFIX is bumped only for a
fix made on `master`.

Files to update:

1. `src/code_indexer/__init__.py` (`__version__`), the source of truth (the package version is read from it).
2. `CHANGELOG.md`: a new `## [X.Y.Z] - YYYY-MM-DD` entry.

Then check that the old version string is not left anywhere it should have changed:
`git grep -nF "<old version>" -- '*.py' '*.md'`. The README badge reads the latest release from GitHub and needs no
edit.

Do not create tags by hand. CI creates the `vX.Y.Z` tag when the pushed `development` commit changed
`__init__.py`; it compares only the last commit (`git diff HEAD~1 HEAD`), so the version-bump commit must be the
tip of the push.

## Project layout

```
src/code_indexer/      Python package (CLI entry point: cli.py, cli_fast_entry.py)
  server/              multi-user server: routers/, mcp/ (tool_docs/), auth/, services/, storage/, web/
  daemon/              daemon mode
  services/, storage/  indexing, embedding providers, vector and chunk storage
  scip/, xray/         SCIP intelligence, X-Ray AST search (Python side)
rust/                  X-Ray engine (xray-core, xray-cli)
prompts/ai_instructions/  content installed by `cidx teach-ai`
docs/                  documentation (map: docs/README.md)
scripts/               lint checks and operator scripts
tests/                 unit/, integration/, e2e/
third_party/hnswlib    hnswlib fork (submodule)
*-automation.sh, lint.sh   test and lint gates
```

## Getting help

- Questions: [GitHub Discussions](https://github.com/LightspeedDMS/code-indexer/discussions)
- Bugs and feature requests: [GitHub Issues](https://github.com/LightspeedDMS/code-indexer/issues)
- Security vulnerabilities: report privately, as described in [SECURITY.md](SECURITY.md).

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
