"""The run_chain step interpreter on Windows (2026-10-24), in-process -- a
port of chain.exe (backend/mcp-servers-src/chain/native/Program.cs), which
was never actually built or shipped, so run_chain could not run on Windows
at all. Same ops, fields, retries, timeouts and result shape; the Win32
work is app.plugins._win32. The Linux counterpart is _linux_chain.py.

Steps arrive as the model sends them: "key"/"modifiers" as key names
(resolved here with _win32.resolve_vk), "pid" as a number or "$name".
Mouse/keyboard steps with an "hwnd" are posted to that window (no focus
change); without one they are real, screen-absolute input.
"""

from __future__ import annotations

import ctypes
import hashlib
import time
from ctypes import wintypes
from typing import Any

from app.plugins import _win32 as w

# name -> pid, from launch/wait_window steps that carry "as" (process lifetime, as chain.exe's PidBindings)
_pid_bindings: dict[str, int] = {}

_RETRY_ELIGIBLE = {"move", "click", "mouse_down", "mouse_up", "key"}


class ChainStepError(Exception):
    pass


def _resolve_hwnd(hwnd_arg: Any) -> int:
    """A literal "0x..." handle, or "$name": the first visible top-level window
    of the process an earlier step bound to that name."""
    text = str(hwnd_arg)
    if not text.startswith("$"):
        return w.parse_hwnd(text)
    name = text[1:]
    pid = _pid_bindings.get(name)
    if pid is None:
        raise ChainStepError(f'No binding named "{name}" (from an earlier launch/wait_window step\'s "as").')
    found = w.find_window(None, None, pid)
    if found is None:
        raise ChainStepError(f'No visible top-level window currently owned by PID {pid} (bound to "{name}").')
    return found


def _resolve_pid(pid_arg: Any) -> int:
    text = str(pid_arg)
    if not text.startswith("$"):
        return int(text)
    pid = _pid_bindings.get(text[1:])
    if pid is None:
        raise ChainStepError(f'No binding named "{text[1:]}" (from an earlier launch step\'s "as").')
    return pid


def _button(step: dict[str, Any]) -> str:
    return step.get("button") or "Left"


def _execute_move_click_down_up(step: dict[str, Any]) -> None:
    op, button = step["op"], _button(step)
    x, y = int(step["x"]), int(step["y"])
    clicks = step.get("clicks") or 1
    if step.get("hwnd") is not None:
        hwnd = _resolve_hwnd(step["hwnd"])
        down, up, wparam_down = w._posted_button_messages(button)
        lparam = w._lparam(x, y)
        if op == "move":
            w.user32.PostMessageW(hwnd, w.WM_MOUSEMOVE, 0, lparam)
        elif op == "mouse_down":
            w.user32.PostMessageW(hwnd, down, wparam_down, lparam)
        elif op == "mouse_up":
            w.user32.PostMessageW(hwnd, up, 0, lparam)
        elif op == "click":
            for i in range(clicks):
                w.user32.PostMessageW(hwnd, down, wparam_down, lparam)
                time.sleep(0.02)
                w.user32.PostMessageW(hwnd, up, 0, lparam)
                if i < clicks - 1:
                    time.sleep(0.05)
        return
    w._set_cursor(x, y)
    down, up = w._global_button_flags(button)
    if op == "mouse_down":
        w._mouse_event(down)
    elif op == "mouse_up":
        w._mouse_event(up)
    elif op == "click":
        for i in range(clicks):
            w._mouse_event(down)
            w._mouse_event(up)
            if i < clicks - 1:
                time.sleep(0.05)


def _execute_drag(step: dict[str, Any]) -> None:
    """Real intermediate moves, not one jump -- a down/jump/up does not register
    as a drag in UE5's Slate UI (global) nor in controls tracking posted moves."""
    button, n = _button(step), step.get("steps") or 10
    x1, y1, x2, y2 = (int(step[k]) for k in ("x1", "y1", "x2", "y2"))
    points = [(round(x1 + (x2 - x1) * i / n), round(y1 + (y2 - y1) * i / n)) for i in range(1, n + 1)]
    if step.get("hwnd") is not None:
        hwnd = _resolve_hwnd(step["hwnd"])
        down, up, wparam_down = w._posted_button_messages(button)
        w.user32.PostMessageW(hwnd, down, wparam_down, w._lparam(x1, y1))
        time.sleep(0.02)
        for ix, iy in points:
            w.user32.PostMessageW(hwnd, w.WM_MOUSEMOVE, wparam_down, w._lparam(ix, iy))
            time.sleep(0.015)
        w.user32.PostMessageW(hwnd, up, 0, w._lparam(x2, y2))
        return
    down, up = w._global_button_flags(button)
    w._set_cursor(x1, y1)
    w._mouse_event(down)
    time.sleep(0.02)
    for ix, iy in points:
        w._set_cursor(ix, iy)
        time.sleep(0.015)
    w._mouse_event(up)


def _execute_key(step: dict[str, Any]) -> None:
    vk = w.resolve_vk(step["key"])
    modifier_vks = [w.resolve_vk(m) for m in step.get("modifiers") or []]
    if step.get("hwnd") is not None:
        w.post_combo(_resolve_hwnd(step["hwnd"]), modifier_vks, vk)
    else:
        w.press_combo(modifier_vks, vk)


def _execute_type(step: dict[str, Any]) -> None:
    delay = step.get("delayMs")
    delay = 10 if delay is None else int(delay)
    if step.get("hwnd") is not None:
        w.post_text(_resolve_hwnd(step["hwnd"]), step.get("text") or "", delay)
    else:
        w.type_text(step.get("text") or "", delay)


def _execute_scroll(step: dict[str, Any]) -> None:
    delta = int(step.get("delta") or 0)
    if step.get("hwnd") is not None:
        hwnd = _resolve_hwnd(step["hwnd"])
        # WM_MOUSEWHEEL is the one posted mouse message whose lParam is in SCREEN coordinates.
        sx, sy = w.client_to_screen(hwnd, step.get("x") or 0, step.get("y") or 0)
        w.user32.PostMessageW(hwnd, w.WM_MOUSEWHEEL, ((delta * 120) & 0xFFFF) << 16, w._lparam(sx, sy))
    else:
        w._mouse_event(w.MOUSEEVENTF_WHEEL, delta * 120)


def _execute_wait_window(step: dict[str, Any]) -> None:
    pid = _resolve_pid(step["pid"]) if step.get("pid") is not None else None
    deadline = time.monotonic() + int(step["timeoutMs"]) / 1000
    while True:
        found = w.find_window(step.get("titleFilter"), step.get("classNameFilter"), pid)
        if found is not None:
            if step.get("as"):
                _pid_bindings[step["as"]] = w.window_pid(found)
            return
        if time.monotonic() >= deadline:
            raise ChainStepError(
                f"wait_window timed out after {step['timeoutMs']}ms (titleFilter=\"{step.get('titleFilter') or ''}\", "
                f"classNameFilter=\"{step.get('classNameFilter') or ''}\", pid={'' if pid is None else pid})"
            )
        time.sleep(0.1)


def _hex(rgb: tuple[int, int, int]) -> str:
    return "#{:02X}{:02X}{:02X}".format(*rgb)


def _execute_wait_pixel(step: dict[str, Any]) -> None:
    color = str(step["color"]).lstrip("#")
    target = (int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16))
    tolerance = int(step.get("tolerance") or 0)
    x, y = int(step["x"]), int(step["y"])
    deadline = time.monotonic() + int(step["timeoutMs"]) / 1000

    def matches(rgb: tuple[int, int, int]) -> bool:
        return all(abs(rgb[i] - target[i]) <= tolerance for i in range(3))

    last = (0, 0, 0)
    if step.get("hwnd") is not None:
        # From the window's own PrintWindow content, so a covered window works too.
        hwnd = _resolve_hwnd(step["hwnd"])
        while True:
            image = w.capture_window_raw(hwnd)
            if 0 <= x < image.width and 0 <= y < image.height:
                last = image.getpixel((x, y))[:3]
                if matches(last):
                    return
            if time.monotonic() >= deadline:
                raise ChainStepError(
                    f"wait_pixel timed out after {step['timeoutMs']}ms at window-relative ({x},{y}), last saw "
                    f"{_hex(last)}, wanted {_hex(target)} +/-{tolerance}"
                )
            time.sleep(0.1)
    while True:
        with w._dpi_aware():
            last = w.screen_pixel(x, y)
        if matches(last):
            return
        if time.monotonic() >= deadline:
            raise ChainStepError(
                f"wait_pixel timed out after {step['timeoutMs']}ms at ({x},{y}), last saw {_hex(last)}, "
                f"wanted {_hex(target)} +/-{tolerance}"
            )
        time.sleep(0.05)


def _execute_wait_idle(step: dict[str, Any]) -> None:
    if step.get("hwnd") is not None:
        hwnd = _resolve_hwnd(step["hwnd"])

        def capture() -> Any:
            return w.capture_window_raw(hwnd)
    else:
        def capture() -> Any:
            return w.capture_screen_region(int(step["x"]), int(step["y"]), int(step["width"]), int(step["height"]))

    deadline = time.monotonic() + int(step["timeoutMs"]) / 1000
    stable_s = int(step["stableMs"]) / 1000
    last_hash: str | None = None
    stable_since = time.monotonic()
    while True:
        digest = hashlib.md5(capture().tobytes()).hexdigest()
        now = time.monotonic()
        if digest != last_hash:
            last_hash, stable_since = digest, now
        elif now - stable_since >= stable_s:
            return
        if now >= deadline:
            raise ChainStepError(f"wait_idle timed out after {step['timeoutMs']}ms without {step['stableMs']}ms of stable content")
        time.sleep(0.15)


class _SHELLEXECUTEINFOW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("fMask", ctypes.c_ulong), ("hwnd", wintypes.HWND),
                ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
                ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE),
                ("lpIDList", ctypes.c_void_p), ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
                ("dwHotKey", wintypes.DWORD), ("hIconOrMonitor", wintypes.HANDLE), ("hProcess", wintypes.HANDLE)]


_SEE_MASK_NOCLOSEPROCESS = 0x00000040
_SW_SHOWNORMAL = 1


def _execute_launch(step: dict[str, Any]) -> None:
    """ShellExecuteEx, as Process.Start with UseShellExecute=true: a path, a
    document or a URL, the args string passed as it is."""
    path = step.get("path")
    if not path:
        raise ChainStepError("launch requires path")
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    info = _SHELLEXECUTEINFOW(cbSize=ctypes.sizeof(_SHELLEXECUTEINFOW), fMask=_SEE_MASK_NOCLOSEPROCESS,
                              lpFile=str(path), lpParameters=step.get("args") or None,
                              lpDirectory=step.get("cwd") or None, nShow=_SW_SHOWNORMAL)
    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        raise ChainStepError(f'Could not start "{path}" (GetLastError={ctypes.get_last_error()})')
    pid = 0
    if info.hProcess:
        pid = w.kernel32.GetProcessId(info.hProcess)
        w.kernel32.CloseHandle(info.hProcess)
    if step.get("as"):
        if not pid:
            raise ChainStepError(f'"{path}" started, but no process was created to bind to "{step["as"]}".')
        _pid_bindings[step["as"]] = pid


def _kill(pid: int, tree: bool) -> None:
    """Process.Kill(force): `force` killed the whole process tree."""
    import psutil

    process = psutil.Process(pid)
    victims = (process.children(recursive=True) if tree else []) + [process]
    for victim in victims:
        try:
            victim.kill()
        except psutil.NoSuchProcess:
            pass


def _execute_kill(step: dict[str, Any]) -> None:
    import psutil

    force = bool(step.get("force"))
    if step.get("imageName") and step.get("all"):
        name = str(step["imageName"]).lower()
        name = name[:-4] if name.endswith(".exe") else name
        for process in psutil.process_iter(["name"]):
            process_name = (process.info.get("name") or "").lower()
            if (process_name[:-4] if process_name.endswith(".exe") else process_name) == name:
                try:
                    _kill(process.pid, force)
                except psutil.NoSuchProcess:
                    pass  # already exited
        return
    if step.get("pid") is not None:
        _kill(_resolve_pid(step["pid"]), force)
        return
    raise ChainStepError("kill requires either pid or imageName+all:true")


def _execute_restart(step: dict[str, Any]) -> None:
    _execute_kill(step)
    time.sleep(0.2)
    _execute_launch(step)


def _save_capture(image: Any, out_file: str) -> str:
    image.save(out_file, format="PNG")
    return out_file


def _execute_checkpoint(step: dict[str, Any], out_file: str | None) -> str | None:
    if out_file is None:
        return None
    if step.get("hwnd") is not None:
        return _save_capture(w.capture_window_raw(_resolve_hwnd(step["hwnd"])), out_file)
    return _save_capture(w.capture_full_screen(), out_file)


_OPS = {
    "move": _execute_move_click_down_up, "click": _execute_move_click_down_up,
    "mouse_down": _execute_move_click_down_up, "mouse_up": _execute_move_click_down_up,
    "drag": _execute_drag, "key": _execute_key, "type": _execute_type, "scroll": _execute_scroll,
    "sleep": lambda step: time.sleep(int(step["ms"]) / 1000),
    "wait_window": _execute_wait_window, "wait_pixel": _execute_wait_pixel, "wait_idle": _execute_wait_idle,
    "launch": _execute_launch, "kill": _execute_kill, "restart": _execute_restart,
}


def _run_step(step: dict[str, Any]) -> None:
    op = step["op"]
    execute = _OPS.get(op)
    if execute is None:
        raise ChainStepError(f"Unknown op: {op}")
    retries = int(step.get("retries") or 0) if op in _RETRY_ELIGIBLE else 0
    retry_delay = int(step.get("retryDelayMs") or 200) / 1000
    attempt = 0
    while True:
        try:
            with w._dpi_aware():
                execute(step)
            return
        except Exception:
            if attempt >= retries:
                raise
            attempt += 1
            time.sleep(retry_delay)


def run_chain_windows(steps: list[dict[str, Any]], out_file: str | None) -> dict[str, Any]:
    """Synchronous; the plugin runs the whole chain in one worker thread.
    Stops at a checkpoint ("paused", with a screenshot) or the first failing
    step ("failed", with a full-screen screenshot), as chain.exe."""
    start = time.monotonic()
    result: dict[str, Any] = {"status": "ok", "completedSteps": 0, "failedAt": None, "elapsedMs": 0, "screenshot": None}
    for i, step in enumerate(steps):
        if step.get("op") == "checkpoint":
            try:
                result["screenshot"] = _execute_checkpoint(step, out_file)
            except Exception:  # noqa: BLE001 -- a failed checkpoint screenshot must not hide the pause
                result["screenshot"] = None
            result["status"] = "paused"
            result["completedSteps"] = i + 1
            break
        try:
            _run_step(step)
            result["completedSteps"] = i + 1
        except Exception as exc:  # noqa: BLE001 -- reported as the chain's own failedAt
            result["status"] = "failed"
            result["completedSteps"] = i
            result["failedAt"] = {"index": i, "op": step.get("op"), "reason": str(exc)}
            if out_file is not None:
                try:
                    result["screenshot"] = _save_capture(w.capture_full_screen(), out_file)
                except Exception:  # noqa: BLE001 -- best effort
                    pass
            break
    result["elapsedMs"] = round((time.monotonic() - start) * 1000)
    return result
