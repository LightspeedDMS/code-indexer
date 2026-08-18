"""EVO-65484: reconcile_scip_from_filesystem — backfill SCIP for SCIP-eligible golden
repos that have none. Opt-in, language-gated, bounded, cooldown-backed."""

import os
from unittest import mock

from code_indexer.server.repositories.golden_repo_manager import GoldenRepoManager


def _proj(lang):
    return type("P", (), {"language": lang})()


def _mgr(tmp_path):
    m = GoldenRepoManager(data_dir=str(tmp_path / "data"))
    gr = tmp_path / "golden"
    gr.mkdir(exist_ok=True)
    m.golden_repos_dir = str(gr)
    return m, gr


def _patches(discover_result):
    """Patch the function-local imports at their source modules."""
    disc = mock.MagicMock()
    disc.return_value.discover.return_value = discover_result
    funnel = mock.MagicMock(return_value=type("O", (), {"value": "enqueued"})())
    cfg = mock.MagicMock()
    cfg.get_config.return_value = object()
    return (
        mock.patch("code_indexer.scip.discovery.ProjectDiscovery", disc),
        mock.patch(
            "code_indexer.server.services.scip_lifecycle.funnel_golden_scip", funnel
        ),
        mock.patch(
            "code_indexer.server.services.config_service.get_config_service",
            return_value=cfg,
        ),
        funnel,
    )


class TestScipReconcile:
    def test_disabled_by_default_is_noop(self, tmp_path):
        m, gr = _mgr(tmp_path)
        (gr / "repoA").mkdir()
        p_disc, p_funnel, p_cfg, funnel = _patches([_proj("python")])
        with mock.patch.dict(os.environ, {}, clear=True), p_disc, p_funnel, p_cfg:
            counts = m.reconcile_scip_from_filesystem()
        funnel.assert_not_called()
        assert counts["enqueued"] == 0

    def test_enqueues_eligible_python_repo(self, tmp_path):
        m, gr = _mgr(tmp_path)
        (gr / "repoA").mkdir()  # eligible, no scip
        p_disc, p_funnel, p_cfg, funnel = _patches([_proj("python")])
        with (
            mock.patch.dict(os.environ, {"CIDX_SCIP_AUTO_RECONCILE_ENABLED": "true"}),
            p_disc,
            p_funnel,
            p_cfg,
        ):
            counts = m.reconcile_scip_from_filesystem()
        funnel.assert_called_once()
        assert funnel.call_args.args[0] == "repoA"
        assert counts["enqueued"] == 1

    def test_skips_when_scip_present(self, tmp_path):
        m, gr = _mgr(tmp_path)
        scip = gr / "repoB" / ".code-indexer" / "scip"
        scip.mkdir(parents=True)
        (scip / "x.scip.db").write_bytes(b"x")
        p_disc, p_funnel, p_cfg, funnel = _patches([_proj("python")])
        with (
            mock.patch.dict(os.environ, {"CIDX_SCIP_AUTO_RECONCILE_ENABLED": "true"}),
            p_disc,
            p_funnel,
            p_cfg,
        ):
            counts = m.reconcile_scip_from_filesystem()
        funnel.assert_not_called()
        assert counts["scip_present"] == 1

    def test_skips_unsupported_language(self, tmp_path):
        m, gr = _mgr(tmp_path)
        (gr / "repoJava").mkdir()
        p_disc, p_funnel, p_cfg, funnel = _patches(
            [_proj("java")]
        )  # not in default set
        with (
            mock.patch.dict(os.environ, {"CIDX_SCIP_AUTO_RECONCILE_ENABLED": "true"}),
            p_disc,
            p_funnel,
            p_cfg,
        ):
            counts = m.reconcile_scip_from_filesystem()
        funnel.assert_not_called()
        assert counts["language_skipped"] == 1

    def test_skips_not_discoverable(self, tmp_path):
        m, gr = _mgr(tmp_path)
        (gr / "docsonly").mkdir()
        p_disc, p_funnel, p_cfg, funnel = _patches([])  # no projects
        with (
            mock.patch.dict(os.environ, {"CIDX_SCIP_AUTO_RECONCILE_ENABLED": "true"}),
            p_disc,
            p_funnel,
            p_cfg,
        ):
            counts = m.reconcile_scip_from_filesystem()
        funnel.assert_not_called()
        assert counts["not_discoverable"] == 1

    def test_max_enqueue_bounds_per_cycle(self, tmp_path):
        m, gr = _mgr(tmp_path)
        for n in ("r1", "r2", "r3"):
            (gr / n).mkdir()
        p_disc, p_funnel, p_cfg, funnel = _patches([_proj("go")])
        with (
            mock.patch.dict(os.environ, {"CIDX_SCIP_AUTO_RECONCILE_ENABLED": "true"}),
            p_disc,
            p_funnel,
            p_cfg,
        ):
            counts = m.reconcile_scip_from_filesystem(max_enqueue=2)
        assert counts["enqueued"] == 2
        assert funnel.call_count == 2

    def test_cooldown_skips_recent_attempt(self, tmp_path):
        m, gr = _mgr(tmp_path)
        repo = gr / "repoA"
        (repo / ".code-indexer").mkdir(parents=True)
        (repo / ".code-indexer" / ".scip_reconcile_attempt").touch()  # fresh marker
        p_disc, p_funnel, p_cfg, funnel = _patches([_proj("python")])
        with (
            mock.patch.dict(os.environ, {"CIDX_SCIP_AUTO_RECONCILE_ENABLED": "true"}),
            p_disc,
            p_funnel,
            p_cfg,
        ):
            counts = m.reconcile_scip_from_filesystem()
        funnel.assert_not_called()
        assert counts["cooldown_skipped"] == 1

    def test_cooldown_skips_discovery_walk(self, tmp_path):
        """EVO-66188: a repo in attempt-cooldown must NOT be re-discovered — the cooldown
        check now runs BEFORE ProjectDiscovery, so the NFS walk is skipped, not just the
        enqueue."""
        m, gr = _mgr(tmp_path)
        repo = gr / "repoA"
        (repo / ".code-indexer").mkdir(parents=True)
        (repo / ".code-indexer" / ".scip_reconcile_attempt").touch()  # fresh marker
        disc = mock.MagicMock()
        disc.return_value.discover.return_value = [_proj("python")]
        funnel = mock.MagicMock(return_value=type("O", (), {"value": "enqueued"})())
        cfg = mock.MagicMock()
        cfg.get_config.return_value = object()
        with (
            mock.patch.dict(os.environ, {"CIDX_SCIP_AUTO_RECONCILE_ENABLED": "true"}),
            mock.patch("code_indexer.scip.discovery.ProjectDiscovery", disc),
            mock.patch(
                "code_indexer.server.services.scip_lifecycle.funnel_golden_scip", funnel
            ),
            mock.patch(
                "code_indexer.server.services.config_service.get_config_service",
                return_value=cfg,
            ),
        ):
            counts = m.reconcile_scip_from_filesystem()
        assert counts["cooldown_skipped"] == 1
        disc.return_value.discover.assert_not_called()  # the walk was skipped
        funnel.assert_not_called()
