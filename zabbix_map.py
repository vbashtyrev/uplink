#!/usr/bin/env python3
"""
Построение карты в Zabbix по данным из файлов и других источников.
Zabbix 7.0: при использовании API — переменные ZABBIX_URL, ZABBIX_TOKEN; токен проверяется при первом запросе.
По умолчанию: hostname, interface, description, ISP. С --zabbix добавляются ключи items Bits received / Bits sent.
"""

import argparse
import json
import math
import os
import re
import sys


DEFAULT_INPUT = "dry-ssh.json"
DESCRIPTION_MAP_FILE = "description_to_name.json"
ZABBIX_CACHE_FILE = "zabbix_uplinks_cache.json"
BITS_RECEIVED_NAME = "Bits received"
BITS_SENT_NAME = "Bits sent"
# Иконки элементов карты (imageid): хосты — роутер, провайдеры — облако. ID взять в Администрирование → Изображения
MAP_ICON_HOST = 130          # Router_symbol_(64)

# В Zabbix LLD триггер «High bandwidth usage» имеет описание вида:
# "Interface Ethernet1/1(описание): High bandwidth usage"


def _get_uplink_triggerids_by_host_iface(url, token, hostids, debug=False):
    """
    Получить триггеры «High bandwidth usage» по хостам. В Zabbix LLD описание вида
    "Interface Ethernet1/1(описание): High bandwidth usage". Возврат dict: (hostid, iface_name_norm) -> triggerid.
    """
    if not hostids:
        return {}
    result, err = zabbix_request(url, token, "trigger.get", {
        "output": ["triggerid", "description"],
        "hostids": list(hostids),
        "selectHosts": ["hostid"],
        "search": {"description": "High bandwidth usage"},
        "searchByAny": False,
    }, debug=debug)
    if err:
        return {}
    if not result:
        result, _ = zabbix_request(url, token, "trigger.get", {
            "output": ["triggerid", "description"],
            "hostids": list(hostids),
            "selectHosts": ["hostid"],
        }, debug=debug)
        if result:
            result = [t for t in result if "High bandwidth usage" in (t.get("description") or "")]
    if not result:
        return {}
    # Извлечь из описания "Interface EthernetX/Y(...): High bandwidth usage" имя интерфейса
    out = {}
    pat = re.compile(r"Interface\s+([^\s(]+)\s*\([^)]*\):\s*High bandwidth usage", re.IGNORECASE)
    for t in result:
        desc = t.get("description") or ""
        m = pat.match(desc.strip())
        if not m:
            continue
        iface = m.group(1).strip()
        hostid = None
        for h in (t.get("hosts") or []):
            hid = h.get("hostid")
            if hid and str(hid) in [str(x) for x in hostids]:
                hostid = str(hid)
                break
        if hostid:
            key = (hostid, _normalize_interface_name(iface))
            out[key] = str(t["triggerid"])
    if debug and out:
        for (hid, iface), tid in list(out.items())[:3]:
            print("DEBUG: триггер (hostid={}, iface={}) -> {}".format(hid, iface, tid), file=sys.stderr)
    return out


def _find_triggerid_for_link(triggerid_by_host_iface, hostid, iface_name):
    """Найти triggerid по hostid и имени интерфейса."""
    if not hostid or not iface_name:
        return None
    key = (str(hostid), _normalize_interface_name(iface_name))
    return triggerid_by_host_iface.get(key)


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
    if debug and method in ("map.create", "map.update"):
        print("--- request body (JSON) ---", file=sys.stderr)
        print(json.dumps(payload, indent=2, ensure_ascii=False), file=sys.stderr)
        print("--- end ---", file=sys.stderr)
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


# PERM_READ = 1, PERM_READ_WRITE = 2 (константы Zabbix для шаринга карты)
MAP_SHARE_PERMISSION = 2

def _get_map_user_groups(url, token, debug=False):
    """
    Список групп пользователя для шаринга карты: userGroups = [{usrgrpid, permission}, ...].
    В Zabbix 7 map.create ожидает userGroups, а не groupids (см. CMap.php validateCreate).
    Возврат (list, None) или (None, error_msg).
    """
    result, err = zabbix_request(
        url, token, "user.get",
        {"output": ["userid"], "selectUsrgrps": ["usrgrpid"], "limit": 1},
        debug=debug,
    )
    if err or not result:
        return None, err or "user.get: пустой ответ"
    usrgrps = result[0].get("usrgrps") or []
    user_groups = []
    for g in usrgrps:
        u = g.get("usrgrpid")
        if u is not None:
            user_groups.append({"usrgrpid": int(u), "permission": MAP_SHARE_PERMISSION})
    if not user_groups:
        return None, "у пользователя нет групп (usrgrps); добавьте пользователя в группу в Zabbix"
    return user_groups, None


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
# В API: 0=host, 4=image (картинка с подписью, иконка cloud_(64))
ELEMENT_TYPE_IMAGE = 4
# Иконка облака для провайдеров (imageid в Администрирование → Изображения)
MAP_ICON_CLOUD = 4

# Расстановка: блоки по провайдерам слева направо; граница карты 30, хосты не ближе 160 от провайдера по горизонтали, по вертикали шаг 100, между хостами по горизонтали 180
LAYOUT_MARGIN = 30
LAYOUT_BLOCK_WIDTH = 500
LAYOUT_ISP_Y_OFFSET = 50
LAYOUT_MIN_HOST_TO_PROVIDER = 160   # минимум по горизонтали от провайдера до хоста
LAYOUT_HOST_HORIZONTAL_GAP = 180    # горизонталь между хостами (две колонки)
LAYOUT_HOST_Y_OFFSET = 100          # вертикаль: первый ряд хостов под провайдером
LAYOUT_HOST_STEP_Y = 100            # вертикальный шаг между рядами хостов
LAYOUT_HOST_COLUMNS = 2
# Минимальное расстояние между центрами элементов (чтобы не накладывались)
LAYOUT_MIN_DISTANCE = 80


def _occupied_positions(host_pos, isp_pos, exclude_xy=None):
    """Список занятых координат (x, y) для проверки коллизий. exclude_xy — не учитывать эту точку."""
    out = []
    for v in host_pos.values():
        if exclude_xy is None or v != exclude_xy:
            out.append(v)
    for v in isp_pos.values():
        if exclude_xy is None or v != exclude_xy:
            out.append(v)
    return out


def _is_free(cx, cy, occupied, min_dist):
    """True, если (cx, cy) не ближе min_dist ни к одной занятой точке."""
    for (ox, oy) in occupied:
        if (cx - ox) ** 2 + (cy - oy) ** 2 < min_dist * min_dist:
            return False
    return True


def _place_single_host_provider(hx, hy, host_pos, isp_pos):
    """
    Подобрать свободную позицию для провайдера с одним хостом (хост уже в другом блоке).
    Порядок: слева, справа, снизу, сверху, между (ближе слева/справа).
    Возврат (x, y) или (hx - 170, hy) если все занято.
    """
    occupied = _occupied_positions(host_pos, isp_pos)
    min_d = LAYOUT_MIN_DISTANCE
    candidates = [
        (hx - 170, hy),   # слева
        (hx + 170, hy),   # справа
        (hx, hy + 100),   # снизу
        (hx, hy - 100),   # сверху
        (hx - 85, hy),    # между (ближе слева)
        (hx + 85, hy),    # между (ближе справа)
    ]
    for (cx, cy) in candidates:
        if _is_free(cx, cy, occupied, min_d):
            return (cx, cy)
    return (hx - 170, hy)


def _compute_layout(edges, map_width, map_height):
    """
    По рёбрам вычислить позиции хостов и провайдеров.
    Провайдеры по убыванию числа подключений; блоки слева направо, при нехватке места — перенос на следующую строку.
    Один хост у провайдера: провайдер и хост сбоку (те же правила ±170), на одной высоте.
    Возврат: (host_pos, isp_pos, required_width, required_height).
    """
    isp_to_hosts = {}
    for hostname, hostid, _if, isp, _in, _out, _ki, _ko in edges:
        if not isp:
            continue
        if isp not in isp_to_hosts:
            isp_to_hosts[isp] = set()
        isp_to_hosts[isp].add((hostname, hostid))

    isps_sorted = sorted(isp_to_hosts.keys(), key=lambda i: -len(isp_to_hosts[i]))

    host_pos = {}
    isp_pos = {}
    placed_hosts = set()

    block_x = LAYOUT_MARGIN
    block_y = LAYOUT_MARGIN
    row_max_height = 0
    max_x = map_width - LAYOUT_MARGIN

    for isp in isps_sorted:
        if block_x + LAYOUT_BLOCK_WIDTH > max_x and block_x > LAYOUT_MARGIN:
            block_x = LAYOUT_MARGIN
            block_y += row_max_height
            row_max_height = 0

        provider_x = block_x + LAYOUT_BLOCK_WIDTH // 2
        hosts_in_block = sorted(isp_to_hosts[isp], key=lambda t: (t[0], t[1]))
        # Один хост у провайдера = по числу подключений к ISP, а не по числу размещаемых в этом блоке
        single_host = len(isp_to_hosts[isp]) == 1
        host_y_row0 = block_y + LAYOUT_ISP_Y_OFFSET + LAYOUT_HOST_Y_OFFSET

        num_placed = 0
        for (hostname, hostid) in hosts_in_block:
            if hostid in placed_hosts:
                continue
            placed_hosts.add(hostid)
            row, subcol = divmod(num_placed, LAYOUT_HOST_COLUMNS)
            if single_host:
                # Провайдер и хост сбоку: те же ±170, провайдер слева, хост справа
                isp_pos[isp] = (provider_x - 170, host_y_row0)
                x = provider_x + 170
                y = host_y_row0
            else:
                offset_x = 170 if subcol == 1 else -170
                x = provider_x + offset_x
                y = host_y_row0 + row * LAYOUT_HOST_STEP_Y
                if num_placed == 0:
                    isp_pos[isp] = (provider_x, block_y + LAYOUT_ISP_Y_OFFSET)
            host_pos[str(hostid)] = (x, y)
            num_placed += 1

        if num_placed == 0:
            if single_host:
                # Провайдер с одним хостом: хост уже в другом блоке — подбираем свободную позицию рядом
                (_, only_hostid) = next(iter(hosts_in_block))
                hx, hy = host_pos.get(str(only_hostid), (provider_x - 170, host_y_row0))
                isp_pos[isp] = _place_single_host_provider(hx, hy, host_pos, isp_pos)
                continue
            else:
                isp_pos[isp] = (provider_x, block_y + LAYOUT_ISP_Y_OFFSET)

        host_rows = math.ceil(num_placed / LAYOUT_HOST_COLUMNS) if num_placed else 0
        if single_host:
            block_height = LAYOUT_ISP_Y_OFFSET + LAYOUT_HOST_Y_OFFSET
        else:
            block_height = LAYOUT_ISP_Y_OFFSET + LAYOUT_HOST_Y_OFFSET + host_rows * LAYOUT_HOST_STEP_Y
        row_max_height = max(row_max_height, block_height)

        block_x += LAYOUT_BLOCK_WIDTH

    required_width = block_x + LAYOUT_MARGIN
    required_height = block_y + row_max_height + LAYOUT_MARGIN
    return host_pos, isp_pos, required_width, required_height


def ensure_map_exists(url, token, debug=False):
    """Создать карту [test] uplinks, если её ещё нет. Возврат (sysmapid или None, err)."""
    existing, err = zabbix_request(url, token, "map.get", {
        "filter": {"name": MAP_NAME},
        "output": ["sysmapid"],
    }, debug=debug)
    if err:
        return None, err
    if existing:
        return existing[0]["sysmapid"], None
    result, err = zabbix_request(url, token, "map.create", {
        "name": MAP_NAME,
        "width": MAP_WIDTH,
        "height": MAP_HEIGHT,
        "label_type": 0,
        "label_type_image": 0,
    }, debug=debug)
    if err:
        return None, err
    return result["sysmapids"][0], None


def update_uplinks_map(url, token, devices, host_id_by_name, items_by_host_iface, desc_to_name, debug=False):
    """
    Обновить карту: добавить/обновить хосты и провайдеры (image), построить линки.
    Существующие элементы других хостов не трогаем. При --host — только этот хост и его линки.
    """
    # Рёбра для линков: (hostname, hostid, iface_name, isp, itemid_in, itemid_out, key_in, key_out)
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
            key_in = rec.get("bits_in") or ""
            key_out = rec.get("bits_out") or ""
            edges.append((hostname, str(hostid), iface_name, isp, itemid_in, itemid_out, key_in, key_out))

    unique_hosts = []  # (hostname, hostid)
    seen_hosts = set()
    for hostname, hostid, _if, _isp, _in, _out, _ki, _ko in edges:
        if (hostname, hostid) not in seen_hosts:
            seen_hosts.add((hostname, hostid))
            unique_hosts.append((hostname, hostid))

    if not unique_hosts:
        return "нет данных для карты (ни одного хост с uplink)", None

    unique_isps = []
    seen_isp = set()
    for _hn, _hid, _if, isp, _in, _out, _ki, _ko in edges:
        if isp and isp not in seen_isp:
            seen_isp.add(isp)
            unique_isps.append(isp)

    # Позиции по провайдерам: слева направо, провайдер с макс. подключений — первым
    host_pos, isp_pos, required_width, required_height = _compute_layout(edges, MAP_WIDTH, MAP_HEIGHT)
    map_width = max(MAP_WIDTH, required_width)
    map_height = max(MAP_HEIGHT, required_height)

    # Получить карту (создать пустую, если нет)
    existing, err = zabbix_request(url, token, "map.get", {
        "filter": {"name": MAP_NAME},
        "output": ["sysmapid"],
        "selectSelements": "extend",
        "selectLinks": "extend",
    }, debug=debug)
    if err:
        return "map.get: {}".format(err), None
    if not existing:
        sysmapid, err = ensure_map_exists(url, token, debug=debug)
        if err:
            return "map.create: {}".format(err), None
        existing = [{"sysmapid": sysmapid, "selements": [], "links": []}]

    sysmapid = existing[0]["sysmapid"]
    old_selements_raw = existing[0].get("selements", [])

    # Один элемент на hostid и один на провайдера (label), чтобы не дублировать при повторных update.
    # selementid_to_canonical: для подмены в линках удалённых дубликатов на оставляемый selementid
    old_selements = []
    old_by_eid = {}
    old_by_image_label = {}
    selementid_to_canonical = {}  # удалённый selementid -> канонический (оставляемый)
    for el in old_selements_raw:
        etype = int(el.get("elementtype", 0))
        sid = el.get("selementid")
        if etype == ELEMENT_TYPE_IMAGE:
            label = el.get("label", "")
            key_img = (ELEMENT_TYPE_IMAGE, label)
            if key_img in old_by_image_label:
                selementid_to_canonical[str(sid)] = str(old_by_image_label[key_img])
                continue
            old_by_image_label[key_img] = sid
        else:
            eid = el.get("elementid")
            if eid is None or eid == "":
                elems = el.get("elements") or []
                if elems and isinstance(elems[0], dict):
                    eid = elems[0].get("hostid")
            if eid is not None and str(eid) != "":
                if str(eid) in old_by_eid:
                    selementid_to_canonical[str(sid)] = str(old_by_eid[str(eid)])
                    continue
                old_by_eid[str(eid)] = sid
        old_selements.append(el)

    # Добавляем только те элементы, которых ещё нет на карте; позиции берём из layout
    new_selements = []
    for hostname, hostid in unique_hosts:
        if str(hostid) in old_by_eid:
            continue
        try:
            eid = int(hostid)
        except (TypeError, ValueError):
            eid = hostid
        x, y = host_pos.get(str(hostid), (LAYOUT_MARGIN, LAYOUT_MARGIN))
        new_selements.append({
            "elementtype": ELEMENT_TYPE_HOST,
            "elementid": eid,
            "hostid": eid,
            "elements": [{"hostid": str(eid)}],
            "x": x,
            "y": y,
            "label": hostname,
            "iconid_off": MAP_ICON_HOST,
        })
    for isp in unique_isps:
        if (ELEMENT_TYPE_IMAGE, isp) in old_by_image_label:
            continue
        x, y = isp_pos.get(isp, (map_width - 250, LAYOUT_MARGIN))
        new_selements.append({
            "elementtype": ELEMENT_TYPE_IMAGE,
            "elementid": 0,
            "elements": [],
            "label": isp,
            "label_location": -1,
            "x": x,
            "y": y,
            "iconid_off": MAP_ICON_CLOUD,
        })

    selements_merged = list(old_selements) + new_selements

    # Применить расстановку ко всем элементам (старым и новым)
    for el in selements_merged:
        etype = int(el.get("elementtype", 0))
        if etype == ELEMENT_TYPE_HOST:
            eid = el.get("elementid")
            if eid is None or eid == "":
                elems = el.get("elements") or []
                if elems and isinstance(elems[0], dict):
                    eid = elems[0].get("hostid")
            if eid is not None and str(eid) != "":
                pos = host_pos.get(str(eid))
                if pos is not None:
                    el["x"], el["y"] = pos
        elif etype == ELEMENT_TYPE_IMAGE:
            label = el.get("label", "")
            if label in isp_pos:
                el["x"], el["y"] = isp_pos[label]

    result, err = zabbix_request(url, token, "map.update", {
        "sysmapid": sysmapid,
        "width": map_width,
        "height": map_height,
        "label_type": 0,
        "label_type_image": 0,
        "selements": selements_merged,
    }, debug=debug)
    if err:
        return "map.update (selements): {}".format(err), sysmapid

    # Получить selementid для построения линков
    result, err = zabbix_request(url, token, "map.get", {
        "sysmapids": [sysmapid],
        "output": ["sysmapid"],
        "selectSelements": "extend",
        "selectLinks": "extend",
    }, debug=debug)
    if err or not result:
        return "map.get: {}".format(err or "карта не найдена"), sysmapid
    elem_list = result[0].get("selements", [])
    links_existing = result[0].get("links", [])
    host_to_selement = {}
    isp_to_selement = {}
    for el in elem_list:
        sid = str(el.get("selementid", ""))
        if not sid:
            continue
        etype = int(el.get("elementtype", 0))
        if etype == ELEMENT_TYPE_HOST:
            # hostid: API может вернуть elementid или только elements[0].hostid
            eid = el.get("elementid")
            if eid is None or eid == "":
                elems = el.get("elements") or []
                if elems and isinstance(elems[0], dict):
                    eid = elems[0].get("hostid")
            if eid is not None and str(eid) != "":
                host_to_selement[str(eid)] = sid
        elif etype == ELEMENT_TYPE_IMAGE:
            isp_to_selement[el.get("label", "")] = sid

    new_links = []
    our_host_sids = set()
    for hostname, hostid, iface_name, isp, itemid_in, itemid_out, key_in, key_out in edges:
        sid1 = host_to_selement.get(str(hostid))
        sid2 = isp_to_selement.get(isp) if isp else None
        if not sid1 or not sid2:
            if debug or (not new_links and not our_host_sids):
                print("DEBUG link skip: hostid={!r} isp={!r} sid1={} sid2={} (host_ids на карте: {!r}, isp labels: {!r})".format(
                    hostid, isp, sid1, sid2, list(host_to_selement.keys())[:10], list(isp_to_selement.keys())[:10]), file=sys.stderr)
            continue
        our_host_sids.add(sid1)
        # Подпись: интерфейс + строки In/Out с макросами {?last(/hostname/key)}
        label_parts = [iface_name or "—"]
        if key_in or key_out:
            if key_in:
                label_parts.append("In: {?last(/" + hostname + "/" + key_in + ")}")
            if key_out:
                label_parts.append("Out: {?last(/" + hostname + "/" + key_out + ")}")
        # Все объекты в links должны иметь один набор полей; для нового линка передаём linkid: 0 (API может трактовать как «создать»).
        link = {
            "linkid": 0,
            "selementid1": sid1,
            "selementid2": sid2,
            "label": "\n".join(label_parts),
        }
        new_links.append(link)

    # Существующие линки: только те, что не от наших хостов; подменяем удалённые дубликаты на канонический selementid
    our_host_sids_str = {str(s) for s in our_host_sids}
    links_merged = []
    for l in links_existing:
        s1 = str(l.get("selementid1", ""))
        if s1 in our_host_sids_str:
            continue
        s2 = str(l.get("selementid2", ""))
        s1 = selementid_to_canonical.get(s1, s1)
        s2 = selementid_to_canonical.get(s2, s2)
        label = str(l.get("label") or "")
        if l.get("linkid"):
            # Обновление существующего линка: linkid, selementid1, selementid2, label. linkid — число (как у нового с linkid: 0).
            entry = {
                "linkid": int(l["linkid"]),
                "selementid1": s1,
                "selementid2": s2,
                "label": label,
            }
        else:
            entry = {
                "selementid1": s1,
                "selementid2": s2,
                "label": label,
            }
        links_merged.append(entry)
    links_merged.extend(new_links)
    # Гарантировать у каждого линка ключ label (строка), чтобы не было пропусков в JSON.
    for link in links_merged:
        if "label" not in link:
            link["label"] = ""
        link["label"] = str(link.get("label") or "")

    if debug or new_links:
        print("Линков: существующих {}, новых {}, всего {}".format(
            len(links_existing), len(new_links), len(links_merged)), file=sys.stderr)
    if not new_links and edges:
        want_hosts = sorted(set(e[1] for e in edges))
        want_isps = sorted(set(e[3] for e in edges if e[3]))
        print("Линки не созданы. Ищем hostid: {!r}, isp: {!r}. На карте hostid: {!r}, isp: {!r}".format(
            want_hosts[:15], want_isps[:15], sorted(host_to_selement.keys())[:15], sorted(isp_to_selement.keys())[:15]), file=sys.stderr)

    # Обновление линков карты
    result, err = zabbix_request(url, token, "map.update", {
        "sysmapid": sysmapid,
        "links": links_merged,
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
        help="Только создать карту [test] uplinks, если её ещё нет (пустая)",
    )
    parser.add_argument(
        "--update-map",
        action="store_true",
        help="Обновить карту: хосты, провайдеры, линки; с --host — только указанный хост и его линки",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Не использовать кэш Zabbix, запросить данные заново (по умолчанию кэш в {})".format(ZABBIX_CACHE_FILE),
    )
    parser.add_argument(
        "--host",
        metavar="HOSTNAME",
        help="Работать только с указанным хостом (имя из devices)",
    )
    parser.add_argument(
        "--export-map",
        metavar="SYSMAPID",
        help="Вывести JSON карты из API (sysmapid) для сравнения с ручной картой; нужны ZABBIX_URL и ZABBIX_TOKEN",
    )
    args = parser.parse_args()

    # Режим экспорта: только map.get и вывод JSON
    if args.export_map:
        url = os.environ.get("ZABBIX_URL", "").rstrip("/")
        token = os.environ.get("ZABBIX_TOKEN", "")
        if not url or not token:
            print("Для --export-map задайте ZABBIX_URL и ZABBIX_TOKEN", file=sys.stderr)
            sys.exit(1)
        if not url.endswith("/api_jsonrpc.php") and not url.endswith("api_jsonrpc.php"):
            url = url.rstrip("/") + "/api_jsonrpc.php"
        result, err = zabbix_request(url, token, "map.get", {
            "sysmapids": [args.export_map],
            "output": "extend",
            "selectSelements": "extend",
            "selectLinks": "extend",
        }, debug=args.debug)
        if err or not result:
            print(err or "карта не найдена", file=sys.stderr)
            sys.exit(1)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        sys.exit(0)

    # Только создать карту — не грузим данные, не выводим таблицу
    if args.create_map and not args.update_map and not args.zabbix:
        url = os.environ.get("ZABBIX_URL", "").rstrip("/")
        token = os.environ.get("ZABBIX_TOKEN", "")
        if not url or not token:
            print("Задайте ZABBIX_URL и ZABBIX_TOKEN", file=sys.stderr)
            sys.exit(1)
        if not url.endswith("/api_jsonrpc.php") and not url.endswith("api_jsonrpc.php"):
            url = url.rstrip("/") + "/api_jsonrpc.php"
        sysmapid, err = ensure_map_exists(url, token, debug=args.debug)
        if err:
            print(err, file=sys.stderr)
            sys.exit(1)
        print("Карта создана (или уже есть): sysmapid={}".format(sysmapid), file=sys.stderr)
        sys.exit(0)

    data, err = load_devices_json(args.file)
    if err:
        print(err, file=sys.stderr)
        sys.exit(1)

    desc_to_name = load_description_map(args.description_map)
    devices = data["devices"]
    if args.host:
        if args.host not in devices:
            print("Хост {!r} не найден в devices. Доступные: {}".format(
                args.host, ", ".join(sorted(devices.keys()))), file=sys.stderr)
            sys.exit(1)
        devices = {args.host: devices[args.host]}

    use_zabbix = args.zabbix or args.create_map or args.update_map
    items_by_host_iface = {}
    if use_zabbix:
        url = os.environ.get("ZABBIX_URL", "").rstrip("/")
        token = os.environ.get("ZABBIX_TOKEN", "")
        if not url or not token:
            print("Для --zabbix, --create-map и --update-map задайте ZABBIX_URL и ZABBIX_TOKEN", file=sys.stderr)
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
        # Триггеры для link indicators (чтобы показать в таблице и передать в update_uplinks_map)
        triggerid_by_host_iface = {}
        if host_id_by_name:
            triggerid_by_host_iface = _get_uplink_triggerids_by_host_iface(
                url, token, list(host_id_by_name.values()), debug=args.debug
            )
    else:
        host_id_by_name = {}
        triggerid_by_host_iface = {}

    rows = [("hostname", "interface", "description", "ISP")]
    if use_zabbix:
        rows[0] = ("hostname", "hostid", "interface", "description", "ISP", "key Bits received", "key Bits sent", "triggerid (link)")
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
                triggerid = _find_triggerid_for_link(triggerid_by_host_iface, hostid, iface_name) if triggerid_by_host_iface else None
                row = (hostname, hostid, iface_name, description, isp, rec.get("bits_in", ""), rec.get("bits_out", ""), triggerid or "—")
            rows.append(row)

    if args.update_map:
        err_msg, sysmapid = update_uplinks_map(
            url, token, devices, host_id_by_name, items_by_host_iface, desc_to_name, debug=args.debug,
        )
        if err_msg:
            print(err_msg, file=sys.stderr)
            sys.exit(1)
        print("Карта обновлена: sysmapid={}".format(sysmapid), file=sys.stderr)

    num_cols = len(rows[0])
    widths = [max(len(str(rows[i][c])) for i in range(len(rows))) for c in range(num_cols)]
    pad = "  "
    for row in rows:
        print(pad.join(str(row[c]).ljust(widths[c]) for c in range(num_cols)))


if __name__ == "__main__":
    main()
