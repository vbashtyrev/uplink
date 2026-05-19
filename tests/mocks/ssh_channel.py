"""Fake Paramiko channel for Arista/Juniper SSH command scripts."""

import json
from collections import deque
from pathlib import Path


class FakeChannel:
    """Scripted recv/send channel: each send advances to the next response chunk."""

    def __init__(self, responses):
        """
        responses: list of str chunks returned sequentially on recv when recv_ready.
        """
        self._queue = deque(responses)
        self._pending = b""
        self.sent = []

    def recv_ready(self):
        return bool(self._pending) or bool(self._queue)

    def recv(self, n):
        if not self._pending and self._queue:
            chunk = self._queue.popleft()
            self._pending = chunk.encode("utf-8") if isinstance(chunk, str) else chunk
        if not self._pending:
            return b""
        data = self._pending[:n]
        self._pending = self._pending[n:]
        return data

    def send(self, data):
        self.sent.append(data)

    def settimeout(self, _timeout):
        pass


class FakeSSHClient:
    def __init__(self, channel):
        self._channel = channel

    def set_missing_host_key_policy(self, _policy):
        pass

    def connect(self, hostname=None, **_kwargs):
        pass

    def invoke_shell(self, **_kwargs):
        return self._channel

    def close(self):
        pass


def arista_script_from_fixtures(fixtures_dir, prompt_host="router"):
    """Build response sequence for get_arista_uplink_stats."""
    fixtures_dir = Path(fixtures_dir)

    def load(name):
        return (fixtures_dir / name).read_text(encoding="utf-8")

    prompt = "admin@{}# ".format(prompt_host)
    desc = load("arista_ssh_descriptions.json")
    vrf = load("arista_ssh_vrf.json")
    iface = load("arista_ssh_interface.json")
    trans = load("arista_ssh_transceiver.json")
    return [
        prompt,
        "Password: \n",
        prompt,
        desc + "\n" + prompt,
        vrf + "\n" + prompt,
        iface + "\n" + prompt,
        trans + "\n" + prompt,
    ]


def juniper_logical_uplink_script(fixtures_dir, prompt_host="mx1"):
    """SSH script for get_juniper_uplink_stats with ae5.0 logical uplink + LAG member."""
    fixtures_dir = Path(fixtures_dir)

    def load(name):
        return (fixtures_dir / name).read_text(encoding="utf-8")

    prompt = "admin@{}> ".format(prompt_host)
    desc = load("juniper_descriptions.json")
    chassis = load("juniper_ssh_chassis.json")
    lacp = load("juniper_ssh_lacp.json")
    ae5 = load("juniper_ssh_ae5.json")
    et = load("juniper_ssh_et.json")
    optics = load("juniper_ssh_optics.json")
    return [
        prompt,
        "Password: \n",
        prompt,
        desc + "\n" + prompt,
        chassis + "\n" + prompt,
        "set routing-instances internet interface ae5.0\n" + prompt,
        lacp + "\n" + prompt,
        ae5 + "\n" + prompt,
        et + "\n" + prompt,
        optics + "\n" + prompt,
    ]
