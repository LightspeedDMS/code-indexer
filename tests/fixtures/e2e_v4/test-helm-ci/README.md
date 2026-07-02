Synthetic fixture: Helm umbrella chart with GitLab CI release-branch model

This directory simulates a Helm chart repository using GitLab CI with template-{env}-{region} job
naming and a release-branch model. Protected branches: dev, stage, prod. Each branch triggers
the corresponding environment's template jobs. Used as a local golden-repo fixture for AC-V4-14
manual E2E testing of the Story #885 Lifecycle Schema v4 analyzer.

Expected analyzer output: ci.environments = {"dev","stage","prod"},
branch_environment_map = {"dev":"dev","stage":"stage","prod":"prod"}.
