#!/usr/bin/env python3
"""Create or update Zabbix services per uplink provider and per-provider SLAs."""

import argparse
import json
import sys

from zabbix_map import (
    _get_zabbix_url_token,
    zabbix_request,
    validate_zabbix_token,
)
from uplinks_config import UPLINKS_AGGREGATE_HOST_PREFIX


DEFAULT_COMMIT_RATES = "commit_rates.json"


def _load_commit_rates(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None, "file not found: {}".format(path)
    except json.JSONDecodeError as e:
        return None, "invalid JSON in {}: {}".format(path, e)
    if not isinstance(data, dict):
        return None, "unexpected JSON root in {}".format(path)
    return data, None


def _get_providers_from_limits(commit_rates):
    limits = commit_rates.get("_provider_limits")
    if not isinstance(limits, dict):
        return []
    providers = []
    for name, val in limits.items():
        if not name or val is None:
            continue
        providers.append(str(name).strip())
    return sorted(set(p for p in providers if p))


def _get_global_provider_sla(commit_rates):
    """Return global target SLA (float) from commit_rates['_provider_sla'] or None."""
    val = commit_rates.get("_provider_sla")
    if isinstance(val, (int, float)):
        try:
            return float(val)
        except (TypeError, ValueError):
            return None
    return None


def _get_or_create_parent_service(url, token, name, debug=False):
    """Return parent serviceid by name, create if missing."""
    if not name:
        return None, None
    res, err = zabbix_request(
        url,
        token,
        "service.get",
        {
            "output": ["serviceid", "name"],
            "filter": {"name": [name]},
        },
        debug=debug,
    )
    if err:
        return None, "service.get (parent): {}".format(err)
    if res:
        return res[0]["serviceid"], None

    payload = {
        "name": name,
        "algorithm": 1,
        "sortorder": 1,
    }
    result, err = zabbix_request(url, token, "service.create", payload, debug=debug)
    if err:
        return None, "service.create (parent): {}".format(err)
    return result["serviceids"][0], None


def _get_or_create_provider_service(url, token, provider, parentid, debug=False):
    """Create or update service for a single provider."""
    service_name = "Uplinks {}".format(provider)
    problem_tags = [
        {
            "tag": "provider",
            "value": provider,
            "operator": 0,  # Equals
        }
    ]
    tags = [
        {
            "tag": "domain",
            "value": "uplinks",
        },
        {
            "tag": "role",
            "value": "provider",
        },
        {
            "tag": "provider",
            "value": provider,
        },
    ]

    # Try to find existing service by name.
    res, err = zabbix_request(
        url,
        token,
        "service.get",
        {
            "output": ["serviceid", "name"],
            "filter": {"name": [service_name]},
            "selectParents": ["serviceid"],
        },
        debug=debug,
    )
    if err:
        return None, "service.get: {}".format(err)

    if res:
        serviceid = res[0]["serviceid"]
        payload = {
            "serviceid": serviceid,
            "name": service_name,
            "problem_tags": problem_tags,
            "tags": tags,
        }
        # Optionally ensure parent link if parentid is given and not already set.
        if parentid:
            existing_parents = res[0].get("parents") or []
            has_parent = any(p.get("serviceid") == str(parentid) for p in existing_parents)
            if not has_parent:
                payload["parents"] = [{"serviceid": str(parentid)}]
        _, err = zabbix_request(url, token, "service.update", payload, debug=debug)
        if err:
            return None, "service.update: {}".format(err)
        return serviceid, None

    # Create new leaf service for this provider.
    payload = {
        "name": service_name,
        "algorithm": 0,  # leaf (no children) – status taken from its own problems
        "sortorder": 1,
        "problem_tags": problem_tags,
        "tags": tags,
    }
    if parentid:
        payload["parents"] = [{"serviceid": str(parentid)}]

    result, err = zabbix_request(url, token, "service.create", payload, debug=debug)
    if err:
        return None, "service.create: {}".format(err)
    return result["serviceids"][0], None


def _ensure_provider_sla(url, token, provider, slo, debug=False):
    """Create or update SLA for a single provider."""
    from time import time

    if slo is None:
        return None, None

    sla_name = "Uplinks {} SLA".format(provider)

    # Try to find existing SLA by name.
    res, err = zabbix_request(
        url,
        token,
        "sla.get",
        {
            "output": ["slaid", "name", "slo", "period", "timezone", "status"],
            "filter": {"name": [sla_name]},
        },
        debug=debug,
    )
    if err:
        return None, "sla.get: {}".format(err)

    payload = {
        "name": sla_name,
        "slo": float(slo),
        "period": 0,  # daily
        "timezone": "UTC",
        "status": 1,
        "schedule": [],  # 24x7
        "service_tags": [
            {
                "tag": "provider",
                "operator": 0,
                "value": provider,
            }
        ],
    }

    if res:
        slaid = res[0]["slaid"]
        payload["slaid"] = slaid
        _, err = zabbix_request(url, token, "sla.update", payload, debug=debug)
        if err:
            return None, "sla.update: {}".format(err)
        return slaid, None

    # Create new SLA starting "now".
    payload["effective_date"] = int(time())
    result, err = zabbix_request(url, token, "sla.create", [payload], debug=debug)
    if err:
        return None, "sla.create: {}".format(err)
    sla_ids = result.get("slaids") or []
    slaid = sla_ids[0] if sla_ids else None
    return slaid, None


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Create/update Zabbix services per provider, "
            "mapping them to aggregate provider triggers via problem tag provider={name}."
        ),
    )
    parser.add_argument(
        "-f",
        "--commit-rates",
        default=DEFAULT_COMMIT_RATES,
        help="Path to commit_rates.json (for _provider_limits / provider list).",
    )
    parser.add_argument(
        "--parent-service",
        default=None,
        help="Optional parent service name for all provider services (created if missing).",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Verbose Zabbix API debug output.",
    )
    args = parser.parse_args()

    commit_rates, err = _load_commit_rates(args.commit_rates)
    if err:
        print(err, file=sys.stderr)
        sys.exit(1)

    url, token = _get_zabbix_url_token()
    if not url or not token:
        print("ZABBIX_URL and ZABBIX_TOKEN are required.", file=sys.stderr)
        sys.exit(1)

    ok, err = validate_zabbix_token(url, token, debug=args.debug)
    if not ok:
        print("Ошибка авторизации в Zabbix (token): {}".format(err), file=sys.stderr)
        sys.exit(1)

    providers = _get_providers_from_limits(commit_rates)
    if not providers:
        print("No providers in _provider_limits; nothing to create.", file=sys.stderr)
    parentid = None
    if args.parent_service:
        parentid, err = _get_or_create_parent_service(
            url, token, args.parent_service, debug=args.debug
        )
        if err:
            print(err, file=sys.stderr)
            sys.exit(1)

    slo = _get_global_provider_sla(commit_rates)

    if providers:
        for provider in providers:
            if not provider:
                continue
            serviceid, err = _get_or_create_provider_service(
                url, token, provider, parentid, debug=args.debug
            )
            if err:
                print("Provider {}: {}".format(provider, err), file=sys.stderr)
                continue
            print("OK: service for provider {} (serviceid={})".format(provider, serviceid))

            if slo is not None:
                slaid, err = _ensure_provider_sla(url, token, provider, slo, debug=args.debug)
                if err:
                    print("Provider {} SLA error: {}".format(provider, err), file=sys.stderr)
                    continue
                print(
                    "OK: SLA for provider {} (slaid={}, slo={:.4f}%)".format(
                        provider, slaid, slo
                    )
                )


if __name__ == "__main__":
    main()

