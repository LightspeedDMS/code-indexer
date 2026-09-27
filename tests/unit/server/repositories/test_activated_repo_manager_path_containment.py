"""
Unit tests for username path-containment defense in
depth inside ActivatedRepoManager.

Every path built from (username, user_alias) --
activation pre-check, the clone destination, metadata read/write, and
get_activated_repo_path -- must be realpath-resolved and verified to lie
strictly under activated_repos_dir/<username>/ before any filesystem
operation, raising ActivatedRepoError otherwise. This is INDEPENDENT of
the account-creation-time validation (UserManager) --
these tests use a real ActivatedRepoManager against real temp directories
and a real LocalCloneBackend (no mocking of the containment logic itself)
so a legacy row with an already-unsafe username is still contained.

Foundation #1 compliant: real filesystem, real git repo, real
LocalCloneBackend (cp --reflink=auto/-a). Only GoldenRepoManager and
BackgroundJobManager are mocked (heavy, unrelated collaborators).
"""

import json
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from code_indexer.server.repositories.activated_repo_manager import (
    ActivatedRepoManager,
    ActivatedRepoError,
)
from code_indexer.server.repositories.golden_repo_manager import GoldenRepo
from code_indexer.server.storage.shared.clone_backend import LocalCloneBackend


@pytest.fixture
def temp_data_dir():
    with tempfile.TemporaryDirectory() as temp_dir:
        yield temp_dir


@pytest.fixture
def golden_repo_source(temp_data_dir):
    """A real, minimal git repository standing in for a golden repo."""
    golden_path = Path(temp_data_dir) / "golden" / "test-repo"
    golden_path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=golden_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=golden_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=golden_path,
        check=True,
        capture_output=True,
    )
    (golden_path / "README.md").write_text("hello\n")
    subprocess.run(
        ["git", "add", "."], cwd=golden_path, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=golden_path,
        check=True,
        capture_output=True,
    )
    return golden_path


@pytest.fixture
def golden_repo_manager_mock(golden_repo_source):
    mock = MagicMock()
    golden_repo = GoldenRepo(
        alias="test-repo",
        repo_url="https://github.com/example/test-repo.git",
        default_branch="master",
        clone_path=str(golden_repo_source),
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    mock.golden_repos = {"test-repo": golden_repo}
    mock.get_golden_repo = MagicMock(return_value=golden_repo)
    mock.get_actual_repo_path = MagicMock(return_value=str(golden_repo_source))
    mock.resource_config = None
    return mock


@pytest.fixture
def background_job_manager_mock():
    mock = MagicMock()
    mock.submit_job.return_value = "job-123"
    return mock


@pytest.fixture
def activated_repo_manager(
    temp_data_dir, golden_repo_manager_mock, background_job_manager_mock
):
    return ActivatedRepoManager(
        data_dir=temp_data_dir,
        golden_repo_manager=golden_repo_manager_mock,
        background_job_manager=background_job_manager_mock,
        clone_backend=LocalCloneBackend(),
    )


class TestActivateRepositoryPrecheckRejectsUnsafeUsername:
    """activate_repository() runs synchronously before any background job
    is submitted -- an unsafe username must be rejected right there,
    for example ('..' + user_alias
    'activated-repos' or 'golden-repos')."""

    def test_dotdot_username_raises_before_job_submission(
        self, activated_repo_manager, background_job_manager_mock, temp_data_dir
    ):
        with pytest.raises(ActivatedRepoError):
            activated_repo_manager.activate_repository(
                username="..",
                golden_repo_alias="test-repo",
                user_alias="activated-repos",
            )
        background_job_manager_mock.submit_job.assert_not_called()
        # No directory escaping activated-repos_dir must have been created.
        activated_repos_dir = Path(temp_data_dir) / "activated-repos"
        assert list(activated_repos_dir.iterdir()) == []

    def test_dotdot_username_with_golden_repos_alias_raises(
        self, activated_repo_manager, background_job_manager_mock
    ):
        """Second traversal variant:
        username='..' + user_alias='golden-repos' would otherwise land
        inside the shared golden-repo tree."""
        with pytest.raises(ActivatedRepoError):
            activated_repo_manager.activate_repository(
                username="..",
                golden_repo_alias="test-repo",
                user_alias="golden-repos",
            )
        background_job_manager_mock.submit_job.assert_not_called()

    def test_username_with_slash_raises(
        self, activated_repo_manager, background_job_manager_mock
    ):
        with pytest.raises(ActivatedRepoError):
            activated_repo_manager.activate_repository(
                username="../../etc",
                golden_repo_alias="test-repo",
                user_alias="passwd",
            )
        background_job_manager_mock.submit_job.assert_not_called()

    def test_normal_username_still_submits_job(
        self, activated_repo_manager, background_job_manager_mock
    ):
        job_id = activated_repo_manager.activate_repository(
            username="alice",
            golden_repo_alias="test-repo",
            user_alias="my-repo",
        )
        assert job_id == "job-123"
        background_job_manager_mock.submit_job.assert_called_once()


class TestDoActivateRepositoryRejectsUnsafeDestination:
    """_do_activate_repository is the background-job body that actually
    clones into the per-user directory.
    Called directly (bypassing the job queue) with a real LocalCloneBackend
    so a genuine `cp -a` merge-into-existing-dir is provably prevented, not
    just asserted against a mock."""

    def test_dotdot_username_raises_and_clones_nothing(
        self, activated_repo_manager, temp_data_dir
    ):
        activated_repos_dir = Path(temp_data_dir) / "activated-repos"
        before = set(activated_repos_dir.iterdir())

        with pytest.raises(ActivatedRepoError):
            activated_repo_manager._do_activate_repository(
                username="..",
                golden_repo_alias="test-repo",
                branch_name="master",
                user_alias="golden-repos",
            )

        after = set(activated_repos_dir.iterdir())
        assert before == after, (
            "no new entry may appear directly under activated_repos_dir "
            "as a result of a traversed username"
        )
        # And nothing was planted one level up either.
        assert not (Path(temp_data_dir) / "golden-repos").exists()

    def test_normal_username_still_clones_successfully(
        self, activated_repo_manager, temp_data_dir, monkeypatch, request
    ):
        # This is the only test in the file that reaches
        # CommitterResolutionService's _create_default_ssh_key_manager(),
        # which resolves server_dir from the process-wide ConfigService
        # singleton -- independent of temp_data_dir above -- and opens
        # <server_dir>/data/cidx_server.db. Point that singleton at the
        # SAME temp directory and pre-initialize a real schema there so
        # this test is hermetic (passes run alone, in any order, in a
        # fresh process), not reliant on another test file having already
        # created a schema at CIDX_SERVER_DATA_DIR (or the real
        # ~/.cidx-server) earlier in the process.
        from code_indexer.server.services.config_service import (
            reset_config_service,
        )
        from code_indexer.server.storage.database_manager import DatabaseSchema

        monkeypatch.setenv("CIDX_SERVER_DATA_DIR", temp_data_dir)
        db_path = Path(temp_data_dir) / "data" / "cidx_server.db"
        DatabaseSchema(str(db_path)).initialize_database()
        reset_config_service()
        request.addfinalizer(reset_config_service)

        result = activated_repo_manager._do_activate_repository(
            username="alice",
            golden_repo_alias="test-repo",
            branch_name="master",
            user_alias="my-repo",
        )
        assert result["success"] is True
        activated_path = Path(temp_data_dir) / "activated-repos" / "alice" / "my-repo"
        assert activated_path.is_dir()
        assert (activated_path / "README.md").exists()


class TestGetActivatedRepoPathRejectsUnsafeInput:
    """get_activated_repo_path must raise for unsafe input instead of silently
    returning an escaping path."""

    def test_dotdot_username_raises(self, activated_repo_manager):
        with pytest.raises(ActivatedRepoError):
            activated_repo_manager.get_activated_repo_path("..", "golden-repos")

    def test_normal_username_returns_contained_path(
        self, activated_repo_manager, temp_data_dir
    ):
        path = activated_repo_manager.get_activated_repo_path("alice", "my-repo")
        expected = str(Path(temp_data_dir) / "activated-repos" / "alice" / "my-repo")
        assert path == expected


class TestMetadataFileHelpersRejectUnsafeInput:
    """_save_metadata_file / _load_metadata_file join username directly
    (the metadata write path). Legacy rows with an
    already-unsafe username stored before account-creation validation existed must
    still be contained here."""

    def test_save_metadata_file_dotdot_username_raises(
        self, activated_repo_manager, temp_data_dir
    ):
        with pytest.raises(ActivatedRepoError):
            activated_repo_manager._save_metadata_file(
                "..", "golden-repos", {"user_alias": "golden-repos"}
            )
        # No metadata file must have been planted one level up.
        assert not (Path(temp_data_dir) / "golden-repos_metadata.json").exists()

    def test_load_metadata_file_dotdot_username_raises(self, activated_repo_manager):
        with pytest.raises(ActivatedRepoError):
            activated_repo_manager._load_metadata_file("..", "golden-repos")

    def test_save_and_load_metadata_file_roundtrip_for_normal_username(
        self, activated_repo_manager
    ):
        activated_repo_manager._save_metadata_file(
            "alice", "my-repo", {"user_alias": "my-repo", "current_branch": "main"}
        )
        loaded = activated_repo_manager._load_metadata_file("alice", "my-repo")
        assert loaded == {"user_alias": "my-repo", "current_branch": "main"}


class TestClonePreservesPreExistingDestination:
    """_reject_existing_directory_destination
    raising FileExistsError must NEVER be treated as a generic clone
    failure. Otherwise the generic `except Exception` clause in
    ActivatedRepoManager._clone_with_copy_on_write would run
    shutil.rmtree(dest_path) on a destination the clone attempt never
    touched -- turning a refusal-to-clone into a deletion of a
    PRE-EXISTING directory -- and the caller's clone-phase handler would then
    run the Bug #1349 orphan-cleanup grace loop (~12s:
    (_ORPHAN_CLEANUP_RETRY_ATTEMPTS - 1) * _ORPHAN_CLEANUP_RETRY_SLEEP_SECONDS)
    against a directory it had itself just deleted, logging a false
    'late-materializing async clone' warning on top of it."""

    def test_preexisting_destination_file_survives_and_raises_fast(
        self, activated_repo_manager, temp_data_dir
    ):
        activated_repos_dir = Path(temp_data_dir) / "activated-repos"
        pre_existing = activated_repos_dir / "alice" / "my-repo"
        pre_existing.mkdir(parents=True)
        sentinel = pre_existing / "do-not-delete.txt"
        sentinel.write_text("precious data\n")

        start = time.monotonic()
        with pytest.raises(ActivatedRepoError):
            activated_repo_manager._do_activate_repository(
                username="alice",
                golden_repo_alias="test-repo",
                branch_name="master",
                user_alias="my-repo",
            )
        elapsed = time.monotonic() - start

        assert sentinel.exists(), (
            "a pre-existing destination directory/file must never be "
            "deleted merely because the clone guard refused to clone "
            "into it"
        )
        assert sentinel.read_text() == "precious data\n"
        # Bug #1349's grace loop alone takes ~12s
        # ((_ORPHAN_CLEANUP_RETRY_ATTEMPTS - 1) * _ORPHAN_CLEANUP_RETRY_SLEEP_SECONDS
        # == 12 * 1.0s) -- a guard refusal must never enter it.
        assert elapsed < 5.0, (
            f"expected a fast fail (<5s), took {elapsed:.1f}s -- indicates "
            "the Bug #1349 orphan-cleanup grace loop ran unnecessarily"
        )


class TestSafeUserDirAcceptsLegacyStyleNames:
    """_safe_user_dir is a STRUCTURAL safety check only (the defense-in-depth
    layer), never a character
    allow-list -- that job belongs to account-creation-time validation.
    Legacy usernames created before that validation
    shipped must still resolve; only the OS-special components and
    embedded separators are rejected."""

    @pytest.mark.parametrize("username", ["-x", "_x", ".hidden", "ab"])
    def test_legacy_style_names_accepted(
        self, activated_repo_manager, temp_data_dir, username
    ):
        user_dir = activated_repo_manager._safe_user_dir(username)
        expected = str(Path(temp_data_dir) / "activated-repos" / username)
        assert user_dir == expected

    @pytest.mark.parametrize("username", [".", "..", "a/b"])
    def test_unsafe_components_rejected(self, activated_repo_manager, username):
        with pytest.raises(ActivatedRepoError):
            activated_repo_manager._safe_user_dir(username)


class TestListReposSkipsUnsafeMetadataEntries:
    """_list_user_repos_fs / _list_all_repos_fs must SKIP (log + continue),
    never crash, when a stored metadata row's user_alias is itself unsafe
    (a legacy row written before account-creation validation, or on-disk
    tampering). The
    skip log message for THIS case must not claim the metadata file is
    'corrupted' -- it is well-formed JSON; only the user_alias it names
    failed the path-safety containment check."""

    def test_list_user_repos_fs_skips_unsafe_user_alias(
        self, activated_repo_manager, temp_data_dir, caplog
    ):
        user_dir = Path(temp_data_dir) / "activated-repos" / "alice"
        user_dir.mkdir(parents=True)
        (user_dir / "bad_metadata.json").write_text(json.dumps({"user_alias": ".."}))

        with caplog.at_level("WARNING"):
            repos = activated_repo_manager._list_user_repos_fs("alice")

        assert repos == []
        assert any(
            "corrupted" not in record.message.lower()
            for record in caplog.records
            if "bad_metadata.json" in record.message
        ), "a traversal-rejected user_alias is not a 'corrupted' metadata file"

    def test_list_all_repos_fs_skips_unsafe_user_alias(
        self, activated_repo_manager, temp_data_dir, caplog
    ):
        user_dir = Path(temp_data_dir) / "activated-repos" / "alice"
        user_dir.mkdir(parents=True)
        (user_dir / "bad_metadata.json").write_text(json.dumps({"user_alias": ".."}))

        with caplog.at_level("WARNING"):
            all_repos = activated_repo_manager._list_all_repos_fs()

        assert all_repos == []
        assert any(
            "corrupted" not in record.message.lower()
            for record in caplog.records
            if "bad_metadata.json" in record.message
        ), "a traversal-rejected user_alias is not a 'corrupted' metadata file"

    def test_list_user_repos_fs_still_reports_genuinely_corrupted_json(
        self, activated_repo_manager, temp_data_dir, caplog
    ):
        """Regression: distinguishing traversal rejections must not lose
        the genuine-corruption message for actually malformed JSON."""
        user_dir = Path(temp_data_dir) / "activated-repos" / "alice"
        user_dir.mkdir(parents=True)
        (user_dir / "bad_metadata.json").write_text("{not valid json")

        with caplog.at_level("WARNING"):
            repos = activated_repo_manager._list_user_repos_fs("alice")

        assert repos == []
        assert any(
            "corrupted" in record.message.lower()
            for record in caplog.records
            if "bad_metadata.json" in record.message
        ), "genuinely malformed JSON should still be reported as corrupted"
