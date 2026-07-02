Synthetic fixture: Kubernetes/Terraform platform infrastructure

This directory simulates a k8s+terraform platform repo with per-environment module stubs for
dev, stage, and prod. The name matches the CIDX Infrastructure-as-Code category regex
(terraform|k8s). Used as a local golden-repo fixture for AC-V4-14 manual E2E testing of the
Story #885 Lifecycle Schema v4 analyzer.

Expected analyzer output: ci.environments = {"dev","stage","prod"}, branch_environment_map omitted or empty.
