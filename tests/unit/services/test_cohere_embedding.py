"""Tests for Cohere embedding provider and multi-provider infrastructure.

Increment 1: ABC embedding_purpose kwarg and VoyageAI acceptance.
Increment 2: Cohere provider, factory, slug, config, and batch tests.
"""

import os
import pytest
from unittest.mock import patch


@pytest.fixture
def voyage_client():
    """Create a VoyageAIClient with mocked API key for testing."""
    with patch.dict(os.environ, {"VOYAGE_API_KEY": "test-key"}):
        from code_indexer.services.voyage_ai import VoyageAIClient
        from code_indexer.config import VoyageAIConfig

        config = VoyageAIConfig()
        yield VoyageAIClient(config, None)


@pytest.fixture
def cohere_provider():
    """Create a CohereEmbeddingProvider with mocked API key for testing."""
    with patch.dict(os.environ, {"CO_API_KEY": "test-cohere-key"}):
        from code_indexer.services.cohere_embedding import CohereEmbeddingProvider
        from code_indexer.config import CohereConfig

        config = CohereConfig()
        yield CohereEmbeddingProvider(config, None)


class TestABCEmbeddingPurposeParam:
    """The ABC and VoyageAI must accept embedding_purpose as keyword-only arg."""

    def test_voyage_ai_get_embedding_accepts_embedding_purpose_document(
        self, voyage_client
    ):
        """VoyageAI.get_embedding must accept embedding_purpose='document' kwarg."""
        # voyage-code-3 expects 1024 dims — use correct dimensions so _validate_embeddings passes
        stub_emb = [0.1] * 1024
        with patch.object(voyage_client, "_make_sync_request") as mock_req:
            mock_req.return_value = {"data": [{"embedding": stub_emb}]}
            result = voyage_client.get_embedding("hello", embedding_purpose="document")
        assert isinstance(result, list)

    def test_voyage_ai_get_embedding_accepts_embedding_purpose_query(
        self, voyage_client
    ):
        """VoyageAI.get_embedding must accept embedding_purpose='query' kwarg."""
        stub_emb = [0.1] * 1024
        with patch.object(voyage_client, "_make_sync_request") as mock_req:
            mock_req.return_value = {"data": [{"embedding": stub_emb}]}
            result = voyage_client.get_embedding("hello", embedding_purpose="query")
        assert isinstance(result, list)

    def test_voyage_ai_get_embeddings_batch_accepts_embedding_purpose(
        self, voyage_client
    ):
        """VoyageAI.get_embeddings_batch must accept embedding_purpose kwarg."""
        stub_emb = [0.1] * 1024
        with patch.object(voyage_client, "_make_sync_request") as mock_req:
            mock_req.return_value = {
                "data": [{"embedding": stub_emb}, {"embedding": stub_emb}]
            }
            result = voyage_client.get_embeddings_batch(
                ["hello", "world"], embedding_purpose="document"
            )
        assert len(result) == 2

    def test_voyage_ai_get_embedding_with_metadata_accepts_embedding_purpose(
        self, voyage_client
    ):
        """VoyageAI.get_embedding_with_metadata must accept embedding_purpose kwarg."""
        stub_emb = [0.1] * 1024
        with patch.object(voyage_client, "_make_sync_request") as mock_req:
            mock_req.return_value = {"data": [{"embedding": stub_emb}]}
            result = voyage_client.get_embedding_with_metadata(
                "hello", embedding_purpose="document"
            )
        assert result.embedding == stub_emb

    def test_voyage_ai_get_embeddings_batch_with_metadata_accepts_embedding_purpose(
        self, voyage_client
    ):
        """VoyageAI.get_embeddings_batch_with_metadata must accept embedding_purpose."""
        stub_emb = [0.1] * 1024
        with patch.object(voyage_client, "_make_sync_request") as mock_req:
            mock_req.return_value = {"data": [{"embedding": stub_emb}]}
            result = voyage_client.get_embeddings_batch_with_metadata(
                ["hello"], embedding_purpose="document"
            )
        assert len(result.embeddings) == 1

    def test_voyage_ai_health_check_accepts_test_api_as_keyword_arg(
        self, voyage_client
    ):
        """VoyageAI.health_check must accept test_api as keyword arg."""
        result = voyage_client.health_check(test_api=False)
        assert result is True

    def test_voyage_ai_embedding_purpose_does_not_affect_request_payload(
        self, voyage_client
    ):
        """VoyageAI ignores embedding_purpose; payload sent to API must be unchanged."""
        stub_emb = [0.1] * 1024  # voyage-code-3 expects 1024 dims
        with patch.object(voyage_client, "_make_sync_request") as mock_req:
            mock_req.return_value = {"data": [{"embedding": stub_emb}]}
            voyage_client.get_embedding("test", embedding_purpose="query")
            call_args = mock_req.call_args
            positional_args = call_args[0]
            # First positional arg is texts list
            assert positional_args[0] == ["test"]


class TestCohereProviderInstantiation:
    """Cohere provider creation and basic property tests."""

    def test_cohere_provider_creation_with_api_key(self, cohere_provider):
        """CohereEmbeddingProvider must instantiate without error when API key is set."""
        assert cohere_provider is not None

    def test_cohere_provider_raises_without_api_key(self):
        """CohereEmbeddingProvider must raise ValueError when no API key available."""
        from code_indexer.services.cohere_embedding import CohereEmbeddingProvider
        from code_indexer.config import CohereConfig

        with patch.dict(os.environ, {}, clear=True):
            # Ensure CO_API_KEY is not set and config has no key
            env_without_cohere = {
                k: v for k, v in os.environ.items() if k != "CO_API_KEY"
            }
            with patch.dict(os.environ, env_without_cohere, clear=True):
                config = CohereConfig(api_key="")
                with pytest.raises(ValueError, match="Cohere API key required"):
                    CohereEmbeddingProvider(config, None)

    def test_cohere_provider_name(self, cohere_provider):
        """get_provider_name() must return 'cohere'."""
        assert cohere_provider.get_provider_name() == "cohere"

    def test_cohere_current_model(self, cohere_provider):
        """get_current_model() must return 'embed-v4.0'."""
        assert cohere_provider.get_current_model() == "embed-v4.0"

    def test_cohere_supports_batch(self, cohere_provider):
        """supports_batch_processing() must return True."""
        assert cohere_provider.supports_batch_processing() is True

    def test_cohere_model_info(self, cohere_provider):
        """get_model_info() must return dict with correct keys."""
        info = cohere_provider.get_model_info()
        assert isinstance(info, dict)
        assert "name" in info
        assert "provider" in info
        assert "dimensions" in info
        assert "available_dimensions" in info
        assert "max_tokens" in info
        assert "max_texts_per_request" in info
        assert "supports_batch" in info
        assert "api_endpoint" in info
        assert info["provider"] == "cohere"
        assert info["name"] == "embed-v4.0"
        assert info["supports_batch"] is True


class TestCohereEmbeddingPurposeMapping:
    """Cohere embedding_purpose to input_type mapping."""

    def test_map_document_to_search_document(self, cohere_provider):
        """_map_embedding_purpose('document') must return 'search_document'."""
        assert cohere_provider._map_embedding_purpose("document") == "search_document"

    def test_map_query_to_search_query(self, cohere_provider):
        """_map_embedding_purpose('query') must return 'search_query'."""
        assert cohere_provider._map_embedding_purpose("query") == "search_query"


class TestFactoryProviderCreation:
    """EmbeddingProviderFactory.create() and provider discovery."""

    def test_factory_create_voyage_ai(self):
        """Factory.create(config) must return VoyageAIClient by default."""
        from code_indexer.services.embedding_factory import EmbeddingProviderFactory
        from code_indexer.services.voyage_ai import VoyageAIClient
        from code_indexer.config import Config

        with patch.dict(os.environ, {"VOYAGE_API_KEY": "test-key"}):
            config = Config()
            provider = EmbeddingProviderFactory.create(config)
            assert isinstance(provider, VoyageAIClient)

    def test_factory_create_cohere_with_provider_name(self):
        """Factory.create(config, provider_name='cohere') must return CohereEmbeddingProvider."""
        from code_indexer.services.embedding_factory import EmbeddingProviderFactory
        from code_indexer.services.cohere_embedding import CohereEmbeddingProvider
        from code_indexer.config import Config

        with patch.dict(os.environ, {"CO_API_KEY": "test-cohere-key"}):
            config = Config()
            provider = EmbeddingProviderFactory.create(config, provider_name="cohere")
            assert isinstance(provider, CohereEmbeddingProvider)

    def test_factory_get_available_providers(self):
        """get_available_providers() must return both providers."""
        from code_indexer.services.embedding_factory import EmbeddingProviderFactory

        providers = EmbeddingProviderFactory.get_available_providers()
        assert providers == ["voyage-ai", "cohere"]

    def test_factory_get_configured_providers_both(self):
        """get_configured_providers() must return both when both API keys set."""
        from code_indexer.services.embedding_factory import EmbeddingProviderFactory
        from code_indexer.config import Config

        with patch.dict(
            os.environ,
            {"VOYAGE_API_KEY": "test-key", "CO_API_KEY": "test-cohere-key"},
        ):
            config = Config()
            providers = EmbeddingProviderFactory.get_configured_providers(config)
            assert "voyage-ai" in providers
            assert "cohere" in providers

    def test_factory_get_configured_providers_server_config_no_cohere_attr(self):
        """get_configured_providers() must not raise when config has no .cohere attribute.

        ServerConfig (used in server context) has no .cohere sub-object.
        The method must fall back to env var checks instead of crashing.
        Bug #602: AttributeError: 'ServerConfig' object has no attribute 'cohere'
        """
        from unittest.mock import MagicMock
        from code_indexer.services.embedding_factory import EmbeddingProviderFactory

        # Simulate ServerConfig: no .cohere attribute at all
        server_config = MagicMock(spec=[])  # spec=[] means NO attributes

        with patch.dict(
            os.environ,
            {"VOYAGE_API_KEY": "test-key", "CO_API_KEY": "test-cohere-key"},
        ):
            # Must not raise AttributeError
            providers = EmbeddingProviderFactory.get_configured_providers(server_config)
            assert "voyage-ai" in providers
            assert "cohere" in providers


class TestSlugSeparator:
    """generate_model_slug uses double-underscore separator."""

    def test_slug_double_underscore_separator(self):
        """generate_model_slug('voyage-ai', 'voyage-code-3') must use __ separator."""
        from code_indexer.services.embedding_factory import EmbeddingProviderFactory

        slug = EmbeddingProviderFactory.generate_model_slug(
            "voyage-ai", "voyage-code-3"
        )
        assert slug == "voyage_ai__voyage_code_3"

    def test_slug_cohere_model(self):
        """generate_model_slug('cohere', 'embed-v4.0') must produce correct slug."""
        from code_indexer.services.embedding_factory import EmbeddingProviderFactory

        slug = EmbeddingProviderFactory.generate_model_slug("cohere", "embed-v4.0")
        assert slug == "cohere__embed_v4_0"


class TestConfigBackwardCompat:
    """Config backward compatibility for multi-provider support."""

    def test_config_default_provider_is_voyage_ai(self):
        """Config().embedding_provider must default to 'voyage-ai'."""
        from code_indexer.config import Config

        config = Config()
        assert config.embedding_provider == "voyage-ai"

    def test_config_accepts_cohere_provider(self):
        """Config(embedding_provider='cohere') must not raise."""
        from code_indexer.config import Config

        config = Config(embedding_provider="cohere")
        assert config.embedding_provider == "cohere"

    def test_config_has_cohere_field(self):
        """Config().cohere must be a CohereConfig instance."""
        from code_indexer.config import Config, CohereConfig

        config = Config()
        assert isinstance(config.cohere, CohereConfig)

    def test_cohere_config_defaults(self):
        """CohereConfig defaults: model='embed-v4.0', default_dimension=1536."""
        from code_indexer.config import CohereConfig

        cohere_config = CohereConfig()
        assert cohere_config.model == "embed-v4.0"
        assert cohere_config.default_dimension == 1536


class TestCohereBatchSplitting:
    """Batch splitting respects texts_per_request limit."""

    def test_batch_respects_texts_per_request_limit(self, cohere_provider):
        """Sending 200 texts must produce multiple batches of <=96 each."""
        captured_batches = []
        # embed-v4.0 expects 1536 dims — use correct dimensions so _validate_embeddings passes
        _COHERE_EMBED_V4_DIMS = 1536

        def capture_request(texts, input_type="search_document"):
            captured_batches.append(len(texts))
            return {
                "embeddings": {
                    "float": [[0.1] * _COHERE_EMBED_V4_DIMS for _ in range(len(texts))]
                }
            }

        with patch.object(
            cohere_provider, "_make_sync_request", side_effect=capture_request
        ):
            with patch.object(cohere_provider, "_count_tokens", return_value=10):
                texts = [f"text {i}" for i in range(200)]
                result = cohere_provider.get_embeddings_batch(
                    texts, embedding_purpose="document"
                )

        # All 200 texts must produce embeddings
        assert len(result) == 200
        # Must have made multiple batch calls
        assert len(captured_batches) >= 2
        # Each batch must respect Cohere's texts_per_request limit (96 per
        # cohere_models.yaml default for embed-v4.0)
        for batch_size in captured_batches:
            assert batch_size <= 96


class TestCohereRetryLoopBug595Issue1:
    """Bug #595 Issue 1: retry loop re-raises raw exception on last attempt.

    When all retries are exhausted, the loop must fall through to the
    categorized RuntimeError rather than re-raising the raw exception.
    """

    def test_network_error_on_last_attempt_raises_runtime_error(self, cohere_provider):
        """After max_retries exhausted, must raise RuntimeError (not raw exception).

        The RuntimeError must contain the attempt count so callers get
        a categorized, human-readable error instead of a raw ConnectionError.
        Also verifies the side_effect is called max_retries+1 times total.
        """
        import httpx
        from unittest.mock import MagicMock

        connection_error = httpx.ConnectError("DNS resolution failed")
        expected_attempts = cohere_provider.config.max_retries + 1

        mock_client_cls = MagicMock()
        mock_client_instance = mock_client_cls.return_value.__enter__.return_value
        mock_client_instance.post.side_effect = connection_error

        with patch("httpx.Client", mock_client_cls):
            with pytest.raises(RuntimeError, match="Cohere API request failed after"):
                cohere_provider._make_sync_request(["test text"])

        assert mock_client_instance.post.call_count == expected_attempts

    def test_network_error_does_not_propagate_as_raw_exception(self, cohere_provider):
        """Raw network exceptions must NOT escape _make_sync_request directly.

        Only RuntimeError (or ValueError for 401) should reach callers.
        """
        import httpx
        from unittest.mock import MagicMock

        mock_client_cls = MagicMock()
        mock_client_instance = mock_client_cls.return_value.__enter__.return_value
        mock_client_instance.post.side_effect = httpx.TimeoutException(
            "Request timed out"
        )

        with patch("httpx.Client", mock_client_cls):
            with pytest.raises(RuntimeError):
                cohere_provider._make_sync_request(["test"])

    def test_runtime_error_mentions_attempt_count(self, cohere_provider):
        """RuntimeError message must include number of attempts made."""
        import httpx
        from unittest.mock import MagicMock

        mock_client_cls = MagicMock()
        mock_client_instance = mock_client_cls.return_value.__enter__.return_value
        mock_client_instance.post.side_effect = httpx.ConnectError("connection refused")

        with patch("httpx.Client", mock_client_cls):
            with pytest.raises(RuntimeError) as exc_info:
                cohere_provider._make_sync_request(["test"])

        error_msg = str(exc_info.value)
        # Must mention attempt count so caller knows how many retries occurred
        assert (
            "attempt" in error_msg.lower()
            or str(cohere_provider.config.max_retries + 1) in error_msg
        )


class TestCohereErrorHandling401Bug595Issue2:
    """Bug #595 Issue 2: no 401 detection — bad API key gives cryptic error.

    A 401 response must raise ValueError with a clear message about
    the CO_API_KEY environment variable.
    """

    def test_401_response_raises_value_error(self, cohere_provider):
        """HTTP 401 must raise ValueError mentioning CO_API_KEY."""
        import httpx
        from unittest.mock import MagicMock

        mock_client_cls = MagicMock()
        mock_instance = mock_client_cls.return_value.__enter__.return_value
        http_error = httpx.HTTPStatusError(
            "401 Unauthorized",
            request=type("Req", (), {})(),
            response=type(
                "Resp", (), {"status_code": 401, "text": "Unauthorized", "headers": {}}
            )(),
        )
        mock_instance.post.side_effect = http_error

        with patch("httpx.Client", mock_client_cls):
            with pytest.raises(ValueError, match="CO_API_KEY"):
                cohere_provider._make_sync_request(["test"])

    def test_401_error_message_mentions_api_key(self, cohere_provider):
        """ValueError for 401 must explicitly mention the environment variable name."""
        import httpx
        from unittest.mock import MagicMock

        mock_client_cls = MagicMock()
        mock_instance = mock_client_cls.return_value.__enter__.return_value
        http_error = httpx.HTTPStatusError(
            "401 Unauthorized",
            request=type("Req", (), {})(),
            response=type(
                "Resp", (), {"status_code": 401, "text": "Unauthorized", "headers": {}}
            )(),
        )
        mock_instance.post.side_effect = http_error

        with patch("httpx.Client", mock_client_cls):
            with pytest.raises(ValueError) as exc_info:
                cohere_provider._make_sync_request(["test"])

        error_msg = str(exc_info.value)
        assert "CO_API_KEY" in error_msg
        assert "Invalid" in error_msg or "invalid" in error_msg


class TestCohereLoadModelSpecsFallbackBug595Issue3:
    """Bug #595 Issue 3: _load_model_specs() has no exception handling.

    When the YAML file is missing or unreadable, instantiation must not crash.
    Instead, a hardcoded fallback for 'embed-v4.0' must be used.
    """

    def test_missing_yaml_does_not_crash_instantiation(self):
        """CohereEmbeddingProvider must instantiate even when YAML is missing."""
        from code_indexer.services.cohere_embedding import CohereEmbeddingProvider
        from code_indexer.config import CohereConfig

        with patch.dict(os.environ, {"CO_API_KEY": "test-key"}):
            config = CohereConfig()
            with patch("builtins.open", side_effect=FileNotFoundError("No such file")):
                # Must not raise — must use fallback specs
                provider = CohereEmbeddingProvider(config, None)
                assert provider is not None

    def test_fallback_specs_contain_embed_v4_0(self):
        """After YAML load failure, model_specs must contain embed-v4.0 fallback."""
        from code_indexer.services.cohere_embedding import CohereEmbeddingProvider
        from code_indexer.config import CohereConfig

        with patch.dict(os.environ, {"CO_API_KEY": "test-key"}):
            config = CohereConfig()
            with patch("builtins.open", side_effect=FileNotFoundError("No such file")):
                provider = CohereEmbeddingProvider(config, None)
                # model_specs must have cohere_models with embed-v4.0 key
                assert "cohere_models" in provider.model_specs
                assert "embed-v4.0" in provider.model_specs["cohere_models"]

    def test_fallback_specs_allow_token_limit_lookup(self):
        """After YAML load failure, _get_model_token_limit() must return a valid integer."""
        from code_indexer.services.cohere_embedding import CohereEmbeddingProvider
        from code_indexer.config import CohereConfig

        with patch.dict(os.environ, {"CO_API_KEY": "test-key"}):
            config = CohereConfig()
            with patch("builtins.open", side_effect=FileNotFoundError("No such file")):
                provider = CohereEmbeddingProvider(config, None)
                limit = provider._get_model_token_limit()
                assert isinstance(limit, int)
                assert limit > 0


class TestCohereHttpxClientContextManagerBug596Issue1:
    """Bug #596 Issue 1: must use httpx.Client context manager, not httpx.post().

    The _make_sync_request method must use `with httpx.Client(...) as client:`
    so connections are properly closed even on exceptions.
    """

    def test_make_sync_request_uses_httpx_client_not_httpx_post(self, cohere_provider):
        """_make_sync_request must use httpx.Client, not module-level httpx.post."""
        from unittest.mock import MagicMock

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"embeddings": {"float": [[0.1, 0.2]]}}

        mock_client_cls = MagicMock()
        mock_client_cls.return_value.__enter__.return_value.post.return_value = (
            mock_response
        )

        with patch("httpx.post") as mock_post:
            with patch("httpx.Client", mock_client_cls):
                cohere_provider._make_sync_request(["test text"])

                # httpx.Client must be called (context manager pattern)
                assert mock_client_cls.called, "httpx.Client must be used"
                # httpx.post must NOT be called (module-level function forbidden)
                assert not mock_post.called, (
                    "httpx.post (module-level) must not be used"
                )

    def test_httpx_client_used_as_context_manager(self, cohere_provider):
        """httpx.Client must be used as a context manager (__enter__/__exit__ called)."""
        from unittest.mock import MagicMock

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"embeddings": {"float": [[0.1, 0.2]]}}

        mock_client_cls = MagicMock()
        mock_client_cls.return_value.__enter__.return_value.post.return_value = (
            mock_response
        )

        with patch("httpx.Client", mock_client_cls):
            cohere_provider._make_sync_request(["test text"])

        # Verify context manager protocol was invoked
        mock_client_cls.return_value.__enter__.assert_called_once()
        mock_client_cls.return_value.__exit__.assert_called_once()


class TestCohereContextManagerProtocolBug596Issue2:
    """Bug #596 Issue 2: CohereEmbeddingProvider must implement context manager protocol.

    close(), __enter__, and __exit__ must exist and work correctly.
    """

    def test_close_method_exists(self, cohere_provider):
        """CohereEmbeddingProvider must have a close() method."""
        assert hasattr(cohere_provider, "close"), "close() method must exist"
        assert callable(cohere_provider.close), "close must be callable"

    def test_close_is_a_no_op(self, cohere_provider):
        """close() must be a no-op (return None, no side effects)."""
        result = cohere_provider.close()
        assert result is None

    def test_enter_method_exists(self, cohere_provider):
        """CohereEmbeddingProvider must have __enter__ method."""
        assert hasattr(cohere_provider, "__enter__"), "__enter__ must exist"

    def test_enter_returns_self(self, cohere_provider):
        """__enter__ must return self for use in with-statement."""
        result = cohere_provider.__enter__()
        assert result is cohere_provider

    def test_exit_method_exists(self, cohere_provider):
        """CohereEmbeddingProvider must have __exit__ method."""
        assert hasattr(cohere_provider, "__exit__"), "__exit__ must exist"

    def test_exit_calls_close(self, cohere_provider):
        """__exit__ must call close()."""
        close_called = []
        original_close = cohere_provider.close

        def tracking_close():
            close_called.append(True)
            return original_close()

        cohere_provider.close = tracking_close
        cohere_provider.__exit__(None, None, None)
        assert close_called, "__exit__ must call close()"

    def test_used_as_context_manager(self):
        """CohereEmbeddingProvider must work in a with-statement without error."""
        from code_indexer.services.cohere_embedding import CohereEmbeddingProvider
        from code_indexer.config import CohereConfig

        with patch.dict(os.environ, {"CO_API_KEY": "test-key"}):
            config = CohereConfig()
            with CohereEmbeddingProvider(config, None) as provider:
                assert provider is not None
                assert provider.get_provider_name() == "cohere"


# ---------------------------------------------------------------------------
# Bug #598 — constants, helpers, and tests
# ---------------------------------------------------------------------------

_DUMMY_EMBED_DIM = 10
_DUMMY_EMBED_VECTOR = [0.1] * _DUMMY_EMBED_DIM
_MODEL_DIM = 1024
_SEARCH_LIMIT = 5
_COHERE_MODEL = "embed-v4.0"


def _build_non_filesystem_vector_store():
    """Return a mock whose type is NOT FilesystemVectorStore."""
    from unittest.mock import MagicMock
    from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

    store = MagicMock()
    assert not isinstance(store, FilesystemVectorStore)
    store.health_check.return_value = True
    store.resolve_collection_name.return_value = "test_collection"
    store.ensure_payload_indexes.return_value = None
    store.search.return_value = []
    store.search_with_model_filter.return_value = []
    return store


def _make_cli_mocks(*, is_git: bool, vector_store, codebase_dir) -> dict:
    """Return shared mock objects for CLI query tests.

    Args:
        is_git: Whether GitTopologyService should report git is available.
        vector_store: The vector store mock to use.
        codebase_dir: A real Path for codebase_dir (required by BackendFactory.create call).
    """
    import pathlib
    from unittest.mock import Mock

    cfg = Mock()
    cfg.codebase_dir = pathlib.Path(codebase_dir)
    cfg.mode = "local"
    cfg.embedding_provider = "cohere"

    mock_config_instance = Mock()
    mock_config_instance.get_config.return_value = cfg
    mock_config_instance.load.return_value = cfg
    mock_config_instance.get_daemon_config.return_value = None  # Skip daemon delegation

    mock_backend_instance = Mock()
    mock_backend_instance.get_vector_store_client.return_value = vector_store

    mock_git_instance = Mock()
    mock_git_instance.is_git_available.return_value = is_git
    mock_git_instance.get_current_branch.return_value = "main"

    mock_query_instance = Mock()
    mock_query_instance.get_current_branch_context.return_value = None
    mock_query_instance.filter_results_by_current_branch.return_value = []

    return {
        "config_instance": mock_config_instance,
        "backend_instance": mock_backend_instance,
        "git_instance": mock_git_instance,
        "query_instance": mock_query_instance,
    }


def _make_capturing_embed_mock():
    """Return (mock, captured_calls_list) where mock records kwargs on get_embedding."""
    from unittest.mock import Mock

    captured: list = []

    def capturing_get_embedding(text, **kwargs):
        captured.append({"text": text, "kwargs": kwargs})
        return _DUMMY_EMBED_VECTOR

    mock = Mock()
    mock.health_check.return_value = True
    mock.get_provider_name.return_value = "cohere"
    mock.get_current_model.return_value = _COHERE_MODEL
    mock.get_model_info.return_value = {"name": _COHERE_MODEL, "dimensions": _MODEL_DIM}
    mock.get_embedding.side_effect = capturing_get_embedding
    return mock, captured


def _setup_filesystem_store(tmp_path):
    """Create a minimal FilesystemVectorStore instance with a valid collection for unit testing."""
    import json
    import threading
    from unittest.mock import MagicMock
    from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

    store = FilesystemVectorStore.__new__(FilesystemVectorStore)
    store.logger = MagicMock()
    store.base_path = tmp_path
    store.hnsw_index_cache = None
    store._id_index = {}
    store._id_index_lock = threading.Lock()

    # Create collection directory with metadata so collection_exists() returns True
    col_path = tmp_path / "test_collection"
    col_path.mkdir()
    meta = {"vector_size": _DUMMY_EMBED_DIM}
    (col_path / "collection_meta.json").write_text(json.dumps(meta))
    return store


class TestEmbeddingPurposeQueryBug598:
    """Bug #598: query paths must pass embedding_purpose='query' to get_embedding().

    All three call sites in the search/query path omitted embedding_purpose,
    causing Cohere to use 'search_document' input_type for queries (wrong).
    """

    def test_filesystem_vector_store_search_passes_embedding_purpose_query(
        self, tmp_path
    ):
        """FilesystemVectorStore.search() must call get_embedding with embedding_purpose='query'.

        The inner generate_embedding() function (~line 2150) must forward
        embedding_purpose='query' so Cohere uses 'search_query' input_type.
        """
        import numpy as np
        from unittest.mock import MagicMock

        embedding_provider = MagicMock()
        embedding_provider.get_embedding.return_value = _DUMMY_EMBED_VECTOR

        mock_hnsw_index = MagicMock()
        mock_hnsw_index.knn_query.return_value = (
            np.array([[]], dtype=np.int64),
            np.array([[]], dtype=np.float32),
        )

        store = _setup_filesystem_store(tmp_path)

        with (
            patch(
                "code_indexer.storage.hnsw_index_manager.HNSWIndexManager"
            ) as mock_hnsw_cls,
            patch(
                "code_indexer.storage.id_index_manager.IDIndexManager"
            ) as mock_id_cls,
        ):
            mock_hnsw_cls.return_value.is_stale.return_value = False
            mock_hnsw_cls.return_value.load_index.return_value = mock_hnsw_index
            mock_hnsw_cls.return_value._load_id_mapping.return_value = {}
            mock_hnsw_cls.return_value.query.return_value = ([], [])
            mock_id_cls.return_value.load_index.return_value = {}

            store.search(
                query="test query",
                embedding_provider=embedding_provider,
                collection_name="test_collection",
                limit=_SEARCH_LIMIT,
                return_timing=False,
            )

        calls = embedding_provider.get_embedding.call_args_list
        assert len(calls) >= 1, "get_embedding must be called during search"
        assert any(c.kwargs.get("embedding_purpose") == "query" for c in calls), (
            f"Expected embedding_purpose='query' in at least one get_embedding call. "
            f"Actual calls: {calls}. "
            "Bug #598: FilesystemVectorStore.search() must pass embedding_purpose='query'."
        )

    def test_cli_git_aware_non_filesystem_path_passes_embedding_purpose_query(
        self, tmp_path
    ):
        """cli.py ~line 6516: git-aware non-FilesystemVectorStore branch must pass embedding_purpose='query'.

        Production (Story #904) now calls _run_embedder_chain(text=query,
        embedding_purpose='query', primary_provider=..., secondary_provider=...,
        health_monitor=...) instead of embedding_provider.get_embedding(query).
        We patch _resolve_embedder_providers and _run_embedder_chain to intercept
        the chain call and verify embedding_purpose='query' is forwarded.
        """
        from click.testing import CliRunner
        from code_indexer.cli import cli

        embed_mock, _unused_captured = _make_capturing_embed_mock()
        vector_store = _build_non_filesystem_vector_store()
        mocks = _make_cli_mocks(
            is_git=True, vector_store=vector_store, codebase_dir=tmp_path
        )

        chain_calls: list = []

        def capturing_chain(**kwargs):
            chain_calls.append(kwargs)
            # Return a valid 5-tuple: (vector, provider_name, failure, elapsed_ms, outcomes)
            return (_DUMMY_EMBED_VECTOR, "cohere", None, 10, [])

        runner = CliRunner()
        with (
            patch("code_indexer.cli.ConfigManager") as mock_cfg_cls,
            patch("code_indexer.cli.BackendFactory.create") as mock_backend_cls,
            patch(
                "code_indexer.services.embedder_provider_resolver._resolve_embedder_providers",
                return_value=(embed_mock, None),
            ),
            patch(
                "code_indexer.services.embedder_chain._run_embedder_chain",
                side_effect=capturing_chain,
            ),
            patch(
                "code_indexer.services.git_topology_service.GitTopologyService",
                return_value=mocks["git_instance"],
            ),
            patch(
                "code_indexer.services.generic_query_service.GenericQueryService",
                return_value=mocks["query_instance"],
            ),
        ):
            mock_cfg_cls.create_with_backtrack.return_value = mocks["config_instance"]
            mock_backend_cls.return_value = mocks["backend_instance"]
            runner.invoke(cli, ["query", "find auth code", "--quiet"])

        query_calls = [c for c in chain_calls if c.get("text") == "find auth code"]
        assert len(query_calls) >= 1, (
            f"_run_embedder_chain must be called with the query text. "
            f"All captured chain calls: {chain_calls}"
        )
        assert all(c.get("embedding_purpose") == "query" for c in query_calls), (
            f"Expected embedding_purpose='query' in all chain calls. "
            f"Actual: {query_calls}. "
            "Bug #598: cli.py git-aware non-FilesystemVectorStore path must pass embedding_purpose='query'."
        )

    def test_cli_non_git_non_filesystem_path_passes_embedding_purpose_query(
        self, tmp_path
    ):
        """cli.py ~line 6607: non-git non-FilesystemVectorStore branch must pass embedding_purpose='query'.

        Production (Story #904) now calls _run_embedder_chain(text=query,
        embedding_purpose='query', primary_provider=..., secondary_provider=...,
        health_monitor=...) instead of embedding_provider.get_embedding(query).
        We patch _resolve_embedder_providers and _run_embedder_chain to intercept
        the chain call and verify embedding_purpose='query' is forwarded.
        """
        from click.testing import CliRunner
        from code_indexer.cli import cli

        embed_mock, _unused_captured = _make_capturing_embed_mock()
        vector_store = _build_non_filesystem_vector_store()
        mocks = _make_cli_mocks(
            is_git=False, vector_store=vector_store, codebase_dir=tmp_path
        )

        chain_calls: list = []

        def capturing_chain(**kwargs):
            chain_calls.append(kwargs)
            # Return a valid 5-tuple: (vector, provider_name, failure, elapsed_ms, outcomes)
            return (_DUMMY_EMBED_VECTOR, "cohere", None, 10, [])

        runner = CliRunner()
        with (
            patch("code_indexer.cli.ConfigManager") as mock_cfg_cls,
            patch("code_indexer.cli.BackendFactory.create") as mock_backend_cls,
            patch(
                "code_indexer.services.embedder_provider_resolver._resolve_embedder_providers",
                return_value=(embed_mock, None),
            ),
            patch(
                "code_indexer.services.embedder_chain._run_embedder_chain",
                side_effect=capturing_chain,
            ),
            patch(
                "code_indexer.services.git_topology_service.GitTopologyService",
                return_value=mocks["git_instance"],
            ),
            patch(
                "code_indexer.services.generic_query_service.GenericQueryService",
                return_value=mocks["query_instance"],
            ),
        ):
            mock_cfg_cls.create_with_backtrack.return_value = mocks["config_instance"]
            mock_backend_cls.return_value = mocks["backend_instance"]
            runner.invoke(cli, ["query", "find auth code", "--quiet"])

        query_calls = [c for c in chain_calls if c.get("text") == "find auth code"]
        assert len(query_calls) >= 1, (
            f"_run_embedder_chain must be called with the query text. "
            f"All captured chain calls: {chain_calls}"
        )
        assert all(c.get("embedding_purpose") == "query" for c in query_calls), (
            f"Expected embedding_purpose='query' in all chain calls. "
            f"Actual: {query_calls}. "
            "Bug #598: cli.py non-git non-FilesystemVectorStore path must pass embedding_purpose='query'."
        )


class TestCohereRetryDelayCapBug602:
    """#602: All retry delays must be capped at 300s to avoid indefinite thread blocking."""

    def test_retry_after_header_capped_at_300s(self, cohere_provider):
        """A 429 response with Retry-After: 86400 must sleep at most 300s."""
        from unittest.mock import MagicMock, patch

        mock_response_429 = MagicMock()
        mock_response_429.status_code = 429
        mock_response_429.headers = {"retry-after": "86400"}

        mock_response_ok = MagicMock()
        mock_response_ok.status_code = 200
        mock_response_ok.json.return_value = {"embeddings": {"float": [[0.1, 0.2]]}}

        mock_client_cls = MagicMock()
        mock_client_instance = mock_client_cls.return_value.__enter__.return_value
        mock_client_instance.post.side_effect = [mock_response_429, mock_response_ok]

        sleep_calls = []
        with patch("httpx.Client", mock_client_cls):
            with patch("time.sleep", side_effect=lambda s: sleep_calls.append(s)):
                cohere_provider._make_sync_request(["test"])

        assert sleep_calls, "time.sleep must be called after a 429 response"
        assert all(s <= 300.0 for s in sleep_calls), (
            f"Bug #602: all sleep durations must be <= 300s. Got: {sleep_calls}"
        )

    def test_5xx_backoff_capped_at_300s(self, cohere_provider):
        """A 500 response must sleep at most 300s regardless of computed backoff."""
        from unittest.mock import MagicMock, patch

        mock_response_500 = MagicMock()
        mock_response_500.status_code = 500

        mock_response_ok = MagicMock()
        mock_response_ok.status_code = 200
        mock_response_ok.json.return_value = {"embeddings": {"float": [[0.1, 0.2]]}}

        mock_client_cls = MagicMock()
        mock_client_instance = mock_client_cls.return_value.__enter__.return_value
        mock_client_instance.post.side_effect = [mock_response_500, mock_response_ok]

        sleep_calls = []
        with patch("httpx.Client", mock_client_cls):
            with patch("time.sleep", side_effect=lambda s: sleep_calls.append(s)):
                cohere_provider._make_sync_request(["test"])

        assert sleep_calls, "time.sleep must be called after a 500 response"
        assert all(s <= 300.0 for s in sleep_calls), (
            f"Bug #602: all sleep durations must be <= 300s. Got: {sleep_calls}"
        )

    def test_network_error_delay_capped_at_300s(self, cohere_provider):
        """A network exception must sleep at most 300s regardless of computed backoff."""
        import httpx
        from unittest.mock import MagicMock, patch

        mock_response_ok = MagicMock()
        mock_response_ok.status_code = 200
        mock_response_ok.json.return_value = {"embeddings": {"float": [[0.1, 0.2]]}}

        mock_client_cls = MagicMock()
        mock_client_instance = mock_client_cls.return_value.__enter__.return_value
        mock_client_instance.post.side_effect = [
            httpx.ConnectError("connection refused"),
            mock_response_ok,
        ]

        sleep_calls = []
        with patch("httpx.Client", mock_client_cls):
            with patch("time.sleep", side_effect=lambda s: sleep_calls.append(s)):
                cohere_provider._make_sync_request(["test"])

        assert sleep_calls, "time.sleep must be called after a network error"
        assert all(s <= 300.0 for s in sleep_calls), (
            f"Bug #602: all sleep durations must be <= 300s. Got: {sleep_calls}"
        )


class TestCohereExponentialBackoffFlagBug603:
    """#603: exponential_backoff=False must use flat delay; True must use increasing delay."""

    def test_exponential_backoff_false_uses_flat_delay_on_5xx(self, cohere_provider):
        """When exponential_backoff=False, all 5xx retry sleeps must use the same fixed delay."""
        from unittest.mock import MagicMock, patch

        cohere_provider.config.exponential_backoff = False
        base_delay = cohere_provider.config.retry_delay

        mock_response_500 = MagicMock()
        mock_response_500.status_code = 500

        mock_response_ok = MagicMock()
        mock_response_ok.status_code = 200
        mock_response_ok.json.return_value = {"embeddings": {"float": [[0.1, 0.2]]}}

        # Three 500 responses then success to capture multiple sleep calls
        mock_client_cls = MagicMock()
        mock_client_instance = mock_client_cls.return_value.__enter__.return_value
        mock_client_instance.post.side_effect = [
            mock_response_500,
            mock_response_500,
            mock_response_ok,
        ]

        sleep_calls = []
        with patch("httpx.Client", mock_client_cls):
            with patch("time.sleep", side_effect=lambda s: sleep_calls.append(s)):
                cohere_provider._make_sync_request(["test"])

        assert len(sleep_calls) >= 2, (
            f"Expected at least 2 sleep calls for 2 failed attempts. Got: {sleep_calls}"
        )
        # All delays must equal base_delay (flat, not exponential)
        assert all(s == min(base_delay, 300.0) for s in sleep_calls), (
            f"Bug #603: with exponential_backoff=False, all sleeps must equal {base_delay}s. "
            f"Got: {sleep_calls}"
        )

    def test_exponential_backoff_true_uses_increasing_delay_on_5xx(
        self, cohere_provider
    ):
        """When exponential_backoff=True, successive 5xx retry sleeps must increase."""
        from unittest.mock import MagicMock, patch

        cohere_provider.config.exponential_backoff = True
        # Use a small base delay so exponential growth is visible before 300s cap
        cohere_provider.config.retry_delay = 1.0

        mock_response_500 = MagicMock()
        mock_response_500.status_code = 500

        mock_response_ok = MagicMock()
        mock_response_ok.status_code = 200
        mock_response_ok.json.return_value = {"embeddings": {"float": [[0.1, 0.2]]}}

        mock_client_cls = MagicMock()
        mock_client_instance = mock_client_cls.return_value.__enter__.return_value
        mock_client_instance.post.side_effect = [
            mock_response_500,
            mock_response_500,
            mock_response_ok,
        ]

        sleep_calls = []
        with patch("httpx.Client", mock_client_cls):
            with patch("time.sleep", side_effect=lambda s: sleep_calls.append(s)):
                cohere_provider._make_sync_request(["test"])

        assert len(sleep_calls) >= 2, (
            f"Expected at least 2 sleep calls for 2 failed attempts. Got: {sleep_calls}"
        )
        # Second sleep must be strictly greater than first (exponential growth)
        assert sleep_calls[1] > sleep_calls[0], (
            f"Bug #603: with exponential_backoff=True, delays must increase. "
            f"Got: {sleep_calls}"
        )


# ---------------------------------------------------------------------------
# Story #619 Gap 6: Embedding dimension validation tests
# ---------------------------------------------------------------------------


class TestEmbeddingDimensionValidation:
    """Tests for _validate_embeddings dimension and NaN/Inf checks (Story #619 Gap 6)."""

    def test_cohere_validate_embeddings_wrong_dimensions(self, cohere_provider):
        """_validate_embeddings must raise RuntimeError when dims don't match model."""
        expected_dims = cohere_provider.get_model_info()["dimensions"]
        wrong_dim = expected_dims + 1  # force mismatch
        with pytest.raises(RuntimeError, match="dims"):
            cohere_provider._validate_embeddings(
                [[0.1] * wrong_dim], cohere_provider.config.model
            )

    def test_cohere_validate_embeddings_nan_values(self, cohere_provider):
        """_validate_embeddings must raise RuntimeError when embedding contains NaN."""
        expected_dims = cohere_provider.get_model_info()["dimensions"]
        nan_embedding = [float("nan")] + [0.1] * (expected_dims - 1)
        with pytest.raises(RuntimeError, match="NaN or Inf"):
            cohere_provider._validate_embeddings(
                [nan_embedding], cohere_provider.config.model
            )

    def test_cohere_validate_embeddings_inf_values(self, cohere_provider):
        """_validate_embeddings must raise RuntimeError when embedding contains Inf."""
        expected_dims = cohere_provider.get_model_info()["dimensions"]
        inf_embedding = [float("inf")] + [0.1] * (expected_dims - 1)
        with pytest.raises(RuntimeError, match="NaN or Inf"):
            cohere_provider._validate_embeddings(
                [inf_embedding], cohere_provider.config.model
            )


# ---------------------------------------------------------------------------
# Story #619 Gap 2: Connect vs Read timeout split tests
# ---------------------------------------------------------------------------


class TestConnectReadTimeoutSplit:
    """Tests for connect vs read timeout split in Cohere provider (Story #619 Gap 2)."""

    def test_cohere_uses_split_timeout(self, cohere_provider):
        """_make_sync_request must pass httpx.Timeout with distinct connect vs read values."""
        import httpx

        captured_timeouts = []

        class CapturingClient:
            def __init__(self, **kwargs):
                captured_timeouts.append(kwargs.get("timeout"))
                raise ConnectionError("test-abort")

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

        with patch("httpx.Client", CapturingClient):
            with pytest.raises(RuntimeError):
                cohere_provider._make_sync_request(["test"])

        assert len(captured_timeouts) >= 1, "httpx.Client must be called at least once"
        timeout_arg = captured_timeouts[0]
        assert isinstance(timeout_arg, httpx.Timeout), (
            f"Expected httpx.Timeout instance, got {type(timeout_arg)}"
        )
        assert timeout_arg.connect != timeout_arg.read, (
            f"connect={timeout_arg.connect} must differ from read={timeout_arg.read}"
        )


# ---------------------------------------------------------------------------
# Latency transport wiring tests
# ---------------------------------------------------------------------------


class _RecordingTracker:
    """Minimal tracker stub that records samples without I/O."""

    def __init__(self) -> None:
        self.samples: list = []

    def record_sample(self, dep: str, latency_ms: float, code: int) -> None:
        self.samples.append(
            {"dependency_name": dep, "latency_ms": latency_ms, "status_code": code}
        )


def _reset_latency_singleton() -> None:
    from code_indexer.server.services.dependency_latency_tracker import set_instance

    set_instance(None)


_VOYAGE_EMBED_URL = "https://api.voyageai.com/v1/embeddings"
_COHERE_EMBED_URL = "https://api.cohere.com/v2/embed"

_VOYAGE_EMBED_RESPONSE = {
    "data": [{"embedding": [0.1] * 1024, "index": 0}],
    "model": "voyage-code-3",
    "usage": {"total_tokens": 1},
}
_COHERE_EMBED_RESPONSE = {
    "id": "test",
    "embeddings": {"float": [[0.1] * 1024]},
    "texts": ["hello"],
    "meta": {"api_version": {"version": "2"}},
}


@pytest.mark.parametrize(
    "client_fixture,url,expected_dep",
    [
        ("voyage_client", _VOYAGE_EMBED_URL, "voyageai_embed"),
        ("cohere_provider", _COHERE_EMBED_URL, "cohere_embed"),
    ],
)
class TestEmbeddingClientLatencyWiring:
    """Verify embedding clients record latency samples via transport wiring."""

    def setup_method(self) -> None:
        _reset_latency_singleton()

    def teardown_method(self) -> None:
        _reset_latency_singleton()

    def test_make_sync_request_records_sample_when_tracker_set(
        self, httpx_mock, request, client_fixture, url, expected_dep
    ) -> None:
        """When tracker is registered, _make_sync_request records sample with correct dep name."""
        from code_indexer.server.services.dependency_latency_tracker import set_instance

        client = request.getfixturevalue(client_fixture)
        tracker = _RecordingTracker()
        set_instance(tracker)

        if "voyage" in url:
            httpx_mock.add_response(method="POST", url=url, json=_VOYAGE_EMBED_RESPONSE)
            client._make_sync_request(["hello"])
        else:
            httpx_mock.add_response(method="POST", url=url, json=_COHERE_EMBED_RESPONSE)
            client._make_sync_request(["hello"])

        assert len(tracker.samples) == 1
        assert tracker.samples[0]["dependency_name"] == expected_dep
        assert tracker.samples[0]["latency_ms"] >= 0.0

    def test_make_sync_request_completes_without_tracker(
        self, httpx_mock, request, client_fixture, url, expected_dep
    ) -> None:
        """When no tracker is set, _make_sync_request completes without error."""
        client = request.getfixturevalue(client_fixture)

        if "voyage" in url:
            httpx_mock.add_response(method="POST", url=url, json=_VOYAGE_EMBED_RESPONSE)
            result = client._make_sync_request(["hello"])
            assert "data" in result
        else:
            httpx_mock.add_response(method="POST", url=url, json=_COHERE_EMBED_RESPONSE)
            result = client._make_sync_request(["hello"])
            assert "embeddings" in result
