---
name: feedback-issue-routing-public-vs-security
description: "Security issues go to the private security-tracking repo; regular bugs go to the PUBLIC repo's issues -- issue_manager.py defaults to the private dev origin, so target public explicitly"
metadata:
  type: feedback
---

Owner rule (2026-09-30): **security issues** are tracked, for now, in the private
security-tracking repository (the parallel one the project CLAUDE.md names). **Regular
issues** (functional bugs, ops problems, test hygiene) are filed as issues on the
**public** GitHub repository, next to the existing `[BUG]` issues.

**Why:** the security remediation is not public yet, so anything that describes a
vulnerability must stay private. Ordinary bugs belong in the normal public tracker so
they are visible and triaged with the rest of the backlog.

**How to apply:**
- Decide first: could the write-up help someone exploit the product? If so it is a
  security issue: private tracker only, with neutral wording.
- `issue_manager.py` picks the FIRST GitHub remote in `git remote -v`. In this repo that is
  `origin` = the private dev repo, NOT the public one. To file a regular issue publicly,
  run issue_manager from a scratch git dir (under `~/.tmp`) whose only remote is the
  public repo, or otherwise target the public repo explicitly. Never let it default.
- Public issues are sanitized: no hostnames, IPs, node names, credentials, or customer
  data (see the Disclosure Discipline section of the project CLAUDE.md).

Related: [[feedback_no_secrets_in_memory]], [[feedback_bug_report_means_report_not_fix]].
