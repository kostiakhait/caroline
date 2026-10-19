# Микроагенты и агентная эволюция: архитектура по состоянию на 2026-10-18

| | |
|---|---|
| **Документ** | Архитектурное описание (как сделано, не план) |
| **Автор** | Konstantin Khait |
| **Версия** | 1.0 |
| **Дата** | 2026-10-18 |
| **Компания** | Partners Solutions |
| **Проект** | Caroline / Camerlengo (reforce) |

Это описание устройства системы **как она есть сейчас**, после переноса домена `reply:` на клиент, достройки протокола клиентского исполнения эволюции и системы границ действий PermissionGuard. Это не план (см. `MICROAGENTS_PLAN.md` — тот про память и ещё частично не сделан), а фиксация принятых решений и реального кода.

## 1. Главный принцип: Камерленго — универсальный сервер

Жёсткое архитектурное правило (2026-10-18): **на Камерленго не должно жить никаких прикладных сервисов под конкретный продукт.** Только то, что действительно общее между продуктами на Камерленго (Caroline, BeautySqrl, Ratatosk и т. д.), остаётся исполняться на сервере. Всё, что специфично одному продукту, исполняется на стороне этого продукта.

Линия раздела проведена по доменам команд v2 Камерленго:

| Домен | Статус | Почему |
|---|---|---|
| `memory:*`, `literary:*`, `contact:*`, `search:*` | **остаётся на сервере навсегда** | настоящая межпродуктовая инфраструктура, используется несколькими продуктами |
| `reply:*` | **перенесён на клиент** (2026-10-18) | все 8 ролей — собственные бизнес-правила Caroline (PermissionGuard, ReplyReview и т. д.), ни один другой продукт их не использует |

Это не временное решение «пока не перенесли», а постоянная граница: `memory:`-домен **не переносится** на клиент никогда, именно потому что он общий. `reply:`-домен переехал потому, что никогда общим не был — просто исторически оказался на сервере.

Критерий перед тем, как класть что-то новое на Камерленго: *«это действительно нужно каждому продукту, или это чья-то одна бизнес-логика, которой там просто было удобно жить»*.

## 2. Общий фреймворк микроагентов (не изменился)

Один модуль (`Microagent.py`), копией в обоих репозиториях — `apps/microagents/Microagent.py` в reforce, `app/microagent_client.py` (обёртка) + копия самого `Microagent.py` на стороне Caroline (вендорится при сборке, `camerlengo-vendor` в `Makefile`).

- **`M.Agent`**: `name`, `version`, `description`, `input_schema`, `output_schema`, `build_prompt`, `verify`, `is_empty`, `execute`, `images`, `ladder`, `deadline_s`. Никакого отдельного UID на агенте нет — различать инстансы (например клонов эволюции) приходится по `name` напрямую, либо по id записи в БД (раздел 4).
- **`M.Runtime`**: реестр агентов по имени (`self._agents`, приватный для инстанса рантайма — глобального реестра нет), функция вызова модели передаётся снаружи при создании.
- **Лестница эскалации** (`ladder`): нано → нано с причиной отказа → мини → сдача. Одна и та же логика на сервере и на клиенте.
- **Контракт конверта**: `{"status": "ok|empty|error|...", "output": {...}, "usage": {...}, "trace": [...]}`.
- **Fail-open как сквозная философия**: сбой/таймаут любого микроагента никогда не ломает ход целиком — вызывающий код либо падает к значению по умолчанию, либо просто не блокирует действие.

## 3. Клиентское исполнение (app/microagent_client.py)

Пилот начат 2026-10-17 на одном агенте (`turn.permission.identify`), сегодня (2026-10-18) распространён на весь домен `reply:`.

```
Caroline (клиент)                         Камерленго (сервер)
─────────────────                         ────────────────────
app/microagent_client.py
  build_local_runtime()
    → Microagent.Runtime(_llm_call, ...)
                                           API/Api2AICommands.py
  _llm_call(modelClass, prompt, ...)  ──→    microagent:call
                                             (реальный вызов провайдера,
                                              биллинг, ключи — ТОЛЬКО тут)
                                           ←── {text, usage}
```

`microagent:call` — тонкий, доменно-нейтральный примитив: `{modelClass, prompt, timeoutS, reasoning, images}` → `{text, usage}`. Он не знает, какой это агент и зачем — именно поэтому он универсален и не противоречит правилу из раздела 1. Реальные ключи провайдера (`Config.OPENROUTER_KEY`) никогда не покидают сервер.

Промпт (`build_prompt`), схемы, `verify` — живут и исполняются **в процессе Caroline**, на модулях, вендоренных «как есть» из reforce (только строка импорта `Microagent` меняется). Это гарантирует, что бизнес-логика бьётся байт-в-байт с тем, что было на сервере.

## 4. Агентная эволюция

### 4.1. Что было до сегодня

`API/Api2Evolution.py::evolve_children` — callback, встроенный в `Runtime._run_children` через `Context.evolver`. Получает **весь** список вызовов одного `.run()`/`.run_many()`, решает по каждому (api_path, scope, role):

- нет записи в `EvolvableRoles.EVOLVABLE_ROLES`, нет строки в `microagent_evolution_settings`, или не выпал дайс по `replication_frequency` → обычный путь без изменений (подавляющее большинство вызовов);
- дайс выпал → мутация (если нет открытого турнира) через `evolution.mutator`, затем champion+challenger параллельно, судит `evolution.selector`, результат побеждающей стороны идёт вызывающему, статистика копится в `microagent_tournaments`.

Это **полностью серверный** механизм: и исполнение агентов, и управление турнирами были в одном процессе. Работает и продолжает работать без изменений для доменов `memory:`/`literary:`/`contact:`/`search:` — они остаются на сервере, значит эволюция для них остаётся прежней, in-process.

**Важное уточнение про «role» (2026-10-18):** `role` — не отдельная сущность, это буквально `agent_name` (`Api2Evolution.py`, `role = agent_name`). Используется как (а) ключ в статическом allow-list `EvolvableRoles.EVOLVABLE_ROLES` и (б) колонка в трёх таблицах БД как часть составного ключа `(api_path, scope, role)`, чтобы один и тот же агент мог независимо эволюционировать в разных связках (api-путь, вызывающий). Своего UID у агента нет — уникальность клона обеспечивают `microagent_clones.id` (авто-инкремент) и `clone_name` (с зашитым в строку UUID-фрагментом).

**Заявление «единственный пилот» было неверным** (исправлено 2026-10-18 в `PermissionIdentify.py`, `Api2ReplyCommands.py`, `test_evolvable_roles.py`): на деле `replication_frequency` стоит ненулевым на **всех** ролях в `EvolvableRoles.py` (порядка 60 строк — `reply:`, `memory:`, `literary:`, `contact:`, `search:` домены целиком), не только на `turn.permission.guard`.

### 4.2. Что добавлено сегодня: протокол клиентского исполнения

Домен `reply:` переехал на клиент (раздел 1) — значит, его агенты больше не исполняются на сервере вообще, но управление эволюцией (настройки, клоны, турниры, продвижение/отбраковка) должно остаться там же, где было: на сервере, как универсальный механизм. Решение: **сервер перестаёт сам исполнять модельные вызовы для `reply:`-домена, но продолжает единолично решать, что именно нужно исполнить.**

Три новые команды в `API/Api2EvolutionCommands.py` (реализация — новые функции в `API/Api2Evolution.py`, аддитивно, старый `evolve_children` не тронут):

```
Caroline (app/evolution_client.py)              Камерленго (API/Api2Evolution.py)
────────────────────────────────                ─────────────────────────────────
evolution:resolveCalls  ───────────────────→     resolve_calls(calls, api_path, scope)
  {apiPath, calls:[{role}]}                        - дайс по replication_frequency
                                                     - читает microagent_evolution_settings
                         ←───────────────────       - {"mode": "plain"|"mutate_needed"|"tournament", ...}

[mode == "mutate_needed"]
  запускает evolution.mutator ЛОКАЛЬНО
evolution:mintClone     ───────────────────→     mint_clone(...)
  {mutatedRulesText, ladderChange, rationale}       - insert_clone + insert_tournament
                         ←───────────────────       - {"mode": "tournament", champion, challenger}

[mode == "tournament"]
  запускает champion+challenger ЛОКАЛЬНО (run_many)
  запускает evolution.selector ЛОКАЛЬНО (рандомизация A/B)
evolution:recordOutcome ───────────────────→     record_outcome(tournamentId, winner)
  {tournamentId, winner}                            - record_tournament_call
                         ←───────────────────       - проверка cull_after_n_calls → promote/cull
                                                     - {"outcome": "open"|"challenger_promoted"|"challenger_culled"}
```

Ни один реальный модельный вызов (champion, challenger, Mutator, Selector) больше не происходит на сервере для `reply:`-домена — сервер только отдаёт, какой геном (`rulesText`+`ladderJson`) использовать, и принимает отчёт о результате. Реальный вызов модели по-прежнему идёт через `microagent:call` (раздел 3) — сервер это видит только как доменно-нейтральный примитив, не зная, что это часть турнира.

**Клиентская сторона** (`app/evolution_client.py`, `app/evolvable_roles_client.py`):
- `evolvable_roles_client.py` — клиентское зеркало `EvolvableRoles.resolve_agent_for_clone`: строит исполняемый `M.Agent` из генома `(rules_text, ladder)`, копируя все фиксированные поля (`input_schema`, `output_schema`, `verify`, `is_empty`, `description`) из собственного вендоренного агента этой роли — та же гарантия «мутация не может тронуть структурный контракт», что и на сервере.
- `evolution_client.run_reply_agent(role, agent_input)` — единственная точка входа, которой пользуются все 8 функций в `voice_api.py` (`review_reply`, `check_permission_guard`, `summarize_action`, `check_refusal_guard`, `check_background_relay_worth`, `check_reply_expected`, `translate_text`, `summarize_content`). При любом сбое протокола (сеть, ACL, рассинхрон ответа) — fail-open на роль по умолчанию, без мутации.

**ACL.** Ключ Caroline (`CAROLINE_SW_KEY`) должен иметь точечные права на `evolution:resolveCalls`/`mintClone`/`recordOutcome` — **не** `evolution:*` wildcard (это дало бы ключу админские права на `evolution:setSettings` для любого api_path/scope). Выданы индивидуально 2026-10-18 через `Api2Auth.grant_key_scope`.

### 4.3. Перенесённые роли (`reply:`-домен, 8 штук + 2 мета-агента)

Вендорены вербатимно (копия + замена строки импорта) в `app/microagents/`:

| Роль | Файл (client) | Назначение |
|---|---|---|
| `turn.permission.guard` | `permission_guard.py` | шаг 2 гейта одобрения (политика) |
| `turn.refusal.guard` | `refusal_guard.py` | обнаружение самовольного отказа модели |
| `reply.review` | `reply_review.py` | фильтр утечек/релевантности/тона |
| `turn.action.summary` | `action_summary.py` | краткое описание исходящего tool-вызова |
| `turn.reply.expected` | `reply_expected.py` | нужен ли ответ после отправки |
| `turn.background.relay.worth` | `background_relay_worth.py` | стоит ли ретранслировать фоновый результат |
| `content.summarize` | `content_summary.py` | сжатие длинного текста |
| `reply.translate` | `reply_translate.py` | перевод исходящего текста |
| `evolution.mutator` | `mutator.py` | мутация генома (не привязан к одной роли) |
| `evolution.selector` | `selector.py` | судья champion/challenger (не привязан к одной роли) |

Уже существовавший пилот — `turn.permission.identify` (`permission_identify.py`) — остаётся **вне** эволюции (чистый fact-finding, нет вопроса «какой вариант лучше служит цели»), как и было.

Домен `memory:`/`literary:`/`contact:`/`search:` (порядка 30 ролей) **не переносится** — явный roadmap-пункт, не задача.

## 5. Система границ действий (PermissionGuard, 6-пунктовая спецификация)

Фиксирует проблему: подтверждение пользователя для операции (например, «отправь письмо Джессике») раньше выводилось заново на каждом вызове из скользящего окна диалога — подтверждение могло «проскроллить» за пределы окна на оживлённой вкладке, и уже одобренное действие блокировалось повторно.

**Новая сущность — «действие» (action), отдельно от «трека».** Трек (`TopicMemory`, серверный, долгоживущий, межвкладочный) — не тронут. Действие — короткоживущее, в пределах вкладки, границы которого и явный флаг одобрения теперь хранятся явно, а не выводятся заново.

```
app/action_registry.py: Action{id, summary, track_hint, approved, created_at, last_touched_at}
                         ActionRegistry: максимум 5 открытых на вкладку, LRU-вытеснение
                         Персист на диск (durability.py: actions-<tab_id>.json)
```

1. **Определение + сопоставление.** `turn.permission.identify` (уже выполняется на каждом send-type вызове) расширен полем `open_actions` на входе и `matched_action_id` на выходе — переиспользует уже идущий вызов, новый вызов не добавляется.
2. **Закрытие.** `_check_action_before_send` (регистрируется как `register_send_action_guard`) запоминает `action_id` в `self._action_id_for_pending_send`; `operations.py::dispatch()` на успехе handler'а зовёт новый хук `register_action_closer`/`get_action_closer` → закрывает действие.
3. **Санкция после сообщения.** Новый клиентский агент `action.sanction` (`app/microagents/action_sanction.py`) — после каждого реального сообщения пользователя решает по каждому открытому действию: `approved` / `revoked` / `unchanged`. Вызывается из `submit()`, пропускается целиком, если открытых действий нет.
4. **Короткое замыкание guard'а.** Если у сопоставленного действия `approved == True` — `_check_action_before_send` возвращает `None` немедленно, **без единого вызова** `turn.permission.guard` для этого вызова.
5. **LRU-5.** `ActionRegistry._evict_lru_if_full()` — вытесняется наименее недавно тронутое; вытесненное = несанкционированное, отдельного учёта не требуется.
6. **Действия без санкции — как раньше.** Если флаг никогда не выставлялся, путь не меняется (тот же `check_permission_guard`, теперь через `evolution_client`).

## 6. Проверка полноты доставки

Реальный инцидент: Кэролайн считала, что показала черновик письма на китайском, хотя фактически не показала. Новый клиентский агент `turn.delivery.completeness` (`app/microagents/delivery_completeness.py`) проверяет, что исходящий текст (после `review_reply`/`translate_text`) реально содержит то, что САМ claims/implies, что доставляет прямо сейчас — не блокирует отправку, при несовпадении шлёт `inject_proactive()` с корректирующей пометкой. Подключён в `dialogue_transcript.py`, на каждый исходящий блок.

## 7. Нарратор и фоновые активности

Подтверждённый живым скриншотом баг (2026-10-18): прогресс-нарратор (`generate_progress_comment`) повторял «ещё обновляю хранилище, резервное копирование заметок» на протяжении 40+ минут, вперемешку с реальной, корректно обрабатываемой перепиской в Telegram — при том что ни одного реального `notes_update`/`save_info` вызова в этот период не было. Причина: нарратор сам себе прямо запрещает статус-апдейты про backup/vault/storage, но слабая nano-модель иногда это игнорирует, если в контексте диалога встречается такая лексика.

**Фикс:** `inject_proactive()` — единая точка входа для всех фоновых/проактивных инъекций (мессенджеры, reminders, watchdog) — теперь имеет `suppress_narration=True` по умолчанию (было `False`). Реальные пользовательские ходы идут через `submit()` напрямую, минуя `inject_proactive`, и не затронуты.

**Отдельная, не исправленная сегодня проблема:** `_inject_messenger_proactive` (main.py) бьёт прямо в `primary_session()`, минуя `pseudo_tab.py`/`deliver_relayed_reply`/`check_background_relay_worth` целиком — в отличие от reminders (`_on_reminder_due`), которые уже корректно изолированы через `pseudo_tab` + `deliver_relayed_reply`. Входящие сообщения мессенджеров не проходят через фильтр «стоит ли вообще об этом говорить». Зафиксировано как известный разрыв, не исправлялось по прямому указанию пользователя (отдельная тема от нарратора).

## 8. Классификация sender_identity: assistant_own

`turn.permission.identify` классифицировал отправку с постоянной собственной идентичности Кэролайн («Caroline Dickens», её собственный почтовый ящик через `email_plugin.py`) как `user_own` — требуя точного подтверждения формулировки, хотя `turn.permission.guard` уже умеет пропускать `assistant_own` без вопросов («never flag it»). Причина: исключение `assistant_own` в промпте `identify` было описано только для случая **ответа** на входящее сообщение под собственной идентичностью, без случая, когда Кэролайн **сама инициирует/продолжает** переписку от той же постоянной идентичности. Промпт расширен (2026-10-18), чтобы покрывать оба случая.

## 9. Известные ограничения и открытые вопросы

- **`memory:`/`literary:`/`contact:`/`search:` остаются на сервере навсегда** — не задача переноса, это и есть правильное место для них.
- **Изоляция мессенджер-уведомлений** (раздел 7) — известный разрыв, не исправлен.
- **`review_reply`/нарратор — слабая модель, может игнорировать явные инструкции.** Задокументированный (2026-10-13 и раньше) класс отказов: нано-модель видит в контексте лексику («backup», «vault», «reminder») и иногда выдаёт шаблонный, не относящийся к делу текст вместо реального ответа или `[[NO_UPDATE]]`, несмотря на прямой запрет. Структурная мера (`check_background_relay_worth`, явные примеры в промптах) снижает частоту, но не устраняет полностью — риск остаётся при любом использовании дешёвой модели на этом месте.
- **Эволюция `reply:`-домена не протестирована живыми турнирами после переезда** — ACL выдан и подтверждён рабочим (`evolution:resolveCalls` отвечает `ok`, `evolution_client.py` больше не падает на проверке статуса), но реальный цикл мутация→турнир→продвижение под новым протоколом живьём не прогонялся до конца (`cull_after_n_calls`, по умолчанию 20 вызовов на роль).
- **Серверные `reply:*`-команды** (`Api2ReplyCommands.py`) физически ещё существуют на сервере как мёртвый код — Caroline их больше не вызывает, но они не удалены (решение оставить как есть, не трогать без необходимости).

## 10. Файлы — быстрый справочник

**reforce-microagents** (ветка `microagents`):
- `API/Api2Evolution.py` — `evolve_children` (старое, memory/literary/contact/search) + `resolve_calls`/`mint_clone`/`record_outcome` (новое, reply-домен).
- `API/Api2EvolutionCommands.py` — команды `evolution:*`.
- `API/Api2EvolutionStore.py` — таблицы `microagent_evolution_settings`/`microagent_clones`/`microagent_tournaments`.
- `apps/microagents/EvolvableRoles.py` — allow-list ролей, остаётся источником правды по схемам/геномам.
- `apps/microagents/PermissionIdentify.py`, `Mutator.py`, `Selector.py`, `PermissionGuard.py`, `ReplyReview.py` и т. д. — серверные оригиналы, с которых вендорятся клиентские копии.

**Caroline** (ветка `main`, `backend-py/app/`):
- `microagent_client.py` — локальный `Runtime` + `microagent:call`.
- `evolution_client.py`, `evolvable_roles_client.py` — клиентская оркестрация эволюции.
- `action_registry.py` — границы действий.
- `microagents/` — все вендоренные + новые (`action_sanction.py`, `delivery_completeness.py`) агенты.
- `chat_session_mixins/permission_guard.py` — интеграция действий с guard'ом.
- `chat_session_mixins/turn_submission.py` — `inject_proactive`/`submit`, подавление нарратора.
- `chat_session_mixins/dialogue_transcript.py` — конвейер исходящего текста (review → translate → guard → completeness).
- `durability.py` — персист действий на диск.
