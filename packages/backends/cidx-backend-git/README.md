# cidx-backend-git

The local implementation of the git seam: `LocalGitBackend`, which runs git in
this process. Registered as entry point `local` on `code_indexer.git_backend`;
the delegated mode is git-mcp (`plugins/cidx_git_backend`).

The backend builds git argvs; HOW git is spawned (non-interactive SSH
environment, `safe.directory`, credentials, cancellation, retries, the git
invoker) is the core's, passed to the factory as a
`cidx_sdk.seams.git.GitProcessRunner`: `make_git_backend(runner)`.

Depends on `cidx-sdk` only. Its tests run the git seam conformance suite from
`cidx-testkit`.
