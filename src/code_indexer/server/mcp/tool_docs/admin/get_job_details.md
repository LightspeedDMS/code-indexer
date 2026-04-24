---
name: get_job_details
category: admin
required_permission: query_repos
tl_dr: Get detailed status and progress information for specific background job using
  job_id.
---

TL;DR: Get detailed status and progress information for specific background job using job_id. Monitor long-running operations like repository indexing, refresh, or removal. QUICK START: get_job_details(job_id) where job_id comes from add_golden_repo, remove_golden_repo, refresh_golden_repo, add_golden_repo_index. OUTPUT FIELDS: job_id (UUID), operation_type (what operation), status (pending/running/completed/failed/cancelled), created_at, started_at, completed_at (ISO 8601 timestamps), progress (0-100%), result (operation output if completed), error (diagnostic message if failed), username (who submitted job). USE CASES: (1) Monitor repository indexing progress after add_golden_repo, (2) Check if refresh_golden_repo completed successfully, (3) Diagnose job failures with error messages, (4) Track long-running operations. JOB LIFECYCLE: pending → running → completed/failed. Poll this endpoint periodically until status is completed or failed. TROUBLESHOOTING: Job not found? job_id may be expired (old jobs are cleaned up). Job stuck in running? Check error field for issues, or contact admin for server logs. RELATED TOOLS: get_job_statistics (overview of all jobs), add_golden_repo (returns job_id), refresh_golden_repo (returns job_id), remove_golden_repo (returns job_id), add_golden_repo_index (returns job_id).