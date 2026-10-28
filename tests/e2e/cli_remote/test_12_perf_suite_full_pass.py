from __future__ import annotations

import json
import os
import site
import sys
import uuid
from pathlib import Path
from typing import Dict, List

import httpx
import pytest
import yaml

from tests.e2e.conftest import E2EConfig
from tests.e2e.helpers import rest_call, wait_for_job
from tests.e2e.server.conftest import AdminTokenProvider
from tests.unit.perf_suite.secret_scan import (
    Masked,
    files_containing,
    remove_golden_repos,
    run_step,
    run_with_teardown,
)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
RUN_PY = PROJECT_ROOT / "scripts" / "analysis" / "perf_suite" / "run.py"
KEY_VARIABLES = ("E2E_VOYAGE_API_KEY", "VOYAGE_API_KEY")
CLI_ENV_ALLOW = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "HOME")
CLI_TIMEOUT_S = 600
GIT_TIMEOUT_S = 60
REQUIRED_DOORS = (
    "POST /auth/login",
    "GET /health",
    "MCP list_global_repos",
    "POST /api/query",
    "MCP search_code",
    "POST /api/regex/search",
    "MCP regex_search",
    "POST /api/xray/search",
    "GET /api/jobs/{job_id}",
    "MCP xray_search",
    "MCP get_job_details",
    "MCP list_files",
    "MCP get_file_content",
    "POST /api/query/multi",
)
PY_FILE = "app/inventory.py"
JAVA_FILE = "src/main/java/com/example/OrderRouter.java"
BILLING_FILE = "app/invoices.py"
INVENTORY_FILES = {
    "README.md": "# Example warehouse service\n",
    PY_FILE: (
        "class InventoryLedger:\n"
        '    """Tracks warehouse stock levels and records incoming shipments."""\n\n'
        "    def record_shipment(self, sku, quantity):\n"
        "        self.levels[sku] = self.levels.get(sku, 0) + quantity\n"
    ),
    "app/pricing.py": (
        "def compute_discount(price, percent):\n"
        '    """Apply a percentage discount to a price."""\n'
        "    return price * (100 - percent) / 100\n"
    ),
    JAVA_FILE: (
        "package com.example;\n\npublic class OrderRouter {\n"
        '    public String route(String region) { return "queue-" + region; }\n}\n'
    ),
}
BILLING_FILES = {
    "README.md": "# Example billing service for the warehouse\n",
    BILLING_FILE: (
        "class InvoiceBatch:\n"
        '    """Groups warehouse invoices and totals the amount due."""\n\n'
        "    def total_due(self, invoices):\n"
        "        return sum(invoice.amount for invoice in invoices)\n"
    ),
}
EVALUATOR = (
    "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {\n"
    '    vec![EvalFinding { pattern: "file".to_string(), line: node.start_line, '
    "snippet: String::new() }]\n}\n"
)


def _provider_key() -> Masked:
    for name in KEY_VARIABLES:
        if os.environ.get(name):
            return Masked(os.environ[name])
    pytest.fail(
        "VoyageAI API key is not configured -- set VOYAGE_API_KEY or "
        "E2E_VOYAGE_API_KEY (.e2e-automation).",
        pytrace=False,
    )


def _fixture_repo(root: Path, files: Dict[str, str]) -> Path:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    git = [
        "git",
        "-C",
        str(root),
        "-c",
        "user.name=perf",
        "-c",
        "user.email=perf@example.com",
    ]
    for args in (
        ["init", "-q", "-b", "main"],
        ["add", "-A"],
        ["commit", "-q", "-m", "fixture"],
    ):
        step = f"fixture git {args[0]}"
        result = run_step(step, git + args, GIT_TIMEOUT_S)
        assert result.returncode == 0, f"{step} exited {result.returncode}"
    return root


def _corpus(path: Path, inventory: str, billing: str) -> Path:
    both = {inventory: PY_FILE, billing: BILLING_FILE}
    corpus = {
        "top_n": 10,
        "xray_evaluator": EVALUATOR,
        "repos": [
            {
                "url": inventory,
                "probe": {"query": "InventoryLedger"},
                "queries": [
                    {
                        "mode": "semantic",
                        "query": "track warehouse stock levels and shipments",
                        "expect": PY_FILE,
                    },
                    {"mode": "fts", "query": "InventoryLedger", "expect": PY_FILE},
                    {
                        "mode": "regex",
                        "query": r"class\s+OrderRouter\b",
                        "expect": JAVA_FILE,
                    },
                    {
                        "mode": "xray",
                        "query": "class InventoryLedger",
                        "include": ["*.py"],
                        "expect": PY_FILE,
                    },
                    {
                        "mode": "xray",
                        "query": "public class OrderRouter",
                        "include": ["*.java"],
                        "expect": JAVA_FILE,
                    },
                    {"mode": "files_list", "path": "app", "expect": "app/pricing.py"},
                    {
                        "mode": "file_read",
                        "path": "app/pricing.py",
                        "expect": "def compute_discount",
                    },
                ],
            },
            {
                "url": billing,
                "probe": {"query": "InvoiceBatch"},
                "queries": [
                    {"mode": "fts", "query": "InvoiceBatch", "expect": BILLING_FILE},
                    {
                        "mode": "regex",
                        "query": r"def\s+total_due",
                        "expect": BILLING_FILE,
                    },
                ],
            },
        ],
        "fanout": [
            {"mode": "fts", "query": "warehouse", "expect": both},
            {"mode": "regex", "query": "class [A-Z][a-z]+", "expect": both},
            {
                "mode": "semantic",
                "query": "apply a percentage discount to a price",
                "expect": {inventory: "app/pricing.py", billing: BILLING_FILE},
            },
        ],
    }
    path.write_text(yaml.safe_dump(corpus))
    return path


def _cli_env() -> Dict[str, str]:
    env = {k: os.environ[k] for k in CLI_ENV_ALLOW if k in os.environ}
    # ^ security CLI child gets no provider key; it only talks HTTP to the server
    env["PYTHONUSERBASE"] = site.getuserbase()
    return env


def test_perf_suite_full_pass_through_every_door(
    e2e_config: E2EConfig,
    e2e_http_client: httpx.Client,
    e2e_admin_token_provider: AdminTokenProvider,
    tmp_path: Path,
) -> None:
    key = _provider_key()
    password = Masked(e2e_config.admin_pass)
    suffix = uuid.uuid4().hex[:8]
    repos = {
        f"perf-inventory-{suffix}": _fixture_repo(
            tmp_path / "inventory", INVENTORY_FILES
        ),
        f"perf-billing-{suffix}": _fixture_repo(tmp_path / "billing", BILLING_FILES),
    }
    registered: List[str] = []

    def body() -> None:
        for alias, repo in repos.items():
            added = rest_call(
                e2e_http_client,
                "POST",
                "/api/admin/golden-repos",
                token=e2e_admin_token_provider.get_token(),
                json={"repo_url": str(repo), "alias": alias},
            )
            added.raise_for_status()
            registered.append(alias)
            job = wait_for_job(
                e2e_http_client,
                added.json()["job_id"],
                token=e2e_admin_token_provider.get_token(),
                timeout=e2e_config.golden_repo_job_timeout,
                poll_interval=e2e_config.golden_repo_job_poll_interval,
            )
            assert job["status"] == "completed", str(job.get("error"))[:300]
        inventory, billing = (str(p) for p in repos.values())
        _run_and_verify(e2e_config, tmp_path, inventory, billing, key, password)

    def teardown() -> List[str]:
        token = e2e_admin_token_provider.get_token()
        return remove_golden_repos(e2e_http_client, token, registered)

    run_with_teardown(body, teardown)


def _run_and_verify(
    e2e_config: E2EConfig,
    tmp_path: Path,
    inventory: str,
    billing: str,
    key: Masked,
    password: Masked,
) -> None:
    corpus = _corpus(tmp_path / "corpus.yaml", inventory, billing)
    out_dir = tmp_path / "reports"
    done = run_step(
        "perf suite CLI",
        [
            sys.executable,
            str(RUN_PY),
            "--url",
            e2e_config.server_url,
            "--username",
            e2e_config.admin_user,
            "--password",
            password,
            "--quick",
            "--corpus",
            str(corpus),
            "--output-dir",
            str(out_dir),
        ],
        CLI_TIMEOUT_S,
        _cli_env(),
    )
    (tmp_path / "cli-stdout.log").write_text(done.stdout)
    (tmp_path / "cli-stderr.log").write_text(done.stderr)
    server_dir = e2e_config.server_data_dir
    assert (server_dir / "server.log").is_file(), "server.log missing; cannot scan it"
    key_hits = files_containing(key, [tmp_path, server_dir])
    assert key_hits == [], "provider key found in these artifacts"
    # ^ why password scan excludes the server data dir, not all roots; seeded e2e password = username the server logs
    password_hits = files_containing(password, [tmp_path])
    assert password_hits == [], "admin password found in these artifacts"
    assert done.returncode == 0, done.stdout[-4000:] + done.stderr[-2000:]
    assert "VERDICT: PASS" in done.stdout
    report = json.loads(next(out_dir.glob("perf_*.json")).read_text())
    assert report["verdict"]["passed"] is True
    for op, doors in report["scenarios"].items():
        for door, by_conc in doors.items():
            for result in (by_conc or {}).values():
                where = f"{op}/{door}"
                assert result["aborted"] is None, where
                assert not result["errors_by_class"], (where, result["errors_by_class"])
                assert result["median"]["served"] > 0, where
                assert result["median"]["wrong"] == 0, (where, result["wrong_samples"])
                assert result["warmup_errors"] == result["warmup_wrong"] == 0, where
    missing = [d for d in REQUIRED_DOORS if report["request_tally"].get(d, 0) < 1]
    assert missing == []
