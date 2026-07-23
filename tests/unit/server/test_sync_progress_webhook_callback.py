"""build_sync_progress_webhook_callback: reusable sync progress webhook (H2).

PR #1424 H2: a work-stolen sync runs on the claiming node, so its webhook (URL
supplied by the client, carried in job metadata options.progress_webhook) must
be rebuilt and pushed from THAT node. This shared builder is used by both the
local sync path (inline_repos) and the executing node (lifespan pod-pull
dispatch) so the webhook-POST logic lives in ONE place.
"""

from unittest.mock import MagicMock, patch

from code_indexer.server.app_helpers import build_sync_progress_webhook_callback

# Single injected constant: a non-routable stand-in endpoint. requests.post is
# always patched, so this value is never dialed -- it only exercises the
# payload/routing logic.
WEBHOOK_URL = "https://example.invalid/hook"


class TestBuildSyncProgressWebhookCallback:
    def test_returns_none_without_url(self):
        assert build_sync_progress_webhook_callback(None, "repoA", "alice") is None
        assert build_sync_progress_webhook_callback("", "repoA", "alice") is None

    def test_returns_callable_that_posts_payload(self):
        cb = build_sync_progress_webhook_callback(WEBHOOK_URL, "repoA", "alice")
        assert callable(cb)

        with patch("code_indexer.server.app_helpers.requests.post") as mock_post:
            mock_post.return_value = MagicMock(ok=True, status_code=200)
            cb(42)

        assert mock_post.call_count == 1
        args, kwargs = mock_post.call_args
        assert args[0] == WEBHOOK_URL
        payload = kwargs["json"]
        assert payload["repository_id"] == "repoA"
        assert payload["progress"] == 42
        assert payload["username"] == "alice"
        assert "timestamp" in payload

    def test_swallows_post_errors(self):
        cb = build_sync_progress_webhook_callback(WEBHOOK_URL, "repoA", "alice")
        with patch(
            "code_indexer.server.app_helpers.requests.post",
            side_effect=RuntimeError("boom"),
        ):
            # Must not raise -- a webhook failure never interrupts the sync.
            cb(10)
