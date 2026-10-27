import re
from pathlib import Path
from typing import Any, List, Tuple

import jinja2

_TEMPLATES = (
    Path(__file__).parent.parent.parent.parent.parent
    / "src/code_indexer/server/web/templates"
)
_METRICS = {
    "semantic_searches": 1,
    "other_index_searches": 2,
    "regex_searches": 3,
    "xray_searches": 9,
    "other_api_calls": 4,
}
_CARD = re.compile(
    r'<h3>([^<]+)</h3>\s*</header>\s*<div class="stat-value">\s*([^<]*?)\s*</div>'
)


def _render(name: str, **context: Any) -> str:
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(_TEMPLATES)))
    return env.get_template(f"partials/{name}").render(**context)


def _cards(html: str) -> List[Tuple[str, str]]:
    return _CARD.findall(html)


def _expected_cards(values: Any) -> List[Tuple[str, str]]:
    return [
        ("Semantic Searches", str(values["semantic_searches"])),
        ("Other Index Searches", str(values["other_index_searches"])),
        ("Regex Searches", str(values["regex_searches"])),
        ("X-Ray Searches", str(values["xray_searches"])),
        ("Other API Calls", str(values["other_api_calls"])),
    ]


class TestPerUserTableXrayColumn:
    def test_xray_column_sits_between_regex_and_other_api(self):
        row = {
            "username": "alice",
            "semantic": 1,
            "other_index": 0,
            "regex": 2,
            "xray": 5,
            "other_api": 3,
            "total": 11,
        }

        html = _render("dashboard_api_per_user.html", per_user_data=[row])

        headers = re.findall(r"<th>([^<]+)</th>", html)
        cells = re.findall(r"<td>(.*?)</td>", html)
        assert headers == [
            "Username",
            "Semantic",
            "Other Index",
            "Regex",
            "X-Ray",
            "Other API",
            "Total",
        ]
        assert cells == ["alice", "1", "0", "2", "5", "3", "<strong>11</strong>"]

    def test_empty_row_spans_every_column(self):
        html = _render("dashboard_api_per_user.html", per_user_data=[])

        assert 'colspan="7"' in html


class TestXraySearchesCard:
    def test_dashboard_stats_shows_xray_card_after_regex(self):
        html = _render("dashboard_stats.html", api_metrics=_METRICS)
        section = html.split('id="api-metrics-data"', 1)[1].split(
            "api-per-user-section", 1
        )[0]

        assert _cards(section) == _expected_cards(_METRICS)

    def test_api_metrics_partial_solo_shows_xray_card_after_regex(self):
        html = _render("dashboard_api_metrics.html", api_metrics=_METRICS)

        assert _cards(html) == _expected_cards(_METRICS)

    def test_api_metrics_partial_cluster_shows_xray_for_total_and_each_node(self):
        node_metrics = {**_METRICS, "xray_searches": 6}

        html = _render(
            "dashboard_api_metrics.html",
            api_metrics=_METRICS,
            is_cluster_mode=True,
            per_node_metrics=[{"node_id": "node-a", "metrics": node_metrics}],
        )

        cards = _cards(html)
        assert cards[:5] == _expected_cards(_METRICS)
        assert cards[5:] == _expected_cards(node_metrics)
        assert "repeat(4, 1fr)" not in html
        assert html.count("repeat(5, 1fr)") == 2
