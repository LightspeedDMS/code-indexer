"""
ONTAP REST API client for FlexClone volume operations.

Provides create, delete, list, and info operations against an ONTAP cluster
using the storage/volumes API endpoint.  All HTTP calls use the ``requests``
library (lazy-imported) with basic-auth and optional SSL verification.
"""

from __future__ import annotations

import json
import logging
from typing import List, Optional

logger = logging.getLogger(__name__)


class OntapFlexCloneClient:
    """ONTAP REST API client for FlexClone volume operations."""

    #: Name prefix used by default when listing or counting CIDX clones.
    DEFAULT_CLONE_PREFIX = "cidx_clone_"

    def __init__(
        self,
        endpoint: str,
        username: str,
        password: str,
        svm_name: str,
        parent_volume: str,
        verify_ssl: bool = False,
    ) -> None:
        """
        Initialise the client.

        Parameters
        ----------
        endpoint:
            Base URL or hostname of the ONTAP cluster, e.g. ``"100.99.60.248"``.
            The scheme (``https://``) is prepended automatically when missing.
        username:
            ONTAP admin username, e.g. ``"fsxadmin"``.
        password:
            ONTAP admin password.
        svm_name:
            Storage Virtual Machine name, e.g. ``"sebaV2"``.
        parent_volume:
            Name of the parent FlexVol from which clones are created,
            e.g. ``"seba_vol1"``.
        verify_ssl:
            Whether to verify the ONTAP server's TLS certificate.  Defaults to
            ``False`` because ONTAP clusters typically use self-signed certs.
        """
        # Normalise endpoint to always have a scheme so URL construction works.
        if not endpoint.startswith(("https://", "http://")):
            endpoint = f"https://{endpoint}"
        self._endpoint = endpoint.rstrip("/")
        self._auth = (username, password)
        self._svm_name = svm_name
        self._parent_volume = parent_volume
        self._verify_ssl = verify_ssl

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _requests(self):  # type: ignore[return]
        """Lazy-import and return the ``requests`` module."""
        import requests  # noqa: PLC0415

        return requests

    def _url(self, path: str) -> str:
        """Build a full API URL from a relative *path*."""
        return f"{self._endpoint}/api/{path.lstrip('/')}"

    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        """Perform a GET request and return the parsed JSON body."""
        requests = self._requests()
        response = requests.get(
            self._url(path),
            params=params,
            auth=self._auth,
            verify=self._verify_ssl,
        )
        response.raise_for_status()
        return dict(response.json())

    def _post(self, path: str, body: dict) -> dict:
        """Perform a POST request and return the parsed JSON body."""
        requests = self._requests()
        response = requests.post(
            self._url(path),
            json=body,
            auth=self._auth,
            verify=self._verify_ssl,
        )
        response.raise_for_status()
        return dict(response.json())

    def _delete(self, path: str) -> dict:
        """Perform a DELETE request and return the parsed JSON body (may be empty)."""
        requests = self._requests()
        response = requests.delete(
            self._url(path),
            auth=self._auth,
            verify=self._verify_ssl,
        )
        response.raise_for_status()
        # DELETE responses may have an empty body (204) or a JSON body (202).
        try:
            return dict(response.json())
        except json.JSONDecodeError:
            logger.debug(
                "DELETE %s returned empty/non-JSON body — treating as success", path
            )
            return {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create_clone(
        self,
        clone_name: str,
        junction_path: Optional[str] = None,
    ) -> dict:
        """
        Create a FlexClone volume.

        Parameters
        ----------
        clone_name:
            Name for the new clone volume, e.g. ``"cidx_clone_myrepo_1700000000"``.
        junction_path:
            NAS junction path at which the clone is mounted.  Defaults to
            ``"/{clone_name}"`` when *None*.

        Returns
        -------
        dict
            ``{"uuid": str, "name": str, "job_uuid": str}`` — the UUID comes from
            a follow-up GET once the async job completes; ``job_uuid`` is the ONTAP
            async job identifier returned in the 202 response.
        """
        if junction_path is None:
            junction_path = f"/{clone_name}"

        body = {
            "svm": {"name": self._svm_name},
            "name": clone_name,
            "clone": {
                "parent_volume": {"name": self._parent_volume},
                "is_flexclone": True,
            },
            "nas": {"path": junction_path},
        }

        logger.info(
            "Creating FlexClone volume '%s' from parent '%s'",
            clone_name,
            self._parent_volume,
        )

        result = self._post("storage/volumes", body)

        # ONTAP returns 202 with a job UUID for async operations.
        job_uuid = ""
        if "job" in result:
            job_uuid = result["job"].get("uuid", "")

        # Retrieve the new volume's UUID from a follow-up GET.
        volume_info = self.get_volume_info(clone_name) or {}
        uuid = volume_info.get("uuid", "")

        return {"uuid": uuid, "name": clone_name, "job_uuid": job_uuid}

    def delete_clone(self, clone_name: str) -> bool:
        """
        Delete a FlexClone volume by name.

        Idempotent: returns ``True`` even when the volume does not exist.

        The deletion is a two-step process:

        1. GET ``/api/storage/volumes?name={clone_name}`` to obtain the UUID.
        2. DELETE ``/api/storage/volumes/{uuid}`` to remove the volume.

        Parameters
        ----------
        clone_name:
            Name of the clone volume to delete.

        Returns
        -------
        bool
            ``True`` on success or when the volume is already absent.
        """
        logger.info("Deleting FlexClone volume '%s'", clone_name)

        # Step 1: resolve UUID.
        volume_info = self.get_volume_info(clone_name)
        if volume_info is None:
            logger.info(
                "FlexClone volume '%s' not found — treating as already deleted",
                clone_name,
            )
            return True

        uuid = volume_info.get("uuid")
        if not uuid:
            logger.warning(
                "FlexClone volume '%s' has no UUID in response — cannot delete",
                clone_name,
            )
            return False

        # Step 2: delete by UUID.
        self._delete(f"storage/volumes/{uuid}")
        logger.info("Deleted FlexClone volume '%s' (uuid=%s)", clone_name, uuid)
        return True

    def get_volume_info(self, volume_name: str) -> Optional[dict]:
        """
        Retrieve details for a single volume by name.

        Parameters
        ----------
        volume_name:
            Exact volume name to look up.

        Returns
        -------
        dict or None
            Volume record dict (includes ``uuid``, ``name``, etc.) or ``None``
            when the volume does not exist.
        """
        result = self._get(
            "storage/volumes",
            params={"name": volume_name, "fields": "uuid,name,state,svm,clone"},
        )
        records = result.get("records", [])
        if not records:
            return None
        return dict(records[0])

    def list_clones(self, prefix: str = DEFAULT_CLONE_PREFIX) -> List[dict]:
        """
        List all FlexClone volumes whose name starts with *prefix*.

        Parameters
        ----------
        prefix:
            Name prefix used to filter volumes.  Defaults to
            :attr:`DEFAULT_CLONE_PREFIX` (``"cidx_clone_"``).

        Returns
        -------
        list[dict]
            List of volume record dicts, each containing at least ``uuid`` and
            ``name``.
        """
        result = self._get(
            "storage/volumes",
            params={"fields": "uuid,name,state,svm,clone"},
        )
        records = result.get("records", [])
        return [r for r in records if r.get("name", "").startswith(prefix)]

    def get_clone_count(self) -> int:
        """
        Return the number of active CIDX FlexClone volumes of the parent volume.

        Uses :meth:`list_clones` with the default prefix to count all CIDX-managed
        clones currently present on the ONTAP cluster.

        Returns
        -------
        int
            Count of active CIDX clone volumes.
        """
        return len(self.list_clones())
