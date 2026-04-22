Synthetic fixture: Kustomize GitOps application

This directory simulates a Kustomize-based GitOps app with overlays for dev, stage, and prod
environments across us-east-2. Used as a local golden-repo fixture for AC-V4-14 manual E2E
testing of the Story #885 Lifecycle Schema v4 analyzer.

Expected analyzer output: ci.environments = {"dev","stage","prod"}, branch_environment_map omitted or empty.
