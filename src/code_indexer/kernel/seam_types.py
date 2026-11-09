"""Seam Protocols and DTOs, re-exported from ``cidx_sdk.seams``.

The names here moved into the SDK (modular plan phase 5); this module keeps
the kernel spelling working, so existing imports, ``isinstance`` checks and
test patches see the very same objects. New code imports from
``cidx_sdk.seams.<seam>``.

* Content seam -- ``cidx_sdk.seams.content``.
* Ripgrep seam -- ``cidx_sdk.seams.ripgrep``.
* X-Ray execution seam and ``XrayCacheBackend`` -- ``cidx_sdk.seams.xray``.
"""

from __future__ import annotations

from cidx_sdk.seams.content import (
    DEFAULT_PAGE_SIZE as DEFAULT_PAGE_SIZE,
    NOT_FOUND_FETCH_ERRORS as NOT_FOUND_FETCH_ERRORS,
    UNREADABLE_FETCH_ERRORS as UNREADABLE_FETCH_ERRORS,
    ContentBackend as ContentBackend,
    ContentEntry as ContentEntry,
    ContentPage as ContentPage,
    ContentSeamSource as ContentSeamSource,
    FetchedFile as FetchedFile,
    ManifestDeadlineExceeded as ManifestDeadlineExceeded,
)
from cidx_sdk.seams.ripgrep import (
    RipgrepBackend as RipgrepBackend,
    RipgrepRun as RipgrepRun,
)
from cidx_sdk.seams.xray import (
    BatchResult as BatchResult,
    XrayCacheBackend as XrayCacheBackend,
    XrayExecutionBackend as XrayExecutionBackend,
    XrayGraphRequest as XrayGraphRequest,
    XrayScanOutcome as XrayScanOutcome,
    XrayScanRequest as XrayScanRequest,
)
