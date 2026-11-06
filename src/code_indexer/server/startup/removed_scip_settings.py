"""Startup guard for the removed ``scip_query_backend`` setting (EVO-64616).

SCIP query delegation was removed: SCIP queries always read this pod's
``index.scip.db`` files. A ``config.json`` still selecting anything else was
never migrated, so startup stops and says what to do. The core runs this
check whether or not the ``scip`` feature is installed (the setting belongs to
no feature any more), before anything strips ``config.json`` to bootstrap
keys. Moved here from the SCIP backend wiring when SCIP became a feature
(modular phase 4d).
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Mapping, Optional

from code_indexer.server.logging_utils import format_error_log

logger = logging.getLogger(__name__)

#: Logged when startup cannot read config.json for this check.
STARTUP_CONFIG_ERROR_CODE = "APP-GENERAL-65021"


#: The one ``scip_query_backend`` value a leftover config.json may still carry.
LOCAL_SCIP_QUERY_BACKEND = "local"

#: Why ``scip_query_backend: delegated`` is refused, and what to do instead.
DELEGATED_SCIP_QUERY_BACKEND_REMOVED = (
    "scip_query_backend='delegated' was removed: SCIP queries always read this "
    "pod's index.scip.db files. Its DelegatedSCIPQueryService called "
    "sandbox_scip_* tools that sandbox-mcp never had, so no delegated query "
    "ever succeeded. Delete scip_query_backend (and scip_sandbox) from "
    "config.json. With scip_backend='delegated-plugin' and "
    "index_store_backend='delegated', every pod serves SCIP queries from the "
    "index it checks out."
)


class UnsupportedScipQueryBackendError(RuntimeError):
    """``config.json`` sets ``scip_query_backend`` to something other than local.

    Stops server startup (:func:`check_scip_query_backend_at_startup`). The
    only value that ever selected anything else, ``delegated``, never worked,
    and is only there because the deployment was not migrated.
    """


def validate_scip_query_backend_setting(raw_config: Mapping[str, Any]) -> None:
    """Refuse a ``scip_query_backend`` other than ``local`` in *raw_config*.

    *raw_config* is ``config.json`` as written (before ServerConfig drops the
    key). An absent, empty or ``local`` value passes.

    Raises
    ------
    UnsupportedScipQueryBackendError
        ``delegated`` (the message names the migration) or any other value.
    """
    value = raw_config.get("scip_query_backend")
    if not value or value == LOCAL_SCIP_QUERY_BACKEND:
        return
    if value == "delegated":
        raise UnsupportedScipQueryBackendError(DELEGATED_SCIP_QUERY_BACKEND_REMOVED)
    raise UnsupportedScipQueryBackendError(
        f"Unsupported scip_query_backend={value!r}. The setting was removed: SCIP "
        f"queries always read this pod's index.scip.db files. Delete it from "
        f"config.json, or set it to {LOCAL_SCIP_QUERY_BACKEND!r}."
    )


def check_scip_query_backend_at_startup(
    read_raw_config: Callable[[], Optional[Mapping[str, Any]]],
) -> None:
    """Validate ``scip_query_backend`` once at server startup.

    Must run before anything strips ``config.json`` to bootstrap keys (the
    runtime-config initialisation): the strip rewrites the file without the
    removed key, so a later check would never see it and a pod still set to
    ``delegated`` would start without saying so.

    A value other than ``local`` (:class:`UnsupportedScipQueryBackendError`)
    is re-raised and stops startup. Failing to read ``config.json`` is logged
    at ERROR under :data:`STARTUP_CONFIG_ERROR_CODE` and startup
    continues: the config load that follows reports a bad file itself.

    Parameters
    ----------
    read_raw_config:
        Returns ``config.json`` as written, or None when there is none; called
        inside the guarded block so a read failure stays non-fatal.

    Raises
    ------
    UnsupportedScipQueryBackendError
        Propagated from :func:`validate_scip_query_backend_setting`.
    """
    try:
        raw_config = read_raw_config()
        if raw_config is None:
            return
        if not isinstance(raw_config, Mapping):
            raise TypeError(
                f"config.json holds a {type(raw_config).__name__}, not an object"
            )
        validate_scip_query_backend_setting(raw_config)
    except UnsupportedScipQueryBackendError:
        raise
    except Exception as exc:
        logger.error(
            format_error_log(
                STARTUP_CONFIG_ERROR_CODE,
                f"Could not check scip_query_backend in config.json at startup: {exc}",
            ),
            exc_info=True,
        )
