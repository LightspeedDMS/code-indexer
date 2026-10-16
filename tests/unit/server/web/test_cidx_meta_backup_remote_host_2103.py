# ruff: noqa: F811
"""Epic #2103 item 15: the cidx-meta backup config route checks that an SSH
key exists for the remote's host, and reads that host with the canonical
git URL parser (``git_remote_host``), driven through the real app.

Every remote here is refused before any bootstrap runs (no SSH key is
configured in the fresh server), so no network call is made.
"""

from __future__ import annotations

import html

from code_indexer.utils.git_remote_url import git_remote_host
from tests.unit.server.web.test_config_status_codes_1554 import (  # noqa: F401 - fixtures
    _scrape_csrf_token,
    admin_session,
    app_with_db,
    client,
    tmpdir_path,
)

# remote URL -> the host the route must name. One app serves every case.
REMOTES = [
    ("git@git.example.com:owner/repo.git", "git.example.com"),
    ("ssh://deploy@git.example.com:2222/owner/repo.git", "git.example.com"),
    # Previously read as "[2001" (split at the first ':').
    ("git@[2001:db8::1]:owner/repo.git", "[2001:db8::1]"),
    # Previously lower-cased here only; host case is kept everywhere.
    ("ssh://git@Git.Example.com/owner/repo.git", "Git.Example.com"),
]


def test_missing_ssh_key_error_names_the_canonical_host(client, admin_session) -> None:
    # admin_session logs in and sets its cookies on the client.
    for remote_url, host in REMOTES:
        assert git_remote_host(remote_url) == host
        # The config form's csrf token is rotated by every submit.
        page = client.get("/admin/config")
        assert page.status_code == 200

        response = client.post(
            "/admin/config/cidx_meta_backup",
            data={
                "enabled": "true",
                "remote_url": remote_url,
                "csrf_token": _scrape_csrf_token(page.text),
            },
        )

        assert response.status_code == 400, remote_url
        assert f"No SSH key configured for {host}" in html.unescape(response.text), (
            remote_url
        )
