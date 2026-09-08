# mcp-notes

MCP-сервер для заметок Squirrel Wisdom (`https://squirrelwisdom.com`) — полноценный
notes-менеджер (заметки, папки, вложения) под аккаунтом пользователя. Задуман в первую
очередь как долговременная память Claude (папка `Claude Memory` по умолчанию для
автономных записей), но одинаково работает с любыми заметками/папками, которые попросит
пользователь — это не ограничено памятью.

Протокол бэкенда описан в `https://squirrelwisdom.com/portal/NOTES_API.md`. С 2026-07 бэкенд
переписан на сессионную авторизацию (Phase 1 security hardening — раньше `hash16 =
sha256(email).slice(0,16)` был единственной "защитой" адресации заметок, и зная/подобрав
чужой email можно было читать/перезаписывать чужие заметки голым `curl`, без сессии вообще).
Ключевые особенности, которые определяют устройство этого сервера:

- Все действия идут через единый плагинный конверт (`plugins:call`, `plugin: "Notes"`) и
  требуют `session` **на каждом вызове**, не только при логине — сервер сам вычисляет
  `hash16` из сессии, клиент его больше не передаёт (кроме конструирования публичного URL
  вложений — см. ниже).
- Сессии живут максимум 24ч простоя, поэтому сервер не кэширует токен сессии на диск —
  вместо этого при каждом запуске процесса (лениво, при первом вызове инструмента) он сам
  логинится заново сохранёнными кредами. Если сессия истекла посреди работы длительного
  процесса, сервер сам перелогинивается и повторяет вызов один раз (`withSession` в
  `session.ts`).
- Скачивание вложений (`files/{hash16}/{filename}`) и создание/апдейт/поиск по частному
  share-ссылке (`view_note.html?id=...&u=...`) всё ещё не требуют сессии — это осознанно
  открытые пути (публичное шаринг-URL, как у Google Docs, плюс ещё не закрытая дыра для
  скачивания вложений, см. "Known gap" в NOTES_API.md).

## Авторизация (однократная)

1. Вызвать `notes_login(email, password)` один раз — проверяет пароль и сохраняет
   `{email, password}` в `~/.mcp-notes/credentials.json` (вне репозитория).
2. При каждом следующем запуске сервера он сам перелогинивается этими кредами — вводить
   пароль повторно не нужно. `notes_whoami()` показывает, какой аккаунт активен.

Пароль хранится в открытом виде локально (как `~/.aws/credentials` у AWS CLI) — сам
бэкенд и так не даёт более сильной изоляции, чем знание email.

## Инструменты

- `notes_login(email, password)` / `notes_whoami()`
- `notes_list(folder?, includeDeleted?)` — точная папка (без вложенных)
- `notes_search(query, folder?, includeDeleted?)` — подстрока, папка ищется рекурсивно
- `notes_get(id)` / `notes_create(text, folder?)` / `notes_update(id, text?, folder?)` / `notes_delete(id)` / `notes_move(id, folder)`
- `notes_list_folders()` / `notes_create_folder(path)` / `notes_rename_folder(oldPath, newPath)` / `notes_delete_folder(path)`
- `notes_attach(noteId, filePath, originalName?)` / `notes_list_attachments(noteId?)` / `notes_download_attachment(filename, savePath)` / `notes_remove_attachment(noteId, filename)`

`notes_remove_attachment` больше не принимает `deleteBlob` — новый `removeAttachment` на
бэкенде всегда удаляет и запись в метаданных, и сам файл одним вызовом; отдельного
"отвязать, но оставить файл" действия в новом API нет.

Вложения ограничены ~47МБ (реальный потолок из-за инфляции base64 при cap 64МБ на nginx).

## Сборка

```bash
npm install
npm run build
```

## Подключение к Claude Code

```bash
claude mcp add notes -- node "D:/REPO/silmarillion/MCP/notes/dist/index.js"
```

Или добавить в `.mcp.json` проекта:

```json
{
  "mcpServers": {
    "notes": {
      "command": "node",
      "args": ["D:/REPO/silmarillion/MCP/notes/dist/index.js"]
    }
  }
}
```
