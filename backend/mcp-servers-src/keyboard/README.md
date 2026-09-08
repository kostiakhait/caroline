# mcp-windows-keyboard

MCP-сервер для инъекции клавиатурных событий в Windows: набор произвольного unicode-текста,
нажатие клавиш и комбинаций, удержание/отпускание клавиш. Node/TypeScript MCP-сервер вызывает
скомпилированный нативный хелпер `keyboard.exe` (`native/`, C#/.NET), использующий тот же
подход, что и `DictateWin` (`SendInput` c `KEYEVENTF_UNICODE`, см.
`DictateWin/src/Services/TextInputService.cs` и `DictateWin/src/Interop/NativeMethods.cs`) —
без PowerShell и без сторонних нативных зависимостей.

## Инструменты

- `type_text(text, delayMs?)` — печатает произвольный unicode-текст посимвольно (как диктовка в DictateWin). `\n` отправляется как обычный символ, большинство текстовых полей трактуют его как Enter.
- `press_key(key, modifiers?)` — нажимает одну клавишу, опционально с зажатыми модификаторами (`["Ctrl","Shift","Alt","Win"]`), например `key:"c", modifiers:["Ctrl"]` = Ctrl+C.
- `key_down(key)` / `key_up(key)` — раздельное нажатие/отпускание клавиши, для удержания через несколько вызовов (например, зажать Shift, нажать другую клавишу, затем отпустить Shift).

Имена клавиш: одиночные буквы/цифры (`a`, `1`), либо именованные — `Enter`, `Escape`/`Esc`,
`Tab`, `Backspace`, `Space`, `Left`/`Right`/`Up`/`Down`, `Home`, `End`, `PageUp`/`PageDown`,
`Insert`, `Delete`, `F1`-`F24`, `Ctrl`, `Shift`, `Alt`, `Win` и др. (полный список — [src/keys.ts](src/keys.ts)).

## Важно про фокус

`SendInput` всегда летит в то окно, которое сейчас в фокусе ОС — сервер не привязывается
к конкретному окну. Перед вызовом инструментов убедитесь, что нужное окно активно.

## Сборка

Требуется .NET 8 SDK (`dotnet --version`).

```bash
npm install
npm run build
```

## Подключение к Claude Code

```bash
claude mcp add windows-keyboard -- node "D:/REPO/silmarillion/MCP/keyboard/dist/index.js"
```

Или добавить в `.mcp.json` проекта:

```json
{
  "mcpServers": {
    "windows-keyboard": {
      "command": "node",
      "args": ["D:/REPO/silmarillion/MCP/keyboard/dist/index.js"]
    }
  }
}
```
