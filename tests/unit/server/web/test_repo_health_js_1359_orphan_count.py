"""
Story #1359 (Epic #1333, S2) AC4: orphan_count exposed on the Web health
surface (repo_health.js).

Per this project's established JS-testing boundary (see
test_health_status_card.py docstring: "JavaScript testing requires a browser
environment"), repo_health.js has no JS unit-test harness -- it is validated
via static content assertions against the real committed file. This test
locks in that both the new multi-collection rendering path and the legacy
single-collection rendering path reference `orphan_count`, so the Web UI
surfaces the zero-tolerance orphan signal alongside min_inbound/max_inbound.
"""

from pathlib import Path

REPO_HEALTH_JS = (
    Path(__file__).parents[4]
    / "src"
    / "code_indexer"
    / "server"
    / "web"
    / "static"
    / "js"
    / "repo_health.js"
)


class TestRepoHealthJsReferencesOrphanCount:
    def test_file_exists(self):
        assert REPO_HEALTH_JS.exists()

    def test_multi_collection_rendering_references_orphan_count(self):
        content = REPO_HEALTH_JS.read_text()
        assert "collection.orphan_count" in content
        assert "Orphan Count" in content

    def test_legacy_single_collection_rendering_references_orphan_count(self):
        content = REPO_HEALTH_JS.read_text()
        assert "healthData.orphan_count" in content
