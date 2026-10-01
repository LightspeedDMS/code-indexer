"""
Invariant: a per-user directory (activated_repos_dir/<username>/) is
never a server-owned entry directly under activated_repos_dir (currently
only '.trash', the shared trash root used by deactivation and swept by
the startup cleanup -- see deactivation_helpers.py), and every
enumeration of activated_repos_dir skips those entries.

ActivatedRepoManager enforces this independently of the
account-creation-time gate in validation/user_validation.py
(RESERVED_ACTIVATED_REPOS_DIR_NAMES, shared by both), for any stored
username.

Foundation #1 compliant: real ActivatedRepoManager, real temp filesystem.
"""

import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from code_indexer.server.repositories.activated_repo_manager import (
    ActivatedRepoError,
    ActivatedRepoManager,
)
from code_indexer.server.repositories.golden_repo_manager import GoldenRepo
from code_indexer.server.repositories.repository_listing_manager import (
    RepositoryListingManager,
)
from code_indexer.server.storage.shared.clone_backend import LocalCloneBackend
from code_indexer.validation.user_validation import RESERVED_ACTIVATED_REPOS_DIR_NAMES


@pytest.fixture
def temp_data_dir():
    with tempfile.TemporaryDirectory() as temp_dir:
        yield temp_dir


@pytest.fixture
def activated_repo_manager(temp_data_dir):
    return ActivatedRepoManager(
        data_dir=temp_data_dir,
        golden_repo_manager=MagicMock(),
        background_job_manager=MagicMock(),
    )


class TestIsSafePathComponentRejectsReservedNames:
    @pytest.mark.parametrize(
        "reserved_name", sorted(RESERVED_ACTIVATED_REPOS_DIR_NAMES)
    )
    def test_rejects_reserved_name(self, reserved_name):
        assert ActivatedRepoManager._is_safe_path_component(reserved_name) is False

    def test_still_accepts_ordinary_name(self):
        assert ActivatedRepoManager._is_safe_path_component("alice") is True


class TestSafeUserDirRejectsReservedNames:
    @pytest.mark.parametrize(
        "reserved_name", sorted(RESERVED_ACTIVATED_REPOS_DIR_NAMES)
    )
    def test_rejects_reserved_name(self, activated_repo_manager, reserved_name):
        with pytest.raises(ActivatedRepoError):
            activated_repo_manager._safe_user_dir(reserved_name)

    def test_still_accepts_ordinary_username(
        self, activated_repo_manager, temp_data_dir
    ):
        user_dir = activated_repo_manager._safe_user_dir("alice")
        assert user_dir == str(Path(temp_data_dir) / "activated-repos" / "alice")


class TestListAllReposSkipsReservedEntries:
    """The admin listing never treats a reserved entry as a user
    directory: a metadata-shaped file inside '.trash' is skipped outright,
    not read and then refused by the per-file path-safety check."""

    def test_metadata_file_inside_trash_is_skipped_without_warning(
        self, activated_repo_manager, temp_data_dir, caplog
    ):
        import json
        import logging

        activated_dir = Path(temp_data_dir) / "activated-repos"
        alice_dir = activated_dir / "alice"
        alice_dir.mkdir(parents=True)
        (alice_dir / "my-repo_metadata.json").write_text(
            json.dumps({"user_alias": "my-repo", "golden_repo_alias": "g"})
        )
        for reserved_name in RESERVED_ACTIVATED_REPOS_DIR_NAMES:
            reserved_dir = activated_dir / reserved_name
            reserved_dir.mkdir()
            (reserved_dir / "leftover_metadata.json").write_text(
                json.dumps({"user_alias": "leftover", "golden_repo_alias": "g"})
            )

        with caplog.at_level(logging.WARNING):
            repos = activated_repo_manager.list_all_activated_repositories()

        assert [(r["username"], r["user_alias"]) for r in repos] == [
            ("alice", "my-repo")
        ]
        assert not [rec for rec in caplog.records if rec.levelno >= logging.WARNING], [
            rec.getMessage() for rec in caplog.records
        ]


# ---------------------------------------------------------------------------
# Real flow: every enumeration loop over activated_repos_dir skips the
# reserved '.trash' entry and keeps listing every real user.
# ---------------------------------------------------------------------------


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
def real_golden_repo_manager(golden_repo_source):
    mock = MagicMock()
    golden_repo = GoldenRepo(
        alias="test-repo",
        repo_url="https://example.com/owner/test-repo.git",
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
def real_flow_manager(temp_data_dir, real_golden_repo_manager, monkeypatch):
    """Real activation reaches CommitterResolutionService via
    _create_default_ssh_key_manager(), which resolves server_dir from the
    process-wide ConfigService singleton -- independent of the data_dir
    passed to ActivatedRepoManager below -- and opens
    <server_dir>/data/cidx_server.db. Point that singleton at the SAME
    temp directory and pre-initialize a real schema there so this test is
    hermetic (passes run alone, in any order, in a fresh process), not
    reliant on another test file having already created a schema at
    CIDX_SERVER_DATA_DIR (or the real ~/.cidx-server) earlier in the
    process.
    """
    from code_indexer.server.services.config_service import reset_config_service
    from code_indexer.server.storage.database_manager import DatabaseSchema

    monkeypatch.setenv("CIDX_SERVER_DATA_DIR", temp_data_dir)
    db_path = Path(temp_data_dir) / "data" / "cidx_server.db"
    DatabaseSchema(str(db_path)).initialize_database()
    reset_config_service()

    yield ActivatedRepoManager(
        data_dir=temp_data_dir,
        golden_repo_manager=real_golden_repo_manager,
        background_job_manager=MagicMock(),
        clone_backend=LocalCloneBackend(),
    )

    reset_config_service()


class TestEnumerationLoopsSkipTrashAfterRealDeactivation:
    """A real deactivation creates the real '.trash' directory (the
    fd-anchored rename-then-purge path renames the repo INTO .trash; the
    .trash directory itself is never removed, only entries within it).
    Every enumeration loop over activated_repos_dir must keep working for
    every other user afterward."""

    def test_alice_repos_still_found_after_a_deactivation_creates_trash(
        self, real_flow_manager, temp_data_dir, monkeypatch
    ):
        # Activate and immediately deactivate a first repo for alice --
        # this is what actually creates activated_repos_dir/.trash.
        result = real_flow_manager._do_activate_repository(
            username="alice",
            golden_repo_alias="test-repo",
            branch_name="master",
            user_alias="first-repo",
        )
        assert result["success"] is True
        real_flow_manager._do_deactivate_repository(
            username="alice", user_alias="first-repo"
        )
        assert (Path(temp_data_dir) / "activated-repos" / ".trash").is_dir(), (
            "expected deactivation to have created the shared .trash directory"
        )

        # A second, still-active repo for alice must remain fully visible
        # through every enumeration path exercised below.
        result2 = real_flow_manager._do_activate_repository(
            username="alice",
            golden_repo_alias="test-repo",
            branch_name="master",
            user_alias="second-repo",
        )
        assert result2["success"] is True

        # ActivatedRepoManager.find_repos_by_golden_alias
        matches = real_flow_manager.find_repos_by_golden_alias("test-repo")
        assert len(matches) == 1
        assert matches[0]["username"] == "alice"
        assert matches[0]["user_alias"] == "second-repo"

        # ActivatedRepoManager.find_by_canonical_url
        url_matches = real_flow_manager.find_by_canonical_url(
            "example.com/owner/test-repo"
        )
        assert any(
            m["username"] == "alice" and m["user_alias"] == "second-repo"
            for m in url_matches
        ), f"expected alice's second-repo among {url_matches!r}"

        # ActivatedRepoManager.list_all_activated_repositories -- the admin
        # dashboard's data source.
        all_repos = real_flow_manager.list_all_activated_repositories()
        assert any(
            r["username"] == "alice" and r["user_alias"] == "second-repo"
            for r in all_repos
        )

        # web/routes.py::_get_all_activated_repos has its OWN independent
        # os.listdir(activated_repos_dir) loop -- it does NOT build on
        # list_all_activated_repositories above, so that assertion alone
        # does not cover it. Call it directly (monkeypatched to resolve
        # real_flow_manager instead of FastAPI app.state) so its own
        # reserved-entry skip is exercised.
        #
        # This real call reaches DashboardService.get_temporal_index_status,
        # which imports `activated_repo_manager` from code_indexer.server.app
        # -- which may lazily initialize the app and point the TokenBlacklist
        # and ElevatedSessionManager SQLite paths at this test's temp dir.
        # Invariant: this test restores the singleton state it replaced, so
        # later tests see the original values. monkeypatch.setattr(obj,
        # attr, obj.attr) records each current value and restores it at
        # teardown.
        import code_indexer.server.web.routes as routes
        from code_indexer.server.app import get_token_blacklist
        from code_indexer.server.auth.elevated_session_manager import (
            elevated_session_manager,
        )

        token_blacklist = get_token_blacklist()
        monkeypatch.setattr(
            token_blacklist, "_sqlite_db_path", token_blacklist._sqlite_db_path
        )
        monkeypatch.setattr(
            elevated_session_manager, "_db_path", elevated_session_manager._db_path
        )

        monkeypatch.setattr(
            routes, "_get_activated_repo_manager", lambda: real_flow_manager
        )
        admin_repos = routes._get_all_activated_repos()
        assert any(
            r["username"] == "alice" and r["user_alias"] == "second-repo"
            for r in admin_repos
        ), f"expected alice's second-repo in the admin listing, got {admin_repos!r}"
        assert not any(
            r["username"] == "alice" and r["user_alias"] == "first-repo"
            for r in admin_repos
        ), "the deactivated first-repo must not reappear in the admin listing"

        # RepositoryListingManager.get_activation_count also has its OWN
        # independent os.listdir loop, separate from both functions above.
        listing_manager = RepositoryListingManager(
            golden_repo_manager=real_flow_manager.golden_repo_manager,
            activated_repo_manager=real_flow_manager,
        )
        assert listing_manager.get_activation_count("test-repo") == 1
