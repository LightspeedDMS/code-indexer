"""Self-tests for the content model behind R-c (content-presence completeness)."""

import hashlib
import json
import subprocess

from content_model import (
    ContentModel,
    FileContent,
    expected_point_ids,
    make_chunk_fn,
    missing_content,
    project_id_for,
)

PROJECT = "repo"


def _fc(text_chunks):
    body = "".join(text_chunks).encode()
    keys = tuple(hashlib.sha256(t.encode()).hexdigest() for t in text_chunks)
    return FileContent(
        file_hash="sha256:" + hashlib.sha256(body).hexdigest(), keys=keys
    )


def _ids(fc):
    return set(expected_point_ids(PROJECT, fc))


def test_expected_point_ids_are_the_indexers_content_addressed_ids():
    fc = _fc(["a", "b"])
    expected = [
        hashlib.md5(f"{PROJECT}_{fc.file_hash}_{i}".encode()).hexdigest()
        for i in range(2)
    ]
    assert expected_point_ids(PROJECT, fc) == expected


def test_duplicate_content_files_pass_when_their_shared_ids_exist():
    original = _fc(["x1", "x2"])
    files = {"a.md": original, "dups/copy-1.md": original, "dups/copy-2.md": original}
    missing = missing_content(
        files, PROJECT, stored_ids=_ids(original), hidden_ids=set()
    )
    assert missing == []


def test_file_with_one_absent_chunk_id_is_missing():
    complete, partial = _fc(["c1"]), _fc(["p1", "p2", "p3"])
    stored = _ids(complete) | set(expected_point_ids(PROJECT, partial)[:2])
    files = {"ok.md": complete, "partial.md": partial}
    assert missing_content(files, PROJECT, stored, hidden_ids=set()) == ["partial.md"]


def test_hidden_rows_do_not_count_as_present():
    fc = _fc(["h1"])
    files = {"b.md": fc}
    assert missing_content(files, PROJECT, _ids(fc), hidden_ids=_ids(fc)) == ["b.md"]


def test_files_without_chunks_are_not_required():
    empty = FileContent(file_hash="sha256:e3b0", keys=())
    assert missing_content({"empty.md": empty}, PROJECT, set(), set()) == []


def test_project_id_is_the_normalised_directory_name(tmp_path):
    repo = tmp_path / "My_Repo"
    repo.mkdir()
    assert project_id_for(repo) == "my-repo"


def test_project_id_uses_the_git_origin_basename(tmp_path):
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "remote",
            "add",
            "origin",
            "https://example.com/org/Example_Service.git",
        ],
        check=True,
    )
    assert project_id_for(repo) == "example-service"


def _init_config(repo):
    (repo / ".code-indexer").mkdir(parents=True)
    (repo / ".code-indexer" / "config.json").write_text(
        json.dumps({"embedding_provider": "voyage-ai"})
    )


def test_chunk_fn_uses_the_real_chunker_and_hashes_chunk_texts(tmp_path):
    from code_indexer.config import ConfigManager
    from code_indexer.indexing.fixed_size_chunker import FixedSizeChunker

    repo = tmp_path / "repo"
    _init_config(repo)
    (repo / "big.md").write_text("line of trace text\n" * 600)
    chunker = FixedSizeChunker(
        ConfigManager(repo / ".code-indexer" / "config.json").load()
    )
    texts = [c["text"] for c in chunker.chunk_file(repo / "big.md", repo)]
    assert len(texts) > 1
    keys = make_chunk_fn(repo)(repo / "big.md")
    assert keys == tuple(hashlib.sha256(t.encode()).hexdigest() for t in texts)


def test_content_model_caches_by_stat_and_rechunks_changed_files(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.md").write_text("alpha")
    (repo / "empty.md").write_text("   \n")
    calls = []

    def chunk_fn(path):
        calls.append(path.name)
        text = path.read_text()
        return (hashlib.sha256(text.encode()).hexdigest(),) if text.strip() else ()

    model = ContentModel(repo, chunk_fn)
    first = model.files(["a.md", "empty.md"])
    assert set(first) == {"a.md"}
    assert first["a.md"].file_hash == "sha256:" + hashlib.sha256(b"alpha").hexdigest()
    model.files(["a.md", "empty.md"])
    assert calls == ["a.md", "empty.md"]
    (repo / "a.md").write_text("alpha, edited")
    second = model.files(["a.md"])
    assert calls[-1] == "a.md"
    assert second["a.md"].keys == (hashlib.sha256(b"alpha, edited").hexdigest(),)
