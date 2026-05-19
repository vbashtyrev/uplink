"""Mock Zabbix JSON-RPC responses by patching requests.post."""

import json
from typing import Any, Callable, Dict, List, Optional, Tuple


JsonDict = Dict[str, Any]
Handler = Callable[[JsonDict], Any]


class _FakeResponse:
    def __init__(self, payload: JsonDict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception("HTTP {}".format(self.status_code))

    def json(self):
        return self._payload


def mock_response(result=None, error=None, status_code=200):
    """Build a fake requests.Response-like object for zabbix_request."""
    body = {"jsonrpc": "2.0", "id": 1}
    if error is not None:
        body["error"] = error
    else:
        body["result"] = result
    return _FakeResponse(body, status_code=status_code)


class ZabbixRpcMocker:
    """
    Route Zabbix API methods to handler functions.
    Usage:
        mocker = ZabbixRpcMocker()
        mocker.on("host.get", lambda p: [...])
        with mocker.activate(monkeypatch):
            ...
    """

    def __init__(self):
        self.handlers: Dict[str, Handler] = {}
        self.calls: List[Tuple[str, JsonDict]] = []

    def on(self, method: str, handler: Handler) -> "ZabbixRpcMocker":
        self.handlers[method] = handler
        return self

    def _dispatch(self, payload: JsonDict) -> _FakeResponse:
        method = payload.get("method", "")
        params = payload.get("params") or {}
        self.calls.append((method, params))
        handler = self.handlers.get(method)
        if handler is None:
            return mock_response(
                error={"code": -32601, "message": "Method not found", "data": method},
            )
        try:
            result = handler(params)
        except Exception as exc:
            return mock_response(error={"code": -32500, "message": str(exc), "data": ""})
        return mock_response(result=result)

    def fake_post(self, url, json=None, headers=None, timeout=None):
        return self._dispatch(json or {})

    def activate(self, monkeypatch):
        """Patch requests.post (import site used by zabbix_map)."""
        monkeypatch.setattr("requests.post", self.fake_post)
        return self

    def method_names(self) -> List[str]:
        return [m for m, _ in self.calls]

    def last_params(self, method: str) -> Optional[JsonDict]:
        for m, p in reversed(self.calls):
            if m == method:
                return p
        return None
