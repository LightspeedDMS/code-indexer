"""
Langfuse REST API client with retry and pagination.

Extracted from langfuse_trace_sync_service.py to reduce file size
and add retry logic for transient HTTP errors.
"""

import logging
import time
from datetime import datetime

import requests
from requests.auth import HTTPBasicAuth

from ..utils.config_manager import LangfusePullProject

logger = logging.getLogger(__name__)


class LangfuseApiClient:
    """HTTP client for Langfuse REST API with retry and pagination."""

    def __init__(self, host: str, creds: LangfusePullProject):
        """
        Initialize API client.

        Args:
            host: Langfuse API host URL
            creds: Project credentials
        """
        self._host = host
        self._auth = HTTPBasicAuth(creds.public_key, creds.secret_key)

    def discover_project(self) -> dict:
        """Discover project name via GET /api/public/projects."""
        response = self._request_with_retry(
            "GET", f"{self._host}/api/public/projects", timeout=15
        )
        projects = response.json().get("data", [])
        if projects:
            return projects[0]
        return {"name": "unknown"}

    def fetch_traces_page(self, page: int, from_time: datetime) -> list:
        """Fetch one page of traces."""
        response = self._request_with_retry(
            "GET",
            f"{self._host}/api/public/traces",
            params={"limit": 100, "page": page, "fromTimestamp": from_time.isoformat()},
            timeout=30,
        )
        return response.json().get("data", [])

    def fetch_observations(self, trace_id: str) -> list:
        """
        Fetch all observations for a trace with pagination.

        Addresses Finding 3: Previously only fetched first 100 observations,
        now paginates through all observations.
        """
        all_observations = []
        page = 1
        while True:
            response = self._request_with_retry(
                "GET",
                f"{self._host}/api/public/observations",
                params={"traceId": trace_id, "limit": 100, "page": page},
                timeout=30,
            )
            data = response.json().get("data", [])
            if not data:
                break
            all_observations.extend(data)
            if len(data) < 100:
                break  # Last page
            page += 1
        return all_observations

    def _request_with_retry(self, method, url, max_retries=3, **kwargs):
        """
        HTTP request with retry for transient errors (429, 502, 503).

        Addresses Finding 4: Add retry logic with exponential backoff
        for rate limiting and server errors.

        Args:
            method: HTTP method
            url: Request URL
            max_retries: Maximum retry attempts
            **kwargs: Additional arguments for requests.request()

        Returns:
            Response object

        Raises:
            requests.HTTPError: On final failure
            requests.ConnectionError: On connection failure after retries
        """
        kwargs["auth"] = self._auth
        for attempt in range(max_retries):
            try:
                response = requests.request(method, url, **kwargs)
                if response.status_code == 429:
                    if attempt < max_retries - 1:
                        # Rate limited - wait with exponential backoff
                        wait = min(2**attempt * 2, 30)
                        logger.warning(
                            f"Rate limited, waiting {wait}s (attempt {attempt + 1})"
                        )
                        time.sleep(wait)
                        continue
                    # Last attempt - fall through to raise_for_status
                if response.status_code in (502, 503) and attempt < max_retries - 1:
                    # Server error - retry with backoff
                    wait = min(2**attempt * 2, 30)
                    logger.warning(
                        f"Server error {response.status_code}, retrying in {wait}s"
                    )
                    time.sleep(wait)
                    continue
                response.raise_for_status()
                return response
            except requests.ConnectionError:
                if attempt < max_retries - 1:
                    wait = min(2**attempt * 2, 30)
                    logger.warning(f"Connection error, retrying in {wait}s")
                    time.sleep(wait)
                else:
                    raise
        # Final attempt - let it raise
        response = requests.request(method, url, **kwargs)
        response.raise_for_status()
        return response
