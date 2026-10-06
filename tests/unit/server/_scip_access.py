"""Caller context for SCIP tests that exercise discovery and query mechanics.

SCIP queries search only repositories the caller may access, so every
SCIPQueryService query needs an access filtering service and the caller's
username (without them the query fails closed). Tests that are not about
access decisions use TEST_USER with GRANT_ALL: a caller granted every
repository. Access decisions themselves are covered by
tests/unit/server/services/test_scip_query_access_scoping.py.
"""

from typing import Any

TEST_USER = "scip-test-user"


class _EveryRepository:
    """Grant set in which every repository name is a member.

    SCIPQueryService.find_scip_files only tests membership
    (``name in accessible_repos``), so membership is all this provides.
    """

    def __contains__(self, item: object) -> bool:
        return True


class _GrantsEveryRepository:
    """Access service test double: the caller is granted every repository."""

    def get_accessible_repos(self, username: str) -> _EveryRepository:
        return _EveryRepository()


# Typed Any: SCIPQueryService's parameter is the concrete AccessFilteringService,
# and this double implements only the one method the service calls.
GRANT_ALL: Any = _GrantsEveryRepository()
