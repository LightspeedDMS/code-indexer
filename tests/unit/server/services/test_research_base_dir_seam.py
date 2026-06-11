"""
Bug #1085 — Part A: Research base-dir seam + $HOME isolation.

Proves that ResearchAssistantService folder construction is no longer hard-wired
to ``Path.home()/".cidx-server"/"research"``. When a ``research_base_dir`` is
injected, every session folder (create_session AND get_default_session) lands
under the injected directory, and the real ``~/.cidx-server/research`` is never
touched.

Following TDD: these tests fail until the base-dir seam is implemented.
"""

from pathlib import Path

import pytest

from src.code_indexer.server.storage.database_manager import DatabaseSchema
from src.code_indexer.server.services.research_assistant_service import (
    ResearchAssistantService,
)


@pytest.fixture
def temp_db(tmp_path):
    """Create a temporary research database with the full schema."""
    db_path = str(tmp_path / "test.db")
    schema = DatabaseSchema(db_path=db_path)
    schema.initialize_database()
    return db_path


class TestResearchBaseDirSeam:
    """Bug #1085 Part A: injected base dir is honored by all folder construction."""

    def test_create_session_uses_injected_base_dir(self, temp_db, tmp_path):
        """create_session() must place the session folder under the injected base dir."""
        base = tmp_path / "research_root"
        service = ResearchAssistantService(db_path=temp_db, research_base_dir=base)

        session = service.create_session()

        folder = Path(session["folder_path"])
        assert base in folder.parents, (
            f"Session folder {folder} must be under injected base {base}"
        )
        assert folder.exists(), "Session folder must be created on disk"

    def test_get_default_session_uses_injected_base_dir(self, temp_db, tmp_path):
        """get_default_session() must place the default folder under the injected base dir."""
        base = tmp_path / "research_root"
        service = ResearchAssistantService(db_path=temp_db, research_base_dir=base)

        session = service.get_default_session()

        folder = Path(session["folder_path"])
        assert base in folder.parents or folder == base / "default", (
            f"Default folder {folder} must be under injected base {base}"
        )
        assert folder.exists(), "Default folder must be created on disk"

    def test_real_home_research_untouched_when_base_dir_injected(
        self, temp_db, tmp_path
    ):
        """
        The whole point of bug #1085: with a base dir injected, the real
        ~/.cidx-server/research must NOT gain a new entry from this service.
        """
        real_research = Path.home() / ".cidx-server" / "research"
        before = (
            {p.name for p in real_research.iterdir()}
            if real_research.exists()
            else set()
        )

        base = tmp_path / "research_root"
        service = ResearchAssistantService(db_path=temp_db, research_base_dir=base)
        created = service.create_session()
        service.get_default_session()

        after = (
            {p.name for p in real_research.iterdir()}
            if real_research.exists()
            else set()
        )

        # The created session id must NOT appear under the real home.
        assert created["id"] not in after, (
            "create_session leaked a folder into real ~/.cidx-server/research"
        )
        assert after == before, (
            "Service mutated real ~/.cidx-server/research despite injected base dir"
        )


class TestResearchHomeIsolationFixture:
    """
    Bug #1085 Part A step 2: the autouse conftest fixture must isolate EVERY
    research test from the real ``$HOME``, even tests that construct the service
    the old way (``ResearchAssistantService(db_path=temp_db)`` with no base dir).
    """

    def test_no_base_dir_service_is_isolated_from_real_home(
        self, temp_db, isolated_research_base_dir
    ):
        """
        A service built WITHOUT research_base_dir must still NOT write into the
        real ~/.cidx-server/research; the autouse fixture redirects the default
        to an isolated dir exposed as ``isolated_research_base_dir``.
        """
        real_research = Path.home() / ".cidx-server" / "research"
        before = (
            {p.name for p in real_research.iterdir()}
            if real_research.exists()
            else set()
        )

        service = ResearchAssistantService(db_path=temp_db)
        session = service.create_session()

        folder = Path(session["folder_path"])
        assert isolated_research_base_dir in folder.parents, (
            f"Default-base service folder {folder} must be under the isolated dir "
            f"{isolated_research_base_dir}"
        )

        after = (
            {p.name for p in real_research.iterdir()}
            if real_research.exists()
            else set()
        )
        assert session["id"] not in after, (
            "Default-base service leaked a folder into real ~/.cidx-server/research"
        )
        assert after == before, (
            "Default-base service mutated real ~/.cidx-server/research"
        )
