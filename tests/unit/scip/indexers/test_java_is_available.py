"""C11: JavaIndexer.is_available must verify Java, not just Coursier.

The old check only looked for `cs`, so on an image with Coursier but no JDK it
falsely reported available -> got selected for Java repos -> failed at
`cs launch` -> empty index. Now it requires both `cs` and a JVM.
"""

from unittest.mock import patch

from code_indexer.scip.indexers.java import JavaIndexer


def _which(present):
    return lambda name: f"/usr/bin/{name}" if name in present else None


def test_available_when_cs_and_java_present():
    with patch("code_indexer.scip.indexers.java.shutil.which", _which({"cs", "java"})):
        assert JavaIndexer().is_available() is True


def test_unavailable_when_java_missing():
    with (
        patch("code_indexer.scip.indexers.java.shutil.which", _which({"cs"})),
        patch.dict("os.environ", {}, clear=True),
    ):
        assert JavaIndexer().is_available() is False


def test_unavailable_when_coursier_missing():
    with patch("code_indexer.scip.indexers.java.shutil.which", _which({"java"})):
        assert JavaIndexer().is_available() is False


def test_falls_back_to_java_home(tmp_path):
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "java").write_text("")  # fake java
    with (
        patch("code_indexer.scip.indexers.java.shutil.which", _which({"cs"})),
        patch.dict("os.environ", {"JAVA_HOME": str(tmp_path)}, clear=True),
    ):
        assert JavaIndexer().is_available() is True
