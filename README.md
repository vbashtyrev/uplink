# Uplinks (NetBox + SSH)

**Версия 1.0**

Скрипты для сверки и обновления данных интерфейсов в NetBox по данным с устройств (SSH / JSON-файл). Поддерживаются Arista и Juniper.

Виртуальное окружение создаётся в `.venv`.

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

Опционально: `SSH_USERNAME` (по умолчанию `admin`), `PARALLEL_DEVICES` (по умолчанию `6`), `SSH_HOST_SUFFIX`, `NETBOX_TAG`.

Без активации venv можно вызывать интерпретатор напрямую:

```bash
.venv/bin/python uplinks_stats.py
```

---

## Скрипты и ключи

### 1. `uplinks_stats.py`

Единый скрипт с двумя режимами.

**Режим отчёта (`--report`):** быстрый отчёт для визуальной сверки «что в NetBox» и «что на устройстве» по uplink-интерфейсам (с описанием, содержащим `Uplink:`). Поддерживаются Juniper и Arista (по `platform.name` в NetBox). Только таблица, без сохранения JSON.

**Режим статистики:** по умолчанию данные читаются из файла `dry-ssh.json` (таблица или `--json`). С флагом `--fetch` — опрос устройств по SSH (NetBox по тегу → только Arista → сбор по каждому uplink'у), результат — таблица или JSON.

**Устройства и интерфейсы в режиме статистики (при `--fetch`):** устройства берутся из NetBox по тегу (переменная `NETBOX_TAG`), учитываются только с платформой Arista EOS. Интерфейсы — только те, у которых в описании (description) есть строка `Uplink:`.

**Что собирается по каждому такому интерфейсу (при `--fetch`):** для каждого интерфейса по SSH выполняются команды `show interfaces <name> | json | no-more` и `show interfaces <name> transceiver | json | no-more`. Из вывода извлекаются поля:

| Поле | Источник | Описание |
|------|----------|----------|
| `name` | show interfaces | Имя интерфейса |
| `description` | show interfaces | Описание интерфейса |
| `bandwidth` | show interfaces | Пропускная способность (bps) |
| `duplex` | show interfaces | Режим дуплекса (full/half и т.д.) |
| `physicalAddress` | show interfaces | MAC-адрес |
| `mtu` | show interfaces | MTU |
| `forwardingModel` | show interfaces | Режим работы порта: `routed` или `bridged` |
| `mediaType` | show interfaces transceiver | Тип модуля/трансивера (например 10GBASE-SR) |
| `txPower` | show interfaces transceiver | Мощность передачи (dBm) |
| `switchportConfiguration` | show interfaces … switchport configuration source | Только при `forwardingModel=bridged`: объект `{ "config": ["switchport …", …], "source": "cli" }` |

Итоговая структура: в JSON ключ `devices`, значение — объект «имя устройства → список таких словарей по каждому uplink-интерфейсу». Этот JSON используется как вход для `netbox_checks.py`.

| Ключ | Описание |
|------|----------|
| `--report` | Режим отчёта: таблица NetBox vs SSH (Juniper + Arista) |
| `--fetch` | Режим статистики: опросить устройства по SSH (иначе по умолчанию читается файл `dry-ssh.json`) |
| `--json` | Вывод в формате JSON (режим статистики) |
| `--from-file FILE` | Путь к JSON с ключом `devices` (по умолчанию `dry-ssh.json`) |

**Переменные:** `NETBOX_URL`, `NETBOX_TOKEN`, `SSH_USERNAME`, `SSH_PASSWORD`, `SSH_HOST_SUFFIX`, `PARALLEL_DEVICES`, `NETBOX_TAG`. Опционально: `DEBUG_SSH_JSON=1` (режим отчёта).

```bash
python uplinks_stats.py
python uplinks_stats.py --json
python uplinks_stats.py --from-file other.json
python uplinks_stats.py --fetch
python uplinks_stats.py --fetch --json
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
| `--duplex` | Сверка duplex (файл vs NetBox) |
| `--mac` | Сверка physicalAddress (файл) и mac_address (NetBox) |
| `--mtu` | Сверка mtu (файл vs NetBox) |
| `--tx-power` | Сверка txPower (файл) и tx_power (NetBox) |
| `--forwarding-model` | Сверка forwardingModel (файл) и mode (NetBox). В NetBox: `routed` → mode=null, `bridged` → mode=tagged |
| `--all` | Включить все проверки сразу (intname, description, mediatype, bandwidth, duplex, mac, mtu, tx-power, forwarding-model) |

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

В NetBox MAC — отдельная сущность (dcim.mac-addresses); при `--mac --apply` создаётся или находится запись, привязывается к интерфейсу и на интерфейсе устанавливается `primary_mac_address`. Примечание 16: «в Netbox заполнено не оба поля» — если есть только сущность или только отображение на интерфейсе.

**Примеры:**

```bash
# Сверка только типов интерфейсов со справочником
python netbox_checks.py -f dry-ssh.json --mediatype --mt-ref

# Один хост, все проверки, колонки «что подставим»
python netbox_checks.py -f dry-ssh.json --host DEVICE-NAME --all --show-change

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

Построение таблицы uplink'ов и опционально карты Zabbix по данным из `dry-ssh.json`. С Zabbix API: поиск хостов и items (Bits received/sent). **Карта Zabbix:** `--create-map` — только создать пустую карту [test] uplinks, если её нет; `--update-map` — обновить карту (хосты, провайдеры, линки); с `--host` обновляются только этот хост и его линки.

**Раскладка карты:** провайдеры сортируются по убыванию числа подключений; блоки слева направо, при нехватке места — перенос на следующую строку. В блоке: провайдер сверху, хосты в две колонки (не ближе 160 px от провайдера по горизонтали, между хостами 180 px, по вертикали шаг 100 px). Провайдер с одним подключением ставится рядом с этим хостом (в том же блоке), без отдельного ряда. Граница карты 30 px; если контент не влезает, размеры карты автоматически увеличиваются. Элементы дедуплицируются: один узел на хост и один на провайдера.

**Подписи линков:** имя интерфейса и строки In/Out с макросами Zabbix `{?last(/host/key)}` (скорость по items Bits received/sent).

**Переменные:** `ZABBIX_URL`, `ZABBIX_TOKEN`.

| Ключ | Описание |
|------|----------|
| `-f`, `--file` | JSON с ключом `devices` (по умолчанию `dry-ssh.json`) |
| `-m`, `--description-map` | Файл сопоставления description → имя ISP |
| `--zabbix` | Запросить Zabbix API, вывести ключи items (Bits received/sent) |
| `--create-map` | Только создать карту [test] uplinks, если её нет (пустая) |
| `--update-map` | Обновить карту: хосты, провайдеры, линки; с `--host` — только указанный хост и его линки |
| `--host HOSTNAME` | Работать только с одним хостом |
| `--debug` | Отладочный вывод и тело запросов map.create/update |
| `--no-cache` | Не использовать кэш Zabbix |
| `--export-map SYSMAPID` | Вывести JSON карты из API (для сравнения с ручной картой) |

```bash
# Только таблица из файла
python zabbix_map.py

# Таблица с hostid и ключами items из Zabbix
python zabbix_map.py --zabbix

# Один хост
python zabbix_map.py --zabbix --host MIA-EQX-7280QR-1

# Создать пустую карту (один раз)
python zabbix_map.py --create-map

# Обновить карту по всем хостам из dry-ssh.json (хосты, провайдеры, линки)
python zabbix_map.py --zabbix --update-map --debug

# Обновить только один хост и его линки
python zabbix_map.py --zabbix --update-map --host MIA-EQX-7280QR-1 --debug

# Выгрузить карту из API (например, созданную вручную) для сравнения формата
python zabbix_map.py --export-map 10 > map_10.json
```

---

## Файлы

| Файл | Описание |
|------|----------|
| `dry-ssh.json` | Пример/результат: JSON с ключом `devices` (имя устройства → список интерфейсов с полями из SSH) |
| `netbox_interface_types.json` | Справочник типов интерфейсов NetBox (value, label); используется `--mt-ref` в `netbox_checks.py` |
| `description_to_name.example.json` | Пример сопоставления description → имя ISP; скопировать в `description_to_name.json` и заполнить |
| `description_to_name.json` | Локальный файл сопоставления (не в git); по умолчанию для `zabbix_map.py -m` |
| `zabbix_uplinks_cache.json` | Кэш данных Zabbix (хосты, items); создаётся при `--zabbix`, не коммитить |
| `requirements.txt` | Зависимости: pynetbox, paramiko, requests |
