# Uplinks (NetBox + SSH)

**Версия 1.1** — мониторинг Burst (`billing_model`), per-link триггеры 90%/100%/SLA breach, сервисы Zabbix и offline‑отчёт SLA (агрегаты + контуры).

Скрипты для сверки и обновления данных интерфейсов в NetBox по данным с устройств (SSH / JSON-файл). Поддерживаются Arista и Juniper.

Виртуальное окружение создаётся в `.venv`.

---

## Краткое содержимое скриптов

| Скрипт | Описание |
|--------|----------|
| `run_uplinks_full.py` | **Один запуск** — полная цепочка: чтение цепочек из NetBox → опрос устройств по SSH → сверка существующих полей интерфейсов → Zabbix (макросы, триггеры, агрегаты, карта, дашборды, сервисы); отчёт о работе и об ошибках. Ключ **`--plan`** даёт предварительный отчёт без записи. См. COMMANDS.md. |
| `netbox_uplinks_inventory.py` | Только чтение NetBox: обход Provider → Circuit → терминация стороны A → кабель → интерфейс на устройстве с тегом `border`. Показывает полные и неполные цепочки. Ключи: `--tag`, `--json`, `--dry-run`, `--debug`. |
| `zabbix_uplinks_plan.py` | Только чтение: сравнивает текущее состояние Zabbix с тем, что сделал бы обычный запуск, и пишет отчёт. Ничего не создаёт, не меняет и не удаляет. |
| `uplinks_stats.py` | Сбор данных с устройств по SSH (Arista/Juniper) или отчёт NetBox vs устройство; выход — таблица или JSON (`dry-ssh.json`). |
| `netbox_checks.py` | Сверка данных из JSON с NetBox (интерфейсы: имя, description, тип, speed, duplex, MAC, MTU, IP, LAG и др.) и при необходимости обновление полей в NetBox. |
| `netbox_interface_types.py` | Скачивание справочника типов интерфейсов NetBox (value/label) из репозитория в JSON для `--mt-ref`. |
| `zabbix_map.py` | Таблица uplink'ов по dry-ssh; карта Zabbix (хосты, провайдеры, линки с items Bits in/out). Цвет линка: **приоритет** per-link **100%** (жирная красная) → агрегат провайдера **100%** → per-link **90%** → агрегат **90%** (при наличии соответствующих триггеров в Zabbix). |
| `zabbix_uplinks_dashboard.py` | Создание/обновление дашборда Zabbix с виджетами-графиками по каждому uplink (Bits received/sent). |
| `grafana_uplinks_graph.py` | Генерация JSON для панели Node graph в Grafana (узлы — хосты и провайдеры, рёбра — линки); опционально создание дашборда через Grafana API. |
| `generate_commit_rates.py` | Только для старого режима `--auto`. Генерация `commit_rates.json` по линкам из dry-ssh (провайдер, circuit_id, commit_rate_gbps). При мерже сохраняет дополнительные поля линков (например `billing_model`), служебные ключи **`_provider_limits`**, **`_provider_sla`** и другие `_…`. |
| `netbox_create_circuits.py` | По умолчанию только ищет в NetBox существующие провайдера, тип контура, контур, Termination A и кабель и сообщает, чего не хватает. Ничего не создаёт и не удаляет. Ключ **`--auto`** включает старое создание и изменение объектов и может заменить существующий кабель. |
| `zabbix_sync_commit_rate.py` | Макросы **{$UPLINK.BPS.MAX}** / **{$UPLINK.BPS.WARN}** из NetBox (bps). По **`dry-ssh.json`**: **{$UPLINK.UTIL.WARN/CRIT}** и триггеры перегрузки порта (**70%** avg 10m, **85%** avg 5m, формула как в шаблоне Arista in/out vs speed) — только для физических интерфейсов, которые нашлись в цепочке NetBox. Burst: **`--create-link-triggers`** — 90%/100%/SLA breach по commit. **`--no-util-triggers`** отключает util. |
| `zabbix_provider_aggregate.py` | Хосты `Uplinks {Provider}`: calculated items суммарного трафика и триггеры **90%**, **100%** и **SLA breach** по общему лимиту поставщика из поля NetBox **`aggregate_limit_gbps`**. Тег **`sla=true`** только на триггере **SLA breach**; на 90%/100% — `scripts:automatization` и `provider`. Зависимость 90% от 100% (как в Zabbix: дочерний не в PROBLEM без родителя). |
| `zabbix_uplinks_cleanup.py` | Очистка артефактов автоматизации в Zabbix: триггеры 90%/100%, старые item'ы порога, карта uplinks, дашборды uplinks (тег `scripts:automatization`). |
| `zabbix_provider_services.py` | Сервисы и встроенные SLA в Zabbix: **`Uplinks {Provider}`** для активных Circuit и **`Uplinks Burst {circuit_id}`** для Circuit с **`billing_model: Burst`**. Целевое значение SLA берётся из NetBox или `PROJECT_PROVIDER_SLO_PERCENT`; чтение **`_provider_sla`** из `commit_rates.json` возможно только с явным legacy-флагом. Опционально общий родитель **`--parent-service`**. |
| `zabbix_provider_sla.py` | Offline‑отчёт по истории событий триггеров для активных Circuit из NetBox: агрегаты и Burst, с приоритетом **SLA breach**, иначе **100%**. Окно времени — `--days` или `--from-ts`/`--to-ts`. Целевой уровень берётся из NetBox или `PROJECT_PROVIDER_SLO_PERCENT`; старый `_provider_sla` доступен только в legacy-режиме. |
| `netbox_uplinks_cleanup.py` | Откат автоматизации в NetBox: кабели, circuit terminations, контуры, при возможности — типы контуров и провайдеры (по тегу из `uplinks_config.NETBOX_AUTOMATION_TAG`). |

---

## Краткий алгоритм (от чего к чему)

### Рабочий режим: NetBox-first

В рабочем режиме NetBox является главным источником данных о подключениях:

```text
Provider → Circuit → Termination A → Cable → Interface → Border device
```

Провайдера, контур, терминацию и кабель создаёт человек в NetBox.
Скрипт не создаёт и не удаляет эти объекты. Он читает готовую цепочку,
получает данные с устройства, обновляет разрешённые поля существующего
интерфейса и настраивает мониторинг.

Для безопасного обновления интерфейсов полный запуск использует
`--existing-only`: отсутствующие интерфейсы, MAC-адреса и IP-адреса не
создаются, а перепривязки не выполняются.

Проверка цепочек без изменений:

```bash
python netbox_uplinks_inventory.py --dry-run
python netbox_uplinks_inventory.py --json --dry-run
```

В мониторинг попадают только активные контуры, у которых кабель доходит до
интерфейса устройства с тегом `border`.

### Переходный автоматический режим

Старый путь создания контуров и кабелей доступен только явно:

```bash
python run_uplinks_full.py --auto
```

Он предназначен для временной совместимости и тестовых сценариев. Его не
следует запускать на рабочем NetBox, где подключения создаёт человек:
старый код может заменить существующий кабель.

0. **Цепочки из NetBox**
   `netbox_uplinks_inventory.py --json` → **`netbox_inventory.json`** (какие Circuit в области мониторинга и на каких устройствах и интерфейсах они заканчиваются). Этот список задаёт, какие устройства опрашивать дальше.

1. **Данные с устройств**  
   `uplinks_stats.py --fetch --json` → опрос по SSH → **`dry-ssh.json`** (устройство → список uplink-интерфейсов с полями из show interfaces). При полном прогоне список устройств берётся из шага 0 через **`--inventory-file`**.

2. **Сверка и обновление NetBox (интерфейсы)**  
   `netbox_checks.py -f dry-ssh.json` → сравнение с NetBox → таблица расхождений или **`--apply`** для записи в NetBox (description, type, speed, duplex, MAC, MTU, IP, LAG и т.д.). При полном прогоне (**run_uplinks_full.py**) шаг 2 вызывается с **`--all --no-tx-power --mt-ref --existing-only --apply`**: обновляются только уже существующие объекты, новые интерфейсы, MAC- и IP-записи не создаются. Мощность передатчика в обычную сверку не входит.

   При необходимости: **`netbox_interface_types.py`** → `netbox_interface_types.json` для приведения типов (`--mt-ref`).

3. **Визуализация (Zabbix)**  
   По **`dry-ssh.json`** + Zabbix API:  
   - **`zabbix_map.py`** — карта uplinks (хосты, провайдеры, линки);  
   - **`zabbix_uplinks_dashboard.py`** — дашборды с графиками по uplink.  
   Grafana (**`grafana_uplinks_graph.py`**) — отдельно, не входит в **`run_uplinks_full.py`**.

4. **Синхронизация макросов commit rate в Zabbix**
   **`zabbix_sync_commit_rate.py`** берёт из цепочек NetBox гарантированную скорость контура (`commit_rate`, в Kbps), переводит в bps и создаёт макросы **{$UPLINK.BPS.MAX:"<интерфейс>"}** и **{$UPLINK.BPS.WARN:"<интерфейс>"}**.

   Для контуров, у которых в NetBox поле **`billing_model`** равно **`Burst`**, по флагу **`--create-link-triggers`** создаются/обновляются **три** простых триггера на интерфейс (90%, 100%, SLA breach по **`TRIGGER_DESC_SLA_BREACH_SUFFIX`** и **`SLA_TRIGGER_FUNCTION_PERIOD`** в `uplinks_config.py`). Для остальных схем billing per-link триггеры по умолчанию не создаются — окраска линков на карте идёт с **агрегатных** хостов провайдера.

   **`zabbix_provider_aggregate.py`** — агрегатные хосты `Uplinks {Provider}` и триггеры 90%/100%/SLA breach по общему лимиту поставщика из поля NetBox **`aggregate_limit_gbps`**.
   **`zabbix_provider_services.py`** — сервисы и SLA в UI Zabbix (провайдеры + Burst-контуры), цель — NetBox или **`PROJECT_PROVIDER_SLO_PERCENT`**.
   **`zabbix_map.py --update-map`** — привязка триггеров к линкам (per-link и агрегат) с приоритетом цвета, описанным в таблице скриптов.

**Итого:** NetBox (цепочки) → SSH/устройства → `dry-ssh.json` → NetBox (обновление существующих полей интерфейсов) → **zabbix_sync_commit_rate.py** (макросы + util; Burst-link триггеры с `--create-link-triggers`) → **zabbix_provider_aggregate.py** → **`zabbix_map.py --update-map`** → дашборды → **`zabbix_provider_services.py`**. Всё это делает **`run_uplinks_full.py`** одной командой (см. COMMANDS.md). Offline‑отчёт SLA: **`zabbix_provider_sla.py`**.

Файлы `commit_rates.json` и `description_to_name.json` в обычном запуске не читаются. Скрипты **`generate_commit_rates.py`** и **`netbox_create_circuits.py --auto`** нужны только старому пути `--auto`.

**Откат в Zabbix:** **zabbix_uplinks_cleanup.py** — удаляет триггеры, карту uplinks и дашборды (по именам). Макросы не трогает. Перед удалением: `--dry-run`.

**Откат в NetBox:** **netbox_uplinks_cleanup.py** — удаляет кабели, circuit terminations, контуры (и при необходимости типы контуров и провайдеры), помеченные тегом из `uplinks_config.NETBOX_AUTOMATION_TAG`. Интерфейсы и устройства не трогает. Перед удалением: `--dry-run`.

---

## Что человек заводит в NetBox

Скрипты не создают и не удаляют ни поставщика, ни контур, ни терминацию, ни
кабель. Всё это заводит человек. Ниже полный список того, что должно быть
заполнено, иначе подключение не попадёт в мониторинг.

**Provider (поставщик).** Создаётся вручную. Дополнительные поля:

| Поле | Тип | Зачем нужно |
|------|-----|-------------|
| `aggregate_limit_gbps` | число | Общий лимит поставщика в Гбит/с. Без него у поставщика не будет агрегатных триггеров на 90% и 100%. |
| `slo_percent` | число | Целевой уровень доступности именно этого поставщика. Если поле пустое, берётся общее значение `PROJECT_PROVIDER_SLO_PERCENT` из `uplinks_config.py`. |

**Circuit (контур, то есть конкретный канал).** Создаётся вручную. Нужны все
четыре условия сразу:

| Что | Значение |
|-----|----------|
| Встроенный статус контура | `Active`. Контур в состоянии `Planned`, `Offline`, `Decommissioned` и любом другом пропускается. |
| Тег | `uplinks` (имя настраивается через `NETBOX_MONITOR_TAG`). Тег ограничен типом объекта `circuits.circuit`. |
| Поле `uplinks_circuit_lifecycle` типа Selection | `active`. Значения `review` и `archived` исключают контур из мониторинга. Регистр букв не важен. |
| Поле `billing_model` типа Selection | Схема расчёта: `Flat`, `Burst`, `95thAggBurst`, `FlatAggCap`. Значение `Burst` включает отдельные триггеры и отдельный сервис на этот канал. |
| Стандартное поле `commit_rate` | Гарантированная скорость канала в Кбит/с. Не заполняется только при `billing_model=FlatAggCap`, когда лимит общий и хранится у поставщика в `aggregate_limit_gbps`. |

**Circuit Termination (терминация, то есть привязка конца канала).**
Нужна сторона **A**. Сторона Z не используется.

**Cable (кабель).** От терминации стороны A до интерфейса устройства. Кабель
может идти через оптический распределительный шкаф, то есть через пару
проходных портов NetBox (задний и передний порт) — такой путь тоже
прослеживается. Путь принимается только если NetBox сообщает, что он дошёл
до конца и не разветвляется.

**Device (устройство).** У устройства, на котором заканчивается кабель,
должен быть тег `border` (имя настраивается переменной окружения
`NETBOX_TAG`). Это тег устройства, а не контура — не путайте его с тегом
`uplinks`.

Один поставщик может одновременно иметь канал в мониторинге и канал в
состоянии `review`. Тег `automatization` остался только у объектов, созданных
когда-то старым автоматическим режимом, и на область мониторинга не влияет.

## Предварительный отчёт перед записью

```bash
python run_uplinks_full.py --plan --from-file \
  --report uplinks_plan_report.txt
```

Режим `--plan` ничего не записывает ни в NetBox, ни в Zabbix. Он читает
цепочки из NetBox, запускает сверку интерфейсов без `--apply`, читает текущие
объекты Zabbix и пишет отчёт о предполагаемых изменениях в
`run_logs/<дата>_zabbix_plan.json` и рядом в текстовом виде.

Ограничения режима:

- обязателен `--from-file` или `--no-fetch`: оборудование не опрашивается,
  берётся уже собранный `dry-ssh.json`. Для нового опроса нужен обычный
  запуск или ключ `--refresh`;
- нельзя совмещать с `--auto`;
- отчёт останавливается с ошибкой, если чтение NetBox прошло не полностью или
  нет ни одной полной цепочки. Отдельные неполные цепочки при успешном чтении
  показываются в разделе `inventory.incomplete`, а план строится по полным
  цепочкам;
- карты, дашборды, сервисы и триггеры Burst точно не сравниваются и помечены
  в отчёте как `not_evaluated`, то есть «не оценено». Для существующих
  агрегатных хостов поставщика так же не сравниваются расчётные элементы
  данных и триггеры лимита.

Разделы отчёта: `create` — будет создано, `update` — будет изменено,
`delete` — будет удалено, `unchanged` — совпадает с текущим состоянием,
`skipped` — работа не будет выполнена (например хост не найден в Zabbix; в
таком случае указана причина), `not_evaluated` — сравнение не выполнялось.

Отдельно тот же отчёт можно получить скриптом **`zabbix_uplinks_plan.py`**:

| Ключ | Описание |
|------|----------|
| `-d`, `--dry-ssh` | Путь к dry-ssh.json (по умолчанию `dry-ssh.json`) |
| `--inventory-file` | Взять цепочки из готового файла шага 0 и не обращаться к NetBox повторно |
| `-o`, `--output` | Записать отчёт в машиночитаемом виде (JSON) в файл |
| `--text` | Записать краткий текстовый отчёт в файл |
| `--json` | Вывести отчёт в формате JSON в стандартный вывод |
| `--create-link-triggers` | Учитывать в отчёте, что обычный запуск будет вызван с этим же ключом |
| `--debug` | Подробная диагностика в поток ошибок |

Ключи полного запуска **`run_uplinks_full.py`**:

| Ключ | Описание |
|------|----------|
| `--plan` | Предварительный отчёт без записи. Требует `--from-file` или `--no-fetch`, несовместим с `--auto` |
| `--no-fetch`, `--from-file` | Не опрашивать оборудование, взять готовый `dry-ssh.json` |
| `--refresh` | Принудительно обновить кэш опроса устройств (иначе используется кэш до 24 часов) |
| `--dry-ssh FILE` | Путь к `dry-ssh.json` |
| `--no-netbox-apply` | Пропустить запись в NetBox на шаге сверки интерфейсов |
| `--no-burst-triggers` | Не создавать триггеры для Burst-контуров |
| `--report FILE` | Дополнительно записать отчёт о запуске в файл |
| `--timeout SEC` | Ограничение на один шаг, по умолчанию 600 секунд |
| `--env-file FILE`, `--no-env-file` | Файл переменных окружения (по умолчанию `urls.env`) или отказ от него |
| `--stop-on-error`, `--no-stop-on-error` | Останавливаться на первой ошибке (по умолчанию) или продолжать |
| `--auto` | Старый путь: создание объектов в NetBox и чтение `commit_rates.json` |
| `--commit-rates FILE` | Путь к `commit_rates.json`; используется только вместе с `--auto` |
| `--location LOC` | Ограничить площадку. Допустим только вместе с `--auto` |

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
| `--inventory-file FILE` | Опрашивать только устройства из цепочек NetBox (файл шага 0). Без этого ключа опрашиваются все устройства с тегом из `NETBOX_TAG`. Полный запуск всегда передаёт этот ключ. Список интерфейсов на устройстве по-прежнему отбирается по тексту `Uplink:` в описании порта |

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
| `--platform {arista,juniper,all}` | Обрабатывать устройства по платформе в NetBox: **all** (по умолчанию), **arista** или **juniper** |

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
| `--all` | Включить все обычные проверки сразу (intname, description, mediatype, bandwidth, duplex, mac, mtu, forwarding-model, ip-address, lag, parent). `tx-power` не входит |
| `--no-tx-power` | Явно исключить мощность передатчика из сверки; имеет приоритет над `--tx-power` |

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
| `--existing-only` | Обновлять только уже существующие объекты. Отсутствующий интерфейс, MAC- или IP-адрес не создаётся, а помечается как пропущенный. Полный запуск использует этот ключ. |
| `--auto` | Разрешить создание отсутствующих интерфейсов, MAC- и IP-адресов (старое поведение). |

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

Построение таблицы uplink'ов по данным из `dry-ssh.json`; опционально — карта Zabbix. При обращении к Zabbix API: поиск хостов и items (Bits received/sent). Файл может содержать логические интерфейсы (Juniper: ae5, ae5.0, et-0/0/3); на карте для одной пары (хост, провайдер) рисуется один линк — выбирается интерфейс с items Zabbix, при равных условиях логический unit (например ae5.0), иначе физический.

**Окраска линка** задаётся списком **link triggers** с приоритетом (что указано раньше — сильнее при конфликте цветов): (1) per-link **100%** — красный **жирный** (`drawtype` bold); (2) агрегат провайдера **100%**; (3) per-link **90%** — жёлтый; (4) агрегат **90%**. Нужны соответствующие триггеры в Zabbix (**zabbix_sync_commit_rate.py** с **`--create-link-triggers`** для Burst; **zabbix_provider_aggregate.py** для лимитов из поля NetBox `aggregate_limit_gbps`).

**Карта Zabbix**

- **По умолчанию** (без ключей): если карты с именем из `uplinks_config.MAP_NAME` нет — создаётся карта со всеми элементами (хосты, провайдеры, линки); если карта уже есть — выводится сообщение, изменения не вносятся.
- `--update-map` — принудительно обновить карту (хосты, провайдеры, линки). С `--host` — только указанный хост и его линки; остальные элементы карты **не удаляются**. Без `--host`: с карты убираются хосты и облака провайдеров, которых нет в текущем `dry-ssh.json`, и любые элементы не типа «хост/картинка» (устаревшие линки пересобираются по данным). Чтобы оставить старые элементы, как раньше: **`--keep-obsolete-map-elements`**.
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

В обычном запуске имя провайдера берётся из области мониторинга NetBox: для интерфейса известен контур, а у контура известен Provider. Файл `description_to_name.json` — только запасной источник на случай, когда интерфейс не удалось связать с контуром, и его же использует Grafana. Отбор интерфейсов по тексту `Uplink:` в описании по умолчанию не применяется — он включается только ключом `--legacy-provider-filter`.

Если в поле `description` встречаются несколько формулировок для одного провайдера (например «Beeline», «Beeline 5», «Uplink: Beeline 5»), маппинг сводит их к одной подписи: ключ — точная строка `description` из данных, значение — подпись на карте. Файл **не генерируется автоматически**, его создают и правят вручную. Чтобы получить шаблон по всем `description` из `dry-ssh.json` (новые — как ключ, так и значение), выполните:

```bash
python zabbix_map.py --generate-description-map -f dry-ssh.json > description_to_name.json
```

Отредактируйте JSON: для одного провайдера задайте одно и то же значение (напр. `"Uplink: Beeline 5": "Beeline"`, `"Beeline 5": "Beeline"`, `"Beeline": "Beeline"`). Если файл уже существует, в вывод попадёт его содержимое плюс недостающие description.

**Переменные:** `ZABBIX_URL` (базовый URL, например `https://zabbix.example.com`; скрипт сам дописывает `/api_jsonrpc.php` при необходимости), `ZABBIX_TOKEN` (Bearer-токен, Zabbix 7), а также `NETBOX_URL` и `NETBOX_TOKEN` — по умолчанию провайдер для линка берётся из области мониторинга NetBox. Имя карты задаётся в `uplinks_config.MAP_NAME`. Иконки элементов (хосты, провайдеры) настраиваются в Zabbix, соответствующие ID можно задать в конфиге.

| Ключ | Описание |
|------|----------|
| `-f`, `--file` | JSON с ключом `devices` (по умолчанию `dry-ssh.json`) |
| `-m`, `--description-map` | Файл сопоставления description → имя ISP |
| `--generate-description-map` | Собрать все description из файла и вывести шаблон JSON (в stdout); объединить с существующим маппингом |
| `--zabbix` | Запросить Zabbix API (для карты или таблицы) |
| `--print-table` | Вывести таблицу в консоль (с `--zabbix` — с hostid и ключами items) |
| `--create-map` | Только создать пустую карту, если её нет |
| `--update-map` | Обновить карту; без `--host` — удалить с карты хосты/провайдеров не из JSON; с `--host` — только один хост |
| `--keep-obsolete-map-elements` | С `--update-map` не удалять с карты хосты/провайдеры, отсутствующие в JSON |
| `--legacy-provider-filter` | Старый отбор интерфейсов по тексту `Uplink:` в описании вместо области мониторинга NetBox |
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

Создание или обновление **дашборда** в Zabbix с виджетами-графиками: по каждому uplink-интерфейсу из `dry-ssh.json` (с учётом дедупликации по паре хост–провайдер, как в карте) добавляется один виджет — график входящего и исходящего трафика (Bits received / Bits sent). Данные по хостам и items берутся из Zabbix API; используется тот же кэш, что и в `zabbix_map.py`. При обновлении страница дашборда **целиком перезаписывается** — виджетов по хостам и интерфейсам, которых больше нет в `dry-ssh.json`, не остаётся.

**Вход:** `dry-ssh.json` и NetBox inventory. `description_to_name.json` используется
только как переходный fallback, если подключение не найдено в NetBox.
Переменные: `ZABBIX_URL`, `ZABBIX_TOKEN`.

| Ключ | Описание |
|------|----------|
| `-f`, `--file` | Путь к dry-ssh.json (по умолчанию `dry-ssh.json`) |
| `-m`, `--description-map` | Файл сопоставления description → имя ISP |
| `--dashboard-name` | Название дашборда в Zabbix (по умолчанию `Uplinks`) |
| `--dashboard-by-location` | Дашборд по локациям (вкладка = локация); пустая строка — не создавать |
| `--dashboard-by-provider` | Сводный дашборд по провайдерам с >1 линком (вкладка = Cogent, HE и др.): стеки по линкам и при наличии хостов «Uplinks {Provider}» — виджеты суммарного трафика (aggregate); пустая строка — не создавать |
| `--providers` | Список имён провайдеров для сводного дашборда (по умолчанию: `PROVIDERS_FOR_SUMMARY` + провайдеры из полных NetBox-подключений) |
| `--no-cache` | Не использовать кэш Zabbix |
| `--no-show-threshold` | Не рисовать пороги триггеров (линию порога) на графиках |
| `--legacy-provider-filter` | Старый отбор интерфейсов по тексту `Uplink:` в описании вместо области мониторинга NetBox |
| `--debug` | Отладочный вывод |

Если дашборд с таким именем уже есть — он обновляется (страница с виджетами перезаписывается). Если нет — создаётся новый.

**Линия порога на графике.** Включена опция Simple triggers: пороги простых триггеров рисуются пунктиром. Явный простой триггер **100%** (`max(Bits received, …) > {$UPLINK.BPS.MAX:…}`) создаётся только там, где в **`zabbix_sync_commit_rate.py`** включён **`--create-link-triggers`** и у контура в NetBox поле **`billing_model`** равно **`Burst`**; иначе линия порога на графике опирается на триггеры шаблонов / другие правила Zabbix. Период **max** задаётся в **`uplinks_config.py`** (`TRIGGER_FUNCTION_PERIOD`, по умолчанию 15m).

```bash
# Создать или обновить дашборд «Uplinks» с графиками по всем uplink из dry-ssh.json
python zabbix_uplinks_dashboard.py -f dry-ssh.json

# Другое имя дашборда
python zabbix_uplinks_dashboard.py -f dry-ssh.json --dashboard-name "Uplinks traffic"

# Сводный дашборд: по умолчанию провайдеры из конфига + из NetBox (тег uplinks); явно задать список:
python zabbix_uplinks_dashboard.py -f dry-ssh.json --providers Cogent Hurricane
```

---

### 6. `netbox_create_circuits.py` — переходный legacy-скрипт

Этот скрипт не входит в обычный рабочий запуск. В рабочем процессе провайдер,
контур, терминацию и кабель создаёт человек. Скрипт оставлен только для
переходных тестов и старого режима `run_uplinks_full.py --auto`.

По умолчанию скрипт работает в lookup-only режиме: ищет существующие
провайдер, тип контура, Circuit, Termination и Cable и не изменяет NetBox.
Флаг `--auto` включает старое создание и изменение объектов. Для виртуальных
интерфейсов (ae5.0 и т.п.) legacy-режим использует физический интерфейс из
`dry-ssh` (`physicalInterface`).

Внимание: `--auto` может удалить существующий кабель на интерфейсе. Не
запускайте этот режим на NetBox, где подключения заведены вручную.

**Тег автоматизации:** скрипт создаёт в NetBox тег с именем из `uplinks_config.NETBOX_AUTOMATION_TAG` (по умолчанию `automatization`), если его ещё нет, и проставляет его всем создаваемым объектам (провайдеры, типы контуров, контуры, кабели). Уже существующим объектам тег добавляется при повторном запуске (без перезаписи остальных тегов).

**Переменные:** `NETBOX_URL`, `NETBOX_TOKEN`, `NETBOX_TAG`.

| Ключ | Описание |
|------|----------|
| `-f`, `--commit-rates` | Путь к commit_rates.json (по умолчанию `commit_rates.json`) |
| `-d`, `--dry-ssh` | dry-ssh.json для маппинга логический → физический интерфейс (если файл есть в текущей директории, подхватывается по умолчанию) |
| `--location LOC` | Обработать только указанную локацию (первый сегмент hostname); по умолчанию — все площадки |
| `--dry-run` | Совместим с legacy-режимом; без `--auto` обычный запуск и так не вносит изменения в NetBox |
| `--auto` | Явно включить legacy-создание и изменение объектов NetBox |
| `--clear-null-commit` | Legacy-режим: если в JSON у контура `commit_rate_gbps: null`, снять commit rate в NetBox у circuit |

```bash
python netbox_create_circuits.py
python netbox_create_circuits.py -d dry-ssh.json
python netbox_create_circuits.py --auto -d dry-ssh.json
python netbox_create_circuits.py --auto --location ALA
python netbox_create_circuits.py --auto -f commit_rates.json --clear-null-commit
```

---

### 7. `zabbix_sync_commit_rate.py` — макросы и (по флагу) per-link триггеры для Burst

Для каждого интерфейса в NetBox, подключённого кабелем к circuit termination (сторона A), скрипт берёт **commit rate** контура (Kbps), переводит в bps и создаёт на хосте два макроса с контекстом по интерфейсу: **{$UPLINK.BPS.MAX:"…"}** (HIGH, по умолчанию 100% от commit) и **{$UPLINK.BPS.WARN:"…"}** (WARN, по умолчанию 90%). Значения макросов = commit_rate × (THRESHOLD_PERCENT_* / 100).

**Простые триггеры на линк** создаются **только** при **`--create-link-triggers`** и только для интерфейсов, у которых контур в NetBox имеет поле **`billing_model: Burst`** (без учёта регистра). Если кабель заведён на физический порт, входящий в логическое объединение, триггер создаётся на логическом интерфейсе, как он назван в Zabbix (например `ae5.0`). На каждый такой интерфейс создаются три объекта:

1. **90%** — `max(Bits received, TRIGGER_FUNCTION_PERIOD) > {$UPLINK.BPS.WARN:"iface"}`; теги: `scripts:automatization`, `provider`, `circuit`, `billing=burst` (**без** `sla=true`). Зависимость от триггера **100%** (как задано в Zabbix API).
2. **100%** — `max(…) > {$UPLINK.BPS.MAX:"iface"}`; те же теги, без `sla=true`.
3. **SLA breach** — `min(…) > {$UPLINK.BPS.MAX:"iface"}` с периодом **`SLA_TRIGGER_FUNCTION_PERIOD`** (например 1h); дополнительно **`sla=true`** (как у агрегатов) — к этому триггеру привязывается сервис **`Uplinks Burst {circuit_id}`** в **zabbix_provider_services.py**.

Описания концов строк задаются в **`uplinks_config.py`**: `TRIGGER_DESC_90_SUFFIX`, `TRIGGER_DESC_100_SUFFIX`, `TRIGGER_DESC_SLA_BREACH_SUFFIX`, `TRIGGER_DESC_UTIL_*_SUFFIX`.

**Утилизация порта (по умолчанию с `-d dry-ssh.json`).** Для **физических** uplink из **`dry-ssh.json`** (на Juniper — `et-*`, без `ae` / `aeN.0` / `isLag` / `isLogical`; на Arista — `Ethernet*`) на хосте Zabbix: макросы **{$UPLINK.UTIL.WARN:"iface"}** = 70, **{$UPLINK.UTIL.CRIT:"iface"}** = 85 (проценты; пороги в `uplinks_config.py`). Два триггера с тегом `scripts:automatization`, формула как у шаблона **Arista by SNMP** (`avg(net.if.in)` или `avg(net.if.out)` vs `(macro/100)*net.if.speed`, `speed>0`). Warning зависит от Critical. Если items `net.if.in/out/speed` на хосте нет — интерфейс пропускается.

**Важно:** обрабатываются только те физические интерфейсы, которые нашлись в
цепочке NetBox. Раньше сюда попадали все uplink из файла опроса, без
требования кабеля в NetBox. Триггеры перегрузки, оставшиеся от прежней схемы
на портах вне области мониторинга, при запуске удаляются — удаляются только
триггеры с тегом `scripts:automatization` или вовсе без тегов. Что именно
будет удалено, видно заранее в предварительном отчёте `--plan`, в разделе
`util_triggers` в списке `delete`.

Старые item'ы **net.if.threshold["..."]**, если остались, удаляются.

**Важно про совместимость с шаблонами Zabbix.**
Ранее использовались макросы `{$IF.UTIL.MAX/WARN}` со значениями в bps, что конфликтует со стандартными шаблонными триггерами, где `{$IF.UTIL.*}` ожидаются в процентах. Актуальная схема: скрипт трогает **{$UPLINK.BPS.*}** и **{$UPLINK.UTIL.*}**, а **{$IF.UTIL.*}** остаются под шаблоны (100% на всех LLD-портах).

Если ранее уже были записаны host-level `{$IF.UTIL.*}` в bps, миграция:
1. Удалить host-level `{$IF.UTIL.*}` на uplink-хостах (чтобы шаблон взял свои значения);
2. Прогнать `python zabbix_sync_commit_rate.py -d dry-ssh.json`;
3. Для Burst — `python zabbix_sync_commit_rate.py -d dry-ssh.json -f commit_rates.json --create-link-triggers`.

**Переменные:** `NETBOX_URL`, `NETBOX_TOKEN`, `NETBOX_TAG`, `ZABBIX_URL`, `ZABBIX_TOKEN`.

| Ключ | Описание |
|------|----------|
| `-f`, `--commit-rates` | Путь к `commit_rates.json`. В обычном запуске файл не читается; он нужен только вместе с `--legacy-commit-rates-fallback` |
| `--legacy-commit-rates-fallback` | Переходный режим: добрать из `commit_rates.json` те Burst-линки, которых нет в NetBox |
| `-d`, `--dry-ssh` | Путь к dry-ssh.json: для кабеля на физике макрос по логическому имени, как в Zabbix |
| `--dry-run` | Не менять макросы в Zabbix, только вывести что бы установили. Нельзя совмещать с ключами удаления триггеров |
| `--debug` | Отладочный вывод |
| `--create-link-triggers` | Создавать/обновлять триггеры 90%/100%/SLA breach только для Burst-линков |
| `--no-util-triggers` | Не создавать макросы/триггеры **{$UPLINK.UTIL.*}** (по умолчанию util включён при наличии dry-ssh) |
| `--delete-link-triggers` | Удалить триггеры commit 90%/100%/SLA breach |
| `--delete-util-triggers` | Удалить триггеры uplink utilization warn/crit |

**BPS-макросы:** только пары с **кабелем** circuit termination (A) → интерфейс, устройство с тегом из **`NETBOX_TAG`** (по умолчанию `border`). **Util:** физические интерфейсы из **`dry-ssh.json`**, ограниченные той же цепочкой NetBox. Для логических имён (Juniper) укажите `-d dry-ssh.json` и для BPS (контекст макроса).

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

Правило действует не в каждом месте кода по отдельности, а в единственной
функции, через которую проходят все обращения к Zabbix (`zabbix_request` в
`uplinks/zabbix/client.py`). При неполных данных она не отправляет опасный
запрос и возвращает ошибку с пояснением. Список опасных вызовов задан в одном
месте — функция `is_destructive_call`; отдельные тесты опасных форм запросов
проверяют сам транспорт и не используют эту функцию как источник ожидаемого
результата, поэтому ошибка в защите не скрывается самой проверкой.

Схема (`zabbix_map.py`) при неполных данных **не обновляется совсем**: связи
на схеме записываются одним набором, и набор, собранный по части подключений,
удалил бы связи остальных. Лучше оставить схему прежней и завершить запуск с
ошибкой, чем показать неполную картину.

```bash
python zabbix_sync_commit_rate.py
python zabbix_sync_commit_rate.py -d dry-ssh.json --dry-run
python zabbix_sync_commit_rate.py -d dry-ssh.json --create-link-triggers
python zabbix_sync_commit_rate.py -d dry-ssh.json --delete-link-triggers
python zabbix_sync_commit_rate.py -d dry-ssh.json --delete-util-triggers
```

---

### 8. `zabbix_provider_aggregate.py` — агрегат по провайдеру в Zabbix

Для схем, где у провайдера общий лимит по всем линкам, лимит хранится в NetBox у Provider в поле **`aggregate_limit_gbps`** (Гбит/с). Скрипт берёт провайдеров из полных цепочек NetBox (тег контура **`NETBOX_MONITOR_TAG`**, по умолчанию `uplinks`) и линки из `dry-ssh.json`, затем создаёт хосты **«Uplinks {Provider}»** в группе **`UPLINKS_AGGREGATE_GROUP`**, с calculated items (сумма Bits in/out по линкам). Для провайдера с лимитом создаются **три** триггера:

- **90%** и **100%** от лимита — теги **`scripts:automatization`** и **`provider={name}`** (без **`sla=true`**).
- **SLA breach** — устойчивое превышение 100% за период **`SLA_TRIGGER_FUNCTION_PERIOD`**, дополнительный тег **`sla=true`** — под них подходит сервис **`Uplinks {Provider}`** и SLA в Zabbix.

Зависимость **90% → 100%** (шум на карте при одновременной сработке разных уровней снижается за счёт семантики зависимостей Zabbix). Если у провайдера в NetBox не заполнено поле **`aggregate_limit_gbps`**, агрегатные триггеры для него могут быть удалены (ключ **`--keep-triggers-without-limits`** это отключает). Если чтение NetBox прошло не полностью, удаление триггеров не выполняется и скрипт завершается с кодом ошибки.

Карта (**`zabbix_map.py`**) и сводный дашборд используют эти триггеры для цвета и виджетов.

**Переменные:** `ZABBIX_URL`, `ZABBIX_TOKEN`, `NETBOX_URL`, `NETBOX_TOKEN`.

| Ключ | Описание |
|------|----------|
| `-d`, `--dry-ssh` | Путь к dry-ssh.json (для списка линков и кэша Zabbix) |
| `-f`, `--commit-rates` | Путь к commit_rates.json. В обычном запуске файл не читается и его отсутствие не является ошибкой; он нужен только вместе с `--legacy-commit-rates-fallback` |
| `--legacy-commit-rates-fallback` | Переходный режим: брать общий лимит поставщика из `_provider_limits` в `commit_rates.json`, если в NetBox поле `aggregate_limit_gbps` не заполнено |
| `-m`, `--description-map` | Файл description_to_name.json (используется как запасной источник имени провайдера) |
| `--legacy-provider-filter` | Старый отбор интерфейсов по тексту `Uplink:` в описании вместо области мониторинга NetBox |
| `--no-cache` | Не использовать кэш Zabbix |
| `--debug` | Отладочный вывод |
| `--keep-triggers-without-limits` | Не удалять агрегатные триггеры у провайдера, у которого нет лимита |

```bash
python zabbix_provider_aggregate.py -d dry-ssh.json
```

---

### 9. `zabbix_provider_services.py` — сервисы и SLA в Zabbix

Создаёт или обновляет **листовые сервисы** и объекты **SLA** в Zabbix по
активным Circuit из NetBox:

- Для каждого Provider, найденного через активные Circuit: сервис **`Uplinks {Provider}`**, **`problem_tags`**: `provider={имя}` и `sla=true` (совпадает с агрегатным триггером **SLA breach**, если для Provider задан общий лимит).
- Для каждого активного Circuit с **`billing_model: Burst`**: сервис **`Uplinks Burst {circuit_id}`**, **`problem_tags`**: `circuit`, `sla=true`, `billing=burst` (совпадает с per-link триггером **SLA breach** после **`zabbix_sync_commit_rate.py --create-link-triggers`**).
  Для SLA этого сервиса фильтр задаётся по **`service_tags: circuit=<id>`** (без `role`) — это предотвращает широкий match в UI некоторых версий Zabbix.

Целевой процент **`SLO`** для создаваемых SLA сначала берётся из NetBox,
затем из **`PROJECT_PROVIDER_SLO_PERCENT`** в `uplinks_config.py`.
Переходное чтение **`_provider_sla`** из `commit_rates.json` доступно только
с флагом **`--legacy-commit-rates-fallback`**. Эффективная дата старта SLA —
**`SLA_EFFECTIVE_DATE_UTC`** в `uplinks_config.py`.

Опционально **`--parent-service NAME`** — общий родитель для всех создаваемых сервисов.

**Переменные:** `ZABBIX_URL`, `ZABBIX_TOKEN`, `NETBOX_URL`, `NETBOX_TOKEN`.

| Ключ | Описание |
|------|----------|
| `-f`, `--commit-rates` | Путь к commit_rates.json. В обычном запуске файл не читается; нужен только вместе с `--legacy-commit-rates-fallback` |
| `--parent-service` | Имя родительского сервиса (создаётся при отсутствии) |
| `--legacy-commit-rates-fallback` | Явно разрешить переходное чтение провайдеров, Burst и SLO из commit_rates.json |
| `--debug` | Отладочный вывод API |

**Порядок с Burst:** сначала **`python zabbix_sync_commit_rate.py -d dry-ssh.json --create-link-triggers`** (чтобы триггеры имели актуальные теги и описание SLA breach), затем **`python zabbix_provider_services.py --parent-service "Uplinks providers"`**.

```bash
python zabbix_provider_services.py
python zabbix_provider_services.py --parent-service "Uplinks"
```

---

### 10. `zabbix_provider_sla.py` — offline отчёт SLA по триггерам

Не использует встроенный модуль SLA Zabbix; считает долю времени в **PROBLEM** по **`event.get`** для выбранных триггеров на окне **`--days`** или **`--from-ts` / --to-ts**.

- **Блок Aggregate:** провайдеры из активных Circuit NetBox. Для каждого: триггер **SLA breach**, если есть в Zabbix, иначе триггер **мгновенного 100%** (90% в расчёт не входит).
- **Блок Burst:** по одному ряду на активный Circuit с **`billing_model: Burst`**; те же правила выбора триггера (SLA breach или 100%). Если хост в Zabbix не найден — в таблице `n/a`.

Колонка **BelowSLA** — сравнение с SLO (целевым уровнем доступности) из NetBox или
`PROJECT_PROVIDER_SLO_PERCENT`. Старый `_provider_sla` из JSON используется только
с флагом `--legacy-commit-rates-fallback`.

**Переменные:** `ZABBIX_URL`, `ZABBIX_TOKEN`, `NETBOX_URL`, `NETBOX_TOKEN`.

| Ключ | Описание |
|------|----------|
| `-f`, `--commit-rates` | Путь к commit_rates.json для legacy-режима |
| `--legacy-commit-rates-fallback` | Явно включить переходное чтение JSON |
| `--dry-ssh` | Путь к dry-ssh.json: перевод физического имени интерфейса в логическое, как оно названо в Zabbix |
| `--days` | Глубина окна в днях (по умолчанию 30) |
| `--from-ts`, `--to-ts` | Границы окна (Unix time) |
| `--debug` | Отладочный вывод |

```bash
python zabbix_provider_sla.py
python zabbix_provider_sla.py --days 7
python zabbix_provider_sla.py -f commit_rates.json --legacy-commit-rates-fallback
```

---

### 11. `zabbix_uplinks_cleanup.py` — очистка артефактов Zabbix

Удаляет в Zabbix объекты, созданные скриптами uplinks: простые триггеры **90%/100%/SLA breach** на интерфейсах (тег `scripts:automatization` / по описанию `Interface …`), старые item'ы **net.if.threshold["..."]**, карту с именем из конфига, дашборды **Uplinks** и связанные (имена задаются ключами). Макросы {$UPLINK.BPS.MAX}, {$UPLINK.BPS.WARN} и шаблонные {$IF.UTIL.*} не удаляются.

**Переменные:** `ZABBIX_URL`, `ZABBIX_TOKEN`.

| Ключ | Описание |
|------|----------|
| `--dashboard-name NAME` | Имя основного дашборда (по умолчанию `Uplinks`) |
| `--dashboard-by-location NAME` | Имя дашборда по локациям (по умолчанию `Uplinks (по локациям)`); пустая строка — не удалять |
| `--dashboard-by-provider NAME` | Имя сводного дашборда по провайдерам (по умолчанию `Uplinks по провайдерам`); пустая строка — не удалять |
| `--dry-run` | Показать, что будет удалено, без изменений в Zabbix |
| `--debug` | Отладочный вывод запросов к API |

```bash
python zabbix_uplinks_cleanup.py --dry-run
python zabbix_uplinks_cleanup.py
```

---

### 12. `netbox_uplinks_cleanup.py` — откат автоматизации в NetBox

Удаляет в NetBox объекты, созданные **netbox_create_circuits.py** и помеченные тегом из **`uplinks_config.NETBOX_AUTOMATION_TAG`** (по умолчанию `automatization`): кабели (cables), circuit terminations (сторона A), контуры (circuits). Типы контуров и провайдеры с этим тегом удаляются только если у них не осталось контуров (после удаления наших контуров). Интерфейсы и устройства не трогает; сам тег не удаляется.

**Порядок удаления:** кабели → terminations → circuits → circuit types → providers.

**Переменные:** `NETBOX_URL`, `NETBOX_TOKEN`.

| Ключ | Описание |
|------|----------|
| `--dry-run` | Показать, что будет удалено, без изменений в NetBox |
| `--debug` | Отладочный вывод |

```bash
python netbox_uplinks_cleanup.py --dry-run
python netbox_uplinks_cleanup.py
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
| `zabbix_sync_commit_rate.py` | Макросы {$UPLINK.BPS.MAX}/{$UPLINK.BPS.WARN}; опционально per-link триггеры **90%/100%/SLA breach** для `billing_model: Burst`; удаление старых item'ов порога |
| `zabbix_provider_aggregate.py` | Хосты «Uplinks {Provider}»: calculated items, триггеры 90%/100%/SLA breach по полю NetBox `aggregate_limit_gbps` |
| `zabbix_provider_services.py` | Сервисы и SLA в Zabbix: активные Circuit и Burst-контуры, цель из NetBox или `PROJECT_PROVIDER_SLO_PERCENT` |
| `zabbix_provider_sla.py` | Offline-отчёт SLA по активным Circuit и их триггерам (агрегаты и Burst) |
| `zabbix_uplinks_cleanup.py` | Очистка: простые триггеры uplinks (включая SLA breach на линках), item'ы порога, карта, дашборды |
| `netbox_uplinks_cleanup.py` | Откат в NetBox: кабели, terminations, контуры (и при возможности типы/провайдеры) по тегу автоматизации |
| `uplinks_config.py` | Имя карты/дашбордов, теги триггеров (`scripts`/`automatization`, `sla`/`true`), описания **90%/100%** и **SLA breach** на линке (`TRIGGER_DESC_*`, `TRIGGER_DESC_SLA_BREACH_SUFFIX`), периоды **`TRIGGER_FUNCTION_PERIOD`** (окно max) и **`SLA_TRIGGER_FUNCTION_PERIOD`** (окно min для breach), **`SLA_EFFECTIVE_DATE_UTC`**, макросы, цвета линков, префиксы агрегатных хостов, тег контура для области мониторинга **`NETBOX_MONITOR_TAG`**, тег старых объектов **`NETBOX_AUTOMATION_TAG`** и общий целевой уровень доступности **`PROJECT_PROVIDER_SLO_PERCENT`**. Копия примера: `uplinks_config.example.py`. |
| `zabbix_uplinks_cache.json` | Кэш данных Zabbix (хосты, items); создаётся при `--zabbix` / дашборде в той же директории, что и файл `-f`, не коммитить |
| `ROADMAP.md` | Планы доработок (например Tenancy для circuits) |
| `requirements.txt` | Зависимости: pynetbox, paramiko, requests |

---

**Формат `commit_rates.json`**  
Ключ — имя устройства, значение — объект «имя интерфейса → { `provider`, `circuit_id`, `commit_rate_gbps`, ... }». `circuit_id` — уникальный идентификатор контура (Unique circuit ID в NetBox). `commit_rate_gbps` — оплаченная скорость в **Гбит/с** (в NetBox Circuit Commit rate хранится в Kbps: умножить на 1 000 000). Провайдер — короткое имя.  
Дополнительные поля линка (например **`billing_model`**: `Flat` / **`Burst`** / `95thAggBurst`) допускаются и сохраняются при merge. Для **`Burst`** в Zabbix по флагу создаются per-link триггеры и сервис **`Uplinks Burst {circuit_id}`**; у линка должны быть заполнены **`provider`**, **`circuit_id`** и по возможности **`commit_rate_gbps`**. Ключи с префиксом `_` на корне не считаются устройствами; служебные ключи:

- **`_provider_limits`** — старый агрегатный лимит по провайдеру (Гбит/с). В
  обычном запуске не читается: лимит берётся из поля NetBox
  `aggregate_limit_gbps` у Provider. Доступен только с ключом
  `--legacy-commit-rates-fallback`.
- **`_provider_sla`** — старое общее значение доступности для legacy-запусков
  с `--legacy-commit-rates-fallback`. В обычном запуске используется
  `PROJECT_PROVIDER_SLO_PERCENT` или значение SLO из NetBox.

Другие **`_…`** ключи (`_billing_models` и т.п.) при merge сохраняются. Файл обычно не коммитится; пример — **`commit_rates.json.example`**.

### Генерация `commit_rates.json` по dry-ssh

Скрипт **`generate_commit_rates.py`** по всем линкам из **`dry-ssh.json`** собирает записи в **`commit_rates.json`**.

- **Провайдер** — из `description_to_name.json` (по `description` интерфейса).
- **`circuit_id`** — формат `провайдер-локация-N`.
- **`commit_rate_gbps`** — для новых пар `null` (заполнить вручную, в Гбит/с).
- **Merge** — сохраняет поля линков (`billing_model` и др.) и служебные ключи `_…`; старый `commit_rate_kbps` конвертируется в `commit_rate_gbps` (значения &lt; 1000 считаются уже в Гбит/с).

```bash
cp commit_rates.json.example commit_rates.json
python generate_commit_rates.py -f dry-ssh.json -o commit_rates.json
```

### Проверка circuits в NetBox

Скрипт **`netbox_create_circuits.py`** по умолчанию **только читает** NetBox. По
записям из **`commit_rates.json`** он ищет провайдера, тип контура, контур,
Termination A и кабель до интерфейса и печатает, чего не хватает. Ничего не
создаёт, не изменяет и не удаляет.

- **Все площадки** — по умолчанию; одна площадка: `--location ALA`.
- **Виртуальные интерфейсы** (ae5.0 и т.п.) — кабель ожидается на **физическом**
  интерфейсе; при `-d dry-ssh.json` берётся `physicalInterface` для логического.
- Кабель, идущий через оптический распределительный шкаф (через проходные порты
  NetBox), тоже распознаётся.

Старое создание объектов включается только ключом **`--auto`**. В этом режиме
скрипт создаёт провайдеров, тип контура «Internet», контуры, Termination A и
кабель, ставит объектам тег **`NETBOX_AUTOMATION_TAG`** (по умолчанию
`automatization`), и **может удалить существующий кабель** и сбросить
`mark_connected`, чтобы подключить свой. На рабочем NetBox, где подключения
заводит человек, `--auto` запускать не следует.

**Переменные:** `NETBOX_URL`, `NETBOX_TOKEN`, `NETBOX_TAG`.

```bash
# только проверка, без записи
python netbox_create_circuits.py -f commit_rates.json -d dry-ssh.json

# старый режим с созданием и заменой объектов
python netbox_create_circuits.py --auto -f commit_rates.json -d dry-ssh.json
```

См. также **### 6. `netbox_create_circuits.py`** в разделе «Скрипты и ключи».
