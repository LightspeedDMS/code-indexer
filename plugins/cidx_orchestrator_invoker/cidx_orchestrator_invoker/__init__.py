"""
Run cidx's description/analysis prompts as agent-orchestrator jobs.

cidx dispatches every description/analysis LLM call -- golden-repo registration
lifecycle, dep-map passes, description refresh, self-monitoring -- through one
CliDispatcher, whose primary invoker normally shells out to the ``claude`` CLI.
In our EKS deployment the CLI is deliberately NOT in the cidx image: LLM
execution belongs inside the orchestrator's sandboxed worker, not in a
long-lived service pod. Without a replacement, every ``lifecycle_registration``
job died with ``ClaudeInvoker: non-zero exit 127`` and ``/health`` degraded.

This module provides an ``IntelligenceCliInvoker`` that satisfies cidx's plugin
seam (``code_indexer.cli_invoker``) by handing the prompt to the orchestrator:

    POST /auth/login              -> JWT (cached, 8h; a 401 forces one re-auth)
    POST /jobs/with-capabilities  -> jobId   (429 honours retryAfterSeconds)
    GET  /jobs/{jobId}            -> poll to terminal -> InvocationResult

The orchestrator runs the agent in its own sandbox, so Claude never runs in the
cidx image. Blocking is fine: the dispatcher already blocks on a subprocess in a
worker thread, so polling occupies the same thread for the same budget.

Configuration (env)
-------------------
CIDX_ORCH_URL        Orchestrator base URL.
                     Default: http://orchestrator.agent-orchestrator.svc.cluster.local:3001
CIDX_ORCH_USER       Orchestrator username (default: admin).
CIDX_ORCH_PASSWORD   Orchestrator password. REQUIRED.
CIDX_ORCH_MODEL      Overrides the model cidx passes (default: cidx's analysis_model).
CIDX_ORCH_POLL_SECS  Poll interval (default: 5).

Enable with:
    CIDX_CLI_INVOKER=cidx_orchestrator_invoker:make_invoker
(installing the package also registers it on the code_indexer.cli_invoker
entry point, so setting the env var is optional.)
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
from typing import Any, Dict, Optional

from code_indexer.server.services.intelligence_cli_invoker import (
    FailureClass,
    InvocationResult,
)

logger = logging.getLogger(__name__)

DEFAULT_URL = "http://orchestrator.agent-orchestrator.svc.cluster.local:3001"
TERMINAL = {"completed", "failed", "cancelled", "error"}
CLI_USED = "orchestrator"


class OrchestratorInvoker:
    """IntelligenceCliInvoker backed by the agent-orchestrator.

    invoke(flow, cwd, prompt, timeout, max_turns) -> InvocationResult.
    Never raises: the dispatcher expects a well-typed result, never an exception.
    """

    def __init__(
        self, analysis_model: str = "opus", soft_timeout_seconds: Optional[int] = None
    ) -> None:
        self._model = os.environ.get("CIDX_ORCH_MODEL") or analysis_model
        self._soft_timeout = soft_timeout_seconds
        # The orchestrator caps concurrent jobs (MAX_CONCURRENT) and answers 429
        # with a retryAfterSeconds hint. cidx fans dep-map/lifecycle work across
        # threads, so serialise submits to avoid stampeding into that cap.
        self._submit_lock = threading.Lock()
        self._token: Optional[str] = None
        self._token_lock = threading.Lock()

    # ── HTTP ─────────────────────────────────────────────────────────────────
    def _base_url(self) -> str:
        return os.environ.get("CIDX_ORCH_URL", DEFAULT_URL).rstrip("/")

    def _request(
        self,
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
            self._base_url() + path, data=data, headers=headers, method=method
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else {}

    def _login(self, force: bool = False) -> str:
        with self._token_lock:
            if self._token and not force:
                return self._token
            password = os.environ.get("CIDX_ORCH_PASSWORD")
            if not password:
                raise _OrchError(
                    "CIDX_ORCH_PASSWORD is not set; cannot authenticate to the orchestrator"
                )
            payload = self._request(
                "/auth/login",
                method="POST",
                body={
                    "username": os.environ.get("CIDX_ORCH_USER", "admin"),
                    "password": password,
                },
            )
            token = payload.get("token")
            if not token:
                raise _OrchError("orchestrator /auth/login returned no token")
            self._token = str(token)
            return self._token

    # ── job lifecycle ────────────────────────────────────────────────────────
    def _submit(self, prompt: str, cwd: str, timeout: int) -> str:
        job_id = str(uuid.uuid4())
        body: Dict[str, Any] = {
            "jobId": job_id,
            "prompt": prompt,
            "options": {
                "agentEngine": "claude",
                "model": self._model,
                "timeout": timeout,
                # Return ONLY the agent's final message, matching plain
                # `claude -p`. Without this the orchestrator returns the full
                # rendered agentic transcript (tool-call trace + answer), and
                # cidx's UnifiedResponseParser -- which takes the first `{` -- hits
                # an early tool-call brace instead of the final structured answer.
                # Requires agent-orchestrator master 2aef293 or later.
                "outputMode": "result",
                # The agent works in the orchestrator's own sandbox; cidx's copy
                # of the repo (cwd) is not reachable from there.
                "gitAware": False,
                "cidxAware": False,
            },
            "metadata": {"source": "cidx", "cwd": cwd},
        }
        deadline = time.monotonic() + timeout
        while True:
            token = self._login()
            try:
                with self._submit_lock:
                    resp = self._request(
                        "/jobs/with-capabilities",
                        method="POST",
                        body=body,
                        token=token,
                    )
                return str(resp.get("jobId") or job_id)
            except urllib.error.HTTPError as exc:
                if exc.code == 401:
                    self._login(force=True)
                    continue
                if exc.code == 429:
                    try:
                        wait = int(
                            json.loads(exc.read().decode("utf-8")).get(
                                "retryAfterSeconds", 10
                            )
                        )
                    except Exception:  # noqa: BLE001 - malformed body -> default
                        wait = 10
                    if time.monotonic() + wait > deadline:
                        raise _OrchError(
                            "orchestrator at capacity (429) and the job budget expired"
                        ) from exc
                    logger.info(
                        "orchestrator at capacity; retrying submit in %ss", wait
                    )
                    time.sleep(wait)
                    continue
                raise _OrchError(
                    f"orchestrator rejected the job: HTTP {exc.code}"
                ) from exc

    def _await(self, job_id: str, deadline: float, poll_secs: int) -> InvocationResult:
        while True:
            if time.monotonic() > deadline:
                return _failure(
                    f"orchestrator job {job_id} did not finish before the timeout",
                    FailureClass.RETRYABLE_ON_SAME,
                )
            try:
                state = self._request(f"/jobs/{job_id}", token=self._login())
            except urllib.error.HTTPError as exc:
                if exc.code == 401:
                    self._login(force=True)
                    continue
                raise _OrchError(
                    f"orchestrator job {job_id}: poll failed HTTP {exc.code}"
                ) from exc

            result = state.get("result") or {}
            status = state.get("status") or result.get("status")
            if status not in TERMINAL:
                time.sleep(poll_secs)
                continue

            output = result.get("output") or ""
            if status == "completed" and int(result.get("exitCode", 0)) == 0:
                return InvocationResult(
                    success=True,
                    output=output,
                    error="",
                    cli_used=CLI_USED,
                    was_failover=False,
                )
            error = result.get("error") or output or "no error detail"
            return _failure(
                f"orchestrator job {job_id} {status}: {error}",
                FailureClass.RETRYABLE_ON_OTHER,
            )

    # ── IntelligenceCliInvoker ───────────────────────────────────────────────
    def invoke(
        self, flow: str, cwd: str, prompt: str, timeout: int, max_turns: int = 0
    ) -> InvocationResult:
        try:
            job_id = self._submit(prompt, cwd, timeout)
            logger.info(
                "orchestrator job %s submitted (flow=%s, cwd=%s)", job_id, flow, cwd
            )
            poll_secs = int(os.environ.get("CIDX_ORCH_POLL_SECS", "5"))
            res = self._await(job_id, time.monotonic() + timeout, poll_secs)
            logger.info(
                "orchestrator job %s finished success=%s (%d chars)",
                job_id,
                res.success,
                len(res.output),
            )
            return res
        except _OrchError as exc:
            logger.error("orchestrator invocation failed: %s", exc)
            return _failure(str(exc), FailureClass.RETRYABLE_ON_SAME)
        except Exception as exc:  # noqa: BLE001 - must never escape into the dispatcher
            logger.exception("orchestrator invocation crashed")
            return _failure(
                f"orchestrator invocation crashed: {exc}",
                FailureClass.RETRYABLE_ON_OTHER,
            )


class _OrchError(RuntimeError):
    """Internal: orchestrator unreachable or refused the job."""


def _failure(msg: str, fclass: FailureClass) -> InvocationResult:
    return InvocationResult(
        success=False,
        output="",
        error=msg,
        cli_used=CLI_USED,
        was_failover=False,
        failure_class=fclass,
    )


def make_invoker(
    analysis_model: str = "opus", soft_timeout_seconds: Optional[int] = None
) -> OrchestratorInvoker:
    """Factory matching cidx's code_indexer.cli_invoker plugin contract."""
    return OrchestratorInvoker(
        analysis_model=analysis_model, soft_timeout_seconds=soft_timeout_seconds
    )
