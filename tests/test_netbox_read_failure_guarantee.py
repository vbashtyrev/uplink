"""NetBox read failures must not trigger destructive Zabbix writes.

Three independent test layers:
  1. Transport guard in zabbix_request (tested directly, not via is_destructive_call).
  2. Per-script skip with the transport guard deliberately disarmed.
  3. Positive controls on healthy NetBox reads so absence of a call is meaningful.
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tests.mocks.inventory_scope import dry_ssh_minimal_inventory_context
from tests.mocks.netbox_api import build_netbox_for_commit_rates
from tests.mocks.zabbix_defaults import build_standard_zabbix_mocker
from tests.mocks.zabbix_rpc import ZabbixRpcMocker, mock_response
from uplinks.netbox.inventory import (
    ERROR_AUTH_DENIED,
    ERROR_PARTIAL_READ,
    ERROR_PROVIDERS_UNAVAILABLE,
)
from uplinks.zabbix.client import (
    clear_incomplete_netbox_data,
    set_incomplete_netbox_data,
    zabbix_request,
)
from uplinks_config import (
    TRIGGER_DESC_UTIL_WARN_SUFFIX,
    TRIGGER_TAG_NAME,
    TRIGGER_TAG_VALUE,
)
from zabbix_sync_commit_rate import THRESHOLD_ITEM_KEY

FIXTURES = Path(__file__).resolve().parent / "fixtures"
DRY_SSH = FIXTURES / "dry_ssh_minimal.json"
HOST = "ALA-KZT-7280TR-1"
HOST_ID = "101"
ZABBIX_URL = "https://z.example/api_jsonrpc.php"
ZABBIX_TOKEN = "test-token"

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _method_names(mocker):
    return [method for method, _ in mocker.calls]


def _calls_with_method(mocker, method):
    return [params for m, params in mocker.calls if m == method]


def _item_update_with_params(mocker):
    return [p for m, p in mocker.calls if m == "item.update" and "params" in (p or {})]


def _map_update_with_links(mocker):
    return [p for m, p in mocker.calls if m == "map.update" and "links" in (p or {})]


def _partial_inventory_report():
    return {
        "complete": [
            {
                "device": HOST,
                "interface": "Ethernet51/1",
                "provider": "Cogent",
                "circuit_id": "CKT-1",
                "commit_rate_kbps": 10000000,
                "billing_model": "Fixed",
            }
        ],
        "incomplete": [],
        "stats": {"error": ERROR_PARTIAL_READ, "read_errors": 1},
    }


def _auth_inventory_report():
    return {
        "complete": [],
        "incomplete": [],
        "stats": {"error": ERROR_AUTH_DENIED},
    }


def _providers_unavailable_inventory_report():
    return {
        "complete": [
            {
                "device": HOST,
                "interface": "Ethernet51/1",
                "provider": "Cogent",
                "circuit_id": "CKT-1",
                "commit_rate_kbps": 10000000,
                "billing_model": "Fixed",
            }
        ],
        "incomplete": [],
        "stats": {"error": ERROR_PROVIDERS_UNAVAILABLE},
    }


def _healthy_inventory_report():
    return {
        "complete": [
            {
                "device": HOST,
                "interface": "Ethernet51/1",
                "provider": "Cogent",
                "circuit_id": "CKT-1",
                "commit_rate_kbps": 10000000,
                "billing_model": "Fixed",
            }
        ],
        "incomplete": [],
        "stats": {},
    }


def _burst_partial_inventory_report():
    report = _partial_inventory_report()
    report["complete"][0]["billing_model"] = "Burst"
    return report


def _trigger_writes(mocker):
    return [m for m in _method_names(mocker) if m in ("trigger.create", "trigger.update")]


def _host_get_alakzt():
    def host_get(params):
        filt = params.get("filter") or {}
        if "host" in filt:
            want = set(filt["host"])
            return [
                {"hostid": HOST_ID, "host": HOST, "name": HOST}
            ] if HOST in want else []
        if "name" in filt:
            want = set(filt["name"])
            return [
                {"hostid": HOST_ID, "host": HOST, "name": HOST}
            ] if HOST in want else []
        return []

    return host_get


def _seeded_handlers():
    seeded_macros = [
        {"hostmacroid": "m1", "macro": '{$UPLINK.BPS.MAX:"Ethernet51/1"}'},
        {"hostmacroid": "m2", "macro": '{$UPLINK.BPS.MAX:"Ethernet52/1"}'},
        {"hostmacroid": "m3", "macro": '{$UPLINK.UTIL.WARN:"Ethernet51/1"}'},
        {"hostmacroid": "m4", "macro": '{$UPLINK.UTIL.WARN:"Ethernet52/1"}'},
    ]
    stale_util_trigger = {
        "triggerid": "t-stale",
        "description": "Interface Ethernet52/1: {}".format(TRIGGER_DESC_UTIL_WARN_SUFFIX),
        "tags": [{"tag": TRIGGER_TAG_NAME, "value": TRIGGER_TAG_VALUE}],
    }
    legacy_threshold_item = {
        "itemid": "i-thresh",
        "hostid": HOST_ID,
        "key_": "net.if.threshold[Ethernet51/1]",
    }
    aggregate_item = {
        "itemid": "i-agg-in",
        "hostid": "999",
        "key_": "aggregate.bits.in[]",
        "formula": "last(/ALA-KZT-7280TR-1/net.if.in[51])+last(/OTHER/net.if.in[99])",
    }
    legacy_sla_service = {"serviceid": "svc-legacy", "name": "Uplinks Cogent SLA source"}
    map_selements = [
        {"selementid": "10", "elementtype": 0, "elementid": 101, "hostid": 101, "label": HOST},
        {"selementid": "20", "elementtype": 4, "elementid": 0, "label": "Cogent"},
    ]
    map_links = [
        {
            "linkid": "1",
            "selementid1": "10",
            "selementid2": "20",
            "label": "Ethernet51/1\nIn: {?last(/ALA-KZT-7280TR-1/net.if.in[51])}",
        },
        {
            "linkid": "2",
            "selementid1": "10",
            "selementid2": "20",
            "label": "Ethernet52/1\nIn: {?last(/ALA-KZT-7280TR-1/net.if.in[52])}",
        },
    ]

    def usermacro_get(params):
        hostids = {str(x) for x in (params.get("hostids") or [])}
        if HOST_ID not in hostids:
            return []
        prefix = (params.get("search") or {}).get("macro", "")
        return [m for m in seeded_macros if prefix and m["macro"].startswith(prefix)]

    def trigger_get(params):
        hostids = params.get("hostids") or []
        if hostids:
            return [stale_util_trigger]
        return []

    def item_get(params):
        search_key = (params.get("search") or {}).get("key_", "")
        search_name = (params.get("search") or {}).get("name", "")
        hostids = {str(x) for x in (params.get("hostids") or [])}
        if search_key == THRESHOLD_ITEM_KEY and HOST_ID in hostids:
            return [legacy_threshold_item]
        if search_key == "aggregate.bits.in[]" or search_key == "aggregate.bits.in":
            return [aggregate_item]
        if search_name == "Bits received":
            return [
                {
                    "itemid": "1",
                    "hostid": HOST_ID,
                    "name": "Interface Ethernet51/1(x): Bits received",
                    "key_": "net.if.in[51]",
                }
            ]
        if search_name == "Bits sent":
            return [
                {
                    "itemid": "2",
                    "hostid": HOST_ID,
                    "name": "Interface Ethernet51/1(x): Bits sent",
                    "key_": "net.if.out[51]",
                }
            ]
        if search_name.startswith("Interface "):
            return [
                {"key_": "net.if.in[51]", "name": "Interface Ethernet51/1(x): Bits received"},
                {"key_": "net.if.out[51]", "name": "Interface Ethernet51/1(x): Bits sent"},
                {"key_": "net.if.speed[51]", "name": "Interface Ethernet51/1(x): Speed"},
            ]
        return []

    def service_get(params):
        filt = (params.get("filter") or {}).get("name") or []
        if "Uplinks Cogent SLA source" in filt:
            return [legacy_sla_service]
        return []

    def map_get(params):
        if params.get("sysmapids"):
            return [{"sysmapid": "42", "selements": map_selements, "links": map_links}]
        filt = (params.get("filter") or {}).get("name")
        if filt:
            return [{"sysmapid": "42", "selements": map_selements, "links": map_links}]
        return []

    return {
        "usermacro_get": usermacro_get,
        "trigger_get": trigger_get,
        "item_get": item_get,
        "service_get": service_get,
        "map_get": map_get,
        "legacy_sla_service": legacy_sla_service,
        "stale_util_trigger": stale_util_trigger,
        "legacy_threshold_item": legacy_threshold_item,
        "aggregate_item": aggregate_item,
        "map_links": map_links,
    }


def _build_guarantee_mocker(hosts=None, items=None):
    seeded = _seeded_handlers()
    mocker = build_standard_zabbix_mocker(hosts=hosts, items=items)
    return (
        mocker.on("host.get", _host_get_alakzt())
        .on("usermacro.get", seeded["usermacro_get"])
        .on("trigger.get", seeded["trigger_get"])
        .on("item.get", seeded["item_get"])
        .on("service.get", seeded["service_get"])
        .on("map.get", seeded["map_get"])
        .on("map.update", lambda p: True)
        .on("dashboard.create", lambda p: {"dashboardids": ["1"]})
        .on("dashboard.update", lambda p: True)
    )


def _items_alakzt():
    return [
        {
            "itemid": "1",
            "hostid": HOST_ID,
            "name": "Interface Ethernet51/1(x): Bits received",
            "key_": "net.if.in[51]",
        },
        {
            "itemid": "2",
            "hostid": HOST_ID,
            "name": "Interface Ethernet51/1(x): Bits sent",
            "key_": "net.if.out[51]",
        },
    ]


def _disarm_guard(monkeypatch, module):
    """Disarm transport guard and per-script arming so only script skips apply."""
    clear_incomplete_netbox_data()
    monkeypatch.setattr(module, "arm_netbox_incomplete_guard", lambda stats: None)


def _aggregate_nb_ctx_partial():
    ctx = dict(dry_ssh_minimal_inventory_context())
    ctx["read_error"] = True
    ctx["stats"] = {"error": ERROR_PARTIAL_READ, "read_errors": 1}
    ctx["provider_limits_gbps"] = {"Cogent": 10}
    return ctx


def _aggregate_nb_ctx_healthy():
    ctx = dict(dry_ssh_minimal_inventory_context())
    ctx["provider_limits_gbps"] = {"Cogent": 10}
    return ctx


def _aggregate_host_items():
    host_items = {HOST: HOST_ID}
    items_by_host = {
        (HOST, "ethernet51/1"): {
            "bits_in": "net.if.in[51]",
            "bits_out": "net.if.out[51]",
        },
    }
    return host_items, items_by_host


# ---------------------------------------------------------------------------
# Layer 1 — transport guard (zabbix_request), independent of is_destructive_call
# ---------------------------------------------------------------------------

DANGEROUS_RPC_SHAPES = [
    ("usermacro.delete", ["m1"]),
    ("item.delete", ["i1"]),
    ("trigger.delete", ["t1"]),
    ("service.delete", ["s1"]),
    ("dashboard.create", {"name": "test"}),
    ("dashboard.update", {"dashboardid": "1"}),
    ("map.update", {"sysmapid": "1", "links": []}),
    ("map.update", {"sysmapid": "1", "selements": []}),
    ("item.update", {"itemid": "1", "params": "last(/x/y)"}),
    ("host.update", {"hostid": "1", "macros": []}),
    ("service.update", {"serviceid": "1", "problem_tags": []}),
]

HARMLESS_RPC_SHAPES = [
    ("host.get", {"filter": {"host": ["x"]}}),
    ("item.get", {"hostids": ["1"]}),
    ("trigger.create", {"description": "x", "expression": "1"}),
    ("map.create", {"name": "m"}),
    ("item.create", {"name": "n", "key_": "k", "hostid": "1"}),
    ("map.update", {"sysmapid": "1", "width": 100}),
    ("item.update", {"itemid": "1", "name": "renamed"}),
    ("host.update", {"hostid": "1", "name": "renamed"}),
    ("service.update", {"serviceid": "1", "name": "renamed"}),
]


@pytest.fixture
def track_zabbix_post(monkeypatch):
    posts = []

    def fake_post(url, json=None, headers=None, timeout=None):
        posts.append(json or {})
        return mock_response(result=True)

    monkeypatch.setattr("requests.post", fake_post)
    return posts


@pytest.mark.parametrize(
    "method,params",
    DANGEROUS_RPC_SHAPES,
    ids=[s[0] + ("+links" if s[0] == "map.update" and "links" in s[1] else "+selements" if s[0] == "map.update" else "") for s in DANGEROUS_RPC_SHAPES],
)
def test_transport_guard_blocks_dangerous_shapes_when_armed(track_zabbix_post, method, params):
    set_incomplete_netbox_data("partial read")
    result, err = zabbix_request(ZABBIX_URL, ZABBIX_TOKEN, method, params)
    assert result is None
    assert err is not None
    assert "skipped" in err
    assert method in err
    assert track_zabbix_post == []


@pytest.mark.parametrize("method,params", DANGEROUS_RPC_SHAPES, ids=[s[0] for s in DANGEROUS_RPC_SHAPES])
def test_transport_guard_sends_dangerous_shapes_when_disarmed(track_zabbix_post, method, params):
    clear_incomplete_netbox_data()
    result, err = zabbix_request(ZABBIX_URL, ZABBIX_TOKEN, method, params)
    assert err is None
    assert result is True
    assert len(track_zabbix_post) == 1
    assert track_zabbix_post[0]["method"] == method


@pytest.mark.parametrize("method,params", HARMLESS_RPC_SHAPES, ids=[s[0] for s in HARMLESS_RPC_SHAPES])
def test_transport_guard_allows_harmless_shapes_when_armed(track_zabbix_post, method, params):
    set_incomplete_netbox_data("partial read")
    result, err = zabbix_request(ZABBIX_URL, ZABBIX_TOKEN, method, params)
    assert err is None
    assert len(track_zabbix_post) == 1
    assert track_zabbix_post[0]["method"] == method


# ---------------------------------------------------------------------------
# Layer 2 — per-script skip with transport guard disarmed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "failure_report_fn,delete_flag,skip_message",
    [
        (
            _partial_inventory_report,
            "--delete-link-triggers",
            "Skipping --delete-link-triggers: NetBox inventory read failed",
        ),
        (
            _partial_inventory_report,
            "--delete-util-triggers",
            "Skipping --delete-util-triggers: NetBox inventory read failed",
        ),
        (
            _providers_unavailable_inventory_report,
            "--delete-link-triggers",
            "Skipping --delete-link-triggers: NetBox inventory read failed",
        ),
        (
            _providers_unavailable_inventory_report,
            "--delete-util-triggers",
            "Skipping --delete-util-triggers: NetBox inventory read failed",
        ),
    ],
    ids=[
        "partial_delete_link",
        "partial_delete_util",
        "providers_unavailable_delete_link",
        "providers_unavailable_delete_util",
    ],
)
def test_sync_delete_only_exits_on_read_failure_without_writes(
    monkeypatch, zabbix_env, netbox_env, tmp_path, capsys, failure_report_fn, delete_flag, skip_message
):
    import zabbix_sync_commit_rate as mod

    failure_report = failure_report_fn()
    cr = tmp_path / "commit_rates.json"
    cr.write_text("{}", encoding="utf-8")
    mocker = _build_guarantee_mocker(
        hosts=[{"hostid": HOST_ID, "host": HOST, "name": HOST}],
        items=_items_alakzt(),
    ).activate(monkeypatch)
    _disarm_guard(monkeypatch, mod)

    monkeypatch.setattr(mod, "validate_zabbix_token", lambda *a, **k: True)
    monkeypatch.setattr(mod.pynetbox, "api", lambda url, token: MagicMock())
    monkeypatch.setattr(mod, "fetch_uplink_inventory_report", lambda *a, **k: failure_report)
    monkeypatch.setattr(
        sys,
        "argv",
        ["zabbix_sync_commit_rate.py", "-d", str(DRY_SSH), "-f", str(cr), delete_flag],
    )
    with pytest.raises(SystemExit) as exc:
        mod.main()
    assert exc.value.code == 1
    assert skip_message in capsys.readouterr().err
    assert "trigger.delete" not in _method_names(mocker)
    assert "trigger.create" not in _method_names(mocker)


@pytest.mark.parametrize(
    "failure_report_fn",
    [
        _partial_inventory_report,
        _auth_inventory_report,
        _providers_unavailable_inventory_report,
    ],
    ids=["partial_read", "auth_denied", "providers_unavailable"],
)
def test_sync_skip_blocks_deletes_on_read_failure(
    monkeypatch, zabbix_env, netbox_env, tmp_path, failure_report_fn
):
    import zabbix_sync_commit_rate as mod

    failure_report = failure_report_fn()
    cr = tmp_path / "commit_rates.json"
    cr.write_text("{}", encoding="utf-8")
    mocker = _build_guarantee_mocker(
        hosts=[{"hostid": HOST_ID, "host": HOST, "name": HOST}],
        items=_items_alakzt(),
    ).activate(monkeypatch)
    _disarm_guard(monkeypatch, mod)

    monkeypatch.setattr(mod, "validate_zabbix_token", lambda *a, **k: True)
    monkeypatch.setattr(mod.pynetbox, "api", lambda url, token: MagicMock())
    monkeypatch.setattr(mod, "fetch_uplink_inventory_report", lambda *a, **k: failure_report)
    monkeypatch.setattr(
        sys,
        "argv",
        ["zabbix_sync_commit_rate.py", "-d", str(DRY_SSH), "-f", str(cr)],
    )
    with pytest.raises(SystemExit) as exc:
        mod.main()
    assert exc.value.code == 1
    assert "usermacro.delete" not in _method_names(mocker)
    assert "trigger.delete" not in _method_names(mocker)
    assert "item.delete" not in _method_names(mocker)


@pytest.mark.parametrize(
    "failure_kind,failure_ctx",
    [
        ("partial_read", {"read_error": True, "stats": {"error": ERROR_PARTIAL_READ, "read_errors": 1}}),
        ("auth_denied", {"read_error": True, "stats": {"error": ERROR_AUTH_DENIED}}),
        (
            "providers_unavailable",
            {"read_error": True, "stats": {"error": ERROR_PROVIDERS_UNAVAILABLE}},
        ),
    ],
)
def test_aggregate_skip_blocks_formula_update_on_read_failure(
    monkeypatch, zabbix_env, tmp_path, failure_kind, failure_ctx
):
    import zabbix_provider_aggregate as agg

    desc_map = tmp_path / "description_to_name.json"
    desc_map.write_text("{}", encoding="utf-8")
    nb_ctx = dict(dry_ssh_minimal_inventory_context())
    nb_ctx.update(failure_ctx)
    nb_ctx["provider_limits_gbps"] = {"Cogent": 10}

    host_items, items_by_host = _aggregate_host_items()
    mocker = (
        _build_guarantee_mocker()
        .on("host.create", lambda p: {"hostids": ["999"]})
        .on("hostgroup.get", lambda p: [{"groupid": "2"}])
    )
    mocker.activate(monkeypatch)
    _disarm_guard(monkeypatch, agg)

    with patch.object(agg, "_load_netbox_aggregate_context", return_value=nb_ctx):
        with patch.object(
            agg,
            "fetch_zabbix_hosts_and_items",
            lambda url, token, hostnames, debug=False: (dict(host_items), dict(items_by_host), None),
        ):
            if failure_kind == "auth_denied":
                monkeypatch.setattr(
                    sys,
                    "argv",
                    [
                        "zabbix_provider_aggregate.py",
                        "--legacy-dry-ssh",
                        "-d",
                        str(DRY_SSH),
                        "-f",
                        str(desc_map),
                        "--commit-rates",
                        str(tmp_path / "missing.json"),
                    ],
                )
                with pytest.raises(SystemExit) as exc:
                    agg.main()
                assert exc.value.code == 1
            else:
                done, err = agg.run(
                    ZABBIX_URL,
                    ZABBIX_TOKEN,
                    str(tmp_path / "missing.json"),
                    str(DRY_SSH),
                    str(desc_map),
                    cache_path=None,
                )
                assert not done
                assert err
    assert _item_update_with_params(mocker) == []
    assert "host.create" not in _method_names(mocker)


@pytest.mark.parametrize(
    "failure_ctx",
    [
        {"read_error": True, "stats": {"error": ERROR_PARTIAL_READ, "read_errors": 1}},
        {"read_error": True, "stats": {"error": ERROR_AUTH_DENIED}},
        {"read_error": True, "stats": {"error": ERROR_PROVIDERS_UNAVAILABLE}},
    ],
    ids=["partial_read", "auth_denied", "providers_unavailable"],
)
def test_dashboard_skip_blocks_writes_on_read_failure(
    monkeypatch, zabbix_env, netbox_env, failure_ctx
):
    import zabbix_uplinks_dashboard as dash

    ctx = dict(dry_ssh_minimal_inventory_context())
    ctx.update(failure_ctx)
    mocker = _build_guarantee_mocker(
        hosts=[{"hostid": HOST_ID, "host": HOST, "name": HOST}],
        items=_items_alakzt(),
    ).activate(monkeypatch)
    _disarm_guard(monkeypatch, dash)

    with patch.object(dash, "load_uplink_provider_context", return_value=ctx):
        monkeypatch.setattr(
            sys,
            "argv",
            ["zabbix_uplinks_dashboard.py", "--legacy-dry-ssh", "-f", str(DRY_SSH), "--no-cache"],
        )
        with pytest.raises(SystemExit) as exc:
            dash.main()
    assert exc.value.code == 1
    assert "dashboard.create" not in _method_names(mocker)
    assert "dashboard.update" not in _method_names(mocker)


@pytest.mark.parametrize(
    "failure_report_fn",
    [
        _partial_inventory_report,
        _auth_inventory_report,
        _providers_unavailable_inventory_report,
    ],
    ids=["partial_read", "auth_denied", "providers_unavailable"],
)
def test_services_skip_blocks_legacy_delete_on_read_failure(
    monkeypatch, zabbix_env, failure_report_fn
):
    import zabbix_provider_services as svc

    failure_report = failure_report_fn()
    ctx = {
        "report": failure_report,
        "providers": {"Cogent"},
        "burst_circuits": [],
        "provider_slo_percent": {},
        "provider_limits_gbps": {},
        "stats": failure_report.get("stats") or {},
        "read_error": True,
    }

    mocker = (
        ZabbixRpcMocker()
        .on("user.get", lambda p: [{"userid": "1"}])
        .on("service.get", _seeded_handlers()["service_get"])
        .on("service.create", lambda p: {"serviceids": ["1"]})
        .on("sla.get", lambda p: [])
        .on("sla.create", lambda p: {"slaids": ["1"]})
        .on("service.delete", lambda p: True)
    )
    mocker.activate(monkeypatch)
    _disarm_guard(monkeypatch, svc)

    monkeypatch.setattr(svc, "_load_netbox_services_context", lambda debug=False: ctx)
    monkeypatch.setattr(sys, "argv", ["zabbix_provider_services.py"])
    with pytest.raises(SystemExit) as exc:
        svc.main()
    assert exc.value.code == 1
    assert "service.delete" not in _method_names(mocker)


@pytest.mark.parametrize(
    "failure_report_fn",
    [
        _partial_inventory_report,
        _auth_inventory_report,
        _providers_unavailable_inventory_report,
    ],
    ids=["partial_read", "auth_denied", "providers_unavailable"],
)
def test_sla_skip_exits_on_read_failure_without_report_reads(
    monkeypatch, zabbix_env, failure_report_fn
):
    import zabbix_provider_sla as sla_mod

    failure_report = failure_report_fn()
    ctx = {
        "report": failure_report,
        "providers": {"Cogent"},
        "burst_circuits": [],
        "provider_slo_percent": {"Cogent": 99.9},
        "provider_limits_gbps": {"Cogent": 10},
        "stats": failure_report.get("stats") or {},
        "read_error": True,
    }

    mocker = (
        ZabbixRpcMocker()
        .on("host.get", lambda p: [{"hostid": HOST_ID, "host": HOST, "name": HOST}])
        .on("trigger.get", lambda p: [{"triggerid": "t1", "description": "x"}])
        .on("event.get", lambda p: [{"eventid": "e1"}])
    )
    mocker.activate(monkeypatch)
    _disarm_guard(monkeypatch, sla_mod)

    monkeypatch.setattr(
        sla_mod,
        "_load_netbox_services_context",
        lambda debug=False, inventory_report=None: ctx,
    )
    monkeypatch.setattr(sys, "argv", ["zabbix_provider_sla.py", "--days", "1"])
    with pytest.raises(SystemExit) as exc:
        sla_mod.main()
    assert exc.value.code == 1
    assert "host.get" not in _method_names(mocker)
    assert "trigger.get" not in _method_names(mocker)
    assert "event.get" not in _method_names(mocker)


@pytest.mark.parametrize(
    "failure_ctx",
    [
        {"read_error": True, "stats": {"error": ERROR_PARTIAL_READ, "read_errors": 1}},
        {"read_error": True, "stats": {"error": ERROR_AUTH_DENIED}},
        {"read_error": True, "stats": {"error": ERROR_PROVIDERS_UNAVAILABLE}},
    ],
    ids=["partial_read", "auth_denied", "providers_unavailable"],
)
def test_map_skip_blocks_map_update_on_read_failure(
    monkeypatch, zabbix_env, tmp_path, failure_ctx
):
    import zabbix_map as zm

    desc = tmp_path / "description_to_name.json"
    desc.write_text('{"Uplink: Cogent 10G": "Cogent"}', encoding="utf-8")
    ctx = dict(dry_ssh_minimal_inventory_context())
    ctx.update(failure_ctx)

    host_id = {HOST: HOST_ID}
    items = {
        (HOST, "ethernet51/1"): {
            "bits_in": 'net.if.in["Ethernet51/1"]',
            "bits_out": 'net.if.out["Ethernet51/1"]',
            "itemid_in": "1",
            "itemid_out": "2",
        },
    }

    mocker = _build_guarantee_mocker().activate(monkeypatch)
    _disarm_guard(monkeypatch, zm)

    with patch.object(zm, "fetch_zabbix_hosts_and_items", lambda *a, **k: (host_id, items, None)):
        with patch.object(zm, "validate_zabbix_token", lambda *a, **k: (True, None)):
            with patch.object(zm, "load_uplink_provider_context", return_value=ctx):
                monkeypatch.setattr(
                    sys,
                    "argv",
                    [
                        "zabbix_map.py",
                        "--legacy-dry-ssh",
                        "-f",
                        str(DRY_SSH),
                        "-m",
                        str(desc),
                        "--update-map",
                        "--no-cache",
                    ],
                )
                with pytest.raises(SystemExit) as exc:
                    zm.main()
    assert exc.value.code == 1
    assert "map.update" not in _method_names(mocker)


@pytest.mark.parametrize(
    "failure_ctx",
    [
        {"read_error": True, "stats": {"error": ERROR_PARTIAL_READ, "read_errors": 1}},
        {"read_error": True, "stats": {"error": ERROR_AUTH_DENIED}},
        {"read_error": True, "stats": {"error": ERROR_PROVIDERS_UNAVAILABLE}},
    ],
    ids=["partial_read", "auth_denied", "providers_unavailable"],
)
def test_map_default_create_skips_writes_on_read_failure(
    monkeypatch, zabbix_env, tmp_path, failure_ctx
):
    import zabbix_map as zm

    desc = tmp_path / "description_to_name.json"
    desc.write_text('{"Uplink: Cogent 10G": "Cogent"}', encoding="utf-8")
    ctx = dict(dry_ssh_minimal_inventory_context())
    ctx.update(failure_ctx)

    host_id = {HOST: HOST_ID}
    items = {
        (HOST, "ethernet51/1"): {
            "bits_in": 'net.if.in["Ethernet51/1"]',
            "bits_out": 'net.if.out["Ethernet51/1"]',
            "itemid_in": "1",
            "itemid_out": "2",
        },
    }

    mocker = (
        _build_guarantee_mocker()
        .on("map.get", lambda p: [])
        .on("map.create", lambda p: {"sysmapids": ["99"]})
    )
    mocker.activate(monkeypatch)
    _disarm_guard(monkeypatch, zm)

    with patch.object(zm, "fetch_zabbix_hosts_and_items", lambda *a, **k: (host_id, items, None)):
        with patch.object(zm, "validate_zabbix_token", lambda *a, **k: (True, None)):
            with patch.object(zm, "load_uplink_provider_context", return_value=ctx):
                monkeypatch.setattr(
                    sys,
                    "argv",
                    [
                        "zabbix_map.py",
                        "--legacy-dry-ssh",
                        "-f",
                        str(DRY_SSH),
                        "-m",
                        str(desc),
                        "--no-cache",
                    ],
                )
                with pytest.raises(SystemExit) as exc:
                    zm.main()
    assert exc.value.code == 1
    assert "map.create" not in _method_names(mocker)
    assert "map.update" not in _method_names(mocker)


# ---------------------------------------------------------------------------
# Layer 3 — positive controls on healthy NetBox reads
# ---------------------------------------------------------------------------


def test_sync_partial_read_blocks_util_trigger_writes(
    monkeypatch, zabbix_env, netbox_env, tmp_path
):
    """Partial NetBox read must not create/update utilization triggers."""
    import zabbix_sync_commit_rate as mod

    failure_report = _partial_inventory_report()
    cr = tmp_path / "commit_rates.json"
    cr.write_text("{}", encoding="utf-8")
    mocker = _build_guarantee_mocker(
        hosts=[{"hostid": HOST_ID, "host": HOST, "name": HOST}],
        items=_items_alakzt(),
    ).activate(monkeypatch)
    _disarm_guard(monkeypatch, mod)

    monkeypatch.setattr(mod, "validate_zabbix_token", lambda *a, **k: True)
    monkeypatch.setattr(mod.pynetbox, "api", lambda url, token: MagicMock())
    monkeypatch.setattr(mod, "fetch_uplink_inventory_report", lambda *a, **k: failure_report)
    monkeypatch.setattr(
        sys,
        "argv",
        ["zabbix_sync_commit_rate.py", "-d", str(DRY_SSH), "-f", str(cr)],
    )
    with pytest.raises(SystemExit) as exc:
        mod.main()
    assert exc.value.code == 1
    assert _trigger_writes(mocker) == []


def test_sync_partial_read_blocks_burst_trigger_writes(
    monkeypatch, zabbix_env, netbox_env, tmp_path
):
    """Partial NetBox read must not create/update Burst link triggers."""
    import zabbix_sync_commit_rate as mod

    failure_report = _burst_partial_inventory_report()
    cr = tmp_path / "commit_rates.json"
    cr.write_text("{}", encoding="utf-8")
    mocker = _build_guarantee_mocker(
        hosts=[{"hostid": HOST_ID, "host": HOST, "name": HOST}],
        items=_items_alakzt(),
    ).activate(monkeypatch)
    _disarm_guard(monkeypatch, mod)

    monkeypatch.setattr(mod, "validate_zabbix_token", lambda *a, **k: True)
    monkeypatch.setattr(mod.pynetbox, "api", lambda url, token: MagicMock())
    monkeypatch.setattr(mod, "fetch_uplink_inventory_report", lambda *a, **k: failure_report)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "zabbix_sync_commit_rate.py",
            "-d",
            str(DRY_SSH),
            "-f",
            str(cr),
            "--create-link-triggers",
        ],
    )
    with pytest.raises(SystemExit) as exc:
        mod.main()
    assert exc.value.code == 1
    assert _trigger_writes(mocker) == []


def test_sync_skip_blocks_macro_delete_on_relations_read_failure(
    monkeypatch, zabbix_env, netbox_env, tmp_path
):
    """Relations read_errors must arm fail-closed gate before macro replacement."""
    import zabbix_sync_commit_rate as mod

    nb = build_netbox_for_commit_rates(
        device_name=HOST,
        iface_name="Ethernet51/1",
        device_tag="border",
        provider_name="Cogent",
    )
    inv_report = _healthy_inventory_report()
    inv_path = tmp_path / "inventory.json"
    inv_path.write_text(json.dumps(inv_report), encoding="utf-8")

    def fail_interfaces_filter(**kwargs):
        raise RuntimeError("dcim.interfaces.filter unavailable")

    nb.dcim.interfaces.filter = fail_interfaces_filter

    mocker = _build_guarantee_mocker(
        hosts=[{"hostid": HOST_ID, "host": HOST, "name": HOST}],
        items=_items_alakzt(),
    ).activate(monkeypatch)
    _disarm_guard(monkeypatch, mod)

    monkeypatch.setattr(mod, "validate_zabbix_token", lambda *a, **k: True)
    monkeypatch.setattr(mod.pynetbox, "api", lambda url, token: nb)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "zabbix_sync_commit_rate.py",
            "--inventory-file",
            str(inv_path),
            "--no-util-triggers",
        ],
    )
    with pytest.raises(SystemExit) as exc:
        mod.main()
    assert exc.value.code == 1
    assert "usermacro.delete" not in _method_names(mocker)


def test_positive_sync_deletes_stale_bps_macros(monkeypatch, zabbix_env, netbox_env, tmp_path):
    import zabbix_sync_commit_rate as mod

    nb = build_netbox_for_commit_rates(
        device_name=HOST,
        iface_name="Ethernet51/1",
        device_tag="border",
        provider_name="Cogent",
    )
    cr = tmp_path / "commit_rates.json"
    cr.write_text("{}", encoding="utf-8")

    macro_deleted = []
    mocker = _build_guarantee_mocker(
        hosts=[{"hostid": HOST_ID, "host": HOST, "name": HOST}],
        items=_items_alakzt(),
    )
    mocker.on("usermacro.delete", lambda p: macro_deleted.extend(p) or True)
    mocker.activate(monkeypatch)

    monkeypatch.setattr(mod, "validate_zabbix_token", lambda *a, **k: True)
    monkeypatch.setattr(mod.pynetbox, "api", lambda url, token: nb)
    monkeypatch.setenv("NETBOX_TAG", "border")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "zabbix_sync_commit_rate.py",
            "-d",
            str(DRY_SSH),
            "-f",
            str(cr),
            "--no-util-triggers",
        ],
    )
    mod.main()
    assert macro_deleted


def test_positive_sync_deletes_stale_util_trigger(monkeypatch, zabbix_env, netbox_env, tmp_path):
    import zabbix_sync_commit_rate as mod

    nb = build_netbox_for_commit_rates(
        device_name=HOST,
        iface_name="Ethernet51/1",
        device_tag="border",
        provider_name="Cogent",
    )
    cr = tmp_path / "commit_rates.json"
    cr.write_text("{}", encoding="utf-8")

    trigger_deleted = []
    mocker = _build_guarantee_mocker(
        hosts=[{"hostid": HOST_ID, "host": HOST, "name": HOST}],
        items=_items_alakzt(),
    )
    mocker.on("trigger.delete", lambda p: trigger_deleted.extend(p) or True)
    mocker.activate(monkeypatch)

    monkeypatch.setattr(mod, "validate_zabbix_token", lambda *a, **k: True)
    monkeypatch.setattr(mod.pynetbox, "api", lambda url, token: nb)
    monkeypatch.setenv("NETBOX_TAG", "border")
    monkeypatch.setattr(
        sys,
        "argv",
        ["zabbix_sync_commit_rate.py", "-d", str(DRY_SSH), "-f", str(cr)],
    )
    mod.main()
    assert trigger_deleted
    assert "t-stale" in trigger_deleted


def test_positive_sync_deletes_threshold_items(monkeypatch, zabbix_env, netbox_env, tmp_path):
    import zabbix_sync_commit_rate as mod

    nb = build_netbox_for_commit_rates(
        device_name=HOST,
        iface_name="Ethernet51/1",
        device_tag="border",
        provider_name="Cogent",
    )
    cr = tmp_path / "commit_rates.json"
    cr.write_text("{}", encoding="utf-8")

    item_deleted = []
    mocker = _build_guarantee_mocker(
        hosts=[{"hostid": HOST_ID, "host": HOST, "name": HOST}],
        items=_items_alakzt(),
    )
    mocker.on("item.delete", lambda p: item_deleted.extend(p) or True)
    mocker.activate(monkeypatch)

    monkeypatch.setattr(mod, "validate_zabbix_token", lambda *a, **k: True)
    monkeypatch.setattr(mod.pynetbox, "api", lambda url, token: nb)
    monkeypatch.setenv("NETBOX_TAG", "border")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "zabbix_sync_commit_rate.py",
            "-d",
            str(DRY_SSH),
            "-f",
            str(cr),
            "--no-util-triggers",
        ],
    )
    mod.main()
    assert item_deleted
    assert "i-thresh" in item_deleted


def test_positive_aggregate_updates_formula_item(monkeypatch, zabbix_env, tmp_path):
    import zabbix_provider_aggregate as agg

    desc_map = tmp_path / "description_to_name.json"
    desc_map.write_text("{}", encoding="utf-8")
    host_items, items_by_host = _aggregate_host_items()

    item_updates = []
    mocker = (
        _build_guarantee_mocker()
        .on("host.create", lambda p: {"hostids": ["999"]})
        .on("hostgroup.get", lambda p: [{"groupid": "2"}])
        .on("item.update", lambda p: item_updates.append(p) or True)
    )
    mocker.activate(monkeypatch)

    with patch.object(agg, "_load_netbox_aggregate_context", return_value=_aggregate_nb_ctx_healthy()):
        with patch.object(
            agg,
            "fetch_zabbix_hosts_and_items",
            lambda url, token, hostnames, debug=False: (dict(host_items), dict(items_by_host), None),
        ):
            done, err = agg.run(
                ZABBIX_URL,
                ZABBIX_TOKEN,
                str(tmp_path / "missing.json"),
                str(DRY_SSH),
                str(desc_map),
                cache_path=None,
            )
    assert err is None
    assert done
    assert any("params" in p for p in item_updates)


def test_positive_map_updates_links_on_healthy_read(monkeypatch, zabbix_env, tmp_path):
    import zabbix_map as zm

    desc = tmp_path / "description_to_name.json"
    desc.write_text('{"Uplink: Cogent 10G": "Cogent"}', encoding="utf-8")
    host_id = {HOST: HOST_ID}
    items = {
        (HOST, "ethernet51/1"): {
            "bits_in": 'net.if.in["Ethernet51/1"]',
            "bits_out": 'net.if.out["Ethernet51/1"]',
            "itemid_in": "1",
            "itemid_out": "2",
        },
    }

    map_updates = []
    mocker = _build_guarantee_mocker()
    mocker.on("map.update", lambda p: map_updates.append(p) or True)
    mocker.activate(monkeypatch)

    with patch.object(zm, "fetch_zabbix_hosts_and_items", lambda *a, **k: (host_id, items, None)):
        with patch.object(zm, "validate_zabbix_token", lambda *a, **k: (True, None)):
            with patch.object(
                zm, "load_uplink_provider_context", return_value=dry_ssh_minimal_inventory_context()
            ):
                monkeypatch.setattr(
                    sys,
                    "argv",
                    [
                        "zabbix_map.py",
                        "--legacy-dry-ssh",
                        "-f",
                        str(DRY_SSH),
                        "-m",
                        str(desc),
                        "--update-map",
                        "--no-cache",
                    ],
                )
                zm.main()
    assert _map_update_with_links(mocker)


def test_positive_dashboard_writes_on_healthy_read(monkeypatch, zabbix_env, netbox_env):
    import zabbix_uplinks_dashboard as dash

    dashboard_items = [
        {
            "itemid": "501",
            "hostid": HOST_ID,
            "name": "Interface Ethernet51/1: Bits received",
            "key_": 'net.if.in["Ethernet51/1"]',
        },
        {
            "itemid": "502",
            "hostid": HOST_ID,
            "name": "Interface Ethernet51/1: Bits sent",
            "key_": 'net.if.out["Ethernet51/1"]',
        },
        {
            "itemid": "601",
            "hostid": "102",
            "name": "Interface ae5.0: Bits received",
            "key_": 'net.if.in["ae5.0"]',
        },
        {
            "itemid": "602",
            "hostid": "102",
            "name": "Interface ae5.0: Bits sent",
            "key_": 'net.if.out["ae5.0"]',
        },
    ]
    hosts = [
        {"hostid": HOST_ID, "host": HOST, "name": HOST},
        {"hostid": "102", "host": "FRN-MX-1", "name": "FRN-MX-1"},
    ]

    def host_get(params):
        filt = params.get("filter") or {}
        if "host" in filt:
            want = set(filt["host"])
            return [h for h in hosts if h["host"] in want]
        if "name" in filt:
            want = set(filt["name"])
            return [h for h in hosts if h["name"] in want]
        return hosts

    mocker = (
        build_standard_zabbix_mocker(hosts=hosts, items=dashboard_items)
        .on("host.get", host_get)
        .on("dashboard.get", lambda p: [])
    )
    mocker.activate(monkeypatch)

    desc_map = {
        "Uplink: Cogent 10G": "Cogent",
        "Uplink: Hurricane": "Hurricane",
        "Uplink: Hurricane member": "Hurricane",
        "Uplink: Hurricane LAG": "Hurricane",
    }
    with patch.object(dash, "load_description_map", return_value=desc_map):
        with patch.object(dash, "load_uplink_provider_context", return_value=dry_ssh_minimal_inventory_context()):
            monkeypatch.setattr(
                sys,
                "argv",
                ["zabbix_uplinks_dashboard.py", "--legacy-dry-ssh", "-f", str(DRY_SSH), "--no-cache"],
            )
            dash.main()
    assert "dashboard.create" in _method_names(mocker) or "dashboard.update" in _method_names(mocker)


def test_positive_services_deletes_legacy_sla_source(monkeypatch, zabbix_env):
    import zabbix_provider_services as svc

    ctx = {
        "report": _healthy_inventory_report(),
        "providers": {"Cogent"},
        "burst_circuits": [],
        "provider_slo_percent": {},
        "provider_limits_gbps": {},
        "stats": {},
        "read_error": False,
    }

    service_deleted = []
    mocker = (
        ZabbixRpcMocker()
        .on("user.get", lambda p: [{"userid": "1"}])
        .on("service.get", _seeded_handlers()["service_get"])
        .on("service.create", lambda p: {"serviceids": ["1"]})
        .on("sla.get", lambda p: [])
        .on("sla.create", lambda p: {"slaids": ["1"]})
        .on("service.delete", lambda p: service_deleted.extend(p) or True)
    )
    mocker.activate(monkeypatch)

    monkeypatch.setattr(svc, "_load_netbox_services_context", lambda debug=False: ctx)
    monkeypatch.setattr(sys, "argv", ["zabbix_provider_services.py"])
    svc.main()
    assert service_deleted
    assert "svc-legacy" in service_deleted
