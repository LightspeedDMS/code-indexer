Synthetic fixture: Source-only repo with no environment evidence

This directory simulates a pure source code repo with no overlays, tfvars, CI jobs, Helm charts,
workflows, or any deploy wiring. Used as a local golden-repo fixture for AC-V4-14 manual E2E
testing of the Story #885 Lifecycle Schema v4 analyzer null-evidence path.

Expected analyzer output: ci.environments = null, branch_environment_map omitted or empty.
