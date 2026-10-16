"""Side-effect import (Bug #1996) -- MUST be the first import of the root
``tests/conftest.py`` (and of any conftest that may load before it).

Points ``CIDX_SERVER_DATA_DIR`` / ``CIDX_DATA_DIR`` (and an unset
``SYSTEMD_UNIT_DIR``) at a per-session scratch directory BEFORE any server
module is imported, because some server modules fix their data directory at
import time (e.g. the auto-updater's launch and restart-signal paths) and
unit tests well outside ``tests/unit/server`` import server code.  A value
already pointing outside the real ``~/.cidx-server`` (the gate's per-chunk
dir, the e2e harness's client home) is kept.  Importing it again is a no-op.
"""

from __future__ import annotations

import atexit
import os
import shutil

from tests.fixtures.real_server_home_guard import isolate_server_home_env

SESSION_SERVER_HOME = isolate_server_home_env(os.environ)
if SESSION_SERVER_HOME is not None:
    atexit.register(shutil.rmtree, SESSION_SERVER_HOME, True)
