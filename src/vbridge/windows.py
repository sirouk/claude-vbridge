"""Opt-in Windows desktop actions. No UI or DLL work happens at import time.

Requires Windows 10+ and a connected interactive WinSta0 / Default desktop.
UAC secure desktops, session 0, locked/disconnected sessions and elevated target
input are not bypassed. SendInput acceptance is not proof of application delivery.
Coordinates are physical desktop pixels, including negative monitor origins.
Application names are executable basenames of visible running windows, not
installed application names. focus_app never launches a program or runs a shell.
"""

from __future__ import annotations

import asyncio
import base64
import ctypes
import io
import math
import ntpath
import sys
from contextlib import contextmanager
from functools import lru_cache

from PIL import Image, ImageGrab

MAX_TEXT = 16000
MAX_WINDOWS = 2048
MAX_DISPLAYS = 32
MAX_CAPTURE_PIXELS = 64_000_000
DWORD = ctypes.c_uint32
LONG = ctypes.c_int32
WORD = ctypes.c_uint16
BOOL = ctypes.c_int32
HANDLE = ctypes.c_void_p
ULONG_PTR = ctypes.c_size_t
WINFUNCTYPE = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)


class POINT(ctypes.Structure):
    _fields_ = [("x", LONG), ("y", LONG)]


class RECT(ctypes.Structure):
    _fields_ = [("left", LONG), ("top", LONG), ("right", LONG), ("bottom", LONG)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", DWORD), ("rcMonitor", RECT), ("rcWork", RECT), ("dwFlags", DWORD)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", LONG),
        ("dy", LONG),
        ("mouseData", DWORD),
        ("dwFlags", DWORD),
        ("time", DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", WORD),
        ("wScan", WORD),
        ("dwFlags", DWORD),
        ("time", DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", DWORD), ("wParamL", WORD), ("wParamH", WORD)]


class INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("event",)
    _fields_ = [("type", DWORD), ("event", INPUT_UNION)]


MONITORPROC = WINFUNCTYPE(BOOL, HANDLE, HANDLE, ctypes.POINTER(RECT), ctypes.c_ssize_t)
ENUMWINDOWSPROC = WINFUNCTYPE(BOOL, HANDLE, ctypes.c_ssize_t)


class _Native:
    """Pointer-sized handles and explicit Win32 prototypes on both x86 and x64."""

    def __init__(self):
        if sys.platform != "win32":
            raise RuntimeError("This tool requires Windows")
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.wtsapi32 = ctypes.WinDLL("wtsapi32", use_last_error=True)
        specs = {
            "user32": {
                "OpenInputDesktop": ([DWORD, BOOL, DWORD], HANDLE),
                "CloseDesktop": ([HANDLE], BOOL),
                "GetThreadDesktop": ([DWORD], HANDLE),
                "GetProcessWindowStation": ([], HANDLE),
                "GetUserObjectInformationW": (
                    [HANDLE, ctypes.c_int, HANDLE, DWORD, ctypes.POINTER(DWORD)],
                    BOOL,
                ),
                "SetThreadDpiAwarenessContext": ([HANDLE], HANDLE),
                "EnumDisplayMonitors": ([HANDLE, ctypes.POINTER(RECT), MONITORPROC, ctypes.c_ssize_t], BOOL),
                "GetMonitorInfoW": ([HANDLE, ctypes.POINTER(MONITORINFO)], BOOL),
                "GetSystemMetrics": ([ctypes.c_int], ctypes.c_int),
                "SendInput": ([ctypes.c_uint, ctypes.POINTER(INPUT), ctypes.c_int], ctypes.c_uint),
                "EnumWindows": ([ENUMWINDOWSPROC, ctypes.c_ssize_t], BOOL),
                "IsWindowVisible": ([HANDLE], BOOL),
                "GetWindow": ([HANDLE, ctypes.c_uint], HANDLE),
                "GetWindowThreadProcessId": ([HANDLE, ctypes.POINTER(DWORD)], DWORD),
                "GetForegroundWindow": ([], HANDLE),
                "IsIconic": ([HANDLE], BOOL),
                "ShowWindowAsync": ([HANDLE, ctypes.c_int], BOOL),
                "SetForegroundWindow": ([HANDLE], BOOL),
                "CreateWindowExW": (
                    [
                        DWORD,
                        ctypes.c_wchar_p,
                        ctypes.c_wchar_p,
                        DWORD,
                        ctypes.c_int,
                        ctypes.c_int,
                        ctypes.c_int,
                        ctypes.c_int,
                        HANDLE,
                        HANDLE,
                        HANDLE,
                        HANDLE,
                    ],
                    HANDLE,
                ),
                "DestroyWindow": ([HANDLE], BOOL),
                "OpenClipboard": ([HANDLE], BOOL),
                "CloseClipboard": ([], BOOL),
                "EmptyClipboard": ([], BOOL),
                "IsClipboardFormatAvailable": ([ctypes.c_uint], BOOL),
                "GetClipboardData": ([ctypes.c_uint], HANDLE),
                "SetClipboardData": ([ctypes.c_uint, HANDLE], HANDLE),
            },
            "kernel32": {
                "GetCurrentProcessId": ([], DWORD),
                "GetCurrentThreadId": ([], DWORD),
                "ProcessIdToSessionId": ([DWORD, ctypes.POINTER(DWORD)], BOOL),
                "OpenProcess": ([DWORD, BOOL, DWORD], HANDLE),
                "CloseHandle": ([HANDLE], BOOL),
                "QueryFullProcessImageNameW": (
                    [HANDLE, DWORD, ctypes.c_wchar_p, ctypes.POINTER(DWORD)],
                    BOOL,
                ),
                "GlobalAlloc": ([ctypes.c_uint, ctypes.c_size_t], HANDLE),
                "GlobalLock": ([HANDLE], HANDLE),
                "GlobalUnlock": ([HANDLE], BOOL),
                "GlobalSize": ([HANDLE], ctypes.c_size_t),
                "GlobalFree": ([HANDLE], HANDLE),
            },
            "wtsapi32": {
                "WTSQuerySessionInformationW": (
                    [HANDLE, DWORD, ctypes.c_int, ctypes.POINTER(HANDLE), ctypes.POINTER(DWORD)],
                    BOOL,
                ),
                "WTSFreeMemory": ([HANDLE], None),
            },
        }
        for library, funcs in specs.items():
            for name, (args, result) in funcs.items():
                function = getattr(getattr(self, library), name)
                function.argtypes = args
                function.restype = result


@lru_cache(maxsize=1)
def _native():
    return _Native()


def _object_name(native, handle):
    buf = ctypes.create_unicode_buffer(256)
    needed = DWORD()
    if not handle or not native.user32.GetUserObjectInformationW(
        handle, 2, buf, ctypes.sizeof(buf), ctypes.byref(needed)
    ):
        return None
    return buf.value


def permission_status() -> dict:
    """Non-capturing preflight only; target integrity/UIPI cannot be inferred."""
    native = _native()
    session = DWORD()
    active = False
    reason = "No connected interactive user session"
    if (
        native.kernel32.ProcessIdToSessionId(native.kernel32.GetCurrentProcessId(), ctypes.byref(session))
        and session.value != 0
    ):
        ptr, size = HANDLE(), DWORD()
        if native.wtsapi32.WTSQuerySessionInformationW(
            None, session.value, 8, ctypes.byref(ptr), ctypes.byref(size)
        ):
            try:
                # WTSConnectState: WTSActive == 0. Buffer is owned by WTS.
                active = bool(
                    ptr.value
                    and size.value >= 4
                    and ctypes.cast(ptr, ctypes.POINTER(ctypes.c_int32)).contents.value == 0
                )
            finally:
                if ptr.value:
                    native.wtsapi32.WTSFreeMemory(ptr)
    desktop = None
    allowed = False
    if active:
        station = _object_name(native, native.user32.GetProcessWindowStation())
        current = _object_name(native, native.user32.GetThreadDesktop(native.kernel32.GetCurrentThreadId()))
        handle = native.user32.OpenInputDesktop(0, False, 0x0001)  # DESKTOP_READOBJECTS
        if handle:
            try:
                desktop = _object_name(native, handle)
            finally:
                native.user32.CloseDesktop(handle)
        allowed = station == "WinSta0" and current == "Default" and desktop == "Default"
        reason = "Interactive Default desktop available" if allowed else "Locked, secure or non-input desktop"
    return {
        "platform": "windows",
        "accessibility": allowed,
        "screen_recording": allowed,
        "interactive_session": active,
        "input_desktop": desktop,
        "reason": reason,
        "limitations": "Preflight only; UAC/UIPI and target focus may still prevent delivery or capture.",
    }


def _require_ui():
    status = permission_status()
    if not status["accessibility"]:
        raise PermissionError(
            status["reason"] + "; use a connected Windows user desktop, not a service/UAC desktop"
        )


@contextmanager
def _dpi_context():
    native = _native()
    # Per-thread PMv2 keeps monitor bounds, SendInput and Pillow in physical pixels
    # without permanently changing the host process DPI mode.
    previous = native.user32.SetThreadDpiAwarenessContext(HANDLE(-4))
    if not previous:
        raise RuntimeError("Cannot establish physical-pixel DPI context (Windows 10+ required)")
    try:
        yield native
    finally:
        if not native.user32.SetThreadDpiAwarenessContext(previous):
            raise RuntimeError("Cannot restore thread DPI context")


def displays() -> list[dict]:
    _require_ui()
    results, errors = [], []
    with _dpi_context() as native:

        @MONITORPROC
        def collect(handle, dc, rectangle, data):
            if len(results) >= MAX_DISPLAYS:
                errors.append("Too many active displays")
                return False
            info = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
            if not native.user32.GetMonitorInfoW(handle, ctypes.byref(info)):
                errors.append("Cannot read monitor bounds")
                return False
            r = info.rcMonitor
            results.append(
                {
                    "id": int(handle),
                    "x": r.left,
                    "y": r.top,
                    "width_points": r.right - r.left,
                    "height_points": r.bottom - r.top,
                    "width_pixels": r.right - r.left,
                    "height_pixels": r.bottom - r.top,
                    "coordinate_units": "physical_pixels",
                    "primary": bool(info.dwFlags & 1),
                }
            )
            return True

        okay = native.user32.EnumDisplayMonitors(None, None, collect, 0)
    if errors or not okay or not results:
        raise RuntimeError(errors[0] if errors else "Cannot enumerate active displays")
    results.sort(key=lambda d: (not d["primary"], d["x"], d["y"]))
    for index, result in enumerate(results, 1):
        result["index"] = index
    return results


def _keyboard(vk=0, scan=0, flags=0):
    event = INPUT(type=1)
    event.ki = KEYBDINPUT(vk, scan, flags, 0, 0)
    return event


def _mouse(x, y, flags):
    event = INPUT(type=0)
    event.mi = MOUSEINPUT(x, y, 0, flags, 0, 0)
    return event


def _send_events(events, releases=()):
    _require_ui()
    native = _native()
    payload = (INPUT * len(events))(*events)
    accepted = native.user32.SendInput(len(payload), payload, ctypes.sizeof(INPUT))
    if accepted != len(payload):
        # Partial input can leave modifiers/buttons held. Best-effort releases;
        # this does not turn failure into success or bypass integrity controls.
        if accepted and releases:
            cleanup = (INPUT * len(releases))(*releases)
            native.user32.SendInput(len(cleanup), cleanup, ctypes.sizeof(INPUT))
        raise RuntimeError(
            f"SendInput accepted {accepted}/{len(payload)} events; delivery failed or was blocked by UAC/UIPI"
        )


def click(x: float, y: float, button: str = "left", clicks: int = 1) -> dict:
    if button not in ("left", "right") or clicks not in (1, 2):
        raise ValueError("button is left/right; clicks is 1/2")
    if not math.isfinite(x) or not math.isfinite(y):
        raise ValueError("coordinates must be finite")
    x, y = round(x), round(y)
    screens = displays()
    if not any(
        d["x"] <= x < d["x"] + d["width_pixels"] and d["y"] <= y < d["y"] + d["height_pixels"]
        for d in screens
    ):
        raise ValueError("coordinates are outside active displays")
    with _dpi_context() as native:
        left, top, width, height = [native.user32.GetSystemMetrics(i) for i in (76, 77, 78, 79)]
        if width <= 1 or height <= 1:
            raise RuntimeError("Invalid virtual desktop bounds")
        px = round((x - left) * 65535 / (width - 1))
        py = round((y - top) * 65535 / (height - 1))
        if not 0 <= px <= 65535 or not 0 <= py <= 65535:
            raise RuntimeError("Display configuration changed; observe again before clicking")
        down, up = (0x0002, 0x0004) if button == "left" else (0x0008, 0x0010)
        move = 0x0001 | 0x8000 | 0x4000  # MOVE | ABSOLUTE | VIRTUALDESK
        events = [_mouse(px, py, move)]
        events += [_mouse(0, 0, flag) for _ in range(clicks) for flag in (down, up)]
        _send_events(events, [_mouse(0, 0, up)])
    return {
        "posted": True,
        "x": x,
        "y": y,
        "button": button,
        "clicks": clicks,
        "delivery_verified": False,
        "note": "SendInput accepted input; observe UI to verify the result.",
    }


async def type_text(text: str) -> dict:
    if len(text) > MAX_TEXT or "\x00" in text:
        raise ValueError("text must be <=16000 chars with no NUL")
    raw = text.encode("utf-16-le")

    def send():
        _require_ui()
        events = []
        for index in range(0, len(raw), 2):
            unit = int.from_bytes(raw[index : index + 2], "little")
            events += [_keyboard(scan=unit, flags=4), _keyboard(scan=unit, flags=4 | 2)]
            if len(events) == 256:
                _send_events(events, [_keyboard(scan=unit, flags=4 | 2)])
                events = []
        if events:
            _send_events(events, [events[-1]])

    await asyncio.to_thread(send)
    return {
        "typed_chars": len(text),
        "delivery_verified": False,
        "note": "Unicode SendInput accepted input; application delivery is not independently verified.",
    }


KEYS = {
    "return": 0x0D,
    "enter": 0x0D,
    "tab": 0x09,
    "space": 0x20,
    "delete": 0x08,
    "backspace": 0x08,
    "forwarddelete": 0x2E,
    "escape": 0x1B,
    "left": 0x25,
    "right": 0x27,
    "down": 0x28,
    "up": 0x26,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pagedown": 0x22,
}
MODIFIERS = {
    "command": 0x5B,
    "win": 0x5B,
    "shift": 0x10,
    "option": 0x12,
    "alt": 0x12,
    "control": 0x11,
    "ctrl": 0x11,
}
EXTENDED_KEYS = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2E, 0x5B}


def _vk_event(vk, up=False):
    return _keyboard(vk=vk, flags=(1 if vk in EXTENDED_KEYS else 0) | (2 if up else 0))


async def press_key(key: str, modifiers: list[str] | None = None) -> dict:
    mods = [m.lower() for m in (modifiers or [])]
    if any(m not in MODIFIERS for m in mods):
        raise ValueError("modifiers: command/win, shift, option/alt, control/ctrl")
    codes = [MODIFIERS[m] for m in mods]
    if len(codes) != len(set(codes)):
        raise ValueError("duplicate modifier (including aliases)")
    vk = KEYS.get(key.lower())
    if vk is None:
        if len(key) != 1 or not key.isascii() or not key.isalnum():
            raise ValueError("key must be a supported named key or one ASCII letter/digit")
        vk = ord(key.upper())
    if vk in codes:
        raise ValueError("key duplicates a modifier")
    events = [_vk_event(code) for code in codes] + [_vk_event(vk), _vk_event(vk, True)]
    releases = [_vk_event(code, True) for code in reversed(codes)]
    await asyncio.to_thread(_send_events, events + releases, [_vk_event(vk, True), *releases])
    return {
        "posted": True,
        "key": key,
        "modifiers": mods,
        "delivery_verified": False,
        "note": "SendInput accepted input; focus and application delivery are not verified.",
    }


def _window_apps():
    _require_ui()
    native = _native()
    windows, overflow = [], []
    visited = 0

    @ENUMWINDOWSPROC
    def collect(hwnd, data):
        nonlocal visited
        visited += 1
        if visited > MAX_WINDOWS:
            overflow.append(True)
            return False
        if not native.user32.IsWindowVisible(hwnd) or native.user32.GetWindow(hwnd, 4):
            return True  # owned popups are not independently activatable applications
        pid = DWORD()
        native.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        process = native.kernel32.OpenProcess(0x1000, False, pid.value)
        if process:
            try:
                buf, count = ctypes.create_unicode_buffer(32768), DWORD(32768)
                if native.kernel32.QueryFullProcessImageNameW(process, 0, buf, ctypes.byref(count)):
                    name = ntpath.basename(buf.value)
                    if name and len(name) <= 256:
                        windows.append((int(hwnd), name))
            finally:
                native.kernel32.CloseHandle(process)
        return True

    okay = native.user32.EnumWindows(collect, 0)
    if overflow or not okay:
        raise RuntimeError("Cannot enumerate application windows within bounded limit")
    return windows


async def list_apps() -> list[str]:
    """Visible running executable basenames; titles/private window text are not read."""
    windows = await asyncio.to_thread(_window_apps)
    names = {name.casefold(): name for _, name in windows}
    return sorted(names.values(), key=str.casefold)


async def focus_app(name: str) -> dict:
    """Activate a running executable name returned by list_apps; never launches."""
    if not name or len(name) > 256 or any(c in name for c in "\x00/\\") or name.startswith("-"):
        raise ValueError("name must be a running executable basename, e.g. notepad.exe")

    def focus():
        windows = _window_apps()
        matches = [hwnd for hwnd, app in windows if app.casefold() == name.casefold()]
        if not matches:
            raise ValueError(
                "No visible running app matches this name; use list_apps (no program is launched)"
            )
        native = _native()
        target = matches[0]  # EnumWindows Z order: prefer the topmost matching window.
        _require_ui()
        if native.user32.IsIconic(target):
            native.user32.ShowWindowAsync(target, 9)  # SW_RESTORE; asynchronous request only
        accepted = bool(native.user32.SetForegroundWindow(target))
        confirmed = native.user32.GetForegroundWindow() == target
        if not accepted or not confirmed:
            raise RuntimeError(
                "Windows denied or did not confirm foreground activation; focus manually and observe"
            )
        return {
            "requested_app": name,
            "activation_requested": True,
            "foreground_confirmed": True,
            "note": "Window foreground confirmed at this instant; focused control is not verified.",
        }

    return await asyncio.to_thread(focus)


@contextmanager
def _clipboard(owner=None):
    native = _native()
    if not native.user32.OpenClipboard(owner):
        raise RuntimeError("Clipboard is busy or inaccessible; retry after the other application closes it")
    try:
        yield native
    finally:
        if not native.user32.CloseClipboard():
            raise RuntimeError("Cannot close clipboard")


def _clipboard_read():
    _require_ui()
    with _clipboard() as native:
        if not native.user32.IsClipboardFormatAvailable(13):  # CF_UNICODETEXT
            return {"text": "", "truncated": False}
        handle = native.user32.GetClipboardData(13)
        size = native.kernel32.GlobalSize(handle) if handle else 0
        if not handle or size < 2 or size % 2:
            raise RuntimeError("Clipboard Unicode data is invalid")
        pointer = native.kernel32.GlobalLock(handle)
        if not pointer:
            raise RuntimeError("Cannot lock clipboard data")
        try:
            # At most two UTF-16 units per character plus terminator; bounded read.
            raw = ctypes.string_at(pointer, min(size, (2 * MAX_TEXT + 1) * 2))
        finally:
            native.kernel32.GlobalUnlock(handle)
        end = next((i for i in range(0, len(raw), 2) if raw[i : i + 2] == b"\0\0"), len(raw))
        text = raw[:end].decode("utf-16-le", errors="replace")
        truncated = len(text) > MAX_TEXT or (end == len(raw) and len(raw) < size)
        if end == len(raw) and size <= len(raw):
            raise RuntimeError("Clipboard Unicode data is not NUL-terminated")
        return {"text": text[:MAX_TEXT], "truncated": truncated}


async def clipboard_read() -> dict:
    return await asyncio.to_thread(_clipboard_read)


def _clipboard_write(text):
    _require_ui()
    native = _native()
    raw = text.encode("utf-16-le") + b"\0\0"
    allocation = native.kernel32.GlobalAlloc(0x0002, len(raw))  # GMEM_MOVEABLE
    if not allocation:
        raise RuntimeError("Cannot allocate clipboard data")
    owner = None
    try:
        pointer = native.kernel32.GlobalLock(allocation)
        if not pointer:
            raise RuntimeError("Cannot lock clipboard allocation")
        try:
            ctypes.memmove(pointer, raw, len(raw))
        finally:
            native.kernel32.GlobalUnlock(allocation)
        # A real HWND is required: OpenClipboard(NULL) + EmptyClipboard makes
        # SetClipboardData fail. STATIC is a built-in class; this HWND is hidden.
        owner = native.user32.CreateWindowExW(0, "STATIC", "", 0, 0, 0, 0, 0, None, None, None, None)
        if not owner:
            raise RuntimeError("Cannot create clipboard owner window")
        with _clipboard(owner):
            _require_ui()
            if not native.user32.EmptyClipboard():
                raise RuntimeError("Cannot clear clipboard")
            if not native.user32.SetClipboardData(13, allocation):
                raise RuntimeError("Cannot set clipboard data; previous contents may have been cleared")
            allocation = None  # Ownership transferred to the OS. Never free it now.
    finally:
        if owner:
            native.user32.DestroyWindow(owner)
        if allocation:
            native.kernel32.GlobalFree(allocation)
    return {"written_chars": len(text)}


async def clipboard_write(text: str) -> dict:
    if len(text) > MAX_TEXT or "\x00" in text:
        raise ValueError("clipboard text must be <=16000 chars with no NUL")
    text.encode("utf-16-le")  # reject unpaired surrogates before touching clipboard
    return await asyncio.to_thread(_clipboard_write, text)


def _screenshot(display, max_width):
    screens = displays()
    if not 1 <= display <= len(screens):
        raise ValueError("display index is not active")
    info = screens[display - 1]
    width, height = info["width_pixels"], info["height_pixels"]
    if width <= 0 or height <= 0 or width * height > MAX_CAPTURE_PIXELS:
        raise ValueError("Display exceeds the bounded capture size")
    # Pillow's Windows backend captures the whole virtual desktop before crop.
    virtual_width = max(d["x"] + d["width_pixels"] for d in screens) - min(d["x"] for d in screens)
    virtual_height = max(d["y"] + d["height_pixels"] for d in screens) - min(d["y"] for d in screens)
    if virtual_width * virtual_height > MAX_CAPTURE_PIXELS:
        raise ValueError("Virtual desktop exceeds the bounded capture size")
    bbox = (info["x"], info["y"], info["x"] + width, info["y"] + height)
    _require_ui()
    with _dpi_context():
        # No persistent files or history. all_screens supports negative origins.
        image = ImageGrab.grab(bbox=bbox, all_screens=True)
    try:
        if image.size != (width, height):
            raise RuntimeError(
                "Display geometry changed during capture; retry after observing display bounds"
            )
        original = image.size
        image.thumbnail((max_width, max_width), Image.Resampling.LANCZOS)
        output = io.BytesIO()
        rgb = image.convert("RGB")
        try:
            rgb.save(output, format="JPEG", quality=75, optimize=True)
        finally:
            rgb.close()
        meta = {
            "width": image.width,
            "height": image.height,
            "original_width": original[0],
            "original_height": original[1],
            "display": info,
            "coordinate_mapping": "Global physical pixel x = display.x + image_x * "
            "display.width_pixels / width; same for y. width_points also means pixels.",
            "privacy": "Visible screen contents are sent to the requesting Claude/MCP client.",
            "limitations": "Protected content may be black; capture does not bypass UAC/secure desktops.",
        }
        return {
            "data": base64.b64encode(output.getvalue()).decode(),
            "mimeType": "image/jpeg",
            "metadata": meta,
        }
    finally:
        image.close()


async def screenshot(display: int = 1, max_width: int = 1600) -> dict:
    if isinstance(display, bool) or not isinstance(display, int) or display < 1:
        raise ValueError("display index must be a positive integer")
    if isinstance(max_width, bool) or not isinstance(max_width, int) or not 320 <= max_width <= 2400:
        raise ValueError("max_width must be320..2400")
    return await asyncio.to_thread(_screenshot, display, max_width)
