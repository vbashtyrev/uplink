#!/usr/bin/env python3
"""
Построение карты в Zabbix по данным из файлов и других источников.
Zabbix 7.0: при использовании API — переменные ZABBIX_URL, ZABBIX_TOKEN; токен проверяется при первом запросе.
По умолчанию: hostname, interface, description, ISP. С --zabbix добавляются ключи items Bits received / Bits sent.
"""

import argparse
import json
import os
import re
import sys


DEFAULT_INPUT = "dry-ssh.json"
DESCRIPTION_MAP_FILE = "description_to_name.json"
ZABBIX_CACHE_FILE = "zabbix_uplinks_cache.json"
BITS_RECEIVED_NAME = "Bits received"
BITS_SENT_NAME = "Bits sent"


def load_devices_json(path):
    """Загрузить JSON с ключом devices. Возврат (data, None) или (None, error_msg)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None, "файл не найден: {}".format(path)
    except json.JSONDecodeError as e:
        return None, "ошибка JSON: {}".format(e)
    if "devices" not in data:
        return None, "в файле нет ключа 'devices'"
    return data, None


def load_description_map(path):
    """Загрузить сопоставление description -> отображаемое имя. Пустой dict если файла нет."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        return {}


def _cache_key(hostname, iface_norm):
    return "{}|{}".format(hostname, iface_norm)


def load_zabbix_cache(path):
    """
    Загрузить кэш из файла. Возврат (host_id_by_name, items_by_host_iface) или (None, None) при ошибке/отсутствии.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None, None
    if not isinstance(data, dict) or "host_id_by_name" not in data or "items_by_host_iface" not in data:
        return None, None
    host_id_by_name = data["host_id_by_name"]
    items_list = data.get("items_by_host_iface")  # список [key, rec]
    items_by_host_iface = {}
    if isinstance(items_list, list):
        for k, rec in items_list:
            items_by_host_iface[(k[0], k[1])] = rec
    elif isinstance(items_list, dict):
        for k, rec in items_list.items():
            parts = k.split("|", 1)
            if len(parts) == 2:
                items_by_host_iface[(parts[0], parts[1])] = rec
    return host_id_by_name, items_by_host_iface


def save_zabbix_cache(path, host_id_by_name, items_by_host_iface):
    """Сохранить host_id_by_name и items_by_host_iface в JSON (ключи элементов — списки для tuple)."""
    items_list = [[list(pair), rec] for pair, rec in items_by_host_iface.items()]
    data = {"host_id_by_name": host_id_by_name, "items_by_host_iface": items_list}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=0)


def zabbix_request(url, token, method, params=None, debug=False):
    """
    Вызов Zabbix API 7 (JSON-RPC 2.0). Authorization: Bearer <token>.
    Возврат (result, None) или (None, error_msg).
    """
    try:
        import requests
    except ImportError:
        return None, "для --zabbix нужен модуль requests (pip install requests)"
    if params is None:
        params = {}
    if debug:
        print("Zabbix API: {} {}".format(method, params), file=sys.stderr)
    payload = {
        "jsonrpc": "2.0",
        "method": method,
        "params": params,
        "id": 1,
    }
    headers = {
        "Content-Type": "application/json-rpc",
        "Authorization": "Bearer {}".format(token),
    }
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=30)
        r.raise_for_status()
        data = r.json()
    except requests.RequestException as e:
        return None, "запрос к Zabbix: {}".format(e)
    if "error" in data:
        err = data["error"]
        return None, "Zabbix API: {} ({})".format(
            err.get("data", err.get("message", "unknown")),
            err.get("code", ""),
        )
    if debug and data.get("result") is not None:
        res = data["result"]
        if isinstance(res, list):
            print("  -> {} записей".format(len(res)), file=sys.stderr)
        else:
            print("  -> ok", file=sys.stderr)
    return data.get("result"), None


def validate_zabbix_token(url, token, debug=False):
    """Проверить токен: вызов user.get с Bearer (Zabbix 7)."""
    result, err = zabbix_request(url, token, "user.get", {"limit": 1}, debug=debug)
    if err:
        return False, err
    return True, None


def _interface_from_key(key):
    """Из ключа вида net.if.in[Ethernet51/1] или net.if.out["eth0"] извлечь имя интерфейса."""
    if not key:
        return None
    m = re.search(r"\[([^]]+)\]", key)
    if not m:
        return None
    iface = m.group(1).strip().strip('"\'')
    return iface if iface else None


def _interface_from_item_name(name):
    """Из имени item вида '... Interface Ethernet51/1(Uplink: ...): Bits received' извлечь Ethernet51/1."""
    if not name:
        return None
    # "Interface Ethernet51/1(" или "Interface Ethernet51/1:"
    m = re.search(r"Interface\s+([^\s(:(]+)", name, re.IGNORECASE)
    return m.group(1).strip() if m else None


def _normalize_interface_name(name):
    """Привести имя интерфейса к одному виду для сравнения (например Ethernet51/1 и ethernet51/1)."""
    if not name:
        return ""
    return name.strip().lower()


def fetch_zabbix_hosts_and_items(url, token, hostnames, debug=False):
    """
    Найти в Zabbix хосты по именам и для каждого хоста собрать items «Bits received» / «Bits sent»
    по ключу (интерфейс в key_). Возврат (host_id_by_name, items_by_host_interface, error).
    items_by_host_interface: (hostname, interface_normalized) -> {"bits_in": key_, "bits_out": key_}
    """
    valid, err = validate_zabbix_token(url, token, debug=debug)
    if not valid:
        return None, None, err

    # host.get: сначала по host (technical name), затем по name (visible name) для недостающих
    result, err = zabbix_request(url, token, "host.get", {
        "output": ["hostid", "host", "name"],
        "filter": {"host": list(hostnames)},
    }, debug=debug)
    if err:
        return None, None, err
    host_id_by_name = {}  # наш ключ (из файла) -> hostid; host (technical) для вывода
    for h in result:
        hid = h["hostid"]
        host_id_by_name[h["host"]] = hid
    missing = hostnames - set(host_id_by_name.keys())
    if missing:
        # попробовать по visible name (name в Zabbix)
        result2, err2 = zabbix_request(url, token, "host.get", {
            "output": ["hostid", "host", "name"],
            "filter": {"name": list(missing)},
        }, debug=debug)
        if not err2 and result2:
            for h in result2:
                host_id_by_name[h["name"]] = h["hostid"]
        missing = hostnames - set(host_id_by_name.keys())
    if missing:
        return None, None, "хосты не найдены в Zabbix: {}".format(", ".join(sorted(missing)))

    hostids = list(host_id_by_name.values())
    # item.get: search принимает строку, не массив — два запроса и объединяем
    all_items = []
    for search_name in (BITS_RECEIVED_NAME, BITS_SENT_NAME):
        result, err = zabbix_request(url, token, "item.get", {
            "output": ["itemid", "hostid", "name", "key_"],
            "hostids": hostids,
            "search": {"name": search_name},
        }, debug=debug)
        if err:
            return None, None, err
        all_items.extend(result)

    # hostid -> наш ключ hostname (из файла); ключи нормализуем к строке (API может вернуть int/str)
    host_by_id = {str(v): k for k, v in host_id_by_name.items()}

    def _item_key(it):
        return it.get("key_") or it.get("key", "")

    # (hostname, interface_normalized) -> {"bits_in": key_, "bits_out": key_}
    items_by_host_iface = {}
    debug_samples = []  # для --debug: первые items с разобранными полями
    skipped_no_host = 0
    skipped_no_iface = 0
    for item in all_items:
        hostid = item.get("hostid")
        hostname = host_by_id.get(str(hostid)) if hostid is not None else None
        if not hostname:
            skipped_no_host += 1
            continue
        key_str = _item_key(item)
        name = item.get("name", "")
        iface_from_k = _interface_from_key(key_str)
        iface_from_n = _interface_from_item_name(name)
        # Приоритет имени из item (Interface Ethernet51/1(...): Bits received), иначе из ключа (там ifHCInOctets.N)
        iface = iface_from_n or iface_from_k
        if not iface:
            skipped_no_iface += 1
            if debug and len(debug_samples) < 5:
                debug_samples.append({
                    "hostid": hostid, "hostname": hostname, "name": name[:80],
                    "key": key_str[:60] if key_str else "", "from_key": iface_from_k, "from_name": iface_from_n,
                })
            continue
        key_norm = _normalize_interface_name(iface)
        if debug and len(debug_samples) < 5 and (hostname, key_norm) not in items_by_host_iface:
            debug_samples.append({
                "hostid": hostid, "hostname": hostname, "name": name[:80],
                "key": key_str[:60] if key_str else "", "iface": iface, "key_norm": key_norm,
            })
        if (hostname, key_norm) not in items_by_host_iface:
            items_by_host_iface[(hostname, key_norm)] = {"bits_in": "", "bits_out": "", "itemid_in": "", "itemid_out": ""}
        # В шаблонах имя может быть "Bits received" или "Interface Ethernet51/1(...): Bits received"
        itemid = item.get("itemid")
        if BITS_RECEIVED_NAME in name or name == BITS_RECEIVED_NAME:
            items_by_host_iface[(hostname, key_norm)]["bits_in"] = key_str
            if itemid is not None:
                items_by_host_iface[(hostname, key_norm)]["itemid_in"] = str(itemid)
        if BITS_SENT_NAME in name or name == BITS_SENT_NAME:
            items_by_host_iface[(hostname, key_norm)]["bits_out"] = key_str
            if itemid is not None:
                items_by_host_iface[(hostname, key_norm)]["itemid_out"] = str(itemid)

    if debug:
        if all_items:
            raw = all_items[0]
            print("DEBUG: один сырой item из API (ключи): {}".format(list(raw.keys())), file=sys.stderr)
            print("DEBUG:   name={!r} key_={!r} key={!r} hostid={!r}".format(
                raw.get("name"), raw.get("key_"), raw.get("key"), raw.get("hostid")), file=sys.stderr)
        print("DEBUG: skipped_no_host={} skipped_no_iface={}".format(skipped_no_host, skipped_no_iface), file=sys.stderr)
        print("DEBUG: items_by_host_iface: {} пар (hostname, interface)".format(len(items_by_host_iface)), file=sys.stderr)
        for i, (hn, kn) in enumerate(sorted(items_by_host_iface.keys())[:15]):
            rec = items_by_host_iface[(hn, kn)]
            print("  [{}] ({!r}, {!r}) -> in={!r} out={!r}".format(i, hn, kn, rec.get("bits_in", "")[:50], rec.get("bits_out", "")[:50]), file=sys.stderr)
        for i, s in enumerate(debug_samples):
            print("DEBUG: sample item {}: hostname={!r} name={!r} key={!r} from_key={!r} from_name={!r} iface={!r} key_norm={!r}".format(
                i, s.get("hostname"), s.get("name"), s.get("key"), s.get("from_key"), s.get("from_name"), s.get("iface"), s.get("key_norm")), file=sys.stderr)

    return host_id_by_name, items_by_host_iface, None


MAP_NAME = "[test] uplinks"
MAP_WIDTH = 1200
MAP_HEIGHT = 800
ELEMENT_TYPE_HOST = 0
ELEMENT_TYPE_HOST_GROUP = 3
ISP_GROUP_PREFIX = "ISP: "


def _get_or_create_isp_hostgroup(url, token, isp_name, debug=False):
    """Найти или создать группу хостов с именем ISP: <isp_name>. Возврат (groupid, err)."""
    name = ISP_GROUP_PREFIX + isp_name
    result, err = zabbix_request(url, token, "hostgroup.get", {"filter": {"name": name}}, debug=debug)
    if err:
        return None, err
    if result:
        return result[0]["groupid"], None
    result, err = zabbix_request(url, token, "hostgroup.create", {"name": name}, debug=debug)
    if err:
        return None, err
    return result["groupids"][0], None


def create_uplinks_map(url, token, devices, host_id_by_name, items_by_host_iface, desc_to_name, debug=False):
    """
    Создать карту [test] uplinks: хосты и ISP (как группы хостов), линки по таблице.
    На каждый линк: подпись с именем интерфейса и индикаторы IN/OUT по item.
    """
    # Рёбра: (hostname, hostid, interface, isp, itemid_in, itemid_out)
    edges = []
    for hostname in sorted(devices.keys()):
        hostid = host_id_by_name.get(hostname)
        if not hostid:
            continue
        for iface in devices[hostname]:
            iface_name = iface.get("name", "")
            description = iface.get("description", "")
            isp = desc_to_name.get(description, description)
            key_norm = _normalize_interface_name(iface_name)
            rec = items_by_host_iface.get((hostname, key_norm), {})
            itemid_in = rec.get("itemid_in") or ""
            itemid_out = rec.get("itemid_out") or ""
            edges.append((hostname, str(hostid), iface_name, isp, itemid_in, itemid_out))

    if not edges:
        return "нет данных для карты (ни одного uplink)", None

    unique_hosts = []  # (hostname, hostid)
    seen_hosts = set()
    for hostname, hostid, _if, _isp, _in, _out in edges:
        if (hostname, hostid) not in seen_hosts:
            seen_hosts.add((hostname, hostid))
            unique_hosts.append((hostname, hostid))

    unique_isps = []  # isp_name
    isp_to_groupid = {}
    for _hn, _hid, _if, isp, _in, _out in edges:
        if isp not in isp_to_groupid:
            unique_isps.append(isp)
            gid, err = _get_or_create_isp_hostgroup(url, token, isp, debug=debug)
            if err:
                return "hostgroup для ISP {}: {}".format(isp, err), None
            isp_to_groupid[isp] = gid

    # Элементы карты: сначала хосты, потом группы ISP (elementid — число для API)
    selements = []
    step_y = 70
    for i, (hostname, hostid) in enumerate(unique_hosts):
        try:
            eid = int(hostid)
        except (TypeError, ValueError):
            eid = hostid
        selements.append({
            "elementtype": ELEMENT_TYPE_HOST,
            "elementid": eid,
            "x": 200,
            "y": 80 + i * step_y,
            "label": hostname,
        })
    for j, isp in enumerate(unique_isps):
        gid = isp_to_groupid[isp]
        try:
            eid = int(gid)
        except (TypeError, ValueError):
            eid = gid
        selements.append({
            "elementtype": ELEMENT_TYPE_HOST_GROUP,
            "elementid": eid,
            "x": MAP_WIDTH - 250,
            "y": 80 + j * step_y,
            "label": isp,
        })

    # Проверить, есть ли уже карта с таким именем
    existing, err = zabbix_request(url, token, "map.get", {"filter": {"name": MAP_NAME}, "output": ["sysmapid"]}, debug=debug)
    if err:
        return "map.get: {}".format(err), None
    # groupids — группы, которым доступна карта; API ожидает массив чисел
    first_groupid = int(isp_to_groupid[unique_isps[0]]) if unique_isps else 1
    map_groupids = [first_groupid]

    if existing:
        sysmapid = existing[0]["sysmapid"]
        result, err = zabbix_request(url, token, "map.update", {
            "sysmapid": sysmapid,
            "width": MAP_WIDTH,
            "height": MAP_HEIGHT,
            "selements": selements,
            "groupids": map_groupids,
        }, debug=debug)
        if err:
            return "map.update (selements): {}".format(err), sysmapid
    else:
        result, err = zabbix_request(url, token, "map.create", {
            "name": MAP_NAME,
            "width": MAP_WIDTH,
            "height": MAP_HEIGHT,
            "groupids": map_groupids,
            "selements": selements,
        }, debug=debug)
        if err:
            return "map.create: {}".format(err), None
        sysmapid = result["sysmapids"][0]

    # Получить selementid для каждого элемента
    result, err = zabbix_request(url, token, "map.get", {
        "sysmapids": [sysmapid],
        "output": ["sysmapid"],
        "selectSelements": "extend",
    }, debug=debug)
    if err or not result:
        return "map.get: {}".format(err or "карта не найдена"), sysmapid
    elem_list = result[0].get("selements", [])
    host_to_selement = {}  # (hostid) -> selementid
    isp_to_selement = {}   # groupid -> selementid
    for el in elem_list:
        sid = el["selementid"]
        etype = int(el.get("elementtype", 0))
        eid = el.get("elementid", "")
        if etype == ELEMENT_TYPE_HOST:
            host_to_selement[str(eid)] = sid
        elif etype == ELEMENT_TYPE_HOST_GROUP:
            isp_to_selement[str(eid)] = sid

    # Линки: host -> ISP, с подписью (интерфейс) и индикаторами IN/OUT
    links = []
    for hostname, hostid, iface_name, isp, itemid_in, itemid_out in edges:
        sid1 = host_to_selement.get(hostid)
        gid = isp_to_groupid.get(isp)
        sid2 = isp_to_selement.get(str(gid)) if gid else None
        if not sid1 or not sid2:
            continue
        link = {"selementid1": sid1, "selementid2": sid2}
        # Подпись на линке: имя интерфейса
        link["label"] = iface_name or "—"
        # Индикаторы на линке: скорости IN/OUT по item (Zabbix 6/7 linkindicators)
        if itemid_in or itemid_out:
            link["linkindicators"] = []
            if itemid_in:
                link["linkindicators"].append({"type": 2, "itemid": itemid_in, "name": "IN"})
            if itemid_out:
                link["linkindicators"].append({"type": 2, "itemid": itemid_out, "name": "OUT"})
        links.append(link)

    if not links:
        return None, sysmapid

    result, err = zabbix_request(url, token, "map.update", {
        "sysmapid": sysmapid,
        "links": links,
    }, debug=debug)
    if err:
        return "map.update (links): {}".format(err), sysmapid
    return None, sysmapid


def main():
    parser = argparse.ArgumentParser(
        description="Данные для карты Zabbix. По умолчанию: hostname, interface, description, ISP."
    )
    parser.add_argument(
        "-f", "--file",
        default=DEFAULT_INPUT,
        metavar="FILE",
        help="Путь к JSON с devices (по умолчанию {})".format(DEFAULT_INPUT),
    )
    parser.add_argument(
        "-m", "--description-map",
        default=DESCRIPTION_MAP_FILE,
        metavar="FILE",
        help="Файл сопоставления description -> имя (по умолчанию {})".format(DESCRIPTION_MAP_FILE),
    )
    parser.add_argument(
        "--zabbix",
        action="store_true",
        help="Запросить Zabbix API: найти хосты и items Bits received/sent, вывести ключи в таблицу",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Вывести отладочную информацию при работе с Zabbix API",
    )
    parser.add_argument(
        "--create-map",
        action="store_true",
        help="Создать или обновить карту [test] uplinks: хосты, ISP, линки с интерфейсом и IN/OUT",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Не использовать кэш Zabbix, запросить данные заново (по умолчанию кэш в {})".format(ZABBIX_CACHE_FILE),
    )
    args = parser.parse_args()

    data, err = load_devices_json(args.file)
    if err:
        print(err, file=sys.stderr)
        sys.exit(1)

    desc_to_name = load_description_map(args.description_map)
    devices = data["devices"]

    use_zabbix = args.zabbix or args.create_map
    items_by_host_iface = {}
    if use_zabbix:
        url = os.environ.get("ZABBIX_URL", "").rstrip("/")
        token = os.environ.get("ZABBIX_TOKEN", "")
        if not url or not token:
            print("Для --zabbix и --create-map задайте ZABBIX_URL и ZABBIX_TOKEN", file=sys.stderr)
            sys.exit(1)
        if not url.endswith("/api_jsonrpc.php") and not url.endswith("api_jsonrpc.php"):
            url = url.rstrip("/") + "/api_jsonrpc.php"
        hostnames = set(devices.keys())
        cache_path = os.path.join(os.path.dirname(os.path.abspath(args.file)) if args.file else ".", ZABBIX_CACHE_FILE)
        host_id_by_name = None
        items_by_host_iface = None
        if not args.no_cache:
            cached_host, cached_items = load_zabbix_cache(cache_path)
            if cached_host is not None and cached_items is not None and set(cached_host.keys()) >= hostnames:
                host_id_by_name = {k: cached_host[k] for k in hostnames if k in cached_host}
                items_by_host_iface = {(h, i): rec for (h, i), rec in cached_items.items() if h in host_id_by_name}
                if args.debug:
                    print("DEBUG: данные загружены из кэша {}".format(cache_path), file=sys.stderr)
        if host_id_by_name is None or items_by_host_iface is None:
            host_id_by_name, items_by_host_iface, err = fetch_zabbix_hosts_and_items(
                url, token, hostnames, debug=args.debug
            )
            if err:
                print(err, file=sys.stderr)
                sys.exit(1)
            if not args.no_cache:
                save_zabbix_cache(cache_path, host_id_by_name, items_by_host_iface)
                if args.debug:
                    print("DEBUG: кэш сохранён в {}".format(cache_path), file=sys.stderr)
    else:
        host_id_by_name = {}

    rows = [("hostname", "interface", "description", "ISP")]
    if use_zabbix:
        rows[0] = ("hostname", "hostid", "interface", "description", "ISP", "key Bits received", "key Bits sent")
    lookup_debug_count = 0
    for hostname in sorted(devices.keys()):
        interfaces = devices[hostname]
        for iface in interfaces:
            iface_name = iface.get("name", "")
            description = iface.get("description", "")
            isp = desc_to_name.get(description, description)
            row = (hostname, iface_name, description, isp)
            if use_zabbix:
                hostid = str(host_id_by_name.get(hostname, ""))
                key_norm = _normalize_interface_name(iface_name)
                rec = items_by_host_iface.get((hostname, key_norm), {})
                if args.debug and lookup_debug_count < 8:
                    found = bool(rec.get("bits_in") or rec.get("bits_out"))
                    print("DEBUG lookup: hostname={!r} iface_name={!r} key_norm={!r} found={}".format(
                        hostname, iface_name, key_norm, found), file=sys.stderr)
                    lookup_debug_count += 1
                row = (hostname, hostid, iface_name, description, isp, rec.get("bits_in", ""), rec.get("bits_out", ""))
            rows.append(row)

    if args.create_map:
        err_msg, sysmapid = create_uplinks_map(
            url, token, devices, host_id_by_name, items_by_host_iface, desc_to_name, debug=args.debug
        )
        if err_msg:
            print(err_msg, file=sys.stderr)
            sys.exit(1)
        print("Карта создана/обновлена: sysmapid={}".format(sysmapid), file=sys.stderr)

    num_cols = len(rows[0])
    widths = [max(len(str(rows[i][c])) for i in range(len(rows))) for c in range(num_cols)]
    pad = "  "
    for row in rows:
        print(pad.join(str(row[c]).ljust(widths[c]) for c in range(num_cols)))


if __name__ == "__main__":
    main()
