"""Track Zabbix map state across map.get / map.update in tests."""

from zabbix_map import MAP_NAME


class MapStateTracker:
    """Simulate map selements/links returned by map.get after map.update."""

    def __init__(self, sysmapid="55", selements=None, links=None, exists=True):
        self.sysmapid = str(sysmapid)
        self.selements = [dict(el) for el in (selements or [])]
        self.links = [dict(el) for el in (links or [])]
        self.exists = exists
        self.creates = []
        self.updates = []
        self.width = None
        self.height = None
        sid_values = [
            int(el["selementid"])
            for el in self.selements
            if str(el.get("selementid", "")).isdigit()
        ]
        self._next_sid = (max(sid_values) if sid_values else 0) + 1

    def _record_dimensions(self, params):
        if "width" in params and params["width"] is not None:
            self.width = int(params["width"])
        if "height" in params and params["height"] is not None:
            self.height = int(params["height"])

    def map_get(self, params):
        if params.get("sysmapids"):
            return [{
                "sysmapid": self.sysmapid,
                "selements": [dict(el) for el in self.selements],
                "links": [dict(el) for el in self.links],
            }]
        filt = (params.get("filter") or {}).get("name")
        if filt == MAP_NAME:
            if not self.exists:
                return []
            payload = {"sysmapid": self.sysmapid}
            if self.selements or self.links:
                payload["selements"] = [dict(el) for el in self.selements]
                payload["links"] = [dict(el) for el in self.links]
            return [payload]
        return []

    def map_update(self, params):
        params = dict(params)
        self.updates.append(params)
        self._record_dimensions(params)
        self.exists = True
        if "selements" in params:
            merged = []
            for el in params["selements"]:
                el = dict(el)
                sid = el.get("selementid")
                if sid in (None, "", 0, "0"):
                    el["selementid"] = str(self._next_sid)
                    self._next_sid += 1
                else:
                    el["selementid"] = str(sid)
                merged.append(el)
            self.selements = merged
        if "links" in params:
            self.links = [dict(el) for el in params["links"]]
        return True

    def map_create(self, params):
        params = dict(params)
        self.creates.append(params)
        self._record_dimensions(params)
        self.exists = True
        return {"sysmapids": [self.sysmapid]}
