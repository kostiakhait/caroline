# mcp-windows-screenshot

MCP-сервер для захвата скриншотов экрана в Windows. Node/TypeScript MCP-сервер вызывает
скомпилированный нативный хелпер `capture.exe` (`native/`, C#/.NET, `System.Windows.Forms` /
`System.Drawing`) — без PowerShell и без сторонних нативных зависимостей. Хелпер явно вызывает
`SetProcessDPIAware()`, иначе Windows DPI-виртуализирует захват и `CopyFromScreen` возвращает
не тот экран, что нужно.

## Инструмент

### `take_screenshot`

Захватывает экран и возвращает PNG-картинку.

Параметры (все опциональные):

- `monitor` — номер монитора (с нуля). Если не указан — захватывается вся виртуальная область
  (все мониторы разом).
- `savePath` — абсолютный путь, куда дополнительно сохранить PNG на диск.

## Сборка

Требуется .NET 8 SDK (`dotnet --version`).

```bash
npm install
npm run build
```

## Подключение к Claude Code

```bash
claude mcp add windows-screenshot -- node "D:/REPO/silmarillion/MCP/screenshot/dist/index.js"
```

Или добавить в `.mcp.json` проекта:

```json
{
  "mcpServers": {
    "windows-screenshot": {
      "command": "node",
      "args": ["D:/REPO/silmarillion/MCP/screenshot/dist/index.js"]
    }
  }
}
```
