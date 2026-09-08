# mcp-windows-window-keyboard

MCP-сервер для ввода с клавиатуры напрямую в конкретное окно/контрол Windows **без кражи
фокуса**: сообщения (`WM_CHAR`, `WM_KEYDOWN`/`UP`) отправляются напрямую в очередь окна через
`PostMessage`, без `SetFocus` и без `SendInput` — целевое окно не обязано быть активным. Работает
для обычных Win32 edit/static-контролов; GPU-рендеренные кастомные текстовые поля
(Chromium/Electron) могут игнорировать такой ввод — тогда нужен настоящий `SendInput` через
`windows-keyboard`. Фоновая передача модификаторов (Ctrl/Shift/Alt) не гарантирована для всех
приложений — часть их проверяет реальное состояние клавиш-модификаторов, а не только сообщения.

Node/TypeScript MCP-сервер вызывает скомпилированный нативный хелпер `windowkeyboard.exe`
(`native/`, C#/.NET, `user32.dll`: `PostMessage`) — без PowerShell и без сторонних нативных
зависимостей. Таблица имён клавиш → VK-код (`src/keys.ts`) продублирована из `MCP/keyboard` (та
же логика, отдельный TS-проект).

## Инструменты

- `type_window(hwnd, text, delayMs?)` — печатает текст в окно/контрол с данным `hwnd` (из
  `windows-inspect`).
- `press_window_key(hwnd, key, modifiers?)` — нажатие именованной клавиши (`"Enter"`, `"Tab"`,
  `"a"`, ...) с опциональными модификаторами (`["Ctrl"]` и т.п.).

## Сборка

Требуется .NET 8 SDK (`dotnet --version`).

```bash
npm install
npm run build
```

## Подключение к Claude Code

Добавить в `.mcp.json` проекта:

```json
{
  "mcpServers": {
    "windows-window-keyboard": {
      "command": "node",
      "args": ["D:/REPO/silmarillion/MCP/window-keyboard/dist/index.js"]
    }
  }
}
```
