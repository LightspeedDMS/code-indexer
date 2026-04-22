Synthetic fixture: Terraform workspace with per-environment tfvars

This directory simulates a Terraform workspace with auto.tfvars files for dev, stage, and prod
environments targeting us-east-2. Used as a local golden-repo fixture for AC-V4-14 manual E2E
testing of the Story #885 Lifecycle Schema v4 analyzer.

Expected analyzer output: ci.environments = {"dev","stage","prod"}, branch_environment_map omitted or empty.
