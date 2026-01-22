---
name: exit_maintenance_mode
category: admin
required_permission: manage_users
tl_dr: Exit server maintenance mode (admin only).
---

Exit server maintenance mode (admin only). Resumes accepting new background jobs.

USE CASES:
- Resume normal operations after maintenance
- Re-enable job processing after updates

RETURNS:
- success: Boolean indicating operation result
- maintenance_mode: Current maintenance mode state (should be false)
- message: Status message

PERMISSIONS: Requires manage_users (admin only).