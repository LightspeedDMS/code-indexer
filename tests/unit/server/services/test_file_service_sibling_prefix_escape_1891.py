"""Bug #1891 round 2 (S1): MCP get_file_content sibling-prefix escape.

``FileListingService.get_file_content`` / ``get_file_content_by_path`` used
a bare string-prefix comparison to confine the resolved path:

    if not str(full_file_path).startswith(str(repo_root)):
        raise PermissionError("Access denied")

With repo root ``.../golden-repos/foo``, a request for
``file_path="../foo-private/secret.txt"`` resolves to
``.../golden-repos/foo-private/secret.txt`` -- and
``str(that).startswith(str(repo_root))`` is TRUE, because the string
"foo-private" starts with the string "foo". The check incorrectly admits
any sibling directory whose name happens to start with the repository
directory's name, leaking its content. Reachable by any non-admin MCP user
via the real ``get_file_content`` handler (notably ``-global`` aliases,
whose target_path is the base golden-repo clone), not just admins.

The fix reuses the shared ``resolve_confined_path()`` primitive (already
used by the REST content branch and the CRUD service since round 1), which
confines via ``Path.relative_to()`` instead of a string prefix and is
immune to this class of bug.

Real filesystem operations throughout (CLAUDE.md Foundation #1) -- no mocks
on the code path under test; only the DB-backed ActivatedRepoManager
resolution is stubbed (matching the established pattern in
test_file_service_non_utf8_bug1449.py).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from code_indexer.server.services.file_service import FileListingService

OUTSIDE_MARKER = "SECRET_SIBLING_SECRET_SEBA_1891"
INSIDE_MARKER = "legitimate inside content"


@pytest.fixture
def sibling_repo_tree(tmp_path):
    """
    tmp_path/
      golden-repos/
        foo/                 <- repository root under test
          src/a.py            <- legitimate nested file
        foo-private/          <- SIBLING directory whose name starts with "foo"
          secret.txt           <- must NEVER be reachable from foo's root
    """
    golden_repos = tmp_path / "golden-repos"
    foo = golden_repos / "foo"
    (foo / "src").mkdir(parents=True)
    (foo / "src" / "a.py").write_text(INSIDE_MARKER)

    foo_private = golden_repos / "foo-private"
    foo_private.mkdir()
    (foo_private / "secret.txt").write_text(OUTSIDE_MARKER)

    return foo, foo_private


class TestGetFileContentBlocksSiblingPrefixEscape:
    def test_get_file_content_blocks_sibling_prefix_escape(self, sibling_repo_tree):
        foo, _foo_private = sibling_repo_tree

        service = FileListingService.__new__(FileListingService)
        arm = MagicMock()
        arm.get_activated_repo_path.return_value = str(foo)
        service.activated_repo_manager = arm  # type: ignore[attr-defined]

        with pytest.raises(PermissionError):
            service.get_file_content(
                repository_alias="foo",
                file_path="../foo-private/secret.txt",
                username="testuser",
            )

    def test_get_file_content_legitimate_nested_path_still_works(
        self, sibling_repo_tree
    ):
        foo, _foo_private = sibling_repo_tree

        service = FileListingService.__new__(FileListingService)
        arm = MagicMock()
        arm.get_activated_repo_path.return_value = str(foo)
        service.activated_repo_manager = arm  # type: ignore[attr-defined]

        result = service.get_file_content(
            repository_alias="foo",
            file_path="src/a.py",
            username="testuser",
            skip_truncation=True,
        )

        assert result["content"] == INSIDE_MARKER

    def test_discrimination_proof_legacy_prefix_check_admits_sibling(
        self, sibling_repo_tree
    ):
        """Proves the OLD string-prefix check genuinely admits the sibling
        directory -- demonstrating the escape test above is discriminating,
        not vacuous."""
        from pathlib import Path

        foo, _foo_private = sibling_repo_tree
        full_file_path = (Path(foo) / "../foo-private/secret.txt").resolve()
        repo_root = Path(foo).resolve()

        assert str(full_file_path).startswith(str(repo_root)), (
            "the legacy bug must reproduce: sibling dir name starting with "
            "the repo dir name passes a bare string-prefix check"
        )


class TestGetFileContentByPathBlocksSiblingPrefixEscape:
    def test_get_file_content_by_path_blocks_sibling_prefix_escape(
        self, sibling_repo_tree
    ):
        foo, _foo_private = sibling_repo_tree
        service = FileListingService()

        with pytest.raises(PermissionError):
            service.get_file_content_by_path(
                repo_path=str(foo),
                file_path="../foo-private/secret.txt",
            )

    def test_get_file_content_by_path_legitimate_nested_path_still_works(
        self, sibling_repo_tree
    ):
        foo, _foo_private = sibling_repo_tree
        service = FileListingService()

        result = service.get_file_content_by_path(
            repo_path=str(foo),
            file_path="src/a.py",
            skip_truncation=True,
        )

        assert result["content"] == INSIDE_MARKER


class TestSecretNeverLeaksIntoErrorMessage:
    def test_permission_error_does_not_contain_secret_content(self, sibling_repo_tree):
        foo, _foo_private = sibling_repo_tree
        service = FileListingService()

        with pytest.raises(PermissionError) as exc_info:
            service.get_file_content_by_path(
                repo_path=str(foo),
                file_path="../foo-private/secret.txt",
            )

        assert OUTSIDE_MARKER not in str(exc_info.value)
