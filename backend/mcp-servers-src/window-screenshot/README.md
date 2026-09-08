# mcp-windows-window-screenshot

MCP-сервер для скриншотов конкретного окна Windows по его handle (`hwnd`, из
`windows-inspect`'s `window_list`/`window_children`) — включая окно, которое **не активно** и
частично перекрыто другими окнами. Использует `PrintWindow(..., PW_RENDERFULLCONTENT)` вместо
захвата экрана с обрезкой — поэтому Z-порядок и фокус не важны. Не работает со свёрнутым
(minimized) окном.

Node/TypeScript MCP-сервер вызывает скомпилированный нативный хелпер `windowscreenshot.exe`
(`native/`, C#/.NET) — без PowerShell и без сторонних нативных зависимостей.

## Инструменты

- `capture_window(hwnd, savePath?)` — один кадр окна, PNG.
- `capture_window_burst(hwnd, count, intervalMs, savePath)` — серия из `count` кадров с
  интервалом `intervalMs` мс, снятая **одним вызовом** (цикл внутри C#-процесса — это специально
  для случаев, когда вызывать инструмент отдельно на каждый кадр слишком медленно). Кадры
  сохраняются в `savePath` как `frame_0001.png`, `frame_0002.png`, ... и не возвращаются целиком
  инлайн (это было бы дорого по контексту) — конкретный кадр читается отдельно при необходимости.

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
    "windows-window-screenshot": {
      "command": "node",
      "args": ["D:/REPO/silmarillion/MCP/window-screenshot/dist/index.js"]
    }
  }
}
```
