Synthetic fixture: Source-only application repo (no direct deploy wiring)

This directory simulates an application repo that has NO overlays, tfvars, CI jobs, Helm charts,
or GitHub Actions workflows of its own. Its environment evidence (dev, prod) comes exclusively
from the test-cross-repo-infra repo which references it. Used as a local golden-repo fixture for
AC-V4-14 manual E2E testing of the Story #885 Lifecycle Schema v4 cross-repo discovery (AC-V4-2).

Expected analyzer output: ci.environments = {"dev","prod"} (via cross-repo), branch_environment_map omitted or empty.
