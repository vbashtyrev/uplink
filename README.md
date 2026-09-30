# Uplinks

Проект берёт область подключений из NetBox и настраивает мониторинг в Zabbix.
Обычный рабочий запуск не опрашивает сетевые устройства и не изменяет NetBox.
Дополнительный ключ `--netbox-checks` запускает `netbox_checks.py --apply`
для сверки `dry-ssh.json` и обновления разрешённых полей интерфейсов.
Проверочные и рабочие отчёты описаны в
[`reports/README.md`](reports/README.md). Виртуальное окружение создаётся в
`.venv`.

---

## Инструменты

| Скрипт | Описание |
|--------|----------|
| `run_uplinks_full.py` | **Один запуск** — чтение полной области из NetBox и настройка Zabbix (макросы, триггеры, агрегаты, карта, дашборды, сервисы); отчёт о работе и ошибках. Ключ **`--plan`** даёт предварительный отчёт без записи. |
| `netbox_uplinks_inventory.py` | Только чтение NetBox: обход Provider → Circuit → терминация стороны A → кабель → интерфейс на устройстве с тегом `border`. В мониторинг входят только Circuit типа `Uplink` со статусом `Active`. |
| `zabbix_uplinks_plan.py` | Только чтение: сравнивает текущее состояние Zabbix с тем, что сделал бы обычный запуск, и пишет отчёт. Для сравнения Burst-триггеров нужен ключ `--create-link-triggers`. Ничего не создаёт, не меняет и не удаляет. |
| `uplinks_stats.py` | Отдельная ручная проверка интерфейсов по SSH и отчёт в таблице или JSON. |
| `netbox_checks.py` | Сверка данных из JSON с NetBox (интерфейсы: имя, description, тип, speed, duplex, MAC, MTU, IP, LAG и др.) и при необходимости обновление полей в NetBox. |
| `netbox_interface_types.py` | Скачивание справочника типов интерфейсов NetBox (value/label) из репозитория в JSON для `--mt-ref`. |
| `zabbix_map.py` | Карта Zabbix по полной области из NetBox (хосты, провайдеры, линки с items Bits in/out). |
| `zabbix_uplinks_dashboard.py` | Создание/обновление дашборда Zabbix с виджетами-графиками по каждому uplink (Bits received/sent). |
| `zabbix_sync_commit_rate.py` | Макросы **{$UPLINK.BPS.MAX}** / **{$UPLINK.BPS.WARN}** из NetBox; с `--create-link-triggers` — также Burst-триггеры по commit rate. Область интерфейсов и связь с логическим интерфейсом берутся из NetBox. |
| `zabbix_provider_aggregate.py` | Хосты `Uplinks {Provider}`: calculated items суммарного трафика и триггеры **90%**, **100%** и **SLA breach** по общему лимиту поставщика из поля NetBox **`aggregate_limit_gbps`**. Тег **`sla=true`** только на триггере **SLA breach**; на 90%/100% — `scripts:automatization` и `provider`. Зависимость 90% от 100% (как в Zabbix: дочерний не в PROBLEM без родителя). |
| `zabbix_provider_services.py` | Сервисы и целевой уровень доступности в Zabbix для активных Circuit и каналов со схемой `Burst`. |
| `zabbix_provider_sla.py` | Отчёт доступности по истории событий Zabbix. Подробности и команды: [`reports/README.md`](reports/README.md). |
| `zabbix_uplinks_cleanup.py` | Отдельная ручная очистка артефактов Zabbix; в рабочий запуск не входит. |

---

## Рабочий путь

NetBox является источником данных о подключениях:

```text
Provider → Circuit → Termination A → Cable → Interface → Border device
```

Проверка цепочек без записи:

```bash
python netbox_uplinks_inventory.py --dry-run
python netbox_uplinks_inventory.py --json --dry-run
```

В мониторинг попадают только Circuit типа `Uplink` со значением `status=Active`,
полной кабельной цепочкой без разветвления и конечным интерфейсом на устройстве
с тегом `border`.

Полный запуск последовательно создаёт снимок области, настраивает макросы и
триггеры, агрегаты, карту, дашборды, сервисы и целевой уровень доступности.
Проверочный режим `--plan` ничего не записывает.

```bash
python run_uplinks_full.py --plan
python run_uplinks_full.py
```

Отключение канала выполняется изменением его `status` в NetBox, после чего
сначала запускается `--plan`. Ручную очистку Zabbix выполняют отдельной
командой `zabbix_uplinks_cleanup.py --dry-run`, а затем — после проверки
отчёта — без `--dry-run`.

Подробное описание результатов находится в
[`reports/README.md`](reports/README.md).

---

## Требования к данным NetBox

Чтобы подключение попало в мониторинг, в NetBox должна быть заполнена
следующая цепочка и указаны нужные поля.

**Provider (поставщик).** Нужны поля:

| Поле | Тип | Зачем нужно |
|------|-----|-------------|
| `aggregate_limit_gbps` | число | Общий лимит поставщика в Гбит/с. Без него у поставщика не будет агрегатных триггеров на 90% и 100%. |
| `slo_percent` | число | Целевой уровень доступности именно этого поставщика. Если поле пустое, берётся общее значение `PROJECT_PROVIDER_SLO_PERCENT` из `uplinks_config.py`. |

**Circuit (канал).** Нужны все условия сразу:

| Что | Значение |
|-----|----------|
| Поле `status` канала | `Active`. Значения `Planned`, `Offline`, `Decommissioned` и другие пропускаются. |
| Тип канала | `Uplink`. Другие типы каналов игнорируются. |
| Поле `billing_model` типа Selection | Схема расчёта: `Flat`, `Burst`, `95thAggBurst`, `FlatAggCap`. Значение `Burst` включает отдельные триггеры и отдельный сервис на этот канал. |
| Стандартное поле `commit_rate` | Гарантированная скорость канала в Кбит/с. Не заполняется только при `billing_model=FlatAggCap`, когда лимит общий и хранится у поставщика в `aggregate_limit_gbps`. |

**Circuit Termination (терминация).**
Нужна сторона **A**. Сторона Z не используется.

**Cable (кабель).** От терминации стороны A до интерфейса устройства. Кабель
может идти через оптический распределительный шкаф через пару проходных
портов NetBox (задний и передний порт) — такой путь тоже прослеживается.
Путь принимается только если NetBox сообщает, что он дошёл
до конца и не разветвляется.

**Device (устройство).** У устройства, на котором заканчивается кабель,
должен быть тег `border` (имя настраивается переменной окружения
`NETBOX_TAG`).

Описание интерфейса не определяет Provider и не включает канал в мониторинг.
Тег `automatization` не влияет на область мониторинга.

Проверьте готовую цепочку без записи:

```bash
python netbox_uplinks_inventory.py --json --dry-run
```

Канал должен попасть в `complete`, а не в `incomplete`.

### Как отключить Circuit

1. В NetBox измените статус Circuit на `Offline` или `Decommissioned`.
   Сам объект не удаляйте.
2. Сначала выполните `netbox_uplinks_inventory.py --json --dry-run`, затем
   `run_uplinks_full.py --plan`.
3. Проверьте отчёт Zabbix и только после этого выполняйте рабочий запуск.
   Объекты NetBox не удаляются. `run_uplinks_full.py`
   пересобирает рабочие объекты Zabbix по оставшимся каналам. Скрипт
   `zabbix_uplinks_cleanup.py` очищает весь набор объектов uplinks, а не один
   Circuit, поэтому используйте его только для полной ручной очистки и сначала
   запускайте `zabbix_uplinks_cleanup.py --dry-run`. Если отключается последний
   полный Circuit, рабочий запуск остановится без записи; отдельную полную
   очистку в этом случае выполняйте только после проверки её отчёта.

## Отчёты

```bash
python run_uplinks_full.py --plan --report /tmp/uplinks-run.log
```

`--plan` читает NetBox и Zabbix и ничего не записывает. Важно: `--report`
сохраняет журнал полного запуска, а сам план записывается в `run_logs/`.
Назначение всех файлов и форматы описаны в
[`reports/README.md`](reports/README.md).

## Запуск

```bash
cd zabbix-uplinks

# Активировать venv
source .venv/bin/activate   # Linux/macOS
# или:  .venv\Scripts\activate  на Windows

# Установить зависимости
pip install -r requirements.txt

# Переменные окружения для рабочего запуска
export NETBOX_URL="https://your-netbox.example.com"
export NETBOX_TOKEN="your-api-token"
export ZABBIX_URL="https://your-zabbix.example.com"
export ZABBIX_TOKEN="your-api-token"
```

Для рабочего запуска нужны только адреса и токены NetBox и Zabbix. Переменные
`SSH_*` относятся к отдельной ручной проверке интерфейсов.

Без активации venv можно вызывать интерпретатор напрямую:

```bash
.venv/bin/python uplinks_stats.py
```

---

## Инструменты и ключи

### `uplinks_stats.py`

Единый скрипт с двумя режимами.

Режим `--report` выводит таблицу сверки интерфейсов. Его назначение и формат
описаны в [`reports/README.md`](reports/README.md).

Режим статистики читает `dry-ssh.json` или, с флагом `--fetch`, опрашивает
поддерживаемые устройства по SSH. Результат — таблица или JSON.

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
| `--host NAME` | При `--fetch`: опросить только указанный хост (имя устройства в NetBox). Платформа берётся из NetBox по устройству, указывать `--platform` вместе с `--host` не нужно. Для обновления существующего файла используйте вместе с `--merge-into`. |
| `--json` | Вывод в формате JSON (режим статистики). При `--fetch --json` прогресс идёт в stderr, в stdout — только JSON (удобно: `--fetch --json > dry-ssh.json`) |
| `--merge-into [FILE]` | При `--fetch`: загрузить FILE (по умолчанию `dry-ssh.json`), подставить в него данные по опрошенным хостам и сохранить. Остальные хосты в файле не меняются. Удобно для обновления одного хоста: `--fetch --host HOST --merge-into` |
| `--from-file FILE` | Путь к JSON с ключом `devices` (по умолчанию `dry-ssh.json`) |
| `--inventory-file FILE` | Опрашивать только устройства из цепочек NetBox. Без этого ключа опрашиваются все устройства с тегом из `NETBOX_TAG`. Обычный полный запуск этот ручной SSH-инструмент не вызывает. Список интерфейсов на устройстве по-прежнему отбирается по тексту `Uplink:` в описании порта |

**Переменные окружения**

- **Обязательные:** `NETBOX_URL`, `NETBOX_TOKEN`, `SSH_USERNAME`, `SSH_PASSWORD`.
- **Со значениями по умолчанию:** `SSH_HOST_SUFFIX` (`.3hc.io`), `PARALLEL_DEVICES` (`6`), `NETBOX_TAG` (`border`).
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

### `netbox_checks.py`

Сверка данных из JSON-файла с NetBox и при необходимости обновление интерфейсов в NetBox. **Подключения по SSH к устройствам нет** — скрипт всегда читает данные только из файла. Если `-f` не указан, используется файл по умолчанию `dry-ssh.json` (его можно получить через `uplinks_stats.py --fetch --json > dry-ssh.json` или использовать уже сохранённый). Устройства в NetBox выбираются по тегу.

**Входной файл:** по умолчанию `dry-ssh.json` (структура с ключом `devices`: имя устройства → список интерфейсов с полями `name`, `description`, `mediaType`, `bandwidth`, `duplex`, `physicalAddress`, `mtu`, `txPower` и т.д.).

**Переменные:** `NETBOX_URL`, `NETBOX_TOKEN`, `NETBOX_TAG`. При неверном или просроченном токене/недоступности NetBox скрипт завершается с сообщением в stderr и кодом 1 (без трассировки).

#### Файл и хост

| Ключ | Описание |
|------|----------|
| `-f`, `--file FILE` | Путь к JSON с устройствами и интерфейсами (по умолчанию `dry-ssh.json`) |
| `--host NAME` | Обработать только один хост (имя устройства) |
| `--platform {arista,juniper,all}` | Обрабатывать устройства по платформе в NetBox: **all** (по умолчанию), **arista** или **juniper** |

#### Проверки (включить нужные колонки и сверку)

| Ключ | Описание |
|------|----------|
| `--intname` | Сверка имён интерфейсов (файл vs NetBox) с поиском по вариантам написания |
| `--description` | Сверка поля description (файл vs NetBox) |
| `--mediatype` | Сверка mediaType из файла и type в NetBox |
| `--mt-ref [FILE]` | Справочник типов для mediaType (по умолчанию `netbox_interface_types.json`). Значения приводятся к одному формату (value/slug). Другой файл: `--mt-ref other.json` |
| `--no-mt-ref` | Не загружать справочник типов (отключить использование по умолчанию) |
| `--bandwidth` | Сверка bandwidth (файл, bps) и speed (NetBox, Kbps) |
| `--duplex` | Сверка duplex (файл vs NetBox). Для интерфейсов с bandwidth ≥ 10 Gbps пустое значение с устройства считается full (проверка duplex для 10G+ не имеет смысла). |
| `--mac` | Сверка physicalAddress (файл) и mac_address (NetBox) |
| `--mtu` | Сверка mtu (файл vs NetBox) |
| `--tx-power` | Сверка txPower (файл) и tx_power (NetBox) |
| `--scope-to-file` | Считать только устройства из входного файла; устройства с тегом `border` вне текущей области не показывать как расхождение |
| `--forwarding-model` | Сверка forwardingModel (файл) и mode (NetBox). В NetBox: `routed` → mode=null, `bridged` → mode=tagged |
| `--ip-address` | Сверка IPv4/IPv6 (файл: ipv4_addresses, ipv6_addresses, ip_vrf) и привязанных к интерфейсу в NetBox (с учётом VRF) |
| `--lag` | Сверка LAG / Related Interfaces: aggregateInterface (файл) и lag (NetBox) у физических интерфейсов — членов LAG |
| `--parent` | Сверка Parent interface: aggregateInterface (файл) и parent (NetBox) у логических интерфейсов (ae5.0 → ae5) |
| `--all` | Включить все обычные проверки сразу (intname, description, mediatype, bandwidth, duplex, mac, mtu, forwarding-model, ip-address, lag, parent). `tx-power` не входит |
| `--no-tx-power` | Явно исключить мощность передатчика из сверки; имеет приоритет над `--tx-power` |

При `--no-mt-ref` значения `mediaType` не приводятся к единому формату,
поэтому расхождения могут быть вызваны разным написанием.

Если ни один ключ проверки не указан (и не передан `--apply`), включаются все проверки, колонки «что подставим» (`--show-change`) и скрытие колонок без расхождений (`--hide-no-diff-cols`). При полном совпадении выводится итог: `Result: all checked fields match NetBox. No discrepancies found.`

#### Вывод и колонки

| Ключ | Описание |
|------|----------|
| `--show-change` | Показать колонки «что подставим в NetBox» по выбранным ключам: mtToSet, descToSet, speedToSet, dupToSet, mtuToSet, txpToSet, fwdToSet |
| `--hide-empty-note-cols` | Не выводить колонки примечаний (nD, nM, …), если во всех строках они пустые |
| `--hide-no-diff-cols` | Не выводить группы колонок (файл/Netbox/примечание), в которых ни в одной строке нет расхождения |
| `--hide-ok-hosts` | Не выводить в таблице хосты без расхождений; вывести их списком (`Hosts without discrepancies (N): …`) и статистику. Если указан один этот ключ (без других проверок), автоматически включаются все проверки |
| `--json` | Вывод в JSON (по умолчанию — таблица) |
| `--version` | Вывести версию скрипта и выйти |

#### Применение изменений в NetBox

| Ключ | Описание |
|------|----------|
| `--apply` | При разнице по выбранным ключам обновлять интерфейс в NetBox. При `--apply` таблица не выводится. Справочник типов используется по умолчанию; `--no-mt-ref` отключает его. |
| `--existing-only` | Не менять связи LAG и parent. MAC- и IP-адреса при `--apply` в любом случае обрабатываются только как существующие объекты. |

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
| `--mac` | сущность MAC (dcim.mac-addresses) + поле интерфейса | При расхождении используется существующая запись MAC. Новая запись и перенос между интерфейсами не выполняются; для MAC на этом интерфейсе при необходимости обновляется `primary_mac_address`. |
| `--ip-address` | IP (ipam.ip_addresses) + VRF | Проверяется привязка существующего IP. Создание, отвязка, перенос и изменение VRF не выполняются; отсутствующий адрес или интерфейс пропускается. |

В NetBox MAC — отдельная сущность (dcim.mac-addresses). При `--apply`
используется только существующая запись MAC, уже связанная с нужным
интерфейсом; при необходимости обновляется `primary_mac_address`. Если
интерфейс, MAC или IP отсутствует, объект не создаётся и отмечается как
пропущенный.

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

# Компактный отчёт: только хосты с расхождениями, список OK и статистика
python netbox_checks.py -f dry-ssh.json --hide-ok-hosts
```

---

### `netbox_interface_types.py`

Скачивание списка типов интерфейсов NetBox из репозитория netbox-community/netbox
(choices.py), извлечение value и label и сохранение для
`netbox_checks.py --mt-ref`.

| Ключ | Описание |
|------|----------|
| `-o`, `--output FILE` | Путь к выходному JSON (по умолчанию `netbox_interface_types.json`) |

```bash
python netbox_interface_types.py
python netbox_interface_types.py -o my_types.json
```

---

## Ручная сверка интерфейсов

Этот сценарий не относится к рабочему запуску и нужен только для сверки
данных устройства с NetBox.

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

### `zabbix_map.py`

Построение карты Zabbix по `--inventory-file` из NetBox. При обращении к
Zabbix API выполняется поиск хостов и items (Bits received/sent). Для одной
пары «хост, провайдер» рисуется один линк — при равных условиях выбирается
логический интерфейс (например `ae5.0`), иначе физический.

Раскладка проверяет границы элементов и подписей. При пересечении обновление
карты блокируется; провайдеры получают подпись сверху, а устройства — снизу.

**Окраска линка** задаётся списком **link triggers** с приоритетом (что указано раньше — сильнее при конфликте цветов): (1) per-link **100%** — красный **жирный** (`drawtype` bold); (2) агрегат провайдера **100%**; (3) per-link **90%** — жёлтый; (4) агрегат **90%**. Нужны соответствующие триггеры в Zabbix (**zabbix_sync_commit_rate.py** с **`--create-link-triggers`** для Burst; **zabbix_provider_aggregate.py** для лимитов из поля NetBox `aggregate_limit_gbps`).

**Карта Zabbix**

- При переданном `--inventory-file` и без команды изменения, если карты с именем из `uplinks_config.MAP_NAME` нет, создаётся карта со всеми элементами; если карта уже есть, изменения не вносятся.
- `--update-map` — принудительно обновить карту (хосты, провайдеры, линки) из `--inventory-file`. С `--host` — только указанный хост и его линки; остальные элементы карты **не удаляются**. Без `--host`: с карты убираются хосты и облака провайдеров, которых нет в текущем inventory, и любые элементы не типа «хост/картинка» (устаревшие линки пересобираются по данным). Чтобы оставить старые элементы: **`--keep-obsolete-map-elements`**.
- `--print-table` — вывести в консоль таблицу (hostname, interface, description, ISP); с `--zabbix` — с hostid и ключами items.
- `--create-map` — только создать пустую карту, если её нет (без элементов).

**Раскладка карты**

- Провайдеры сортируются по убыванию числа подключений; блоки идут слева направо, при нехватке места — перенос на следующую строку.
- В блоке провайдер получает подпись сверху, хосты — снизу.
- Провайдер с одним подключением рисуется рядом с этим хостом в том же блоке, без отдельного ряда.
- При переполнении размеры карты увеличиваются автоматически.
- Один узел на хост и один на провайдера (элементы не дублируются).

**Подписи линков**

Имя интерфейса и строки In/Out с макросами Zabbix `{?last(/host/key)}` (скорость по items Bits received/sent).

**Ссылки у хостов (URLs)**

У каждого элемента-хоста на карте задаются ссылки на график по каждому uplink-интерфейсу: подпись — имя провайдера и «Bits received» (например «Beeline 5 Bits received»), URL — `history.php?action=showgraph&itemids[]=<itemid>&from=now-1d&to=now` (график за последние сутки). Базовый URL берётся из `ZABBIX_URL` (обрезается `/api_jsonrpc.php`).

**Переменные:** `ZABBIX_URL` (базовый URL, например `https://zabbix.example.com`; скрипт сам дописывает `/api_jsonrpc.php` при необходимости) и `ZABBIX_TOKEN` (Bearer-токен, Zabbix 7). Провайдер и интерфейс берутся из `--inventory-file`. Имя карты задаётся в `uplinks_config.MAP_NAME`. Иконки элементов (хосты, провайдеры) настраиваются в Zabbix, соответствующие ID можно задать в конфиге.

| Ключ | Описание |
|------|----------|
| `--inventory-file` | Inventory из NetBox для обычного запуска |
| `--zabbix` | Запросить Zabbix API (для карты или таблицы) |
| `--print-table` | Вывести таблицу в консоль (с `--zabbix` — с hostid и ключами items) |
| `--create-map` | Только создать пустую карту, если её нет |
| `--update-map` | Обновить карту; без `--host` — удалить с карты хосты/провайдеров не из JSON; с `--host` — только один хост |
| `--keep-obsolete-map-elements` | С `--update-map` не удалять с карты хосты/провайдеры, отсутствующие в JSON |
| `--host HOSTNAME` | Работать только с одним хостом |
| `--debug` | Отладочный вывод и тело запросов map.create/update |
| `--no-cache` | Не использовать кэш Zabbix |
| `--export-map SYSMAPID` | Вывести JSON карты из API (для сравнения с ручной картой) |

```bash
# Обновить карту из NetBox inventory
python zabbix_map.py --inventory-file netbox_inventory.json --zabbix --update-map

# Принудительно обновить карту, например после создания триггеров
python zabbix_map.py --inventory-file netbox_inventory.json --zabbix --update-map

# Таблица в консоль (без Zabbix / с Zabbix)
python zabbix_map.py --inventory-file netbox_inventory.json --print-table
python zabbix_map.py --inventory-file netbox_inventory.json --print-table --zabbix

# Один хост
python zabbix_map.py --inventory-file netbox_inventory.json --zabbix --host router-001

# Создать пустую карту (один раз)
python zabbix_map.py --create-map

# Обновить карту по всем хостам (с отладкой)
python zabbix_map.py --inventory-file netbox_inventory.json --zabbix --update-map --debug

# Обновить только один хост и его линки
python zabbix_map.py --inventory-file netbox_inventory.json --zabbix --update-map --host router-001 --debug

# Выгрузить карту из API (например, созданную вручную) для сравнения формата
python zabbix_map.py --export-map 10 > map_10.json
```

---

### `zabbix_uplinks_dashboard.py` — дашборд Zabbix с графиками uplink

Создание или обновление дашборда в Zabbix выполняется по NetBox inventory.
На странице локации создаётся один итоговый график на Provider, а физический
участник LAG не считается отдельно от логического интерфейса.

**Вход:** NetBox inventory и Zabbix API.
Переменные: `ZABBIX_URL`, `ZABBIX_TOKEN`.

| Ключ | Описание |
|------|----------|
| `--inventory-file` | Inventory из NetBox для обычного запуска |
| `--dashboard-name` | Название дашборда в Zabbix (по умолчанию `Uplinks`) |
| `--dashboard-by-location` | Дашборд по локациям (вкладка = локация); пустая строка — не создавать |
| `--dashboard-by-provider` | Сводный дашборд по провайдерам с >1 линком (вкладка = Cogent, HE и др.): стеки по линкам и при наличии хостов «Uplinks {Provider}» — виджеты суммарного трафика (aggregate); пустая строка — не создавать |
| `--providers` | Список имён провайдеров для сводного дашборда (по умолчанию: `PROVIDERS_FOR_SUMMARY` + провайдеры из полных NetBox-подключений) |
| `--no-cache` | Не использовать кэш Zabbix |
| `--no-show-threshold` | Не рисовать пороги триггеров (линию порога) на графиках |
| `--debug` | Отладочный вывод |

Если дашборд с таким именем уже есть — он обновляется (страница с виджетами перезаписывается). Если нет — создаётся новый.

**Линия порога на графике.** Включена опция Simple triggers: пороги простых триггеров рисуются пунктиром. Явный простой триггер **100%** (`max(Bits received, …) > {$UPLINK.BPS.MAX:…}`) создаётся только там, где в **`zabbix_sync_commit_rate.py`** включён **`--create-link-triggers`** и у контура в NetBox поле **`billing_model`** равно **`Burst`**; иначе линия порога на графике опирается на триггеры шаблонов / другие правила Zabbix. Период **max** задаётся в **`uplinks_config.py`** (`TRIGGER_FUNCTION_PERIOD`, по умолчанию 15m).

```bash
# Создать или обновить дашборд «Uplinks» с графиками по inventory NetBox
python zabbix_uplinks_dashboard.py --inventory-file netbox_inventory.json

# Другое имя дашборда
python zabbix_uplinks_dashboard.py --inventory-file netbox_inventory.json --dashboard-name "Uplinks traffic"

# Сводный дашборд: по умолчанию провайдеры из конфига + из inventory; явно задать список:
python zabbix_uplinks_dashboard.py --inventory-file netbox_inventory.json --providers Cogent Hurricane
```

---

### `zabbix_sync_commit_rate.py` — макросы и (по флагу) per-link триггеры для Burst

Для каждого интерфейса в NetBox, подключённого кабелем к circuit termination (сторона A), скрипт берёт **commit rate** контура (Kbps), переводит в bps и создаёт на хосте два макроса с контекстом по интерфейсу: **{$UPLINK.BPS.MAX:"…"}** (HIGH, по умолчанию 100% от commit) и **{$UPLINK.BPS.WARN:"…"}** (WARN, по умолчанию 90%). Значения макросов = commit_rate × (THRESHOLD_PERCENT_* / 100).

**Простые триггеры на линк** создаются **только** при **`--create-link-triggers`** и только для интерфейсов, у которых контур в NetBox имеет поле **`billing_model: Burst`** (без учёта регистра). Если кабель заведён на физический порт, входящий в логическое объединение, триггер создаётся на логическом интерфейсе, как он назван в Zabbix (например `ae5.0`). На каждый такой интерфейс создаются три объекта:

1. **90%** — `max(Bits received, TRIGGER_FUNCTION_PERIOD) > {$UPLINK.BPS.WARN:"iface"}`; теги: `scripts:automatization`, `provider`, `circuit`, `billing=burst` (**без** `sla=true`). Зависимость от триггера **100%** (как задано в Zabbix API).
2. **100%** — `max(…) > {$UPLINK.BPS.MAX:"iface"}`; те же теги, без `sla=true`.
3. **SLA breach** — `min(…) > {$UPLINK.BPS.MAX:"iface"}` с периодом **`SLA_TRIGGER_FUNCTION_PERIOD`** (например 1h); дополнительно **`sla=true`** (как у агрегатов) — к этому триггеру привязывается сервис **`Uplinks Burst {circuit_id}`** в **zabbix_provider_services.py**.

Описания концов строк задаются в **`uplinks_config.py`**: `TRIGGER_DESC_90_SUFFIX`, `TRIGGER_DESC_100_SUFFIX`, `TRIGGER_DESC_SLA_BREACH_SUFFIX`, `TRIGGER_DESC_UTIL_*_SUFFIX`.

**Утилизация порта.** Для **физических** uplink из NetBox inventory (на
Juniper — `et-*`, без `ae` / `aeN.0` / `isLag` / `isLogical`; на Arista —
`Ethernet*`) на хосте Zabbix создаются макросы
**{$UPLINK.UTIL.WARN:"iface"}** = 70 и **{$UPLINK.UTIL.CRIT:"iface"}** = 85
(проценты; пороги в `uplinks_config.py`). Два триггера получают тег
`scripts:automatization`, формула соответствует шаблону **Arista by SNMP**.
Файл `dry-ssh.json` можно передать дополнительно для сопоставления физического
и логического имени. Если items `net.if.in/out/speed` на хосте нет, интерфейс
пропускается.

Обрабатываются только физические интерфейсы из цепочки NetBox. Триггеры
перегрузки на портах вне области мониторинга при запуске удаляются — удаляются только
триггеры с тегом `scripts:automatization` или вовсе без тегов. Что именно
будет удалено, видно заранее в предварительном отчёте `--plan`, в разделе
`util_triggers` в списке `delete`.

Старые item'ы **net.if.threshold["..."]**, если остались, удаляются.

Макросы **{$UPLINK.UTIL.*}** используются в процентах. Макросы
**{$IF.UTIL.*}** скрипт не изменяет; ими управляют шаблоны Zabbix.

**Переменные:** `NETBOX_URL`, `NETBOX_TOKEN`, `NETBOX_TAG`, `ZABBIX_URL`, `ZABBIX_TOKEN`.

| Ключ | Описание |
|------|----------|
| `--inventory-file` | Inventory из NetBox для обычного запуска |
| `-d`, `--dry-ssh` | Необязательный JSON от `uplinks_stats.py` для сопоставления физического и логического имени |
| `--dry-run` | Не менять макросы в Zabbix, только вывести что бы установили. Нельзя совмещать с ключами удаления триггеров |
| `--debug` | Отладочный вывод |
| `--create-link-triggers` | Создавать/обновлять триггеры 90%/100%/SLA breach только для Burst-линков |
| `--no-util-triggers` | Не создавать макросы/триггеры **{$UPLINK.UTIL.*}** (по умолчанию включены для физических uplink из inventory) |
| `--delete-link-triggers` | Удалить триггеры commit 90%/100%/SLA breach |
| `--delete-util-triggers` | Удалить триггеры uplink utilization warn/crit |

**BPS-макросы:** только пары с **кабелем** circuit termination (A) → интерфейс, устройство с тегом из **`NETBOX_TAG`** (по умолчанию `border`). **Util:** физические интерфейсы берутся из той же цепочки NetBox. Для логических имён (Juniper) можно дополнительно передать `-d dry-ssh.json` для сопоставления имени.

**Поведение при неудачном чтении NetBox.** Неудачным считается любой из трёх
случаев: отказано в доступе, не удалось получить список поставщиков, часть
запросов завершилась ошибкой. Тогда действует общее правило для всех скриптов,
которые пишут в Zabbix:

- ничего не удаляется;
- не выполняются записи, которые заменяют сразу целый набор объектов, потому
  что набор, собранный по неполным данным, молча теряет часть записей. Это
  относится к макросам хоста (весь набор `{$UPLINK.BPS.*}` или
  `{$UPLINK.UTIL.*}` переписывается одной операцией), к дашбордам (страницы и
  виджеты переписываются целиком), к связям и элементам схемы, к формуле
  агрегатного вычисляемого item (формула — это и есть перечень слагаемых) и к
  составу сервисов;
- безопасные создание и обновление по уже полученным данным выполняются;
- скрипт завершается с кодом ошибки и понятным сообщением, поэтому полный
  запуск помечает результат неуспешным.

Это правило применяется ко всем обращениям к Zabbix. При неполных данных
опасные запросы не отправляются, а запуск завершается с ошибкой.

Схема (`zabbix_map.py`) при неполных данных **не обновляется совсем**: связи
на схеме записываются одним набором, и набор, собранный по части подключений,
удалил бы связи остальных. Лучше оставить схему прежней и завершить запуск с
ошибкой, чем показать неполную картину.

```bash
python zabbix_sync_commit_rate.py --inventory-file netbox_inventory.json
python zabbix_sync_commit_rate.py --inventory-file netbox_inventory.json --dry-run
python zabbix_sync_commit_rate.py --inventory-file netbox_inventory.json --create-link-triggers
python zabbix_sync_commit_rate.py --inventory-file netbox_inventory.json --delete-link-triggers
python zabbix_sync_commit_rate.py --inventory-file netbox_inventory.json --delete-util-triggers
```

---

### `zabbix_provider_aggregate.py` — агрегат по провайдеру в Zabbix

Для схем, где у провайдера общий лимит по всем каналам, лимит хранится в
NetBox у Provider в поле **`aggregate_limit_gbps`** (Гбит/с). Скрипт берёт
провайдеров и каналы из полных цепочек NetBox и создаёт хосты
**«Uplinks {Provider}»** в группе **`UPLINKS_AGGREGATE_GROUP`** с calculated
items (сумма Bits in/out по каналам). Для провайдера с лимитом создаются
**три** триггера:

- **90%** и **100%** от лимита — теги **`scripts:automatization`** и **`provider={name}`** (без **`sla=true`**).
- **SLA breach** — устойчивое превышение 100% за период **`SLA_TRIGGER_FUNCTION_PERIOD`**, дополнительный тег **`sla=true`** — под них подходит сервис **`Uplinks {Provider}`** и SLA в Zabbix.

Зависимость **90% → 100%** (шум на карте при одновременной сработке разных уровней снижается за счёт семантики зависимостей Zabbix). Если у провайдера в NetBox не заполнено поле **`aggregate_limit_gbps`**, агрегатные триггеры для него могут быть удалены (ключ **`--keep-triggers-without-limits`** это отключает). Если чтение NetBox прошло не полностью, удаление триггеров не выполняется и скрипт завершается с кодом ошибки.

Карта (**`zabbix_map.py`**) и сводный дашборд используют эти триггеры для цвета и виджетов.

**Переменные:** `ZABBIX_URL`, `ZABBIX_TOKEN`. Данные NetBox передаются через
`--inventory-file`.

| Ключ | Описание |
|------|----------|
| `--inventory-file` | Inventory из NetBox для обычного запуска |
| `--no-cache` | Не использовать кэш Zabbix |
| `--debug` | Отладочный вывод |
| `--keep-triggers-without-limits` | Не удалять агрегатные триггеры у провайдера, у которого нет лимита |

```bash
python zabbix_provider_aggregate.py --inventory-file netbox_inventory.json
```

---

### `zabbix_provider_services.py` — сервисы и SLA в Zabbix

Создаёт или обновляет **листовые сервисы** и объекты **SLA** в Zabbix по
активным Circuit из NetBox:

- Для каждого Provider, найденного через активные Circuit: сервис **`Uplinks {Provider}`**, **`problem_tags`**: `provider={имя}` и `sla=true` (совпадает с агрегатным триггером **SLA breach**, если для Provider задан общий лимит).
- Для каждого активного Circuit с **`billing_model: Burst`**: сервис **`Uplinks Burst {circuit_id}`**, **`problem_tags`**: `circuit`, `sla=true`, `billing=burst` (совпадает с per-link триггером **SLA breach** после **`zabbix_sync_commit_rate.py --create-link-triggers`**).
  Для SLA этого сервиса фильтр задаётся по **`service_tags: circuit=<id>`** (без `role`) — это предотвращает широкий match в UI некоторых версий Zabbix.

Целевой процент **`SLO`** для создаваемых SLA сначала берётся из NetBox,
затем из **`PROJECT_PROVIDER_SLO_PERCENT`** в `uplinks_config.py`.
Эффективная дата старта SLA — **`SLA_EFFECTIVE_DATE_UTC`** в
`uplinks_config.py`.

Опционально **`--parent-service NAME`** — общий родитель для всех создаваемых сервисов.

**Переменные:** `ZABBIX_URL`, `ZABBIX_TOKEN`, `NETBOX_URL`, `NETBOX_TOKEN`.

| Ключ | Описание |
|------|----------|
| `--inventory-file` | Inventory из NetBox для обычного запуска |
| `--parent-service` | Имя родительского сервиса (создаётся при отсутствии) |
| `--debug` | Отладочный вывод API |

**Порядок с Burst:** сначала **`python zabbix_sync_commit_rate.py --inventory-file netbox_inventory.json --create-link-triggers`** (чтобы триггеры имели актуальные теги и описание SLA breach), затем **`python zabbix_provider_services.py --inventory-file netbox_inventory.json --parent-service "Uplinks providers"`**.

```bash
python zabbix_provider_services.py \
  --inventory-file netbox_inventory.json \
  --parent-service "Uplinks providers"
```

---

### `zabbix_provider_sla.py`

Отчёт доступности по истории событий Zabbix. Команды и описание формата
вынесены в [`reports/README.md`](reports/README.md).

---

### `zabbix_uplinks_cleanup.py` — очистка артефактов Zabbix

Удаляет в Zabbix объекты, созданные скриптами uplinks: простые триггеры **90%/100%/SLA breach** на интерфейсах (тег `scripts:automatization` / по описанию `Interface …`), старые item'ы **net.if.threshold["..."]**, карту с именем из конфига, дашборды **Uplinks** и связанные (имена задаются ключами). Макросы {$UPLINK.BPS.MAX}, {$UPLINK.BPS.WARN} и шаблонные {$IF.UTIL.*} не удаляются.

**Переменные:** `ZABBIX_URL`, `ZABBIX_TOKEN`.

| Ключ | Описание |
|------|----------|
| `--dashboard-name NAME` | Имя основного дашборда (по умолчанию `Uplinks`) |
| `--dashboard-by-location NAME` | Имя дашборда по локациям (по умолчанию `Uplinks (by location)`); пустая строка — не удалять |
| `--dashboard-by-provider NAME` | Имя сводного дашборда по провайдерам (по умолчанию `Uplinks by providers`); пустая строка — не удалять |
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
| `dry-ssh.json` | Локальный результат отдельного SSH-сбора для `uplinks_stats.py` и `netbox_checks.py`; используется при запуске с `--netbox-checks` |
| `netbox_interface_types.json` | Справочник типов интерфейсов NetBox (value, label); используется `--mt-ref` в `netbox_checks.py` |
| `uplinks_config.py` | Имя карты/дашбордов, теги триггеров (`scripts`/`automatization`, `sla`/`true`), описания **90%/100%** и **SLA breach** на линке (`TRIGGER_DESC_*`, `TRIGGER_DESC_SLA_BREACH_SUFFIX`), периоды **`TRIGGER_FUNCTION_PERIOD`** и **`SLA_TRIGGER_FUNCTION_PERIOD`**, макросы, цвета линков, префиксы агрегатных хостов, тег старых объектов **`NETBOX_AUTOMATION_TAG`** и общий целевой уровень доступности **`PROJECT_PROVIDER_SLO_PERCENT`**. Копия примера: `uplinks_config.example.py`. |
| `zabbix_uplinks_cache.json` | Кэш данных Zabbix (хосты, items); создаётся рядом с переданным inventory-файлом, не коммитить |
| `requirements.txt` | Зависимости: pynetbox, paramiko, requests |
