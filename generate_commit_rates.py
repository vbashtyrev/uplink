#!/usr/bin/env python3
"""
Сгенерировать commit_rates.json по всем линкам (устройство + интерфейс) из dry-ssh.json.
Существующие значения в commit_rates.json сохраняются; для новых пар подставляются
провайдер из description_to_name и пустые circuit_id / commit_rate_gbps.
"""

import argparse
import json
import os
import sys

DEFAULT_DRY_SSH = "dry-ssh.json"
DEFAULT_DESC_MAP = "description_to_name.json"
DEFAULT_OUTPUT = "commit_rates.json"


def load_json(path, default=None):
    """Загрузить JSON; при отсутствии файла вернуть default; при ошибке парсинга — (None, error_msg)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except json.JSONDecodeError as e:
        return (None, str(e))


def is_uplink(iface):
    """Интерфейс считаем uplink'ом, если в description есть 'Uplink:'."""
    desc = (iface.get("description") or "").strip()
    return "Uplink:" in desc


def location_from_hostname(hostname):
    """Локация: первый сегмент до дефиса (WAW-EQX-7280QR-2 -> WAW)."""
    parts = (hostname or "").split("-")
    return parts[0] if parts and parts[0] else (hostname or "other")


def build_circuit_id_map(data, desc_to_provider, existing):
    """
    Собрать для всех линков circuit_id формата {provider}-{location}-{N}.
    Линки в рамках одной пары (провайдер, локация) нумеруются с 1 по порядку (сортировка: устройство, интерфейс).
    Возврат: dict (dev_name, iface_name) -> circuit_id.
    """
    rows = []
    for dev_name in sorted(data["devices"].keys()):
        ifaces = data["devices"].get(dev_name)
        if not isinstance(ifaces, list):
            continue
        for iface in ifaces:
            name = (iface.get("name") or "").strip()
            if not name or not is_uplink(iface):
                continue
            desc = (iface.get("description") or "").strip()
            provider = desc_to_provider.get(desc, desc or "")
            if provider and provider == desc and len(desc) > 30:
                provider = provider[:27] + "..."
            provider = (provider or "").strip() or "Uplink"
            loc = location_from_hostname(dev_name)
            entry = (existing.get(dev_name) or {}).get(name)
            rows.append((provider, loc, dev_name, name, entry))

    rows.sort(key=lambda r: (r[0], r[1], r[2], r[3]))  # provider, location, dev, iface
    # Нумерация по (provider, location)
    counter = {}
    cid_map = {}
    for provider, loc, dev_name, iface_name, entry in rows:
        key = (provider, loc)
        counter[key] = counter.get(key, 0) + 1
        num = counter[key]
        default_cid = "{}-{}-{}".format(provider, loc, num)
        existing_cid = entry.get("circuit_id") if isinstance(entry, dict) else None
        cid_map[(dev_name, iface_name)] = (existing_cid or "").strip() or default_cid
    return cid_map


def main():
    parser = argparse.ArgumentParser(
        description="Сгенерировать commit_rates.json по всем линкам из dry-ssh.json.",
    )
    parser.add_argument("-f", "--file", default=DEFAULT_DRY_SSH, help="Путь к dry-ssh.json")
    parser.add_argument("-m", "--description-map", default=DEFAULT_DESC_MAP, help="Файл description → имя провайдера")
    parser.add_argument("-o", "--output", default=DEFAULT_OUTPUT, help="Куда записать commit_rates.json")
    parser.add_argument("--no-merge", action="store_true", help="Не подмешивать существующий commit_rates.json (перезаписать)")
    args = parser.parse_args()

    data = load_json(args.file)
    if isinstance(data, tuple):
        print("Ошибка JSON в {}: {}".format(args.file, data[1]), file=sys.stderr)
        sys.exit(1)
    if data is None:
        print("Файл не найден: {}".format(args.file), file=sys.stderr)
        sys.exit(1)
    if "devices" not in data:
        print("В файле нет ключа 'devices'.", file=sys.stderr)
        sys.exit(1)

    desc_to_provider = load_json(args.description_map) if os.path.isfile(args.description_map) else {}
    if isinstance(desc_to_provider, tuple):
        print("Ошибка в {}: {}".format(args.description_map, desc_to_provider[1]), file=sys.stderr)
        sys.exit(1)

    existing = {}
    if not args.no_merge and os.path.isfile(args.output):
        existing = load_json(args.output)
        if isinstance(existing, tuple):
            print("Ошибка в {}: {}".format(args.output, existing[1]), file=sys.stderr)
            sys.exit(1)
        # Убрать служебные ключи
        existing = {k: v for k, v in existing.items() if not k.startswith("_")}

    cid_map = build_circuit_id_map(data, desc_to_provider, existing)
    out = {"_comment": "Оплаченная скорость (commit_rate_gbps, Гбит/с), провайдер и Unique circuit ID. В NetBox Circuit Commit rate хранится в Kbps (умножить на 1000000)."}

    for dev_name in sorted(data["devices"].keys()):
        ifaces = data["devices"][dev_name]
        if not isinstance(ifaces, list):
            continue
        dev_existing = existing.get(dev_name, {})
        dev_out = {}
        for iface in ifaces:
            name = (iface.get("name") or "").strip()
            if not name:
                continue
            if not is_uplink(iface):
                continue
            desc = (iface.get("description") or "").strip()
            provider = desc_to_provider.get(desc, desc or "")
            if provider and provider == desc and len(desc) > 30:
                provider = desc[:27] + "..."
            entry = dev_existing.get(name)
            cid = cid_map.get((dev_name, name), "")
            # Сохраняем скорость в Гбит/с; старый commit_rate_kbps: если >= 1000, считаем Kbps и конвертируем, иначе уже Gbps (1, 8, 10)
            rate_gbps = None
            if entry and isinstance(entry, dict):
                rate_gbps = entry.get("commit_rate_gbps")
                if rate_gbps is None and entry.get("commit_rate_kbps") is not None:
                    v = entry["commit_rate_kbps"]
                    rate_gbps = (v / 1_000_000.0) if v >= 1000 else v
                dev_out[name] = {
                    "provider": entry.get("provider", provider),
                    "circuit_id": cid,
                    "commit_rate_gbps": rate_gbps,
                }
            else:
                dev_out[name] = {
                    "provider": provider,
                    "circuit_id": cid,
                    "commit_rate_gbps": None,
                }
        if dev_out:
            out[dev_name] = dev_out

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    n_dev = sum(1 for k in out if not k.startswith("_"))
    n_links = sum(len(v) for k, v in out.items() if not k.startswith("_") and isinstance(v, dict))
    print("Записано {}: устройств {}, линков {}.".format(args.output, n_dev, n_links))


if __name__ == "__main__":
    main()
