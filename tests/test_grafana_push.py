"""grafana_uplinks_graph _grafana_push_dashboard with mocked requests."""

from unittest.mock import MagicMock, patch

import grafana_uplinks_graph as grafana


def test_grafana_push_dashboard_create():
    graph = {
        "nodes": [{"id": "n1", "title": "Host"}],
        "edges": [{"id": "e1", "source": "n1", "target": "n2"}],
    }
    resp_get = MagicMock(status_code=404)
    resp_post = MagicMock(status_code=200, json=lambda: {"uid": "dash1"})

    with patch("requests.get", return_value=resp_get):
        with patch("requests.post", return_value=resp_post):
            err = grafana._grafana_push_dashboard(
                "https://grafana.example",
                "key",
                graph,
                "uplinks",
                "Uplinks",
                None,
                "infinity",
                debug=True,
            )
    assert err is None


def test_grafana_push_dashboard_update_existing():
    graph = {"nodes": [], "edges": []}
    resp_get = MagicMock(
        status_code=200,
        json=lambda: {"dashboard": {"id": 5, "version": 2}},
    )
    resp_post = MagicMock(status_code=200, json=lambda: {"uid": "dash1"})

    with patch("requests.get", return_value=resp_get):
        with patch("requests.post", return_value=resp_post):
            err = grafana._grafana_push_dashboard(
                "https://grafana.example",
                "key",
                graph,
                "uplinks",
                "Uplinks",
                "folder",
                None,
                debug=False,
            )
    assert err is None


def test_grafana_push_import_error(monkeypatch):
    def fake_import(name, *a, **k):
        if name == "requests":
            raise ImportError("no requests")
        return __import__(name, *a, **k)

    graph = {"nodes": [], "edges": []}
    with patch("builtins.__import__", side_effect=fake_import):
        err = grafana._grafana_push_dashboard(
            "https://g.example", "k", graph, "u", "t", None, None
        )
    assert err and "requests" in err
