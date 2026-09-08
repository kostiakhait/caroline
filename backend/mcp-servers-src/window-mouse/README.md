# mcp-windows-window-mouse

MCP-сервер для клика по координатам внутри конкретного окна Windows (client-area-relative, не
абсолютные координаты экрана) **без кражи фокуса**: сообщения мыши (`WM_LBUTTONDOWN`/`UP` и т.д.)
отправляются напрямую в очередь окна через `PostMessage` — курсор не двигается,
`SetForegroundWindow` не вызывается, целевое окно не обязано быть активным. Работает для обычных
Win32-контролов; GPU-рендеренные кастомные элементы (Chromium/Electron, игры) могут не реагировать
на такие сообщения — тогда нужен настоящий клик через `windows-mouse`.

Node/TypeScript MCP-сервер вызывает скомпилированный нативный хелпер `windowmouse.exe`
(`native/`, C#/.NET, `user32.dll`: `PostMessage`) — без PowerShell и без сторонних нативных
зависимостей.

## Инструменты

- `click_window(hwnd, x, y, button?)` — клик в точке (x, y) относительно client-area окна с
  данным `hwnd` (из `windows-inspect`). `button`: Left/Right/Middle, по умолчанию Left.

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
    "windows-window-mouse": {
      "command": "node",
      "args": ["D:/REPO/silmarillion/MCP/window-mouse/dist/index.js"]
    }
  }
}
```
