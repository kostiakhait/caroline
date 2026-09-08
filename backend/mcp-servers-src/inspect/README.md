# mcp-windows-inspect

MCP-сервер для интроспекции окон Windows в стиле WinSpy/Spy++: перечисление окон верхнего
уровня и дочерних контролов с их handle, классом, заголовком, координатами, состоянием
видимости/доступности и control ID. Не двигает мышь, не шлёт ввод — только читает. Возвращаемый
`hwnd` (hex-строка вида `"0x001A04F2"`) — это то, что передаётся в `windows-window-screenshot`,
`windows-window-mouse` и `windows-window-keyboard` для нацеливания на конкретное окно.

Node/TypeScript MCP-сервер вызывает скомпилированный нативный хелпер `inspect.exe` (`native/`,
C#/.NET, `user32.dll`: `EnumWindows`, `EnumChildWindows`, `GetWindowRect`, `GetClassName`,
`GetWindowLongPtr` и т.д.) — без PowerShell и без сторонних нативных зависимостей. В отличие от
остальных серверов этого репозитория (которые возвращают короткий текстовый токен), этот
возвращает JSON — данные по своей природе структурные (массивы записей об окнах).

## Инструменты

- `window_list(titleFilter?, classNameFilter?, pid?, includeInvisible?)` — окна верхнего уровня.
- `window_children(hwnd, titleFilter?, classNameFilter?, includeInvisible?)` — дочерние
  окна/контролы заданного окна (так находится, например, hwnd конкретного текстового поля).
- `window_info(hwnd)` — полная запись по одному конкретному handle.

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
    "windows-inspect": {
      "command": "node",
      "args": ["D:/REPO/silmarillion/MCP/inspect/dist/index.js"]
    }
  }
}
```
