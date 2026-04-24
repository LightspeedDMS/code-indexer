---
name: cs_register_repository
category: admin
required_permission: delegate_open
tl_dr: 'Register a CIDX golden repo alias on Claude Server so it can be used in delegation
  jobs.


  REQUIRED PERMISSION: delegate_open (power_user or admin role)


  BEHAVIOR:

  1.'
---

Register a CIDX golden repo alias on Claude Server so it can be used in delegation jobs.

REQUIRED PERMISSION: delegate_open (power_user or admin role)

BEHAVIOR:
1. Looks up the git URL and branch from CIDX golden repo metadata for the given alias
2. Checks if the repository is already registered on Claude Server (GET /repositories/{alias})
3. If already registered, returns the current cloneStatus without re-registering
4. If not registered (404), calls POST /repositories/register with name, gitUrl, branch, and cidxAware=true
5. Returns success with clone_status indicating current state

CLONE STATUS VALUES:
- cloning: Repository is being cloned (registration just started or in progress)
- completed: Repository is ready for use in delegation jobs
- failed: Clone failed, repository cannot be used
- unknown: Status not yet determined

CIDX INDEXING:
Repositories are registered with cidxAware=true, which means Claude Server will run CIDX semantic and full-text indexing on the repository after cloning. This enables code search capabilities within delegation jobs that use the repository.

ERRORS:
- 'Claude Delegation not configured' -> Delegation configuration not set up
- 'Access denied' -> User does not have delegate_open permission
- 'Missing required parameter: alias' -> alias is required
- 'Alias not found in CIDX golden repos' -> The alias is not a registered CIDX golden repo
- 'Failed to register repository' -> Claude Server communication error
