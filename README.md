# Uplinks (NetBox + SSH)

**Версия 1.0**

Скрипты для сверки и обновления данных интерфейсов в NetBox по данным с устройств (SSH / JSON-файл). Поддерживаются Arista и Juniper.

Виртуальное окружение создаётся в `.venv`.

---

## Краткое содержимое скриптов

| Скрипт | Описание |
|--------|----------|
| `run_uplinks_full.py` | **Один запуск** — полная цепочка: сбор → commit_rates → NetBox circuits → Zabbix (sync, карта, дашборды); отчёт о работе и об ошибках. См. COMMANDS.md. |
| `uplinks_stats.py` | Сбор данных с устройств по SSH (Arista/Juniper) или отчёт NetBox vs устройство; выход — таблица или JSON (`dry-ssh.json`). |
| `netbox_checks.py` | Сверка данных из JSON с NetBox (интерфейсы: имя, description, тип, speed, duplex, MAC, MTU, IP, LAG и др.) и при необходимости обновление полей в NetBox. |
| `netbox_interface_types.py` | Скачивание справочника типов интерфейсов NetBox (value/label) из репозитория в JSON для `--mt-ref`. |
| `zabbix_map.py` | Таблица uplink'ов по dry-ssh; карта Zabbix (хосты, провайдеры, линки с items Bits in/out). К линкам привязываются триггеры 90%/100% — цвет линка: жёлтый при 90%, красный при 100%. |
| `zabbix_uplinks_dashboard.py` | Создание/обновление дашборда Zabbix с виджетами-графиками по каждому uplink (Bits received/sent). |
| `grafana_uplinks_graph.py` | Генерация JSON для панели Node graph в Grafana (узлы — хосты и провайдеры, рёбра — линки); опционально создание дашборда через Grafana API. |
| `generate_commit_rates.py` | Генерация `commit_rates.json` по линкам из dry-ssh (провайдер, circuit_id, commit_rate_gbps). |
| `netbox_create_circuits.py` | Создание circuits в NetBox по `commit_rates.json`: провайдер, тип «Internet», контур, Termination A на site, кабель до интерфейса. |
| `zabbix_sync_commit_rate.py` | Синхронизация макросов **{$IF.UTIL.MAX}** и **{$IF.UTIL.WARN}** в Zabbix из NetBox; создаёт триггеры по порогам WARN/HIGH (жёлтый/красный линк на карте, линия порога на дашборде). Пороги задаются в `uplinks_config.py` (по умолчанию 90% и 100%). |
| `zabbix_uplinks_cleanup.py` | Очистка артефактов автоматизации в Zabbix: триггеры 90%/100%, старые item'ы порога, карта uplinks, дашборды uplinks (тег `scripts:automatization`). |

---

## Краткий алгоритм (от чего к чему)

1. **Данные с устройств**  
   `uplinks_stats.py --fetch --json` → опрос по SSH (NetBox по тегу) → **`dry-ssh.json`** (устройство → список uplink-интерфейсов с полями из show interfaces).

2. **Сверка и обновление NetBox (интерфейсы)**  
   `netbox_checks.py -f dry-ssh.json` → сравнение с NetBox → таблица расхождений или **`--apply`** для записи в NetBox (description, type, speed, duplex, MAC, MTU, IP, LAG и т.д.).

   При необходимости: **`netbox_interface_types.py`** → `netbox_interface_types.json` для приведения типов (`--mt-ref`).

3. **Визуализация (Zabbix / Grafana)**  
   По **`dry-ssh.json`** + Zabbix API:  
   - **`zabbix_map.py`** — карта [test] uplinks (хосты, провайдеры, линки); после шагов 4–5 повторно **`--update-map`**, чтобы привязать триггеры к линкам (цвет 90%/100%);  
   - **`zabbix_uplinks_dashboard.py`** — дашборд с графиками по uplink;  
   - **`grafana_uplinks_graph.py`** — Node graph в Grafana (узлы и рёбра по тем же данным).

4. **Commit rates и circuits в NetBox (обязательно до Zabbix)**  
   **`generate_commit_rates.py -f dry-ssh.json`** → **`commit_rates.json`** (провайдер, circuit_id, commit_rate_gbps по устройству/интерфейсу).

   **`netbox_create_circuits.py -f commit_rates.json -d dry-ssh.json`** → в NetBox: провайдеры, контуры (cid, commit rate), Termination A на site, кабель до интерфейса.

   Circuits нужны до настройки Zabbix: в Zabbix commit rate будет браться из NetBox circuits.

5. **Синхронизация макросов commit rate в Zabbix**  
   **`zabbix_sync_commit_rate.py`** по NetBox (интерфейсы с circuit по кабелю) получает commit rate в Kbps, переводит в bps и создаёт макросы **{$IF.UTIL.MAX:"<интерфейс>"}** и **{$IF.UTIL.WARN:"<интерфейс>"}**.

   Создаёт два триггера на интерфейс: при пороге WARN — Warning (жёлтый линк на карте), при пороге HIGH — High (красный линк, линия порога на дашборде). Пороги и период в выражении триггера задаются в **`uplinks_config.py`** (`THRESHOLD_PERCENT_WARN`, `THRESHOLD_PERCENT_HIGH`, `TRIGGER_FUNCTION_PERIOD`).

   **`zabbix_map.py --update-map`** привязывает эти триггеры к линкам карты.

**Итого:** SSH/устройства → `dry-ssh.json` → NetBox (интерфейсы; commit_rates → circuits) → **zabbix_sync_commit_rate.py** (макросы и триггеры 90%/100%) → **zabbix_map.py --update-map** (карта с цветом линков), дашборд с линией порога.

**Откат изменений в Zabbix:** скрипт **zabbix_uplinks_cleanup.py** удаляет созданные автоматизацией триггеры, карту uplinks и дашборды (по именам). Макросы {$IF.UTIL.MAX}, {$IF.UTIL.WARN} не трогает. Запуск: `python zabbix_uplinks_cleanup.py` (перед удалением — `--dry-run`).

---

## Первый запуск

```bash
cd zabbix/uplinks

# Активировать venv
source .venv/bin/activate   # Linux/macOS
# или:  .venv\Scripts\activate  на Windows

# Установить зависимости
pip install -r requirements.txt

# Переменные окружения (обязательные для работы с NetBox и SSH)
export NETBOX_URL="https://your-netbox.example.com"
export NETBOX_TOKEN="your-api-token"
export SSH_PASSWORD="password-for-devices"
```

**Опциональные переменные**

- `SSH_USERNAME` — обязательно при `--report` и `--fetch` (по умолчанию не задан, нужно указать явно).
- `PARALLEL_DEVICES` (по умолчанию `6`), `SSH_HOST_SUFFIX`, `NETBOX_TAG`, `SSH_TIMEOUT`, `SSH_COMMAND_TIMEOUT` (сек).
- По умолчанию используется `~/.ssh/config`: HostName и User берутся из конфига (как при ручном `ssh DEVICE`). Чтобы отключить — задайте `USE_SSH_CONFIG=0`.
- При ошибке SSH в лог выводится тип исключения и errno (например `TimeoutError`, `OSError [Errno 51]`), чтобы различать таймаут подключения и отсутствие маршрута.

Без активации venv можно вызывать интерпретатор напрямую:

```bash
.venv/bin/python uplinks_stats.py
```

---

## Скрипты и ключи

### 1. `uplinks_stats.py`

Единый скрипт с двумя режимами.

**Режим отчёта (`--report`):** быстрый отчёт для визуальной сверки «что в NetBox» и «что на устройстве» по uplink-интерфейсам (с описанием, содержащим `Uplink:`). Поддерживаются Juniper и Arista (по `platform.name` в NetBox). Только таблица, без сохранения JSON.

**Режим статистики:** по умолчанию данные читаются из файла `dry-ssh.json` (таблица или `--json`). С флагом `--fetch` — опрос по SSH всех устройств Arista и Juniper (NetBox по тегу), сбор по каждому uplink'у в едином формате, результат — таблица или JSON.

**Устройства и интерфейсы в режиме статистики (при `--fetch`):** устройства берутся из NetBox по тегу (переменная `NETBOX_TAG`), учитываются платформы Arista EOS и Juniper (Junos). Интерфейсы — только те, у которых в описании (description) есть строка `Uplink:`.

**Что собирается по каждому интерфейсу (при `--fetch`):**

- **Arista**
  - Список интерфейсов: по описаниям (интерфейсы с `Uplink:` в description).
  - По каждому uplink: `show interfaces <name> | json | no-more`, `show interfaces <name> transceiver | json | no-more`.
  - При `forwardingModel=bridged`: дополнительно `show interfaces <name> switchport configuration source | json | no-more`.
- **Juniper**
  - Список: `show interfaces descriptions | display json`. При нуле uplink'ов — fallback на `display xml` (на части Junos в JSON дубликаты ключей, парсер оставляет последнее значение).
  - Учитываются только link up и **unit 0** (upstream; unit ≠ 0 — VLAN, пока не проверяются).
  - Агрегаты (ae*.0): члены LAG — `show lacp interfaces <aeN>`, по каждому физическому — `show interfaces <name> | display json`; модель SFP — `show chassis hardware | display json` (слот FPC/PIC/port по имени интерфейса).
  - Duplex: на 10G/40G/100G в Junos часто не выводится → при bandwidth ≥ 10 Gbps подставляется `full`.

Набор полей в выводе (name, description, bandwidth, mtu, physicalAddress и т.д.) совпадает у Arista и Juniper. **Таблица полей** (источники указаны для Arista):

| Поле | Источник | Описание |
|------|----------|----------|
| `name` | show interfaces | Имя интерфейса |
| `description` | show interfaces | Описание интерфейса |
| `bandwidth` | show interfaces | Пропускная способность (bps) |
| `duplex` | show interfaces | Режим дуплекса (full/half). На 10G/40G/100G по стандарту только full duplex; Junos часто не выводит duplex в JSON — скрипт подставляет `full` при bandwidth ≥ 10 Gbps. |
| `physicalAddress` | show interfaces | MAC-адрес |
| `mtu` | show interfaces | MTU |
| `forwardingModel` | show interfaces | Режим работы порта: `routed` или `bridged` |
| `mediaType` | show interfaces transceiver | Тип модуля/трансивера (например 10GBASE-SR) |
| `txPower` | show interfaces transceiver | Мощность передачи (dBm) |
| `switchportConfiguration` | show interfaces … switchport configuration source | Только при `forwardingModel=bridged`: объект `{ "config": ["switchport …", …], "source": "cli" }` |
| `isLag` | только Juniper | Признак агрегата (ae): `true` для строки по `show interfaces aeN` (LAG). У таких строк `mediaType` и `txPower` всегда `null` (у LAG нет трансивера). В NetBox — тип «Link Aggregation Group (LAG)», в Related Interfaces задаётся parent. |

Итоговая структура: в JSON ключ `devices`, значение — объект «имя устройства → список таких словарей по каждому uplink-интерфейсу». Этот JSON используется как вход для `netbox_checks.py`.

| Ключ | Описание |
|------|----------|
| `--report` | Режим отчёта: таблица NetBox vs SSH (Juniper + Arista) |
| `--fetch` | Режим статистики: опросить по SSH (иначе читается файл `dry-ssh.json`) |
| `--platform {arista,juniper,all}` | При `--fetch`: только Arista, только Juniper или все (по умолчанию `all`) |
| `--host NAME` | При `--fetch`: опросить только указанный хост (имя устройства в NetBox). Платформа берётся из NetBox по устройству, указывать `--platform` вместе с `--host` не нужно. |
| `--json` | Вывод в формате JSON (режим статистики). При `--fetch --json` прогресс идёт в stderr, в stdout — только JSON (удобно: `--fetch --json > dry-ssh.json`) |
| `--merge-into [FILE]` | При `--fetch`: загрузить FILE (по умолчанию `dry-ssh.json`), подставить в него данные по опрошенным хостам и сохранить. Остальные хосты в файле не меняются. Удобно для обновления одного хоста: `--fetch --host HOST --merge-into` |
| `--from-file FILE` | Путь к JSON с ключом `devices` (по умолчанию `dry-ssh.json`) |

**Переменные окружения**

- **Обязательные:** `NETBOX_URL`, `NETBOX_TOKEN`, `SSH_USERNAME`, `SSH_PASSWORD`, `SSH_HOST_SUFFIX`, `PARALLEL_DEVICES`, `NETBOX_TAG`.
- **Опционально:**
  - `SSH_TIMEOUT`, `SSH_COMMAND_TIMEOUT` — таймауты в секундах;
  - по умолчанию HostName и User берутся из `~/.ssh/config`; отключить: `USE_SSH_CONFIG=0`;
  - `DEBUG_SSH_JSON=1` — режим отчёта (`--report`): отладочный вывод JSON;
  - `DEBUG_JUNIPER_UPLINKS=1` — при `--fetch` для Juniper: пошаговый лог (JSON/XML, блоки, парсинг, причины пропуска интерфейсов).

```bash
python uplinks_stats.py
python uplinks_stats.py --json
python uplinks_stats.py --from-file other.json
python uplinks_stats.py --fetch
python uplinks_stats.py --fetch --platform arista
python uplinks_stats.py --fetch --host router-001
python uplinks_stats.py --fetch --json
python uplinks_stats.py --fetch --host router-001 --merge-into
python uplinks_stats.py --fetch --host router-001 --merge-into other.json
python uplinks_stats.py --report
```

---

### 2. `netbox_checks.py`

Сверка данных из JSON-файла с NetBox и при необходимости обновление интерфейсов в NetBox. **Подключения по SSH к устройствам нет** — скрипт всегда читает данные только из файла. Если `-f` не указан, используется файл по умолчанию `dry-ssh.json` (его можно получить через `uplinks_stats.py --fetch --json > dry-ssh.json` или использовать уже сохранённый). Устройства в NetBox выбираются по тегу.

**Входной файл:** по умолчанию `dry-ssh.json` (структура с ключом `devices`: имя устройства → список интерфейсов с полями `name`, `description`, `mediaType`, `bandwidth`, `duplex`, `physicalAddress`, `mtu`, `txPower` и т.д.).

**Переменные:** `NETBOX_URL`, `NETBOX_TOKEN`, `NETBOX_TAG`. При неверном или просроченном токене/недоступности NetBox скрипт завершается с сообщением в stderr и кодом 1 (без трассировки).

**Версия:** `netbox_checks.py --version` выводит версию (например 1.0).

#### Файл и хост

| Ключ | Описание |
|------|----------|
| `-f`, `--file FILE` | Путь к JSON с устройствами и интерфейсами (по умолчанию `dry-ssh.json`) |
| `--host NAME` | Обработать только один хост (имя устройства) |
| `--platform {arista,juniper,all}` | Обрабатывать устройства по платформе в NetBox: **arista** (по умолчанию), **juniper** или **all** |

#### Проверки (включить нужные колонки и сверку)

| Ключ | Описание |
|------|----------|
| `--intname` | Сверка имён интерфейсов (файл vs NetBox) с поиском по вариантам написания |
| `--description` | Сверка поля description (файл vs NetBox) |
| `--mediatype` | Сверка mediaType (файл/SSH) и type (NetBox) |
| `--mt-ref [FILE]` | Справочник типов для mediaType (по умолчанию `netbox_interface_types.json`). Значения приводятся к одному формату (value/slug). Другой файл: `--mt-ref other.json` |
| `--no-mt-ref` | Не загружать справочник типов (отключить использование по умолчанию) |
| `--bandwidth` | Сверка bandwidth (файл, bps) и speed (NetBox, Kbps) |
| `--duplex` | Сверка duplex (файл vs NetBox). Для интерфейсов с bandwidth ≥ 10 Gbps пустое значение с устройства считается full (проверка duplex для 10G+ не имеет смысла). |
| `--mac` | Сверка physicalAddress (файл) и mac_address (NetBox) |
| `--mtu` | Сверка mtu (файл vs NetBox) |
| `--tx-power` | Сверка txPower (файл) и tx_power (NetBox) |
| `--forwarding-model` | Сверка forwardingModel (файл) и mode (NetBox). В NetBox: `routed` → mode=null, `bridged` → mode=tagged |
| `--ip-address` | Сверка IPv4/IPv6 (файл: ipv4_addresses, ipv6_addresses, ip_vrf) и привязанных к интерфейсу в NetBox (с учётом VRF) |
| `--lag` | Сверка LAG / Related Interfaces: aggregateInterface (файл) и lag (NetBox) у физических интерфейсов — членов LAG |
| `--parent` | Сверка Parent interface: aggregateInterface (файл) и parent (NetBox) у логических интерфейсов (ae5.0 → ae5) |
| `--all` | Включить все проверки сразу (intname, description, mediatype, bandwidth, duplex, mac, mtu, tx-power, forwarding-model, ip-address, lag, parent) |

Без `--mt-ref` при `--mediatype` выводится предупреждение: значения не приводятся к одному формату, расхождения могут быть из-за разного написания.

Если ни один ключ проверки не указан (и не передан `--apply`), включаются все проверки, колонки «что подставим» (`--show-change`) и скрытие колонок без расхождений (`--hide-no-diff-cols`). При полном совпадении выводится итог: «Все проверенные поля совпадают с NetBox. Расхождений не найдено.»

#### Вывод и колонки

| Ключ | Описание |
|------|----------|
| `--show-change` | Показать колонки «что подставим в NetBox» по выбранным ключам: mtToSet, descToSet, speedToSet, dupToSet, mtuToSet, txpToSet, fwdToSet |
| `--hide-empty-note-cols` | Не выводить колонки примечаний (nD, nM, …), если во всех строках они пустые |
| `--hide-no-diff-cols` | Не выводить группы колонок (файл/Netbox/примечание), в которых ни в одной строке нет расхождения |
| `--hide-ok-hosts` | Не выводить в таблице хосты без расхождений; вывести их списком («Хосты без расхождений (N): …») и статистику: хосты OK / с расхождениями, интерфейсы OK / с расхождениями. Если указан один этот ключ (без других проверок), автоматически включаются все проверки |
| `--json` | Вывод в JSON (по умолчанию — таблица) |
| `--version` | Вывести версию скрипта и выйти |

#### Применение изменений в NetBox

| Ключ | Описание |
|------|----------|
| `--apply` | При разнице по выбранным ключам обновлять интерфейс в NetBox. При `--apply` таблица не выводится. Для типа желательно указывать `--mt-ref`. |

**Реализованные обновления при `--apply`** (при наличии соответствующего ключа и расхождении данные из файла записываются в NetBox):

| Ключ запуска | Поле в NetBox | Источник в JSON |
|--------------|---------------|-----------------|
| `--intname` | `name` | Имя интерфейса из файла (при отличии от NetBox) |
| `--description` | `description` | `description` |
| `--mediatype` | `type` | `mediaType` (через справочник → slug) |
| `--bandwidth` | `speed` | `bandwidth` (bps → Kbps) |
| `--duplex` | `duplex` | `duplex` (нормализация full/half) |
| `--mtu` | `mtu` | `mtu` |
| `--tx-power` | `tx_power` | `txPower` |
| `--forwarding-model` | `mode` | `forwardingModel`: в NetBox записывается `routed`→null, `bridged`→`tagged` |
| `--mac` | сущность MAC (dcim.mac-addresses) + поле интерфейса | При расхождении или отсутствии: поиск по MAC (формат с двоеточиями, верхний регистр). Если запись есть — выводится её URL; иначе создаётся новая и привязывается к интерфейсу. На интерфейсе дополнительно выставляется `primary_mac_address` (ID записи MAC) для отображения в NetBox 4. |
| `--ip-address` | IP (ipam.ip_addresses) + VRF | При расхождении: создание/обновление привязки IP к интерфейсу; при необходимости обновление VRF у существующего адреса. Учитывается поле `ip_vrf` из файла (только VRF «internet» при сборе через uplinks_stats). |
| `--intname` (создание) | lag, parent | При создании интерфейса: у физического члена LAG выставляется `lag` (Related Interfaces), у логического юнита (ae5.0) — `parent` (Parent interface). Если агрегат создаётся позже в том же запуске, второй проход выставит связи после создания всех интерфейсов. |

В NetBox MAC — отдельная сущность (dcim.mac-addresses); при `--mac --apply` создаётся или находится запись, привязывается к интерфейсу и на интерфейсе устанавливается `primary_mac_address`. Примечание 16: «в Netbox заполнено не оба поля» — если есть только сущность или только отображение на интерфейсе.

**Примеры:**

```bash
# Сверка только типов интерфейсов со справочником
python netbox_checks.py -f dry-ssh.json --mediatype --mt-ref

# Один хост, все проверки, колонки «что подставим»
python netbox_checks.py -f dry-ssh.json --host router-001 --all --show-change

# Сверка и применение изменений в NetBox (таблица не выводится)
python netbox_checks.py -f dry-ssh.json --mediatype --mt-ref --apply

# Только просмотр расхождений по описанию и типу, вывод в JSON
python netbox_checks.py -f dry-ssh.json --description --mediatype --mt-ref --json

# Компактный отчёт: только хосты с расхождениями + список OK и статистика
python netbox_checks.py -f dry-ssh.json --hide-ok-hosts
```

---

### 3. `netbox_interface_types.py`

Скачивание списка типов интерфейсов NetBox из репозитория netbox-community/netbox (choices.py), извлечение value и label, сохранение в JSON для использования в `netbox_checks.py --mt-ref`.

**Зависимость:** `requests` (установить отдельно при необходимости: `pip install requests`).

| Ключ | Описание |
|------|----------|
| `-o`, `--output FILE` | Путь к выходному JSON (по умолчанию `netbox_interface_types.json`) |

```bash
python netbox_interface_types.py
python netbox_interface_types.py -o my_types.json
```

---

## Типичный сценарий

1. Собрать данные с устройств в JSON (опрос по SSH):
   ```bash
   python uplinks_stats.py --fetch --json > dry-ssh.json
   ```
2. Сверить с NetBox и посмотреть расхождения:
   ```bash
   python netbox_checks.py -f dry-ssh.json --all --mt-ref --show-change
   ```
3. При необходимости обновить NetBox:
   ```bash
   python netbox_checks.py -f dry-ssh.json --mediatype --mt-ref --apply
   ```
4. При обновлении справочника типов интерфейсов:
   ```bash
   python netbox_interface_types.py
   ```

---

### 4. `zabbix_map.py`

Построение таблицы uplink'ов по данным из `dry-ssh.json`; опционально — карта Zabbix. При обращении к Zabbix API: поиск хостов и items (Bits received/sent). Файл может содержать логические интерфейсы (Juniper: ae5, ae5.0, et-0/0/3); на карте для одной пары (хост, провайдер) рисуется один линк — выбирается интерфейс с items Zabbix, при равных условиях логический unit (например ae5.0), иначе физический. При создании/обновлении линков к каждому линку привязываются триггеры 90% и 100% (создаются `zabbix_sync_commit_rate.py`): при срабатывании 90% линк отображается жёлтым, при 100% — красным.

**Карта Zabbix**

- **По умолчанию** (без ключей): если карты с именем из `uplinks_config.MAP_NAME` нет — создаётся карта со всеми элементами (хосты, провайдеры, линки); если карта уже есть — выводится сообщение, изменения не вносятся.
- `--update-map` — принудительно обновить карту (хосты, провайдеры, линки). С `--host` — только указанный хост и его линки.
- `--print-table` — вывести в консоль таблицу (hostname, interface, description, ISP); с `--zabbix` — с hostid и ключами items.
- `--create-map` — только создать пустую карту, если её нет (без элементов).

**Раскладка карты**

- Провайдеры сортируются по убыванию числа подключений; блоки идут слева направо, при нехватке места — перенос на следующую строку.
- В блоке: провайдер сверху, хосты — в две колонки (отступ от провайдера по горизонтали 160 px, между хостами 180 px, шаг по вертикали 100 px).
- Провайдер с одним подключением рисуется рядом с этим хостом в том же блоке, без отдельного ряда.
- Граница карты 30 px; при переполнении размеры карты увеличиваются автоматически.
- Один узел на хост и один на провайдера (элементы не дублируются).

**Подписи линков**

Имя интерфейса и строки In/Out с макросами Zabbix `{?last(/host/key)}` (скорость по items Bits received/sent).

**Ссылки у хостов (URLs)**

У каждого элемента-хоста на карте задаются ссылки на график по каждому uplink-интерфейсу: подпись — имя провайдера и «Bits received» (например «Beeline 5 Bits received»), URL — `history.php?action=showgraph&itemids[]=<itemid>&from=now-1d&to=now` (график за последние сутки). Базовый URL берётся из `ZABBIX_URL` (обрезается `/api_jsonrpc.php`).

**Сопоставление description → имя провайдера (`description_to_name.json`)**

На карте провайдер подписывается по полю `description` интерфейса. Если в файле есть несколько формулировок для одного провайдера (например «Beeline», «Beeline 5», «Uplink: Beeline 5»), без маппинга на карте появятся три отдельных блока. Файл `description_to_name.json` задаёт соответствие: ключ — точная строка `description` из данных, значение — подпись на карте. Файл **не генерируется автоматически**, его создают и правят вручную. Чтобы получить актуальный шаблон по всем `description` из `dry-ssh.json` (новые — как ключ, так и значение), выполните:

```bash
python zabbix_map.py --generate-description-map -f dry-ssh.json > description_to_name.json
```

Отредактируйте JSON: для одного провайдера задайте одно и то же значение (напр. `"Uplink: Beeline 5": "Beeline"`, `"Beeline 5": "Beeline"`, `"Beeline": "Beeline"`). Если файл уже существует, в вывод попадёт его содержимое плюс недостающие description.

**Переменные:** `ZABBIX_URL` (базовый URL, например `https://zabbix.example.com`; скрипт сам дописывает `/api_jsonrpc.php` при необходимости), `ZABBIX_TOKEN` (Bearer-токен, Zabbix 7). Имя карты: `[test] uplinks`. Иконки элементов (хосты, провайдеры) задаются в Администрирование → Изображения; при смене окружения при необходимости поправьте константы в скрипте.

| Ключ | Описание |
|------|----------|
| `-f`, `--file` | JSON с ключом `devices` (по умолчанию `dry-ssh.json`) |
| `-m`, `--description-map` | Файл сопоставления description → имя ISP |
| `--generate-description-map` | Собрать все description из файла и вывести шаблон JSON (в stdout); объединить с существующим маппингом |
| `--zabbix` | Запросить Zabbix API (для карты или таблицы) |
| `--print-table` | Вывести таблицу в консоль (с `--zabbix` — с hostid и ключами items) |
| `--create-map` | Только создать пустую карту, если её нет |
| `--update-map` | Принудительно обновить карту (хосты, провайдеры, линки); с `--host` — только указанный хост |
| `--host HOSTNAME` | Работать только с одним хостом |
| `--debug` | Отладочный вывод и тело запросов map.create/update |
| `--no-cache` | Не использовать кэш Zabbix |
| `--export-map SYSMAPID` | Вывести JSON карты из API (для сравнения с ручной картой) |

```bash
# По умолчанию: создать карту со всеми элементами, если её нет (иначе — сообщение)
python zabbix_map.py

# Принудительно обновить карту (например после создания триггеров в шаге 5)
python zabbix_map.py --update-map

# Таблица в консоль (без Zabbix / с Zabbix)
python zabbix_map.py --print-table
python zabbix_map.py --print-table --zabbix

# Один хост
python zabbix_map.py --zabbix --host router-001

# Создать пустую карту (один раз)
python zabbix_map.py --create-map

# Обновить карту по всем хостам (с отладкой)
python zabbix_map.py --update-map --debug

# Обновить только один хост и его линки
python zabbix_map.py --zabbix --update-map --host router-001 --debug

# Выгрузить карту из API (например, созданную вручную) для сравнения формата
python zabbix_map.py --export-map 10 > map_10.json

# Сгенерировать/обновить шаблон description_to_name по dry-ssh.json, сохранить и отредактировать
python zabbix_map.py --generate-description-map -f dry-ssh.json > description_to_name.json
```

---

### 5. `zabbix_uplinks_dashboard.py` — дашборд Zabbix с графиками uplink

Создание или обновление **дашборда** в Zabbix с виджетами-графиками: по каждому uplink-интерфейсу из `dry-ssh.json` (с учётом дедупликации по паре хост–провайдер, как в карте) добавляется один виджет — график входящего и исходящего трафика (Bits received / Bits sent). Данные по хостам и items берутся из Zabbix API; используется тот же кэш, что и в `zabbix_map.py`.

**Вход:** `dry-ssh.json`, `description_to_name.json`. Переменные: `ZABBIX_URL`, `ZABBIX_TOKEN`.

| Ключ | Описание |
|------|----------|
| `-f`, `--file` | Путь к dry-ssh.json (по умолчанию `dry-ssh.json`) |
| `-m`, `--description-map` | Файл сопоставления description → имя ISP |
| `--dashboard-name` | Название дашборда в Zabbix (по умолчанию `Uplinks`) |
| `--no-cache` | Не использовать кэш Zabbix |
| `--no-show-threshold` | Не рисовать пороги триггеров (линию порога) на графиках |
| `--debug` | Отладочный вывод |

Если дашборд с таким именем уже есть — он обновляется (страница с виджетами перезаписывается). Если нет — создаётся новый.

**Линия порога на графике.** Включена опция Simple triggers: пороги простых триггеров рисуются пунктиром. Скрипт **zabbix_sync_commit_rate.py** при каждом запуске создаёт на хостах простой триггер `max(Bits received, <period>) > {$IF.UTIL.MAX:"<интерфейс>"}` — период задаётся в **`uplinks_config.py`** (`TRIGGER_FUNCTION_PERIOD`, по умолчанию 15m). По нему Zabbix рисует горизонтальную линию порога на графиках дашборда.

```bash
# Создать или обновить дашборд «Uplinks» с графиками по всем uplink из dry-ssh.json
python zabbix_uplinks_dashboard.py -f dry-ssh.json

# Другое имя дашборда
python zabbix_uplinks_dashboard.py -f dry-ssh.json --dashboard-name "Uplinks traffic"
```

---

### 6. `netbox_create_circuits.py` — создание circuits в NetBox

Создание контуров (circuits) в NetBox по `commit_rates.json`: провайдер, тип «Internet», контур (cid, commit rate), Termination A на site устройства, кабель до интерфейса. Termination Z не создаётся. При существующем кабеле или mark_connected у интерфейса — удаление кабеля и сброс mark_connected, затем создание кабеля по нашим данным. Для виртуальных интерфейсов (ae5.0 и т.п.) кабель подключается к физическому (из dry-ssh `physicalInterface`). В конце выводится отчёт: создано / удалено / отключено / где использована физика вместо логики.

**Тег автоматизации:** скрипт создаёт в NetBox тег с именем из `uplinks_config.NETBOX_AUTOMATION_TAG` (по умолчанию `automatization`), если его ещё нет, и проставляет его всем создаваемым объектам (провайдеры, типы контуров, контуры, кабели). Уже существующим объектам тег добавляется при повторном запуске (без перезаписи остальных тегов).

**Переменные:** `NETBOX_URL`, `NETBOX_TOKEN`, `NETBOX_TAG`.

| Ключ | Описание |
|------|----------|
| `-f`, `--commit-rates` | Путь к commit_rates.json (по умолчанию `commit_rates.json`) |
| `-d`, `--dry-ssh` | dry-ssh.json для маппинга логический → физический интерфейс (если файл есть в текущей директории, подхватывается по умолчанию) |
| `--location LOC` | Обработать только указанную локацию (первый сегмент hostname); по умолчанию — все площадки |
| `--dry-run` | Не вносить изменения в NetBox, только вывод |

```bash
python netbox_create_circuits.py
python netbox_create_circuits.py -d dry-ssh.json --dry-run
python netbox_create_circuits.py --location ALA
```

---

### 7. `zabbix_sync_commit_rate.py` — макросы и триггеры 90%/100% в Zabbix из NetBox

Для каждого интерфейса в NetBox, подключённого кабелем к circuit termination (сторона A), скрипт берёт **commit rate** контура (Kbps), переводит в bps и создаёт на хосте два макроса с контекстом по интерфейсу: **{$IF.UTIL.MAX:"Ethernet51/1"}** (порог HIGH, по умолчанию 100%) и **{$IF.UTIL.WARN:"Ethernet51/1"}** (порог WARN, по умолчанию 90%). Значения макросов = commit_rate × (THRESHOLD_PERCENT_* / 100).

Создаёт два простых триггера на интерфейс: при пороге WARN — `max(Bits received, <period>) > {$IF.UTIL.WARN:"<интерфейс>"}` (Warning, на карте линк жёлтый), при пороге HIGH — `max(Bits received, <period>) > {$IF.UTIL.MAX:"<интерфейс>"}` (High, линия порога на дашборде и красный линк на карте). Период и пороги задаются в **`uplinks_config.py`** (`TRIGGER_FUNCTION_PERIOD` — по умолчанию 15m; `THRESHOLD_PERCENT_WARN`, `THRESHOLD_PERCENT_HIGH`). Карта (`zabbix_map.py --update-map`) привязывает эти триггеры к линкам.

Старые item'ы **net.if.threshold["..."]**, если остались, удаляются.

**В своих триггерах** используйте тот же формат макросов: **{$IF.UTIL.MAX:"Ethernet51/1"}**, **{$IF.UTIL.WARN:"Ethernet51/1"}**.

**Переменные:** `NETBOX_URL`, `NETBOX_TOKEN`, `NETBOX_TAG`, `ZABBIX_URL`, `ZABBIX_TOKEN`.

| Ключ | Описание |
|------|----------|
| `-d`, `--dry-ssh` | Путь к dry-ssh.json: для кабеля на физическом интерфейсе (напр. et-0/0/3) макрос будет по логическому имени (ae5.0, ae3.0), как в Zabbix |
| `--dry-run` | Не менять макросы в Zabbix, только вывести что бы установили |
| `--debug` | Отладочный вывод (статистика по NetBox, подстановка логических имён) |

Учитываются только пары, где в NetBox есть **кабель** от circuit termination (A) к интерфейсу; **обязателен фильтр по тегу** `NETBOX_TAG`. Для устройств, где в NetBox кабель на физике (MX204: et-0/0/3), а в Zabbix — логические ae5.0/ae3.0, укажите `-d dry-ssh.json`, тогда макрос будет {$IF.UTIL.MAX:"ae5.0"} и т.д.

```bash
python zabbix_sync_commit_rate.py
python zabbix_sync_commit_rate.py -d dry-ssh.json --dry-run
python zabbix_sync_commit_rate.py -d dry-ssh.json --debug
```

---

### 8. `zabbix_uplinks_cleanup.py` — очистка артефактов Zabbix

Удаляет в Zabbix объекты, созданные скриптами uplinks: триггеры 90%/100% (с тегом `scripts:automatization` или по описанию), старые item'ы **net.if.threshold["..."]**, карту **[test] uplinks**, дашборды **Uplinks** и **Uplinks (по локациям)** (имена задаются ключами). Макросы {$IF.UTIL.MAX}, {$IF.UTIL.WARN} не удаляются.

**Переменные:** `ZABBIX_URL`, `ZABBIX_TOKEN`.

| Ключ | Описание |
|------|----------|
| `--dashboard-name NAME` | Имя основного дашборда (по умолчанию `Uplinks`) |
| `--dashboard-by-location NAME` | Имя дашборда по локациям (по умолчанию `Uplinks (по локациям)`); пустая строка — не удалять |
| `--dry-run` | Показать, что будет удалено, без изменений в Zabbix |
| `--debug` | Отладочный вывод запросов к API |

```bash
python zabbix_uplinks_cleanup.py --dry-run
python zabbix_uplinks_cleanup.py
```

---

## Файлы

| Файл | Описание |
|------|----------|
| `dry-ssh.json` | Пример/результат: JSON с ключом `devices` (имя устройства → список интерфейсов с полями из SSH; могут быть логические интерфейсы, поля isLogical, isLag и др.) |
| `netbox_interface_types.json` | Справочник типов интерфейсов NetBox (value, label); используется `--mt-ref` в `netbox_checks.py` |
| `description_to_name.example.json` | Пример сопоставления description → имя ISP; скопировать в `description_to_name.json` и заполнить |
| `description_to_name.json` | Локальный файл сопоставления (не в git); по умолчанию для `zabbix_map.py -m` |
| `commit_rates.json.example` | Пример структуры commit_rates (обезличенный); скопировать в `commit_rates.json` и заполнить |
| `commit_rates.json` | Локальный файл (не в git): оплаченная скорость (commit_rate_gbps, Гбит/с), провайдер и circuit ID по паре устройство — интерфейс; для NetBox Circuit (Commit rate в Kbps = × 1 000 000) |
| `generate_commit_rates.py` | Генерация commit_rates.json по всем линкам из dry-ssh.json (провайдер, circuit_id по локации, commit_rate_gbps) |
| `netbox_create_circuits.py` | Создание circuits в NetBox по commit_rates.json (провайдер, тип, circuit, Termination A + cable к интерфейсу; отчёт в конце) |
| `zabbix_sync_commit_rate.py` | Синхронизация макросов {$IF.UTIL.MAX} и {$IF.UTIL.WARN} (90%) в Zabbix из NetBox; триггеры 90%/100% для карты и линии порога на дашборде, удаление старых item'ов порога |
| `zabbix_uplinks_cleanup.py` | Очистка артефактов в Zabbix: триггеры 90%/100%, item'ы порога, карта uplinks, дашборды uplinks |
| `uplinks_config.py` | **Настраиваемые** названия и значения для Zabbix и NetBox: имя карты, дашбордов, тег триггеров Zabbix и тег NetBox для circuits (`scripts`/`automatization`), описания триггеров, макросы, период триггера (`TRIGGER_FUNCTION_PERIOD`), иконки карты, цвета линков. Меняйте под своё окружение. |
| `zabbix_uplinks_cache.json` | Кэш данных Zabbix (хосты, items); создаётся при `--zabbix` / дашборде в той же директории, что и файл `-f`, не коммитить |
| `ROADMAP.md` | Планы доработок (например Tenancy для circuits) |
| `requirements.txt` | Зависимости: pynetbox, paramiko, requests |

---

**Формат `commit_rates.json`**  
Ключ — имя устройства, значение — объект «имя интерфейса → { `provider`, `circuit_id`, `commit_rate_gbps` }». `circuit_id` — уникальный идентификатор контура (Unique circuit ID в NetBox). `commit_rate_gbps` — оплаченная скорость в **Гбит/с** (в NetBox Circuit Commit rate хранится в Kbps: умножить на 1 000 000). Провайдер — короткое имя. Ключи с префиксом `_` игнорируются при чтении. Файл не коммитится; пример структуры — `commit_rates.json.example` (скопировать в `commit_rates.json`).

**Генерация по dry-ssh**  
Скрипт `generate_commit_rates.py` по всем линкам из `dry-ssh.json` собирает записи в `commit_rates.json`. Провайдер из `description_to_name.json`; `circuit_id` в формате провайдер-локация-N; для новых пар `commit_rate_gbps` — null (заполнить вручную, в Гбит/с). При merge старый ключ `commit_rate_kbps` конвертируется в `commit_rate_gbps` (значения &lt; 1000 считаются уже Гбит/с). Пример: `cp commit_rates.json.example commit_rates.json` затем `python generate_commit_rates.py -f dry-ssh.json -o commit_rates.json`.

**Создание circuits в NetBox**  
Скрипт `netbox_create_circuits.py` по `commit_rates.json` создаёт в NetBox провайдеров (при отсутствии), тип контура «Internet», контуры (cid, commit rate в Kbps), **Termination A** на site устройства и кабель до интерфейса. Тег из `uplinks_config.NETBOX_AUTOMATION_TAG` (по умолчанию `automatization`) создаётся при отсутствии и проставляется новым и существующим объектам. **Termination Z** не создаётся.

По умолчанию обрабатываются все площадки; ограничить одной: `--location ALA`. Если у интерфейса уже есть кабель или «mark connected» — скрипт удаляет кабель и сбрасывает mark_connected (через REST PATCH), затем подключает кабель по нашим данным. Для виртуальных интерфейсов (ae5.0 и т.п.) кабель вешается на **физический** интерфейс: при указании `-d dry-ssh.json` из dry-ssh берётся `physicalInterface` для логического. В конце выводится отчёт: что создано, что удалено, где использована физика вместо виртуального. Переменные: `NETBOX_URL`, `NETBOX_TOKEN`, `NETBOX_TAG`. Пример: `python netbox_create_circuits.py -f commit_rates.json -d dry-ssh.json` (проверка: `--dry-run`).
