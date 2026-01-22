---
name: check_health
category: admin
required_permission: query_repos
tl_dr: Check CIDX server health and availability.
---

TL;DR: Check CIDX server health and availability. Returns system status, uptime, and service availability indicators. QUICK START: check_health() with no parameters returns health status. USE CASES: (1) Verify server is operational before operations, (2) Debug connection issues, (3) Monitor system availability. OUTPUT: Returns success boolean, status (healthy/degraded/down), uptime, and component health checks (database, indexes, embeddings). TROUBLESHOOTING: If success=false or status='down', indicates server issues that may affect operations. Check error field for diagnostic information. WHEN TO USE: Before starting work session to confirm server availability, or when experiencing unexpected errors. NO PARAMETERS REQUIRED: Health check needs no input arguments. RELATED TOOLS: get_repository_status (check specific repo health), get_all_repositories_status (all repos health), get_job_statistics (background job health).