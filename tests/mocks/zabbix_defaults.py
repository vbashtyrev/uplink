"""Default Zabbix JSON-RPC handlers for integration tests."""

from tests.mocks.zabbix_rpc import ZabbixRpcMocker


def build_standard_zabbix_mocker(hosts=None, items=None):
    """
    Return ZabbixRpcMocker with user.get, host.get, item.get, hostgroup.get,
    and no-op create/update/delete handlers.
    hosts: list of {hostid, host, name}
    items: list of item dicts
    """
    hosts = hosts or [
        {"hostid": "101", "host": "ALA-R1", "name": "ALA-R1"},
        {"hostid": "102", "host": "FRN-MX-1", "name": "FRN-MX-1"},
    ]
    items = items or []

    def host_get(params):
        filt = params.get("filter") or {}
        if "host" in filt:
            want = set(filt["host"])
            return [h for h in hosts if h["host"] in want]
        if "name" in filt:
            want = set(filt["name"])
            return [h for h in hosts if h["name"] in want]
        return hosts

    def item_get(params):
        hostids = {str(x) for x in (params.get("hostids") or [])}
        search = (params.get("search") or {}).get("name", "")
        out = [it for it in items if str(it.get("hostid")) in hostids]
        if search:
            out = [it for it in out if search in (it.get("name") or "")]
        return out

    mocker = (
        ZabbixRpcMocker()
        .on("user.get", lambda p: [{"userid": "1"}])
        .on("host.get", host_get)
        .on("hostgroup.get", lambda p: [{"groupid": "2", "name": "Uplinks"}])
        .on("item.get", item_get)
        .on("host.create", lambda p: {"hostids": ["999"]})
        .on("item.create", lambda p: {"itemids": ["1"]})
        .on("item.update", lambda p: True)
        .on("item.delete", lambda p: True)
        .on("trigger.get", lambda p: [])
        .on("trigger.create", lambda p: {"triggerids": ["1"]})
        .on("trigger.update", lambda p: True)
        .on("trigger.delete", lambda p: True)
        .on("usermacro.get", lambda p: [])
        .on("usermacro.create", lambda p: {"hostmacroids": ["1"]})
        .on("usermacro.delete", lambda p: True)
        .on("map.get", lambda p: [])
        .on("map.create", lambda p: {"sysmapids": ["1"]})
        .on("map.update", lambda p: True)
        .on("dashboard.get", lambda p: [])
        .on("dashboard.create", lambda p: {"dashboardids": ["1"]})
        .on("dashboard.update", lambda p: True)
        .on("service.get", lambda p: [])
        .on("service.create", lambda p: {"serviceids": ["1"]})
        .on("service.update", lambda p: True)
        .on("sla.get", lambda p: [])
        .on("sla.create", lambda p: {"slaids": ["1"]})
        .on("sla.update", lambda p: True)
        .on("event.get", lambda p: [])
    )
    return mocker
