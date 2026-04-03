---
name: Admin password must stay admin/admin
description: NEVER change admin password without restoring it — always restore via DB if needed
type: feedback
---

The admin credentials on dev and cluster machines are SACRED.
If a test needs to change the password, ALWAYS restore it afterward
via direct DB update (bypassing password validation).

For credentials, DB connection details, and the restore command, read `.local-testing`.

**Why:** The admin password doesn't meet the server's password validation
requirements (9+ chars, uppercase, digit, special char). Changing it via
the API to a compliant password and failing to change back locks everyone
out. This happened during Bug #538 E2E testing.

**How to apply:** Any test that changes admin password MUST restore it
in the same test, using the DB bypass (see `.local-testing` for the psql command).
Never leave the admin account with a changed password.
