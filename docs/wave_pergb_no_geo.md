# Wave PERGB-NO-GEO — orchestrator (Part A)

Убрать гео-метку (флаг + код страны) у pay-per-GB SKU **в отображении**.
Кросс-репо волна (orchestrator + netrun-tg_bot). Ветка `wave/pergb-no-geo` от
`origin/main@d666fd1`. BACKUP `backup/pergb-no-geo`. Без миграций — только рендер.

## Причина
pergb-SKU `dc_pergb_de` засеян с `geo_code='DE'` (миграция 025, легаси-пилот на
Германии), но pergb **гео-агностик** (раздаёт любое гео). Везде рисовалось
«🇩🇪 Datacenter Pay-per-GB DE» — сбивает с толку. `geo_code` в БД НЕ трогаем
(immutable), меняем только как рендерится.

## Изменение (1 точка)
`orchestrator/admin_catalog.py :149 _compute_display_name`:
- для `kind == "datacenter_pergb"`: флаг = нейтральный 🌐 (вместо `_geo_flag(geo)`
  который давал 🇩🇪), и `geo_code` НЕ добавляется в parts. Protocol/duration у
  pergb и так не добавлялись.
- Итог pergb (при ЛЮБОМ geo_code): «🌐 Datacenter Pay-per-GB».
- ipv6/прочие kind — без изменений (флаг гео + код + protocol + duration).
- Покрывает все 4 call-site функции (list/get/create/update SKU :268/:387/:704/:743)
  + display_name в заказах через ту же функцию.

## Тесты
`tests/test_admin_catalog.py`:
- `test_display_name_pergb_drops_geo_flag_and_code` (заменил
  `..._with_geo_keeps_geo_drops_protocol_duration`): pergb с geo_code='DE' →
  «🌐 Datacenter Pay-per-GB» (без 🇩🇪/DE), и с None protocol/duration — то же.
- Регресс цел: `test_display_name_pergb_no_geo`, `..._unknown_geo_falls_back_to_white_flag`
  (ipv6/ZZ → 🏳️), `test_list_skus_includes_display_name` (ipv6 US → «🇺🇸 IPv6 US SOCKS5 (30d)»).
- Весь файл: 59 passed. ruff чисто.

Деплой: orch-first. STATUS: Part A DONE. НЕ запушено.
