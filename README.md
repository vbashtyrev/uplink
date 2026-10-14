# Uplinks

Проект определяет по NetBox набор подключений, которые нужно контролировать,
и настраивает для них мониторинг в Zabbix. Под набором подключений здесь
понимаются активные каналы типа `Uplink`, для которых в NetBox прослеживается
полная цепочка от канала до интерфейса устройства.

Проект использует NetBox только для чтения. Данные о каналах, кабелях,
интерфейсах и устройствах изменяются человеком в NetBox.
Проверочные и рабочие отчёты описаны в
[`reports/README.md`](reports/README.md). Виртуальное окружение создаётся в
`.venv`.

---

## Инструменты

| Скрипт | Описание |
|--------|----------|
| `run_uplinks_full.py` | Полный запуск: читает набор подключений из NetBox, затем создаёт или обновляет в Zabbix макросы, триггеры, агрегаты, карту связей, дашборды и сервисы. `--plan` только показывает предстоящие изменения. |
| `netbox_uplinks_inventory.py` | Читает NetBox и строит снимок подключений: поставщик → канал → терминация стороны A → кабель → интерфейс устройства с тегом `border`. В снимок входят активные каналы типа `Uplink` с полной цепочкой. |
| `zabbix_uplinks_plan.py` | Читает снимок подключений и текущее состояние Zabbix, затем показывает, что обычный запуск создаст, обновит или удалит. Сам ничего не меняет. |
| `zabbix_map.py` | Читает снимок подключений, хосты и показатели трафика в Zabbix. `--print-table` показывает найденные связи, `--create-map` создаёт пустую карту, а `--update-map` создаёт или обновляет карту связей в Zabbix. |
| `zabbix_uplinks_dashboard.py` | Создаёт или обновляет в Zabbix дашборд — страницу с графиками входящего и исходящего трафика по подключениям. |
| `zabbix_sync_commit_rate.py` | Читает гарантированную скорость канала из NetBox и создаёт или обновляет в Zabbix макросы с порогами. Для схемы расчёта `Burst` флаг `--create-link-triggers` также создаёт триггеры превышения 90%, 100% и длительного превышения. |
| `zabbix_provider_aggregate.py` | Создаёт или обновляет в Zabbix отдельный технический хост `Uplinks {Provider}` для каждого поставщика. Его вычисляемые показатели складывают входящий и исходящий трафик всех каналов поставщика. На них создаются пороги 90%, 100% и длительного превышения общего лимита из NetBox. |
| `zabbix_provider_services.py` | Создаёт или обновляет логические сервисы Zabbix для контроля доступности: один сервис поставщика и отдельный сервис для каждого канала со схемой расчёта `Burst`. |
| `zabbix_provider_sla.py` | Читает историю событий Zabbix и строит отчёт о доступности поставщиков и каналов. Целевой уровень доступности берётся из NetBox или настройки проекта. |
| `zabbix_uplinks_cleanup.py` | Отдельная ручная массовая очистка объектов Zabbix, созданных проектом. Удаляет только совпавшие триггеры и старые элементы `net.if.threshold`, карту и дашборды с заданными именами. В полный запуск не входит; сначала выполняется в режиме `--dry-run`. |

---

## Рабочий путь

NetBox является источником данных о подключениях:

```text
Provider → Circuit → Termination A → Cable → Interface → Device
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

Чтобы вывести из мониторинга один канал, измените поле `status` именно у
объекта `Circuit` в NetBox. Поставщик, кабель и интерфейс при этом не меняются.
После изменения сначала запустите `--plan`. Ручную очистку Zabbix выполняют
отдельной командой `zabbix_uplinks_cleanup.py --dry-run`, а затем — после
проверки отчёта — без `--dry-run`.

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

Проверьте готовую цепочку без записи:

```bash
python netbox_uplinks_inventory.py --json --dry-run
```

Канал должен попасть в `complete`, а не в `incomplete`.

### Как отключить один Circuit (канал)

1. В NetBox измените поле `status` выбранного объекта `Circuit` на `Offline`
   или `Decommissioned`. Сам объект не удаляйте; Provider, Cable и Interface
   не изменяйте.
2. Сначала выполните `netbox_uplinks_inventory.py --json --dry-run`, затем
   `run_uplinks_full.py --plan`.
3. Проверьте отчёт Zabbix и только после этого выполняйте рабочий запуск.
   Объекты NetBox не удаляются. `run_uplinks_full.py`
   пересобирает рабочие объекты Zabbix по оставшимся каналам. Скрипт
   `zabbix_uplinks_cleanup.py` не удаляет объекты одного Circuit. Это массовая
   очистка в Zabbix: она удаляет все найденные старые элементы
   `net.if.threshold`, подходящие триггеры, карту с именем из конфигурации и
   дашборды с указанными именами. Хосты, обычные показатели трафика, макросы и
   объекты NetBox не затрагиваются. Поэтому запускайте её только для полной
   ручной очистки и сначала используйте `zabbix_uplinks_cleanup.py --dry-run`.
   Если отключается последний полный Circuit, рабочий запуск остановится без
   записи; отдельную очистку выполняйте только после проверки её отчёта.

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

### `zabbix_map.py`

Построение карты связей в Zabbix по `--inventory-file` из NetBox. При обращении к
Zabbix API выполняется поиск хостов и items (Bits received/sent). Для одной
пары «хост, провайдер» рисуется один линк — при равных условиях выбирается
логический интерфейс (например `ae5.0`), иначе физический.

Раскладка проверяет границы элементов и подписей. При пересечении обновление
карты блокируется; провайдеры получают подпись сверху, а устройства — снизу.

**Окраска линка** задаётся списком **link triggers** с приоритетом (что указано раньше — сильнее при конфликте цветов): (1) per-link **100%** — красный **жирный** (`drawtype` bold); (2) агрегат провайдера **100%**; (3) per-link **90%** — жёлтый; (4) агрегат **90%**. Нужны соответствующие триггеры в Zabbix (**zabbix_sync_commit_rate.py** с **`--create-link-triggers`** для Burst; **zabbix_provider_aggregate.py** для лимитов из поля NetBox `aggregate_limit_gbps`).

**Карта связей в Zabbix**

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

Тег `scripts:automatization` ставится на создаваемые триггеры Zabbix. Он нужен
для безопасной очистки этих триггеров и не используется для выбора подключений
из NetBox.

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
Физические и логические интерфейсы сопоставляются по связям NetBox
`netbox_interface_relations`. Если items `net.if.in/out/speed` на хосте нет,
интерфейс пропускается.

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
| `--dry-run` | Не менять макросы в Zabbix, только вывести что бы установили. Нельзя совмещать с ключами удаления триггеров |
| `--debug` | Отладочный вывод |
| `--create-link-triggers` | Создавать/обновлять триггеры 90%/100%/SLA breach только для Burst-линков |
| `--no-util-triggers` | Не создавать макросы/триггеры **{$UPLINK.UTIL.*}** (по умолчанию включены для физических uplink из inventory) |
| `--delete-link-triggers` | Удалить триггеры commit 90%/100%/SLA breach |
| `--delete-util-triggers` | Удалить триггеры uplink utilization warn/crit |

**BPS-макросы:** только пары с **кабелем** circuit termination (A) → интерфейс,
устройство с тегом из **`NETBOX_TAG`** (по умолчанию `border`). **Util:**
физические интерфейсы и их логические связи берутся из той же цепочки NetBox.

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

Карта связей в Zabbix (`zabbix_map.py`) при неполных данных **не обновляется совсем**: связи
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
**«Uplinks {Provider}»** в группе **`UPLINKS_AGGREGATE_GROUP`**. На этом
техническом хосте два вычисляемых показателя складывают входящий и исходящий
трафик всех каналов поставщика. Для провайдера с лимитом создаются **три**
условия срабатывания:

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

Создаёт или обновляет в Zabbix логические сервисы и объекты контроля
доступности по активным Circuit из NetBox. Сервис — это объект Zabbix,
к которому привязываются события и по которому считается доступность:

- Для каждого Provider, найденного через активные Circuit: сервис **`Uplinks {Provider}`**. В его `problem_tags` указаны `provider={имя}` и `sla=true`, поэтому к сервису привязываются события агрегатного условия **SLA breach**, если для Provider задан общий лимит.
- Для каждого активного Circuit с **`billing_model: Burst`**: сервис **`Uplinks Burst {circuit_id}`**. Его `problem_tags` (`circuit`, `sla=true`, `billing=burst`) связывают сервис с условием длительного превышения именно этого канала.
  Для расчёта доступности этого сервиса используется **`service_tags: circuit=<id>`** (без `role`) — это не даёт привязать события другого канала.

Целевой уровень доступности (**`SLO`**) сначала берётся из NetBox,
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

Удаляет в Zabbix все найденные объекты, совпадающие с признаками автоматизации:
простые триггеры **90%/100%/SLA breach** на интерфейсах (тег
`scripts:automatization` или описание `Interface …`), старые элементы
`net.if.threshold["..."]`, карту с именем из конфигурации и дашборды с именами
из ключей запуска. Это массовая очистка, а не удаление объектов одного
Circuit. Хосты, обычные показатели трафика, макросы
`{$UPLINK.BPS.MAX}`, `{$UPLINK.BPS.WARN}`, шаблонные `{$IF.UTIL.*}` и NetBox
не затрагиваются.

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
| `uplinks_config.py` | Имя карты/дашбордов, теги триггеров (`scripts`/`automatization`, `sla`/`true`), описания **90%/100%** и **SLA breach** на линке (`TRIGGER_DESC_*`, `TRIGGER_DESC_SLA_BREACH_SUFFIX`), периоды **`TRIGGER_FUNCTION_PERIOD`** и **`SLA_TRIGGER_FUNCTION_PERIOD`**, макросы, цвета линков, префиксы агрегатных хостов и общий целевой уровень доступности **`PROJECT_PROVIDER_SLO_PERCENT`**. Копия примера: `uplinks_config.example.py`. |
| `zabbix_uplinks_cache.json` | Кэш данных Zabbix (хосты, items); создаётся рядом с переданным inventory-файлом, не коммитить |
| `requirements.txt` | Зависимости: pynetbox и requests |
