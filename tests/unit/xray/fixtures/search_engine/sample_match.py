"""Fixture file: contains prepareStatement so regex driver matches it."""


def execute_query(conn, sql):
    stmt = conn.prepareStatement(sql)
    return stmt.execute()
