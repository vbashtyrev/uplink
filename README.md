# Uplinks (NetBox + SSH)

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

Опционально: `SSH_USERNAME` (по умолчанию `admin`), `SSH_HOST_SUFFIX` (по умолчанию `.3hc.io`), `PARALLEL_DEVICES` (по умолчанию `6`), `NETBOX_TAG` (по умолчанию `border`).

Без активации venv можно вызывать интерпретатор напрямую:

```bash
.venv/bin/python uplinks_report.py
```

---

## Скрипты и ключи

### 1. `uplinks_report.py`

**Назначение:** быстрый отчёт для визуальной сверки «что в NetBox» и «что на устройстве» по uplink-интерфейсам (с описанием, содержащим `Uplink:`). Только таблица, без сохранения JSON.

**Соотношение с другими скриптами:** по сути дублирует `arista_uplinks_stats.py` для Arista (устройства по тегу, SSH, сравнение с NetBox). Таблицу только по description можно получить и так: `arista_uplinks_stats.py --json > dry-ssh.json`, затем `netbox_checks.py -f dry-ssh.json --description` — колонки descF/descN/nD. Отличия `uplinks_report`: поддерживает **Juniper** (по `platform.name` в NetBox) и даёт один запуск без предварительного сбора JSON; при этом только uplink-интерфейсы (с `Uplink:` в description). Для парка только на Arista достаточно связки `arista_uplinks_stats` + `netbox_checks.py --description`; `uplinks_report` имеет смысл при смешанном Juniper/Arista или когда нужна одна команда без сохранения JSON.

**Аргументы:** нет (всё через переменные окружения).

**Переменные:** `NETBOX_URL`, `NETBOX_TOKEN`, `SSH_USERNAME`, `SSH_PASSWORD`, `SSH_HOST_SUFFIX`, `PARALLEL_DEVICES`. Опционально: `DEBUG_SSH_JSON=1` — выводить в консоль JSON с SSH.

```bash
python uplinks_report.py
```

---

### 2. `arista_uplinks_stats.py`

Сбор uplink-статистики Arista: устройства из NetBox по тегу, опрос по SSH, результат — таблица или JSON. Либо чтение уже готового JSON без SSH (`--from-file`).

**Что собирается с устройств:** берутся только интерфейсы, у которых в description есть строка `Uplink:`. Для каждого такого интерфейса по SSH выполняются команды `show interfaces <name> | json | no-more` и `show interfaces <name> transceiver | json | no-more`. Из вывода извлекаются поля:

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
| `--json` | Вывод в формате JSON (по умолчанию — таблица) |
| `--from-file FILE` | Не опрашивать SSH; взять данные из JSON-файла и вывести таблицу/JSON |

**Переменные:** `NETBOX_URL`, `NETBOX_TOKEN`, `NETBOX_TAG`, `SSH_USERNAME`, `SSH_PASSWORD`, `SSH_HOST_SUFFIX`.

```bash
python arista_uplinks_stats.py
python arista_uplinks_stats.py --json
python arista_uplinks_stats.py --from-file dry-ssh.json
```

---

### 3. `netbox_checks.py`

Сверка данных из JSON-файла с NetBox и при необходимости обновление интерфейсов в NetBox. **Подключения по SSH к устройствам нет** — скрипт всегда читает данные только из файла. Если `-f` не указан, используется файл по умолчанию `dry-ssh.json` (его нужно заранее получить, например через `arista_uplinks_stats.py --json > dry-ssh.json`). Устройства в NetBox выбираются по тегу.

**Входной файл:** по умолчанию `dry-ssh.json` (структура с ключом `devices`: имя устройства → список интерфейсов с полями `name`, `description`, `mediaType`, `bandwidth`, `duplex`, `physicalAddress`, `mtu`, `txPower` и т.д.).

**Переменные:** `NETBOX_URL`, `NETBOX_TOKEN`, `NETBOX_TAG` (по умолчанию `border`).

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

#### Вывод и колонки

| Ключ | Описание |
|------|----------|
| `--show-change` | Показать колонки «что подставим в NetBox» по выбранным ключам: mtToSet, descToSet, speedToSet, dupToSet, mtuToSet, txpToSet, fwdToSet |
| `--hide-empty-note-cols` | Не выводить колонки примечаний (nD, nM, …), если во всех строках они пустые |
| `--hide-no-diff-cols` | Не выводить группы колонок (файл/Netbox/примечание), в которых ни в одной строке нет расхождения |
| `--json` | Вывод в JSON (по умолчанию — таблица) |

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

Сверка `--mac` (physicalAddress / mac_address) в NetBox при `--apply` не обновляется.

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
```

---

### 4. `netbox_interface_types.py`

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

1. Собрать данные с устройств в JSON:
   ```bash
   python arista_uplinks_stats.py --json > dry-ssh.json
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

## Файлы

| Файл | Описание |
|------|----------|
| `dry-ssh.json` | Пример/результат: JSON с ключом `devices` (имя устройства → список интерфейсов с полями из SSH) |
| `netbox_interface_types.json` | Справочник типов интерфейсов NetBox (value, label); используется `--mt-ref` в `netbox_checks.py` |
| `requirements.txt` | Зависимости: pynetbox, paramiko |
