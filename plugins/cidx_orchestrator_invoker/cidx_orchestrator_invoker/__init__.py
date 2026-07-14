"""
Run cidx's LLM prompts as agent jobs on the agent-orchestrator, not as a local
`claude` subprocess.

Why this exists
---------------
cidx's repo-analysis / golden-repo-registration lifecycle shells out to the
`claude` CLI. In our EKS deployment the CLI is deliberately NOT in the cidx
image: LLM execution belongs inside the orchestrator's sandboxed worker, not in
a long-lived service pod. Without it, every `lifecycle_registration` job died
with `exit 127` and `/health` drifted to degraded.

This plugin satisfies the upstream ``code_indexer.llm_invoker`` contract
(a callable returning ``(ok, output)``) by handing the prompt to the
orchestrator instead:

    POST /auth/login              -> JWT (cached, 8h)
    POST /jobs/with-capabilities  -> jobId
    GET  /jobs/{jobId}            -> poll to terminal, return result.output

The orchestrator clones the repo itself (``repoUrl`` + ``gitAware``), so the
agent never touches cidx's filesystem and Claude never runs in this image.

Blocking is fine: the caller already blocks on a subprocess in a worker thread,
so polling occupies exactly the same thread for the same budget.

Configuration (env)
-------------------
CIDX_ORCH_URL        Orchestrator base URL.
                     Default: http://orchestrator.agent-orchestrator.svc.cluster.local:3001
CIDX_ORCH_USER       Orchestrator username (default: admin).
CIDX_ORCH_PASSWORD   Orchestrator password. REQUIRED.
CIDX_ORCH_MODEL      Model for the agent job (default: opus).
CIDX_ORCH_POLL_SECS  Poll interval (default: 5).

Enable with:
    CIDX_LLM_INVOKER=cidx_orchestrator_invoker:invoke
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_URL = "http://orchestrator.agent-orchestrator.svc.cluster.local:3001"
TERMINAL = {"completed", "failed", "cancelled", "error"}

# The orchestrator caps concurrent jobs (MAX_CONCURRENT) and answers 429 with a
# retryAfterSeconds hint. cidx's LifecycleBatchRunner fans repos out across its
# own thread pool, so without this it would stampede straight into that cap.
_submit_lock = threading.Lock()

_token: Optional[str] = None
_token_lock = threading.Lock()


class OrchestratorError(RuntimeError):
    """The orchestrator could not be reached or refused the job."""


def _base_url() -> str:
    return os.environ.get("CIDX_ORCH_URL", DEFAULT_URL).rstrip("/")


def _request(
    path: str,
    *,
    method: str = "GET",
    body: Optional[Dict[str, Any]] = None,
    token: Optional[str] = None,
    timeout: int = 30,
) -> Dict[str, Any]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        _base_url() + path, data=data, headers=headers, method=method
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
    return json.loads(raw) if raw else {}


def _login(force: bool = False) -> str:
    """Return a JWT, logging in once and caching it (8h lifetime upstream)."""
    global _token
    with _token_lock:
        if _token and not force:
            return _token

        password = os.environ.get("CIDX_ORCH_PASSWORD")
        if not password:
            raise OrchestratorError(
                "CIDX_ORCH_PASSWORD is not set; cannot authenticate to the orchestrator"
            )
        payload = _request(
            "/auth/login",
            method="POST",
            body={
                "username": os.environ.get("CIDX_ORCH_USER", "admin"),
                "password": password,
            },
        )
        token = payload.get("token")
        if not token:
            raise OrchestratorError("orchestrator /auth/login returned no token")
        _token = str(token)
        return _token


def _submit(prompt: str, repo_path: str, timeout_seconds: int) -> str:
    """Submit the job, honouring the orchestrator's 429 backpressure."""
    job_id = str(uuid.uuid4())
    body: Dict[str, Any] = {
        "jobId": job_id,
        "prompt": prompt,
        "options": {
            "agentEngine": "claude",
            "model": os.environ.get("CIDX_ORCH_MODEL", "opus"),
            "timeout": timeout_seconds,
            # The agent works in the orchestrator's own sandbox. cidx's copy of
            # the repo is not reachable from there and must not be assumed.
            "gitAware": False,
            "cidxAware": False,
        },
        "metadata": {"source": "cidx", "repoPath": repo_path},
    }

    deadline = time.monotonic() + timeout_seconds
    while True:
        token = _login()
        try:
            with _submit_lock:
                resp = _request(
                    "/jobs/with-capabilities",
                    method="POST",
                    body=body,
                    token=token,
                )
            submitted = resp.get("jobId") or job_id
            return str(submitted)
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                _login(force=True)  # JWT expired mid-run; re-auth once and retry
                continue
            if exc.code == 429:
                # Over MAX_CONCURRENT. The orchestrator tells us how long to wait.
                try:
                    hint = json.loads(exc.read().decode("utf-8"))
                    wait = int(hint.get("retryAfterSeconds", 10))
                except Exception:  # noqa: BLE001 - malformed body -> default
                    wait = 10
                if time.monotonic() + wait > deadline:
                    raise OrchestratorError(
                        "orchestrator at capacity (429) and the job budget expired"
                    ) from exc
                logger.info(
                    "orchestrator at capacity; retrying submit in %ss", wait
                )
                time.sleep(wait)
                continue
            raise OrchestratorError(
                f"orchestrator rejected the job: HTTP {exc.code}"
            ) from exc


def _await_result(job_id: str, deadline: float, poll_secs: int) -> Tuple[bool, str]:
    while True:
        if time.monotonic() > deadline:
            return False, f"orchestrator job {job_id} did not finish before the timeout"

        try:
            state = _request(f"/jobs/{job_id}", token=_login())
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                _login(force=True)
                continue
            raise OrchestratorError(
                f"orchestrator job {job_id}: poll failed HTTP {exc.code}"
            ) from exc

        result = state.get("result") or {}
        status = state.get("status") or result.get("status")
        if status not in TERMINAL:
            time.sleep(poll_secs)
            continue

        output = result.get("output") or ""
        if status == "completed" and int(result.get("exitCode", 0)) == 0:
            return True, output

        error = result.get("error") or output or "no error detail"
        return False, f"orchestrator job {job_id} {status}: {error}"


def invoke(
    repo_path: str,
    prompt: str,
    shell_timeout_seconds: int,
    outer_timeout_seconds: int,
) -> Tuple[bool, str]:
    """code_indexer.llm_invoker contract -> (ok, output).

    Never raises: every failure is reported as ``(False, message)``, matching the
    Claude CLI wrapper this replaces. A raised exception here would escape into
    a cidx background job and fail the whole registration.
    """
    try:
        job_id = _submit(prompt, repo_path, shell_timeout_seconds)
        logger.info("orchestrator job %s submitted for %s", job_id, repo_path)

        poll_secs = int(os.environ.get("CIDX_ORCH_POLL_SECS", "5"))
        deadline = time.monotonic() + outer_timeout_seconds
        ok, output = _await_result(job_id, deadline, poll_secs)

        logger.info(
            "orchestrator job %s finished ok=%s (%d chars)", job_id, ok, len(output)
        )
        return ok, output
    except OrchestratorError as exc:
        logger.error("orchestrator invocation failed: %s", exc)
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001 - must not escape into the caller's job
        logger.exception("orchestrator invocation crashed")
        return False, f"orchestrator invocation crashed: {exc}"
