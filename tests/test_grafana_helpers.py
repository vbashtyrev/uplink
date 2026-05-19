"""Unit tests for grafana_uplinks_graph helpers."""

from unittest.mock import MagicMock

import pytest

from grafana_uplinks_graph import (
    _csv_escape,
    _get_grafana_env,
    _graph_to_inline_csv,
    _grafana_push_dashboard,
    build_edges,
    load_devices_json,
)


def test_csv_escape_and_graph_csv():
    assert _csv_escape('a"b') == '"a""b"'
    graph = {
        "nodes": [{"id": "h1", "title": "Host"}],
        "edges": [{
            "id": "e1",
            "source": "h1",
            "target": "isp1",
            "detail__hostname": "H",
            "detail__iface": "Eth1",
            "detail__isp": "ISP",
        }],
    }
    nodes_csv, edges_csv = _graph_to_inline_csv(graph)
    assert "id,title" in nodes_csv
    assert "detail__hostname" in edges_csv


def test_build_edges_from_devices():
    from pathlib import Path

    FIXTURES = Path(__file__).resolve().parent / "fixtures"
    data, err = load_devices_json(str(FIXTURES / "dry_ssh_minimal.json"))
    assert err is None
    edges = build_edges(data["devices"], {"ALA-KZT-7280TR-1": "101"}, {}, {})
    assert edges


def test_grafana_push_dashboard_create(monkeypatch):
    monkeypatch.setenv("GRAFANA_URL", "https://grafana.example")
    monkeypatch.setenv("GRAFANA_API_KEY", "key")

    class Resp:
        status_code = 404

        def json(self):
            return {}

    class OkResp:
        status_code = 200

        def json(self):
            return {"id": 1, "uid": "uplinks", "url": "/d/uplinks"}

        def raise_for_status(self):
            pass

    posts = []

    def fake_post(url, **kw):
        posts.append(url)
        return OkResp()

    def fake_get(url, **kw):
        return Resp()

    monkeypatch.setattr("requests.get", fake_get)
    monkeypatch.setattr("requests.post", fake_post)
    graph = {"nodes": [{"id": "1", "title": "A"}], "edges": []}
    err = _grafana_push_dashboard(
        "https://grafana.example",
        "key",
        graph,
        dashboard_uid="uplinks",
        dashboard_title="Uplinks",
        folder_uid=None,
        infinity_uid="infinity",
    )
    assert err is None
    assert posts


def test_get_grafana_env_missing():
    import os

    for k in ("GRAFANA_URL", "GRAFANA_API_KEY", "GRAFANA_TOKEN"):
        os.environ.pop(k, None)
    url, key = _get_grafana_env()
    assert url == ""
    assert key == ""
