"""
Bug #1891 sibling-site fix: FileCRUDService (create/edit/delete) joins a
caller-supplied `file_path` onto the activated repository's root
(`repo_path / file_path`) and only checks ".." literal path components and
absolute paths in `_validate_crud_path` -- neither check catches a symlink
that is already present inside the repository (e.g. checked into the
underlying git repo as a tracked symlink) and points outside the
repository root. `edit_file`/`delete_file` then `open()`/read that target
BEFORE any containment check, leaking its SHA-256 hash (and, for
`delete_file`, permitting deletion of the on-disk symlink after reading
through it) to any authenticated user with write-mode access to the
repository -- no admin role required.

This test module proves the gap existed and that the fix (reusing
FileListingService.resolve_confined_path(), the same Bug #1891 helper
used by the REST GET .../files?content=true fix) closes it for all three
CRUD operations, called BEFORE any exists()/open() touches the
filesystem.

Real filesystem operations throughout (CLAUDE.md Foundation #1) -- only
the DI-wired ActivatedRepoManager resolution is stubbed, mirroring the
established `service_with_mock_repo` fixture pattern in
test_file_crud_service.py.
"""

import hashlib
import os
import subprocess
from unittest.mock import Mock

import pytest

from code_indexer.server.services import (
    file_crud_service as file_crud_service_module,
)
from code_indexer.server.services.file_crud_service import FileCRUDService

OUTSIDE_MARKER = "SECRET_OUTSIDE_CONTENT_1891_CRUD"


@pytest.fixture
def repo_with_outside_symlink(tmp_path):
    """
    tmp_path/
      repo/                      <- activated repository root
        existing.py               <- legitimate tracked file
        link_outside -> ../outside.txt   (symlink escaping the repo, as if
                                           checked into the underlying git
                                           repo as a tracked symlink)
      outside.txt                 <- file OUTSIDE the repository root
    """
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    (repo_dir / "existing.py").write_text("print('hello')")

    outside_file = tmp_path / "outside.txt"
    outside_file.write_text(OUTSIDE_MARKER)

    os.symlink(outside_file, repo_dir / "link_outside")

    return repo_dir, outside_file


@pytest.fixture
def service_with_symlink_repo(repo_with_outside_symlink, monkeypatch):
    repo_dir, _outside_file = repo_with_outside_symlink
    service = FileCRUDService()

    mock_manager = Mock()
    mock_manager.get_activated_repo_path.return_value = str(repo_dir)
    mock_manager.user_has_activated_repo.return_value = True

    monkeypatch.setattr(
        file_crud_service_module,
        "_get_activated_repo_manager",
        lambda: mock_manager,
    )

    return service


class TestEditFileBlocksSymlinkEscape:
    def test_edit_file_raises_permission_error_not_hash_mismatch(
        self, service_with_symlink_repo
    ):
        """Pre-fix, this call read through the symlink and raised
        HashMismatchError with the outside file's real SHA-256 hash in the
        message (a content-confirmation oracle). Post-fix it must be
        rejected as a PermissionError before the file is ever opened."""
        service = service_with_symlink_repo

        with pytest.raises(PermissionError) as exc_info:
            service.edit_file(
                repo_alias="test-repo",
                file_path="link_outside",
                old_string="x",
                new_string="y",
                content_hash="0" * 64,
                replace_all=False,
                username="testuser",
            )

        assert OUTSIDE_MARKER not in str(exc_info.value)


class TestDeleteFileBlocksSymlinkEscape:
    def test_delete_file_raises_permission_error_and_does_not_touch_symlink(
        self, service_with_symlink_repo, repo_with_outside_symlink
    ):
        repo_dir, outside_file = repo_with_outside_symlink
        service = service_with_symlink_repo

        with pytest.raises(PermissionError):
            service.delete_file(
                repo_alias="test-repo",
                file_path="link_outside",
                content_hash=None,
                username="testuser",
            )

        # The symlink (and the real outside file it points to) must be
        # untouched -- the confinement check must fire before os.remove().
        assert (repo_dir / "link_outside").is_symlink()
        assert outside_file.read_text() == OUTSIDE_MARKER


class TestCreateFileBlocksSymlinkEscape:
    def test_create_file_raises_permission_error_before_exists_check(
        self, service_with_symlink_repo
    ):
        """The confinement check must fire before the exists() check, so a
        create attempt through an escaping symlink is rejected as
        PermissionError, not FileExistsError (which would also confirm
        the outside target's existence)."""
        service = service_with_symlink_repo

        with pytest.raises(PermissionError):
            service.create_file(
                repo_alias="test-repo",
                file_path="link_outside",
                content="malicious",
                username="testuser",
            )


class TestMalformedPathRaisesPermissionErrorNotRawException:
    def test_edit_file_nul_byte_path_raises_permission_error(
        self, service_with_symlink_repo
    ):
        """Bug #1891 round 2 (S4): a malformed file_path (embedded NUL
        byte) must map to PermissionError, not let a raw ValueError from
        Path.resolve() escape uncaught."""
        service = service_with_symlink_repo

        with pytest.raises(PermissionError):
            service.edit_file(
                repo_alias="test-repo",
                file_path="existing.py\x00evil",
                old_string="x",
                new_string="y",
                content_hash="0" * 64,
                replace_all=False,
                username="testuser",
            )


class TestLegitimateSymlinkInsideRepoStillWorks:
    def test_edit_file_through_symlink_pointing_inside_repo_still_works(
        self, repo_with_outside_symlink, monkeypatch
    ):
        repo_dir, _outside_file = repo_with_outside_symlink
        (repo_dir / "target.py").write_text("hello=1")
        os.symlink(repo_dir / "target.py", repo_dir / "link_inside")

        service = FileCRUDService()
        mock_manager = Mock()
        mock_manager.get_activated_repo_path.return_value = str(repo_dir)
        mock_manager.user_has_activated_repo.return_value = True
        monkeypatch.setattr(
            file_crud_service_module,
            "_get_activated_repo_manager",
            lambda: mock_manager,
        )

        import hashlib

        content_hash = hashlib.sha256(b"hello=1").hexdigest()
        result = service.edit_file(
            repo_alias="test-repo",
            file_path="link_inside",
            old_string="hello=1",
            new_string="hello=2",
            content_hash=content_hash,
            replace_all=False,
            username="testuser",
        )

        assert result["success"] is True
        # full_path is resolve_confined_path()'s RESOLVED target
        # (repo_dir/target.py), NOT the lexical "link_inside" path -- so
        # _atomic_write_file's temp-file-then-rename writes THROUGH the
        # symlink to target.py itself. The symlink node "link_inside" is
        # never touched (it still exists as a symlink pointing at
        # target.py). The point of this test is that a same-repo symlink
        # is not rejected by the confinement check, AND that editing
        # through it legitimately writes the resolved target rather than
        # replacing the symlink.
        assert (repo_dir / "link_inside").is_symlink()
        assert (repo_dir / "target.py").read_text() == "hello=2"
        assert (repo_dir / "link_inside").read_text() == "hello=2"


class TestDeleteFileThroughInRepoSymlinkRemovesOnlyLink:
    def test_delete_removes_only_the_symlink_not_its_target(
        self, repo_with_outside_symlink, monkeypatch
    ):
        """Bug #1891 round 2 (S3): delete_file must operate on the
        LEXICAL (unresolved) in-repo path for the actual removal, using
        the RESOLVED path only for the containment decision + hash
        validation. Pre-fix, delete_file called os.remove() on
        resolve_confined_path()'s resolved target -- deleting the
        symlink's TARGET file and leaving the symlink itself dangling,
        instead of removing the symlink the caller actually named."""
        repo_dir, _outside_file = repo_with_outside_symlink
        (repo_dir / "target.py").write_text("keep me")
        os.symlink(repo_dir / "target.py", repo_dir / "link_inside")

        service = FileCRUDService()
        mock_manager = Mock()
        mock_manager.get_activated_repo_path.return_value = str(repo_dir)
        mock_manager.user_has_activated_repo.return_value = True
        monkeypatch.setattr(
            file_crud_service_module,
            "_get_activated_repo_manager",
            lambda: mock_manager,
        )

        result = service.delete_file(
            repo_alias="test-repo",
            file_path="link_inside",
            content_hash=None,
            username="testuser",
        )

        assert result["success"] is True
        # The symlink itself is gone (not even as a dangling link)...
        assert not (repo_dir / "link_inside").is_symlink()
        assert not (repo_dir / "link_inside").exists()
        # ...but its target file survives, untouched.
        assert (repo_dir / "target.py").exists()
        assert (repo_dir / "target.py").read_text() == "keep me"


class TestDeleteFileBlocksParentDirectorySymlinkEscape:
    """Bug #1891 round 3 (D1): a symlink in a PARENT path component (not
    the final component) that escapes the repository let os.remove()
    delete a node OUTSIDE the repository, even though
    resolve_confined_path() on the FULL lexical path (which also follows
    the FINAL symlink) happened to land back inside the repository and
    passed containment -- os.remove() itself never follows the final
    component, but the kernel DOES follow symlinks in every intermediate
    (parent) component when the lexical path is finally opened for
    removal. The fix additionally confines the PARENT directory and joins
    the literal final-component name onto that confinement-verified
    parent."""

    def test_exact_repro_link_to_parent_pointer_blocked(self, tmp_path, monkeypatch):
        """
        golden-repos/
          foo/                          <- repo root under test
            src/a.py                     <- real file inside the repo
            link_to_parent -> ..          <- symlink escaping the repo
          pointer -> foo/src/a.py         <- OUTSIDE the repo, but points
                                             at a file INSIDE it

        delete_file("foo", "link_to_parent/pointer", None, "u") must be
        REJECTED, not succeed and delete the outside `pointer` symlink.
        """
        golden_repos = tmp_path / "golden-repos"
        foo = golden_repos / "foo"
        (foo / "src").mkdir(parents=True)
        (foo / "src" / "a.py").write_text("real content")
        os.symlink("..", foo / "link_to_parent", target_is_directory=True)

        pointer = golden_repos / "pointer"
        os.symlink(foo / "src" / "a.py", pointer)

        service = FileCRUDService()
        mock_manager = Mock()
        mock_manager.get_activated_repo_path.return_value = str(foo)
        mock_manager.user_has_activated_repo.return_value = True
        monkeypatch.setattr(
            file_crud_service_module,
            "_get_activated_repo_manager",
            lambda: mock_manager,
        )

        with pytest.raises(PermissionError):
            service.delete_file(
                repo_alias="foo",
                file_path="link_to_parent/pointer",
                content_hash=None,
                username="testuser",
            )

        # The OUTSIDE symlink must survive the rejected attempt, untouched.
        assert pointer.is_symlink()
        assert (foo / "src" / "a.py").exists()
        assert (foo / "src" / "a.py").read_text() == "real content"

    def test_foo_alias_ptr_variant_outside_symlink_to_repo_root_blocked(
        self, tmp_path, monkeypatch
    ):
        """Variant: the outside symlink points at the REPO ROOT itself
        (mirroring a real golden-repo alias-pointer symlink, e.g.
        ``foo-global -> foo``) rather than at a file inside it. The
        full-path confinement check alone does not catch this either --
        the fully-resolved candidate equals repo_root exactly, which
        Path.relative_to() accepts trivially (a path is relative to
        itself)."""
        golden_repos = tmp_path / "golden-repos"
        foo = golden_repos / "foo"
        (foo / "src").mkdir(parents=True)
        os.symlink("..", foo / "link_to_parent", target_is_directory=True)

        foo_alias_ptr = golden_repos / "foo_alias_ptr"
        os.symlink(foo, foo_alias_ptr, target_is_directory=True)

        service = FileCRUDService()
        mock_manager = Mock()
        mock_manager.get_activated_repo_path.return_value = str(foo)
        mock_manager.user_has_activated_repo.return_value = True
        monkeypatch.setattr(
            file_crud_service_module,
            "_get_activated_repo_manager",
            lambda: mock_manager,
        )

        with pytest.raises(PermissionError):
            service.delete_file(
                repo_alias="foo",
                file_path="link_to_parent/foo_alias_ptr",
                content_hash=None,
                username="testuser",
            )

        assert foo_alias_ptr.is_symlink()


class TestCreateAndEditFileAlsoConfineParentSymlinkChains:
    """Bug #1891 round 3 (D1) explicitly asks to check create_file/edit_file
    for the same parent-vs-final-component class. Unlike delete_file's
    os.remove() (which never follows the FINAL symlink), create_file and
    edit_file both write via ``full_path`` -- the FULLY resolved
    candidate, which follows every symlink including the final one -- so
    the existing full-path confinement check already governs exactly
    where their write lands. These tests prove there is no equivalent gap
    for a NEW file reached through an escaping parent symlink (the write
    target itself is computed post-resolution and must independently pass
    containment)."""

    def test_create_file_through_escaping_parent_symlink_blocked(
        self, tmp_path, monkeypatch
    ):
        golden_repos = tmp_path / "golden-repos"
        foo = golden_repos / "foo"
        foo.mkdir(parents=True)
        os.symlink("..", foo / "link_to_parent", target_is_directory=True)

        service = FileCRUDService()
        mock_manager = Mock()
        mock_manager.get_activated_repo_path.return_value = str(foo)
        mock_manager.user_has_activated_repo.return_value = True
        monkeypatch.setattr(
            file_crud_service_module,
            "_get_activated_repo_manager",
            lambda: mock_manager,
        )

        with pytest.raises(PermissionError):
            service.create_file(
                repo_alias="foo",
                file_path="link_to_parent/new_outside_file.txt",
                content="malicious",
                username="testuser",
            )

        assert not (golden_repos / "new_outside_file.txt").exists()

    def test_edit_file_through_escaping_parent_symlink_blocked(
        self, tmp_path, monkeypatch
    ):
        golden_repos = tmp_path / "golden-repos"
        foo = golden_repos / "foo"
        foo.mkdir(parents=True)
        os.symlink("..", foo / "link_to_parent", target_is_directory=True)

        outside_target = golden_repos / "outside_target.txt"
        outside_target.write_text("outside content")

        service = FileCRUDService()
        mock_manager = Mock()
        mock_manager.get_activated_repo_path.return_value = str(foo)
        mock_manager.user_has_activated_repo.return_value = True
        monkeypatch.setattr(
            file_crud_service_module,
            "_get_activated_repo_manager",
            lambda: mock_manager,
        )

        with pytest.raises(PermissionError):
            service.edit_file(
                repo_alias="foo",
                file_path="link_to_parent/outside_target.txt",
                old_string="outside",
                new_string="pwned",
                content_hash="0" * 64,
                replace_all=False,
                username="testuser",
            )

        assert outside_target.read_text() == "outside content"


def _wire_mock_manager(service, repo_dir, monkeypatch):
    mock_manager = Mock()
    mock_manager.get_activated_repo_path.return_value = str(repo_dir)
    mock_manager.user_has_activated_repo.return_value = True
    monkeypatch.setattr(
        file_crud_service_module,
        "_get_activated_repo_manager",
        lambda: mock_manager,
    )
    return mock_manager


@pytest.fixture
def repo_with_gitlink_symlink(tmp_path):
    """
    tmp_path/
      repo/                     <- activated repository root (a real git repo)
        .git/{config,HEAD,hooks/...}   <- real git internals
        gitlink -> .git                 <- symlink INSIDE the repo pointing
                                            at .git, COMMITTED so it is a
                                            genuinely tracked symlink that
                                            survives `git clone` (matching
                                            the attack described in
                                            Bug #1891).
    """
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    subprocess.run(
        ["git", "init", "-q", str(repo_dir)],
        check=True,
        capture_output=True,
    )
    os.symlink(".git", repo_dir / "gitlink")
    subprocess.run(
        ["git", "add", "gitlink"],
        cwd=str(repo_dir),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=bug1891@test.local",
            "-c",
            "user.name=bug1891-test",
            "commit",
            "-q",
            "-m",
            "add gitlink symlink (Bug #1891 repro)",
        ],
        cwd=str(repo_dir),
        check=True,
        capture_output=True,
    )
    return repo_dir


@pytest.fixture
def service_with_gitlink_repo(repo_with_gitlink_symlink, monkeypatch):
    repo_dir = repo_with_gitlink_symlink
    service = FileCRUDService()
    _wire_mock_manager(service, repo_dir, monkeypatch)
    return service


class TestCreateFileGitDirectoryEscapeViaTrackedGitlinkSymlink:
    """Bug #1891 (final SECURITY round): a committed symlink (e.g.
    "gitlink") pointing at ".git" is itself INSIDE the repository root,
    so resolve_confined_path() on "gitlink/hooks/pre-commit" resolves to
    repo_root/.git/hooks/pre-commit -- a path that IS confined to
    repo_root (passes Path.relative_to()) yet lands inside git
    internals. _validate_crud_path()'s literal ".git" component check
    only inspects the CALLER-SUPPLIED path string
    ("gitlink/hooks/pre-commit"), which contains no literal ".git"
    component, so pre-fix this reaches the filesystem. A planted hook
    here executes on the server's next `git commit` (run without
    --no-verify) -- remote code execution for any write-mode user, no
    admin role required."""

    def test_create_file_into_git_hooks_via_gitlink_rejected(
        self, service_with_gitlink_repo, repo_with_gitlink_symlink
    ):
        repo_dir = repo_with_gitlink_symlink
        service = service_with_gitlink_repo

        with pytest.raises(PermissionError):
            service.create_file(
                repo_alias="test-repo",
                file_path="gitlink/hooks/pre-commit",
                content="#!/bin/sh\necho pwned\n",
                username="testuser",
            )

        assert not (repo_dir / ".git" / "hooks" / "pre-commit").exists()


class TestEditFileGitDirectoryEscapeViaTrackedGitlinkSymlink:
    """Same gap as TestCreateFileGitDirectoryEscapeViaTrackedGitlinkSymlink,
    exercised through edit_file(): "gitlink/config" resolves to
    repo_root/.git/config, confined to repo_root, yet still inside git
    internals."""

    def test_edit_file_into_git_config_via_gitlink_rejected(
        self, service_with_gitlink_repo, repo_with_gitlink_symlink
    ):
        repo_dir = repo_with_gitlink_symlink
        service = service_with_gitlink_repo
        config_path = repo_dir / ".git" / "config"
        original_bytes = config_path.read_bytes()
        matching_hash = hashlib.sha256(original_bytes).hexdigest()

        with pytest.raises(PermissionError):
            service.edit_file(
                repo_alias="test-repo",
                file_path="gitlink/config",
                old_string="[core]",
                new_string="[pwned]",
                content_hash=matching_hash,
                replace_all=False,
                username="testuser",
            )

        assert config_path.read_bytes() == original_bytes


class TestDeleteFileGitDirectoryEscapeViaTrackedGitlinkSymlink:
    """Same gap as the create/edit classes above, exercised through
    delete_file() -- both a subpath under gitlink AND gitlink itself
    (whose resolved target IS the ".git" directory)."""

    def test_delete_file_git_head_via_gitlink_rejected(
        self, service_with_gitlink_repo, repo_with_gitlink_symlink
    ):
        repo_dir = repo_with_gitlink_symlink
        service = service_with_gitlink_repo

        with pytest.raises(PermissionError):
            service.delete_file(
                repo_alias="test-repo",
                file_path="gitlink/HEAD",
                content_hash=None,
                username="testuser",
            )

        assert (repo_dir / ".git" / "HEAD").exists()

    def test_delete_file_gitlink_itself_rejected(
        self, service_with_gitlink_repo, repo_with_gitlink_symlink
    ):
        """The resolved target IS the .git directory itself
        (file_path == "gitlink")."""
        repo_dir = repo_with_gitlink_symlink
        service = service_with_gitlink_repo

        with pytest.raises(PermissionError):
            service.delete_file(
                repo_alias="test-repo",
                file_path="gitlink",
                content_hash=None,
                username="testuser",
            )

        assert (repo_dir / ".git").exists()


class TestGitDirectoryEscapeAdditionalVariants:
    """Two edge cases the three classes above do not cover: a
    parent-symlink chain that re-enters the SAME repo's own .git (round
    3's escaping-parent trick, but landing back inside .git instead of
    outside the repo), and ".git" as a plain FILE (git worktrees/
    submodules use a "gitdir: <path>" pointer file instead of a
    directory) reached through a symlink."""

    def test_parent_symlink_chain_landing_back_in_own_git_rejected(
        self, tmp_path, monkeypatch
    ):
        """link_to_parent -> ".." escapes to the golden-repos dir, then
        "<reponame>/.git/HEAD" re-enters THIS repo's own .git -- must
        still be rejected even though the fully-resolved candidate is,
        once again, confined to repo_root."""
        golden_repos = tmp_path / "golden-repos"
        repo_dir = golden_repos / "foo"
        repo_dir.mkdir(parents=True)
        subprocess.run(
            ["git", "init", "-q", str(repo_dir)],
            check=True,
            capture_output=True,
        )
        os.symlink("..", repo_dir / "link_to_parent", target_is_directory=True)

        service = FileCRUDService()
        _wire_mock_manager(service, repo_dir, monkeypatch)

        with pytest.raises(PermissionError):
            service.delete_file(
                repo_alias="foo",
                file_path="link_to_parent/foo/.git/HEAD",
                content_hash=None,
                username="testuser",
            )

        assert (repo_dir / ".git" / "HEAD").exists()

    def test_edit_file_into_git_as_worktree_gitfile_via_symlink_rejected(
        self, tmp_path, monkeypatch
    ):
        """.git is sometimes a plain FILE, not a directory (git worktrees,
        submodules), containing a "gitdir: <path>" pointer. A symlink
        aimed at that file must be rejected just the same."""
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        git_file = repo_dir / ".git"
        git_file.write_text("gitdir: ../.git/worktrees/foo\n")
        os.symlink(".git", repo_dir / "gitlink")

        service = FileCRUDService()
        _wire_mock_manager(service, repo_dir, monkeypatch)

        original_bytes = git_file.read_bytes()
        matching_hash = hashlib.sha256(original_bytes).hexdigest()

        with pytest.raises(PermissionError):
            service.edit_file(
                repo_alias="test-repo",
                file_path="gitlink",
                old_string="foo",
                new_string="pwned",
                content_hash=matching_hash,
                replace_all=False,
                username="testuser",
            )

        assert git_file.read_bytes() == original_bytes


class TestLegitimateGitNamedPathsStillWorkAlongsideGitlinkGuard:
    """The .git-escape guard must not collaterally block real filenames
    that merely CONTAIN "git" as a substring or live under a directory
    literally named ".github"/"git" -- only an actual ".git" PATH
    COMPONENT (post symlink-resolution) is forbidden."""

    def test_create_file_dotgithub_workflow_still_works(
        self, service_with_gitlink_repo, repo_with_gitlink_symlink
    ):
        repo_dir = repo_with_gitlink_symlink
        service = service_with_gitlink_repo

        result = service.create_file(
            repo_alias="test-repo",
            file_path=".github/workflows/ci.yml",
            content="name: ci",
            username="testuser",
        )

        assert result["success"] is True
        assert (repo_dir / ".github" / "workflows" / "ci.yml").read_text() == "name: ci"

    def test_create_file_dotgitignore_still_works(
        self, service_with_gitlink_repo, repo_with_gitlink_symlink
    ):
        repo_dir = repo_with_gitlink_symlink
        service = service_with_gitlink_repo

        result = service.create_file(
            repo_alias="test-repo",
            file_path=".gitignore",
            content="*.pyc",
            username="testuser",
        )

        assert result["success"] is True
        assert (repo_dir / ".gitignore").read_text() == "*.pyc"

    def test_create_file_docs_git_subdir_still_works(
        self, service_with_gitlink_repo, repo_with_gitlink_symlink
    ):
        repo_dir = repo_with_gitlink_symlink
        service = service_with_gitlink_repo

        result = service.create_file(
            repo_alias="test-repo",
            file_path="docs/git/usage.md",
            content="# git usage",
            username="testuser",
        )

        assert result["success"] is True
        assert (repo_dir / "docs" / "git" / "usage.md").read_text() == ("# git usage")
