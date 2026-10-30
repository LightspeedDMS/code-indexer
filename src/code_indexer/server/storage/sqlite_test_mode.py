import os
import sqlite3

TEST_FAST_SQLITE_ENV = "CIDX_TEST_FAST_SQLITE"


def apply_test_mode_synchronous(conn: sqlite3.Connection) -> None:
    # ^ why every connection, not the bootstrap one only; PRAGMA synchronous is per connection
    # ^ hazard OFF on bootstrap only => runtime commits fsync under the write lock; test:test_connection_manager_connection_skips_fsync_in_test_mode
    if os.environ.get(TEST_FAST_SQLITE_ENV) == "1":
        conn.execute("PRAGMA synchronous = OFF")
