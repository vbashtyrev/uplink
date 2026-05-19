"""zabbix_provider_services helper coverage."""

import json

from zabbix_provider_services import (
    _burst_circuits_unique,
    _get_global_provider_sla,
    _get_providers_from_limits,
    _iter_burst_links,
    _load_commit_rates,
)


def test_load_commit_rates_and_providers(tmp_path):
    p = tmp_path / "cr.json"
    p.write_text(
        json.dumps(
            {
                "_provider_limits": {"A": 1, "": None, "B": 2},
                "_provider_sla": 99.5,
                "H": {
                    "Eth1": {
                        "billing_model": "Burst",
                        "provider": "A",
                        "circuit_id": "C1",
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    data, err = _load_commit_rates(str(p))
    assert err is None
    names = _get_providers_from_limits(data)
    assert "A" in names and "B" in names
    assert _get_global_provider_sla(data) == 99.5
    assert list(_iter_burst_links(data))
    assert _burst_circuits_unique(data) == [("C1", "A")]
