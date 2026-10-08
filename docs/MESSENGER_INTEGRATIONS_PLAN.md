# Caroline: мессенджер-интеграции (Slack, Telegram, Discord, WhatsApp, Signal)

## Статус (2026-10-07)

Все пять реализованы в коде: `secret_store.py`, `sidecar_process.py`, `slack_channel.py`+`slack_plugin.py`, `telegram_channel.py`+`telegram_plugin.py`, `discord_channel.py`+`discord_plugin.py`, `whatsapp_channel.py`+`whatsapp_plugin.py`+`companion-apps/whatsapp-sidecar/`, `signal_channel.py`+`signal_plugin.py`. Инсталлятор дополнен: `pywin32`/`slack_sdk`/`setuptools`/`telethon`/`discord.py`/`qrcode`/`Pillow` в `PythonInstaller.cs`, реактивирован `NodeInstaller.cs`, добавлены `WhatsappSidecarInstaller.cs`, `JavaRuntimeInstaller.cs` (реальный хеш Temurin 17 JRE, проверен загрузкой), `SignalCliInstaller.cs` (реальный хеш signal-cli 0.14.9, проверен загрузкой).

Проверено: синтаксис, полный импорт `app.main` со всеми пятью плагинами одновременно, сборка обоих C#-проектов, логика каждого канала на моках (без реальных аккаунтов) — хранение/шифрование токенов, отказ при отсутствии провижининга, QR рендерится в реальный PNG, безусловный автозапуск нигде не всплывает без явного действия пользователя.

Не проверено (нужны реальные внешние условия, недоступные в этой среде): полный прогон `npm install` для whatsapp-sidecar (пакет `@whiskeysockets/baileys` не устанавливался), сам Baileys и JSON-RPC-поверхность signal-cli 0.14.9 — не исполнялись вживую, писаны по документированным паттернам. Slack/Telegram/Discord/WhatsApp/Signal App/Developer-регистрации — не сделаны (откладывались по ходу сессии, SLACK_CLIENT_ID и TELEGRAM_API_ID/HASH пустые).

## Context

Пользователь спросил, как оснастить Caroline адаптерами ко всем основным мессенджерам, по аналогии с OpenClaw. Исследование показало, что у OpenClaw нет единого универсального механизма — под каждый мессенджер своя модель доступа (официальный bot-токен, официальный user-токен, либо неофициальная библиотека/CLI с линковкой устройства по QR). План ниже берёт ту же логику и привязывает её к реальной архитектуре Caroline.

Решено (подтверждено пользователем):
- Доступ — личный аккаунт везде, где это официально возможно: Slack (user-token), Telegram (Client API/MTProto), Signal (linked device). Discord — только бот (личный self-bot нарушает ToS). WhatsApp — личный через Baileys.
- Секреты мессенджеров (токены, сессии) — шифруются через Windows DPAPI, а не хранятся plaintext, как остальные JSON-файлы workspace сейчас.
- Первый этап — чтение и отправка сразу для каждого мессенджера (не read-only).

## Архитектурные находки (подтверждено чтением кода)

- Плагины для LLM: `backend-py/app/plugins/*.py`, модуль с `PLUGIN = Plugin(name, tools=[PluginTool(...)], usage_instructions=...)`. Подхватываются автоматически через `pkgutil.iter_modules` в `app/plugins/loader.py::discover_plugins()` — **новый плагин не нужно нигде регистрировать вручную**, достаточно создать файл. Образец по форме и стилю: `backend-py/app/plugins/companion_plugin.py` (`companion_sms_send`, `companion_search_sms`, `companion_list_sms_threads`) — у мессенджеров будут инструменты той же формы (`slack_send_message`, `slack_search_messages`, ...).
- Фоновые долгоживущие соединения: стартуют один раз в `main.py`'s `@app.on_event("startup")` (рядом с `start_ratatosk_owner_channel`, `start_sms_sync_loop`), каждое — `async def _loop(): while True: ...` обёрнутое в `app/task_supervisor.py::supervise()` (логирует исключение, спит 2с, перезапускает). Образец: `app/ratatosk_channel.py::start_ratatosk_owner_channel`. Проактивные сообщения в чат — через `session_context.get_inject_proactive()` → `ChatSession.inject_proactive()`.
- Несколько мессенджеров (Slack Socket Mode, Telegram Telethon, Discord gateway) живут как чистый Python — без сайдкара, подключаются к этому же циклу напрямую.
- Два мессенджера (WhatsApp/Baileys — Node.js; Signal/signal-cli — Java) требуют внешний рантайм и постоянный дочерний процесс. Образец долгоживущего дочернего процесса с JSON-RPC по stdio уже есть: `backend-py/app/engines/codex_rpc.py` (reader-поток, т.к. Windows asyncio не может спавнить сабпроцессы под selector loop). `backend/mcp-servers-src/` (Node) — **легаси, живой бэкенд его не запускает** (подтверждено: `docs/LINUX_PORT_PLAN.md:16-19`), поэтому Baileys-сайдкар — это новый, отдельный Node-процесс, не реюз той папки, хотя `NodeInstaller.cs` (который сейчас не используется) можно переиспользовать для провижининга Node-рантайма.
- Хранение секретов: сейчас везде plaintext JSON в `workspace_dir` (`ratatosk-own-account.json` и т.п.) — шифрования нет вообще. Для этой фичи — новый модуль.

## Новая общая инфраструктура

1. **`backend-py/app/secret_store.py`** (новый) — DPAPI-шифрование через `ctypes`/`pywin32` (`CryptProtectData`/`CryptUnprotectData`, без доп. пароля — привязано к учётке Windows пользователя). API: `encrypt_to_file(path, data: bytes)`, `decrypt_from_file(path) -> bytes | None`. Каждый мессенджер хранит свои секреты (токен, сессию, auth-state) в `workspace_dir/messengers/<service>/` этим способом — новый паттерн "директория на сервис", по аналогии с `dehydrated_dir(WORKSPACE_DIR)`.
2. **Сайдкар-раннер** — общий helper (`app/sidecar_process.py`, новый), обобщающий `codex_rpc.py`'s паттерн (запуск, reader-поток, перезапуск при падении, graceful shutdown) — используется и Baileys-, и signal-cli-сайдкарами, чтобы не дублировать логику дважды.
3. **Показ QR-кода пользователю** (для WhatsApp и Signal linking) — переиспользовать существующий механизм показа изображений в `DocumentViewerWindow` (уже используется для Visual Mode/слайдшоу), не писать новый UI с нуля.
4. **Интерактивный ввод кода** (для Telegram: номер телефона → код из SMS → опционально 2FA-пароль) — по аналогии с `login_api.py`'s `open_login` нативным диалогом для входа SquirrelWisdom.

## По мессенджерам

### 1. Slack (делать первым — чистый Python, без сайдкара, доказывает паттерн целиком)
- Библиотека: `slack_sdk` (Socket Mode — вебсокет, не нужен публичный HTTPS-эндпоинт).
- Авторизация: user OAuth token (`xoxp-...`) — пользователь создаёт Slack App в своём воркспейсе, выдаёт нужные scopes, разово вставляет токен в Caroline (без redirect-листенера — слишком сложно для v1 без публичного сервера). Токен шифруется через `secret_store.py`.
- Новые файлы: `app/slack_channel.py` (фоновый цикл, `start_slack_channel()`), `app/plugins/slack_plugin.py` (`slack_send_message`, `slack_search_messages`, `slack_list_channels`).

### 2. Telegram (личный аккаунт, чистый Python)
- Библиотека: Telethon (MTProto Client API — НЕ Bot API).
- Авторизация: разовый интерактивный вход (номер телефона → код → опц. 2FA) через `login_api.py`-подобный диалог. Сессия Telethon хранится как DPAPI-зашифрованный файл.
- Новые файлы: `app/telegram_channel.py`, `app/plugins/telegram_plugin.py` (`telegram_send_message`, `telegram_search_messages`, `telegram_list_chats`).

### 3. Discord (только бот — личный self-bot запрещён ToS)
- Библиотека: `discord.py` (gateway websocket).
- Авторизация: bot-токен из Discord Developer Portal, вставляется разово, шифруется.
- Видит только серверы/каналы, куда бота пригласили — явно сообщать об этом ограничении в `usage_instructions` плагина.
- Новые файлы: `app/discord_channel.py`, `app/plugins/discord_plugin.py`.

### 4. WhatsApp (личный аккаунт, Node-сайдкар)
- Библиотека: Baileys (Node.js, неофициальная реализация протокола WhatsApp Web).
- Новый тонкий Node-сервис в репозитории (не в легаси `mcp-servers-src`): постоянный процесс, JSON-RPC/HTTP-мост к Python через `sidecar_process.py`. Auth-state Baileys (мульти-файловая папка) — хранится в `workspace_dir/messengers/whatsapp/`, по возможности оборачивается DPAPI.
- Линковка: QR-код от Baileys → показывается через `DocumentViewerWindow`.
- Инсталлятор: реактивировать `NodeInstaller.cs` (сейчас не используется живым продуктом) для провижининга Node-рантайма под этот сайдкар.
- Новые файлы: `companion-apps/whatsapp-sidecar/` (Node), `app/whatsapp_channel.py`, `app/plugins/whatsapp_plugin.py`.
- Риск: неофициальная библиотека, реальный риск бана номера — явно предупредить пользователя перед первой линковкой.

### 5. Signal (личный аккаунт, JVM-сайдкар)
- Инструмент: `signal-cli`, демон с JSON-RPC по локальному TCP-сокету (127.0.0.1:7583 по умолчанию) + SSE.
- Новая зависимость инсталлятора: JVM-рантайм (аналог `NodeInstaller.cs`, но для Java — `JavaRuntimeInstaller.cs`, новый) + сам `signal-cli` бинарь.
- Линковка: `signal-cli link -n "Caroline"` → QR-код → показывается так же, через `DocumentViewerWindow`.
- Данные линкованного устройства — `workspace_dir/messengers/signal/`, DPAPI.
- Новые файлы: `app/signal_channel.py` (говорит с демоном по TCP JSON-RPC, использует `sidecar_process.py` для запуска самого демона), `app/plugins/signal_plugin.py`.

## Рекомендуемая последовательность

1. Общая инфраструктура: `secret_store.py`, `sidecar_process.py`.
2. Slack целиком (доказывает весь паттерн: фоновый цикл → плагин → DPAPI → проактивный инжект).
3. Telegram (личный аккаунт, но всё ещё чистый Python — следующий по сложности).
4. Discord (просто, т.к. только бот).
5. WhatsApp (первый сайдкар, Node + QR-линковка + новый инсталлятор-шаг).
6. Signal (второй сайдкар, JVM + QR-линковка + новый инсталлятор-шаг).

Каждый пункт — отдельный коммит/деплой с подтверждением, как заведено в этом проекте. Это большой объём работы (5 разных интеграций, 2 новых внешних рантайма в инсталляторе, новый криптопримитив) — после пункта 2 (Slack) имеет смысл сверить, подходит ли паттерн, прежде чем тиражировать его ещё 4 раза.

## Проверка

- Slack/Telegram/Discord: реальная отправка тестового сообщения себе + реальное чтение существующего диалога, сверка с тем, что видно в родном клиенте.
- WhatsApp/Signal: линковка на реальном номере (риск для WhatsApp — предупредить и делать на второстепенном номере, если есть), то же чтение/отправка.
- Для каждого: синтаксис-чек, затем ручной прогон через `companion_api.py`-подобный тест (прямой вызов инструмента в обход LLM, с реальными данными).
