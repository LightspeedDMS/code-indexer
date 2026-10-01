"""Bug #1650 remediation regression guard: FileListingService.activated_repo_manager
setter must keep working after conversion to a lazy property.

Several existing test files assign `service.activated_repo_manager` directly
(e.g. test_file_service_exclusions.py, test_file_service_path_pattern.py,
test_file_service_repo_path_logging.py, test_file_service_non_utf8_bug1449.py,
test_bug1080_file_content_coherence.py), and some construct the service via
`FileListingService.__new__(FileListingService)` (bypassing __init__
entirely) before assigning. Both patterns must be unaffected by converting
`activated_repo_manager` from a plain instance attribute into a lazy
property with a setter.

Patch target note: verified empirically (PYTHONPATH=./src python3 -c ...,
showing call_count == 1 and the mocked instance returned) that
`code_indexer.server.repositories.activated_repo_manager.ActivatedRepoManager`
(the source module) correctly intercepts FileListingService.__init__'s
LOCAL import of ActivatedRepoManager -- file_service.py has no
module-level `ActivatedRepoManager` binding to patch instead.
"""

from unittest.mock import MagicMock, patch


class TestActivatedRepoManagerSetterStillWorksForTestPatching:
    def test_direct_assignment_and_readback(self) -> None:
        from code_indexer.server.services.file_service import FileListingService

        with patch(
            "code_indexer.server.repositories.activated_repo_manager.ActivatedRepoManager"
        ) as mock_arm_cls:
            service = FileListingService()

            sentinel = object()
            service.activated_repo_manager = sentinel
            assert service.activated_repo_manager is sentinel

            service.activated_repo_manager = None
            mock_arm_cls.return_value = "fresh-instance"
            assert service.activated_repo_manager == "fresh-instance"

    def test_new_bypass_then_direct_assignment(self) -> None:
        """Mirrors test_file_service_repo_path_logging.py's pattern:
        FileListingService.__new__(FileListingService) bypasses __init__
        entirely, then the test assigns activated_repo_manager directly."""
        from code_indexer.server.services.file_service import FileListingService

        service = FileListingService.__new__(FileListingService)
        mock_arm = MagicMock()
        service.activated_repo_manager = mock_arm
        assert service.activated_repo_manager is mock_arm
