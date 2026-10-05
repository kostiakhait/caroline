"""Linux port (2026-10-04): a Python-native linear step interpreter, the
direct analog of chain.exe's own interpreter loop (see
backend/mcp-servers-src/chain/native/Program.cs) -- reuses
app.plugins._x11_input/_x11_window instead of Win32 SendInput/PostMessage,
matching the Linux Port Plan's "skip the subprocess layer entirely"
approach every other automation plugin's Linux branch already takes.

Step shape matches the SAME JSON the model already sends chain_plugin.py
(op/hwnd/x/y/x1/y1/x2/y2/button/clicks/steps/key/modifiers/text/delayMs/
delta/ms/titleFilter/classNameFilter/pid/timeoutMs/as/color/tolerance/
width/height/stableMs/path/args/cwd/imageName/all/force/retries/
retryDelayMs) -- UNLIKE the Windows path, key/modifiers stay as name
strings here (resolved via _x11_input.resolve_keysym), not pre-resolved
vk/modifierVks codes: chain_plugin.py's run_chain skips its own
_to_wire_step VK conversion on this platform and passes steps through
as-is.

Known fidelity gaps vs the Windows native interpreter (documented, not
silently glossed over -- same posture every other Linux-branch module in
this port takes):
- wait_pixel/wait_idle capture via mss (screen-crop) for window mode too,
  NOT an occlusion-safe PrintWindow-equivalent -- same gap
  window_screenshot_plugin.py's own Linux branch already documents.
- drag/move/click/mouse_down/mouse_up/key/type/scroll's window-scoped
  (hwnd) mode use the same XSendEvent posted-input mechanism as
  window_mouse_plugin.py/window_keyboard_plugin.py -- same toolkit-trust
  caveat (a synthetic send_event=true event some GTK/Qt/Electron apps
  ignore).
- launch's cwd/args use subprocess.Popen with shlex-split arguments
  rather than Windows' UseShellExecute=true semantics -- no shell
  expansion of the args string.
"""

from __future__ import annotations

import shlex
import subprocess
import time
from pathlib import Path
from typing import Any

from app.plugins import _x11_input as x11
from app.plugins import _x11_window as x11win

# name -> pid, populated by launch/wait_window steps that carry "as" --
# matches Program.cs's own PidBindings dict, module-level/process-lifetime
# for the same reason (a chain step referencing an earlier step's binding
# needs it to outlive that one RunStep call, but not past this process).
_pid_bindings: dict[str, int] = {}

_RETRY_ELIGIBLE = {"move", "click", "mouse_down", "mouse_up", "key"}


class ChainStepError(Exception):
    pass


def _resolve_hwnd(hwnd_arg: str) -> str:
    if not hwnd_arg.startswith("$"):
        return hwnd_arg
    name = hwnd_arg[1:]
    pid = _pid_bindings.get(name)
    if pid is None:
        raise ChainStepError(f'No binding named "{name}" (from an earlier launch/wait_window step\'s "as").')
    found = x11win.list_windows(pid_filter=pid)
    if not found:
        raise ChainStepError(f'No visible top-level window currently owned by PID {pid} (bound to "{name}").')
    return found[0]["hwnd"]


def _resolve_pid(pid_arg: str) -> int:
    if not pid_arg.startswith("$"):
        return int(pid_arg)
    name = pid_arg[1:]
    pid = _pid_bindings.get(name)
    if pid is None:
        raise ChainStepError(f'No binding named "{name}" (from an earlier launch step\'s "as").')
    return pid


def _window_click_sequence(hwnd: str, x: int, y: int, button: str, clicks: int) -> None:
    rect = x11win.get_window_rect(hwnd)
    if rect["x"] is None:
        raise ChainStepError(f"Could not resolve window {hwnd}'s screen position.")
    window = x11win.window_for(hwnd)
    for i in range(clicks):
        x11.send_window_click(window, x, y, rect["x"] + x, rect["y"] + y, button)
        if i < clicks - 1:
            time.sleep(0.05)


def _execute_move_click_down_up(step: dict[str, Any]) -> None:
    button = step.get("button") or "left"
    op = step["op"]
    x, y = step.get("x"), step.get("y")
    if step.get("hwnd") is not None:
        hwnd = _resolve_hwnd(step["hwnd"])
        if op == "move":
            return  # posted-move has no real analog worth a synthetic event; a no-op, same as a
                     # screen-absolute "move" step targeting a window being mostly informational
        if op == "mouse_down":
            window = x11win.window_for(hwnd)
            rect = x11win.get_window_rect(hwnd)
            if rect["x"] is None:
                raise ChainStepError(f"Could not resolve window {hwnd}'s screen position.")
            x11.send_window_button(window, x, y, rect["x"] + x, rect["y"] + y, press=True, button=button)
            return
        if op == "mouse_up":
            window = x11win.window_for(hwnd)
            rect = x11win.get_window_rect(hwnd)
            if rect["x"] is None:
                raise ChainStepError(f"Could not resolve window {hwnd}'s screen position.")
            x11.send_window_button(window, x, y, rect["x"] + x, rect["y"] + y, press=False, button=button)
            return
        if op == "click":
            _window_click_sequence(hwnd, x, y, button, step.get("clicks") or 1)
            return
    else:
        if op == "move":
            x11.move_mouse(x, y)
            return
        if op == "mouse_down":
            x11.move_mouse(x, y)
            x11.mouse_button("down", button)
            return
        if op == "mouse_up":
            x11.move_mouse(x, y)
            x11.mouse_button("up", button)
            return
        if op == "click":
            for i in range(step.get("clicks") or 1):
                x11.click_mouse(x, y, button)
                if i < (step.get("clicks") or 1) - 1:
                    time.sleep(0.05)
            return


def _execute_drag(step: dict[str, Any]) -> None:
    button = step.get("button") or "left"
    n_steps = step.get("steps") or 10
    x1, y1, x2, y2 = step["x1"], step["y1"], step["x2"], step["y2"]
    if step.get("hwnd") is not None:
        hwnd = _resolve_hwnd(step["hwnd"])
        window = x11win.window_for(hwnd)
        rect = x11win.get_window_rect(hwnd)
        if rect["x"] is None:
            raise ChainStepError(f"Could not resolve window {hwnd}'s screen position.")
        rx, ry = rect["x"], rect["y"]
        x11.send_window_button(window, x1, y1, rx + x1, ry + y1, press=True, button=button)
        time.sleep(0.02)
        for i in range(1, n_steps + 1):
            t = i / n_steps
            ix, iy = round(x1 + (x2 - x1) * t), round(y1 + (y2 - y1) * t)
            x11.send_window_button(window, ix, iy, rx + ix, ry + iy, press=True, button=button)
            time.sleep(0.015)
        x11.send_window_button(window, x2, y2, rx + x2, ry + y2, press=False, button=button)
    else:
        x11.move_mouse(x1, y1)
        x11.mouse_button("down", button)
        time.sleep(0.02)
        for i in range(1, n_steps + 1):
            t = i / n_steps
            ix, iy = round(x1 + (x2 - x1) * t), round(y1 + (y2 - y1) * t)
            x11.move_mouse(ix, iy)
            time.sleep(0.015)
        x11.mouse_button("up", button)


def _execute_key(step: dict[str, Any]) -> None:
    key = step["key"]
    modifiers = step.get("modifiers") or []
    if step.get("hwnd") is not None:
        window = x11win.window_for(_resolve_hwnd(step["hwnd"]))
        x11.send_window_combo(window, key, modifiers)
    else:
        x11.press_key(key, modifiers)


def _execute_type(step: dict[str, Any]) -> None:
    text = step.get("text") or ""
    if step.get("hwnd") is not None:
        window = x11win.window_for(_resolve_hwnd(step["hwnd"]))
        x11.send_window_text(window, text)
    else:
        x11.type_text(text)


def _execute_scroll(step: dict[str, Any]) -> None:
    delta = step.get("delta") or 0
    if step.get("hwnd") is not None:
        hwnd = _resolve_hwnd(step["hwnd"])
        rect = x11win.get_window_rect(hwnd)
        if rect["x"] is not None:
            x11.move_mouse(rect["x"] + (step.get("x") or 0), rect["y"] + (step.get("y") or 0))
    x11.scroll_mouse(delta)


def _execute_wait_window(step: dict[str, Any]) -> None:
    pid = _resolve_pid(step["pid"]) if step.get("pid") is not None else None
    timeout_s = step["timeoutMs"] / 1000
    deadline = time.monotonic() + timeout_s
    while True:
        found = x11win.list_windows(step.get("titleFilter"), step.get("classNameFilter"), pid)
        if found:
            if step.get("as"):
                _pid_bindings[step["as"]] = found[0]["pid"]
            return
        if time.monotonic() >= deadline:
            raise ChainStepError(
                f"wait_window timed out after {step['timeoutMs']}ms "
                f"(titleFilter={step.get('titleFilter')!r}, classNameFilter={step.get('classNameFilter')!r}, pid={pid})"
            )
        time.sleep(0.1)


def _capture_region_rgb(x: int, y: int, width: int, height: int) -> bytes:
    import mss

    with mss.mss() as sct:
        return bytes(sct.grab({"left": x, "top": y, "width": width, "height": height}).rgb)


def _execute_wait_pixel(step: dict[str, Any]) -> None:
    hexcolor = step["color"].lstrip("#")
    target = (int(hexcolor[0:2], 16), int(hexcolor[2:4], 16), int(hexcolor[4:6], 16))
    tolerance = step.get("tolerance") or 0

    def matches(rgb: tuple[int, int, int]) -> bool:
        return all(abs(rgb[i] - target[i]) <= tolerance for i in range(3))

    timeout_s = step["timeoutMs"] / 1000
    deadline = time.monotonic() + timeout_s
    last: tuple[int, int, int] = (0, 0, 0)
    base_x, base_y = 0, 0
    if step.get("hwnd") is not None:
        hwnd = _resolve_hwnd(step["hwnd"])
        rect = x11win.get_window_rect(hwnd)
        if rect["x"] is None:
            raise ChainStepError(f"Could not resolve window {hwnd}'s screen position.")
        base_x, base_y = rect["x"], rect["y"]
    while True:
        px_x, px_y = base_x + step["x"], base_y + step["y"]
        data = _capture_region_rgb(px_x, px_y, 1, 1)
        last = (data[0], data[1], data[2])
        if matches(last):
            return
        if time.monotonic() >= deadline:
            raise ChainStepError(
                f"wait_pixel timed out after {step['timeoutMs']}ms at ({step['x']},{step['y']}), "
                f"last saw #{last[0]:02X}{last[1]:02X}{last[2]:02X}, "
                f"wanted #{target[0]:02X}{target[1]:02X}{target[2]:02X} +/-{tolerance}"
            )
        time.sleep(0.1)


def _hash_rgb(data: bytes) -> str:
    import hashlib

    return hashlib.md5(data).hexdigest()


def _execute_wait_idle(step: dict[str, Any]) -> None:
    if step.get("hwnd") is not None:
        hwnd = _resolve_hwnd(step["hwnd"])

        def capture() -> bytes:
            rect = x11win.get_window_rect(hwnd)
            if rect["x"] is None:
                raise ChainStepError(f"Could not resolve window {hwnd}'s screen position.")
            return _capture_region_rgb(rect["x"], rect["y"], rect["width"], rect["height"])
    else:
        def capture() -> bytes:
            return _capture_region_rgb(step["x"], step["y"], step["width"], step["height"])

    timeout_s = step["timeoutMs"] / 1000
    stable_s = step["stableMs"] / 1000
    deadline = time.monotonic() + timeout_s
    last_hash: str | None = None
    stable_since = time.monotonic()
    while True:
        h = _hash_rgb(capture())
        now = time.monotonic()
        if h != last_hash:
            last_hash = h
            stable_since = now
        elif now - stable_since >= stable_s:
            return
        if now >= deadline:
            raise ChainStepError(f"wait_idle timed out after {step['timeoutMs']}ms without {step['stableMs']}ms of stable content")
        time.sleep(0.15)


def _execute_launch(step: dict[str, Any]) -> None:
    path = step.get("path")
    if not path:
        raise ChainStepError("launch requires path")
    args = shlex.split(step["args"]) if step.get("args") else []
    proc = subprocess.Popen([path, *args], cwd=step.get("cwd") or None, start_new_session=True)
    if step.get("as"):
        _pid_bindings[step["as"]] = proc.pid


def _execute_kill(step: dict[str, Any]) -> None:
    import os
    import signal

    force = bool(step.get("force"))
    sig = signal.SIGKILL if force else signal.SIGTERM

    if step.get("imageName") and step.get("all"):
        subprocess.run(["pkill", "-9" if force else "-15", "-f", step["imageName"]], check=False)
        return
    if step.get("pid") is not None:
        pid = _resolve_pid(step["pid"])
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            pass
        return
    raise ChainStepError("kill requires either pid or imageName+all:true")


def _execute_restart(step: dict[str, Any]) -> None:
    _execute_kill(step)
    time.sleep(0.2)
    _execute_launch(step)


def _execute_checkpoint(step: dict[str, Any], out_file: str | None) -> str | None:
    if out_file is None:
        return None
    import mss
    import mss.tools

    if step.get("hwnd") is not None:
        hwnd = _resolve_hwnd(step["hwnd"])
        rect = x11win.get_window_rect(hwnd)
        if rect["x"] is None:
            return None
        region = {"left": rect["x"], "top": rect["y"], "width": rect["width"], "height": rect["height"]}
    else:
        region = None
    with mss.mss() as sct:
        shot = sct.grab(region) if region else sct.grab(sct.monitors[0])
        mss.tools.to_png(bytes(shot.rgb), (shot.width, shot.height), output=out_file)
    return out_file


def _run_step(step: dict[str, Any]) -> None:
    op = step["op"]
    retries = step.get("retries") or 0 if op in _RETRY_ELIGIBLE else 0
    retry_delay_s = (step.get("retryDelayMs") or 200) / 1000

    attempt = 0
    while True:
        try:
            if op in ("move", "click", "mouse_down", "mouse_up"):
                _execute_move_click_down_up(step)
            elif op == "drag":
                _execute_drag(step)
            elif op == "key":
                _execute_key(step)
            elif op == "type":
                _execute_type(step)
            elif op == "scroll":
                _execute_scroll(step)
            elif op == "sleep":
                time.sleep(step["ms"] / 1000)
            elif op == "wait_window":
                _execute_wait_window(step)
            elif op == "wait_pixel":
                _execute_wait_pixel(step)
            elif op == "wait_idle":
                _execute_wait_idle(step)
            elif op == "launch":
                _execute_launch(step)
            elif op == "kill":
                _execute_kill(step)
            elif op == "restart":
                _execute_restart(step)
            else:
                raise ChainStepError(f"Unknown op: {op}")
            return
        except Exception:
            if attempt < retries:
                attempt += 1
                time.sleep(retry_delay_s)
                continue
            raise


def run_chain_linux(steps: list[dict[str, Any]], out_file: str | None) -> dict[str, Any]:
    """Synchronous, blocking entry point -- callers (chain_plugin.py) run
    this via asyncio.to_thread for the WHOLE chain in one call, matching
    chain.exe's own "one process invocation per run_chain call" contract
    exactly (not one await per step)."""
    start = time.monotonic()
    result: dict[str, Any] = {"status": "ok", "completedSteps": 0, "failedAt": None, "elapsedMs": 0, "screenshot": None}

    for i, step in enumerate(steps):
        if step["op"] == "checkpoint":
            try:
                result["screenshot"] = _execute_checkpoint(step, out_file)
            except Exception:
                result["screenshot"] = None
            result["status"] = "paused"
            result["completedSteps"] = i + 1
            break
        try:
            _run_step(step)
            result["completedSteps"] = i + 1
        except Exception as exc:
            result["status"] = "failed"
            result["completedSteps"] = i
            result["failedAt"] = {"index": i, "op": step["op"], "reason": str(exc)}
            try:
                if out_file is not None:
                    import mss
                    import mss.tools

                    with mss.mss() as sct:
                        shot = sct.grab(sct.monitors[0])
                        mss.tools.to_png(bytes(shot.rgb), (shot.width, shot.height), output=out_file)
                    result["screenshot"] = out_file
            except Exception:
                pass
            break

    result["elapsedMs"] = round((time.monotonic() - start) * 1000)
    return result
