"""grafana_uplinks_graph _grafana_push_dashboard error paths."""

import grafana_uplinks_graph as g


def test_grafana_push_missing_credentials():
    err = g._grafana_push_dashboard(
        "",
        "",
        {"nodes": [], "edges": []},
        "uid",
        "title",
        None,
        None,
    )
    assert "GRAFANA" in err
