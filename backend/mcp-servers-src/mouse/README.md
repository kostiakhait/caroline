# mcp-windows-mouse

MCP-сервер для инъекции событий мыши в Windows: перемещение курсора, клики, зажатие/отпускание
кнопки (для drag) и прокрутка колеса. Node/TypeScript MCP-сервер вызывает скомпилированный
нативный хелпер `mouse.exe` (`native/`, C#/.NET, `user32.dll`: `SetCursorPos`, `GetCursorPos`,
`mouse_event`) — без PowerShell и без сторонних нативных зависимостей.

Двигает реальный системный курсор мыши, как если бы это делал человек — так что во время работы
инструмента лучше не трогать мышь параллельно.

## Инструменты

- `get_mouse_position` — текущая позиция курсора.
- `move_mouse(x, y)` — абсолютное перемещение курсора.
- `click_mouse(button?, x?, y?, clicks?)` — клик (button: Left/Right/Middle, clicks: 2 = двойной клик). Если `x`/`y` не заданы — клик в текущей позиции.
- `mouse_button(action: down|up, button?, x?, y?)` — раздельное нажатие/отпускание кнопки, для drag: `down` в начальной точке, затем `move_mouse`, затем `up` в конечной.
- `scroll_mouse(delta)` — прокрутка колеса в «нотчах» (позитивное значение — вверх, негативное — вниз).

## Ограничения

- Не работает с элементами на secure desktop (UAC-диалоги, экран блокировки) — это защита самой Windows, обойти её инъекцией нельзя.
- Некоторые приложения (античит, RDP-сессии с захватом ввода) могут игнорировать синтетические события мыши.

## Сборка

Требуется .NET 8 SDK (`dotnet --version`).

```bash
npm install
npm run build
```

## Подключение к Claude Code

```bash
claude mcp add windows-mouse -- node "D:/REPO/silmarillion/MCP/mouse/dist/index.js"
```

Или добавить в `.mcp.json` проекта:

```json
{
  "mcpServers": {
    "windows-mouse": {
      "command": "node",
      "args": ["D:/REPO/silmarillion/MCP/mouse/dist/index.js"]
    }
  }
}
```
