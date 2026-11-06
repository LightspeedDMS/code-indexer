"""An evicted refresh base never costs a re-embed (EVO-64616).

Under ``index_store_backend: delegated`` a refresh builds its index in place
at ``golden-repos/<repo>`` -- the incremental base -- and publishes it. The
served-index cache may evict that base to free the pod's disk. Before
indexing, the refresh now checks the latest published version out there
(``RefreshScheduler._seed_incremental_base``), so ``cidx index`` builds on top
of it instead of embedding the whole repository again.

Pinned with REAL indexing (the daemon's in-process ``index_blocking``: real
chunking, real FilesystemVectorStore, real progressive metadata) and an
embedding provider that counts what it embeds:

* after an eviction and the seed, an unchanged repository embeds NOTHING and
  one changed file embeds only that file;
* the control: without the seed, the same refresh re-embeds every file;
* the seed is a no-op for the local store, for a base still on the pod and
  when nothing is published (a first build); state it cannot explain is left
  alone; a store failure fails the refresh instead of rebuilding silently.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator, List, Optional, cast
from unittest.mock import patch

import pytest

from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.server.services import index_materializer as im
from code_indexer.server.services import served_index_cache as sic
from code_indexer.utils.index_store import (
    INDEX_DIRNAME,
    DelegatedIndexStore,
    LocalIndexStore,
    index_store_alias,
)
from tests.unit.server.index_store_fakes import FakeArtifacts
from tests.unit.server.services.test_search_service_multimodal_real_infra_1480 import (
    DeterministicFakeEmbeddingProvider,
)

FILES = 5
ALIAS = "repo-global"


class CountingProvider(DeterministicFakeEmbeddingProvider):
    """Counts every text it is asked to embed, through any entry point."""

    def __init__(self) -> None:
        super().__init__()
        self.embedded = 0

    def _get_model_token_limit(self) -> int:
        return 120000

    def get_embedding(self, text: str, *args: Any, **kwargs: Any) -> List[float]:
        self.embedded += 1
        return super().get_embedding(text, *args, **kwargs)

    def get_embeddings_batch(
        self, texts: List[str], *args: Any, **kwargs: Any
    ) -> List[List[float]]:
        self.embedded += len(texts)
        return super().get_embeddings_batch(texts, *args, **kwargs)

    def get_embeddings_batch_with_metadata(
        self, texts: List[str], *args: Any, **kwargs: Any
    ) -> Any:
        self.embedded += len(texts)
        return super().get_embeddings_batch_with_metadata(texts, *args, **kwargs)


@pytest.fixture
def provider() -> Iterator[CountingProvider]:
    counting = CountingProvider()
    with patch(
        "code_indexer.services.embedding_factory.EmbeddingProviderFactory.create",
        return_value=counting,
    ):
        yield counting


@pytest.fixture
def pod(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    source = tmp_path / "data" / "golden-repos" / "repo"
    (source / "src").mkdir(parents=True)
    for i in range(FILES):
        (source / "src" / f"m{i}.py").write_text(f"def f{i}():\n    return {i}\n" * 20)
    (source / INDEX_DIRNAME).mkdir()
    (source / INDEX_DIRNAME / "config.json").write_text(
        '{"codebase_dir": "%s", "embedding_provider": "voyage-ai", '
        '"voyage_api_key": "k", "vector_store": {"provider": "filesystem"}}' % source
    )
    # fix-config rewrites paths that are already this source's own.
    monkeypatch.setattr(im, "run_fix_config", lambda target: None)
    artifacts = FakeArtifacts(tmp_path / "store")
    return SimpleNamespace(
        source=source,
        artifacts=artifacts,
        store=DelegatedIndexStore(artifacts),
        golden=source.parent,
        activated=tmp_path / "data" / "activated-repos",
    )


def _index(source: Path) -> None:
    """One refresh's ``cidx index`` on the base: real, incremental indexing."""
    from code_indexer.daemon.service import CIDXDaemonService

    result = CIDXDaemonService().exposed_index_blocking(str(source), enable_fts=False)
    assert result["status"] == "completed", result


def _publish(pod: SimpleNamespace) -> str:
    """What the refresh does after indexing: publish the base and mark it."""
    version = pod.store.publish(pod.source, pod.store.index_root(pod.source))
    (pod.source / im.CHECKED_OUT_MARKER).write_text(str(version))
    return str(version)


def _cache(pod: SimpleNamespace) -> sic.ServedIndexCache:
    """A served-index cache over this pod, over its limits and long idle."""
    return sic.ServedIndexCache(
        activated_repos_dir=pod.activated,
        golden_repos_dir=pod.golden,
        limits=sic.ServedIndexCacheLimits(
            max_bytes=1,
            low_bytes=1,
            min_free_percent=0,
            target_free_percent=0,
            min_idle_seconds=1,
        ),
        clock=lambda: 4_000_000_000.0,
    )


def _evict(pod: SimpleNamespace) -> None:
    """The base's index state gone from this pod: a pod that never built it,
    or an eviction from before the served-index cache left base clones alone
    (EVO-64616) -- removed exactly as that eviction removed it."""
    sic.purge(sic.retire_pod_state(pod.source))
    assert not (pod.source / INDEX_DIRNAME).exists()


def test_the_served_index_cache_never_evicts_the_base(
    pod: SimpleNamespace, provider: CountingProvider
) -> None:
    """EVO-64616: even marked, idle and over every limit, the base clone is
    the refresh's incremental base, not a cache entry."""
    _index(pod.source)
    _publish(pod)
    assert _cache(pod).run_pass().evicted == []
    assert im.is_served_here(pod.store, pod.source)


def _seed(pod: SimpleNamespace, store: Optional[Any] = None) -> None:
    scheduler = SimpleNamespace(_index_store=store or pod.store)
    RefreshScheduler._seed_incremental_base(
        cast(Any, scheduler), ALIAS, str(pod.source)
    )


def _built_and_evicted(pod: SimpleNamespace, provider: CountingProvider) -> None:
    _index(pod.source)
    assert provider.embedded == FILES  # the first build embeds everything
    _publish(pod)
    _evict(pod)
    provider.embedded = 0


def test_an_evicted_base_is_checked_out_and_nothing_is_embedded_again(
    pod: SimpleNamespace, provider: CountingProvider
) -> None:
    _built_and_evicted(pod, provider)
    _seed(pod)
    assert im.is_served_here(pod.store, pod.source)
    _index(pod.source)
    assert provider.embedded == 0


def test_after_the_seed_only_a_changed_file_is_embedded(
    pod: SimpleNamespace, provider: CountingProvider
) -> None:
    _built_and_evicted(pod, provider)
    _seed(pod)
    (pod.source / "src" / "m0.py").write_text("def changed():\n    return 'new'\n")
    _index(pod.source)
    assert provider.embedded == 1


def test_without_the_seed_the_refresh_would_re_embed_everything(
    pod: SimpleNamespace, provider: CountingProvider
) -> None:
    """The control: what the seed saves."""
    _built_and_evicted(pod, provider)
    (pod.source / INDEX_DIRNAME).mkdir()
    shutil.copy(
        pod.artifacts.root
        / index_store_alias("repo")
        / pod.artifacts.current_version(index_store_alias("repo"))
        / "config.json",
        pod.source / INDEX_DIRNAME / "config.json",
    )
    _index(pod.source)
    assert provider.embedded == FILES


def test_the_refresh_seeds_its_base_before_it_indexes(
    pod: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    order: List[str] = []

    class _Stop(Exception):
        pass

    def indexing(alias: str) -> None:
        order.append("index")  # the first thing the index step reads
        raise _Stop

    scheduler = SimpleNamespace(
        _seed_incremental_base=lambda alias, path: order.append(f"seed {alias}"),
        _initialize_unconfigured_source=lambda alias, path, cancel: order.append(
            f"configure {alias}"
        ),
        registry=SimpleNamespace(get_global_repo=indexing),
    )
    with pytest.raises(_Stop):
        RefreshScheduler._index_source(cast(Any, scheduler), ALIAS, str(pod.source))
    assert order == [f"seed {ALIAS}", f"configure {ALIAS}", "index"]


def test_a_base_still_on_the_pod_is_left_as_it_is(
    pod: SimpleNamespace, caplog: pytest.LogCaptureFixture
) -> None:
    _publish_empty_index(pod)
    (pod.source / INDEX_DIRNAME / "index").mkdir()
    (pod.source / INDEX_DIRNAME / "index" / "mine").write_text("local")
    with caplog.at_level(logging.INFO):
        _seed(pod)
    assert caplog.records == []  # the normal case: quiet, nothing to seed
    assert (pod.source / INDEX_DIRNAME / "index" / "mine").read_text() == "local"
    assert not [c for c in pod.artifacts.calls if c[0] == "checkout"]


def _publish_empty_index(pod: SimpleNamespace) -> None:
    scratch = pod.source.parent.parent / "scratch" / INDEX_DIRNAME
    (scratch / "index").mkdir(parents=True)
    pod.artifacts.publish(index_store_alias("repo"), None, str(scratch))


def test_nothing_published_means_a_first_build(pod: SimpleNamespace) -> None:
    shutil.rmtree(pod.source / INDEX_DIRNAME)
    _seed(pod)
    assert not (pod.source / INDEX_DIRNAME).exists()
    assert pod.artifacts.calls == []


def test_the_local_store_seeds_nothing(pod: SimpleNamespace) -> None:
    shutil.rmtree(pod.source / INDEX_DIRNAME)
    _publish_empty_index(pod)
    pod.artifacts.calls.clear()
    _seed(pod, store=LocalIndexStore())
    RefreshScheduler._seed_incremental_base(
        cast(Any, SimpleNamespace()), ALIAS, str(pod.source)
    )
    assert pod.artifacts.calls == [] and not (pod.source / INDEX_DIRNAME).exists()


def test_index_state_it_cannot_explain_is_left_alone(pod: SimpleNamespace) -> None:
    _publish_empty_index(pod)
    pod.artifacts.calls.clear()
    _seed(pod)  # config.json only, no index, no checkout in progress
    assert pod.artifacts.calls == []
    assert sorted(p.name for p in (pod.source / INDEX_DIRNAME).iterdir()) == [
        "config.json"
    ]


def test_an_evicted_base_the_backfill_wrote_into_is_seeded(
    pod: SimpleNamespace, provider: CountingProvider
) -> None:
    """EVO-64616: after an eviction the SCIP backfill re-creates the index
    root for its cooldown marker and its output. That root holds no index:
    it is seeded like a missing one (leftovers kept), not left alone -- left
    alone, ``cidx index`` ran there with no configuration on every cycle."""
    _built_and_evicted(pod, provider)
    root = pod.source / INDEX_DIRNAME
    (root / "scip").mkdir(parents=True)
    (root / "scip" / "index.scip").write_bytes(b"scip output")
    (root / ".scip_reconcile_attempt").touch()

    _seed(pod)

    assert im.is_served_here(pod.store, pod.source)
    assert (root / "config.json").is_file()
    assert (root / "scip" / "index.scip").read_bytes() == b"scip output"
    assert (root / ".scip_reconcile_attempt").is_file()
    _index(pod.source)
    assert provider.embedded == 0, "seeded: nothing embedded again"


def test_an_interrupted_seed_is_redone(pod: SimpleNamespace) -> None:
    _publish_empty_index(pod)
    (pod.source / im.IN_PROGRESS_MARKER).write_text("v")
    _seed(pod)
    assert im.is_served_here(pod.store, pod.source)


def test_a_store_failure_fails_the_refresh_instead_of_rebuilding(
    pod: SimpleNamespace,
) -> None:
    shutil.rmtree(pod.source / INDEX_DIRNAME)
    _publish_empty_index(pod)
    pod.artifacts.fail_checkout = True
    with pytest.raises(im.PublishedIndexUnavailableError):
        _seed(pod)


def test_a_store_that_cannot_read_versions_fails_loud(pod: SimpleNamespace) -> None:
    shutil.rmtree(pod.source / INDEX_DIRNAME)
    store = SimpleNamespace(is_local=False, index_root=pod.store.index_root)
    with pytest.raises(RuntimeError, match="cannot read a published version"):
        _seed(pod, store=store)
