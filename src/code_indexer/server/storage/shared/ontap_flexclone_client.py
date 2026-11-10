"""ONTAP REST API client for FlexClone volume operations.

Moved with the ``ontap`` clone mode to the local workspace backends
(``cidx_backend_workspace.ontap_flexclone_client``); re-exported so existing
imports keep resolving to the same class.
"""

from __future__ import annotations

from cidx_backend_workspace.ontap_flexclone_client import (
    OntapFlexCloneClient as OntapFlexCloneClient,
)
