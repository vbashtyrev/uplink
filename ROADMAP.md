# Roadmap

Планы доработок проекта uplinks (не обязательно в указанном порядке).

## Ближайшее

- [x] CI: GitHub Actions + pytest на push/PR
- [x] Единый env-loader (`env_urls.load_env_file` с `overwrite=`)
- [x] Исправить/актуализировать runbook (`MANUAL.md`) — порядок Zabbix-шагов

## NetBox

- [ ] **Tenancy для circuits** — привязка контуров к tenant/site group там, где это требуется политикой NetBox
- [ ] Пакетная структура: вынести `netbox_checks`, `netbox_create_circuits` в модуль `uplinks/netbox/`

## Zabbix

- [ ] Вынести JSON-RPC клиент и кэш из `zabbix_map.py` в `uplinks/zabbix/client.py`
- [ ] Опционально: единый CLI (`uplinks run`, `uplinks sync`, …) с сохранением текущих скриптов как thin wrappers

## Качество

- [ ] Консолидация тестов (`*_wave2.py`, `*_85.py` → доменные файлы)
- [ ] Опционально: ruff/mypy в CI

## Grafana

- [ ] Решить судьбу экспериментов MapGL / business charts (были в ветке `feature/grafana-uplinks`, не влиты в main)
- [ ] Интеграция `grafana_uplinks_graph.py` в документацию полного прогона (сейчас вне `run_uplinks_full.py` — осознанно)
