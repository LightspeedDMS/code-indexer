Synthetic fixture: Infrastructure repo referencing test-cross-repo-app

This directory simulates an infrastructure repo that deploys the test-cross-repo-app image to dev
and prod environments via tfvars files. Used as a local golden-repo fixture for AC-V4-14 manual
E2E testing of the Story #885 Lifecycle Schema v4 cross-repo discovery (AC-V4-2).

Expected analyzer output: ci.environments = {"dev","prod"}, branch_environment_map omitted or empty.
