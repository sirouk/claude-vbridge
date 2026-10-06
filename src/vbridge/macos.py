"""Opt-in macOS UI actions; effective OS permissions are never bypassed.

No private data is logged. Screenshots are transient and sent to the requesting
MCP client; callers must understand that visible data leaves the Mac.
"""

from __future__ import annotations

import asyncio
import base64
import ctypes
import io
import json
import os
import signal
import sys
import tempfile
from pathlib import Path

from PIL import Image as PILImage

MAX_TEXT = 16000


def _mac_only():
    if sys.platform != "darwin":
        raise RuntimeError("This tool requires macOS")


def _framework(name):
    _mac_only()
    return ctypes.CDLL(f"/System/Library/Frameworks/{name}.framework/{name}")


def permission_status() -> dict:
    """Preflight checks only. No prompts, captures or private data reads."""
    _mac_only()
    ax = _framework("ApplicationServices")
    ax.AXIsProcessTrusted.argtypes = []
    ax.AXIsProcessTrusted.restype = ctypes.c_bool
    cg = _framework("CoreGraphics")
    cg.CGPreflightScreenCaptureAccess.argtypes = []
    cg.CGPreflightScreenCaptureAccess.restype = ctypes.c_bool
    return {
        "accessibility": bool(ax.AXIsProcessTrusted()),
        "screen_recording": bool(cg.CGPreflightScreenCaptureAccess()),
    }


async def _exec(argv: list[str], input_data: bytes | None = None, timeout_s: int = 15) -> str:
    _mac_only()
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE if input_data is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )

    async def read_bounded(stream, limit):
        result = bytearray()
        while data := await stream.read(8192):
            result.extend(data[: max(0, limit - len(result))])
        return bytes(result)

    async def write_input():
        if proc.stdin is not None:
            proc.stdin.write(input_data or b"")
            await proc.stdin.drain()
            proc.stdin.close()

    work = asyncio.gather(read_bounded(proc.stdout, 128000), read_bounded(proc.stderr, 4000), write_input())
    try:
        async with asyncio.timeout(timeout_s):
            out, err, _ = await work
            await proc.wait()
    except BaseException:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await proc.wait()
        work.cancel()
        await asyncio.gather(work, return_exceptions=True)
        raise
    if proc.returncode:
        # Native error text, not application output, on failures.
        raise RuntimeError(err.decode(errors="replace")[:1000] or f"Command failed ({proc.returncode})")
    return out.decode(errors="replace")


async def applescript(script: str, args: list[str] | None = None) -> str:
    if not script.strip() or len(script) > 32000:
        raise ValueError("script must be non-empty and <=32000 characters")
    return await _exec(["/usr/bin/osascript", "-e", script, *(args or [])])


async def list_apps() -> list[str]:
    result = await applescript(
        'tell application "System Events" to get name of every application process whose background only is false'
    )
    return [name.strip() for name in result.strip().split(", ") if name.strip()]


async def focus_app(name: str) -> dict:
    if not name or len(name) > 256 or name.startswith("-"):
        raise ValueError("name must identify an installed application")
    await _exec(["/usr/bin/open", "-a", name])
    return {"requested_app": name, "activation_requested": True}


def _require_accessibility():
    if not permission_status()["accessibility"]:
        raise PermissionError("Accessibility is not granted. Enable the bridge runtime in System Settings.")


class CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


class CGSize(ctypes.Structure):
    _fields_ = [("width", ctypes.c_double), ("height", ctypes.c_double)]


class CGRect(ctypes.Structure):
    _fields_ = [("origin", CGPoint), ("size", CGSize)]


def displays() -> list[dict]:
    cg = _framework("CoreGraphics")
    cg.CGGetActiveDisplayList.argtypes = [
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_uint32),
    ]
    cg.CGGetActiveDisplayList.restype = ctypes.c_int32
    cg.CGDisplayBounds.argtypes = [ctypes.c_uint32]
    cg.CGDisplayBounds.restype = CGRect
    cg.CGDisplayPixelsWide.argtypes = [ctypes.c_uint32]
    cg.CGDisplayPixelsWide.restype = ctypes.c_size_t
    cg.CGDisplayPixelsHigh.argtypes = [ctypes.c_uint32]
    cg.CGDisplayPixelsHigh.restype = ctypes.c_size_t
    ids = (ctypes.c_uint32 * 32)()
    count = ctypes.c_uint32()
    if cg.CGGetActiveDisplayList(32, ids, ctypes.byref(count)) != 0:
        raise RuntimeError("Cannot enumerate active displays")
    result = []
    for index in range(count.value):
        bounds = cg.CGDisplayBounds(ids[index])
        result.append(
            {
                "index": index + 1,
                "id": ids[index],
                "x": bounds.origin.x,
                "y": bounds.origin.y,
                "width_points": bounds.size.width,
                "height_points": bounds.size.height,
                "width_pixels": cg.CGDisplayPixelsWide(ids[index]),
                "height_pixels": cg.CGDisplayPixelsHigh(ids[index]),
            }
        )
    return result


def click(x: float, y: float, button: str = "left", clicks: int = 1) -> dict:
    """Global desktop coordinates in points (not resized screenshot pixels)."""
    _require_accessibility()
    if button not in ("left", "right") or clicks not in (1, 2):
        raise ValueError("button is left/right; clicks is 1/2")
    import math

    if not math.isfinite(x) or not math.isfinite(y):
        raise ValueError("coordinates must be finite")
    if not any(
        d["x"] <= x < d["x"] + d["width_points"] and d["y"] <= y < d["y"] + d["height_points"]
        for d in displays()
    ):
        raise ValueError("coordinates are outside active displays")
    cg = _framework("CoreGraphics")
    cf = _framework("CoreFoundation")
    cg.CGEventCreateMouseEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint32, CGPoint, ctypes.c_uint32]
    cg.CGEventCreateMouseEvent.restype = ctypes.c_void_p
    cg.CGEventSetIntegerValueField.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int64]
    cg.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
    cf.CFRelease.argtypes = [ctypes.c_void_p]
    down, up, mouse_button = (1, 2, 0) if button == "left" else (3, 4, 1)
    for count in range(1, clicks + 1):
        for kind in (down, up):
            event = cg.CGEventCreateMouseEvent(None, kind, CGPoint(x, y), mouse_button)
            if not event:
                raise RuntimeError("Cannot create mouse event")
            try:
                cg.CGEventSetIntegerValueField(event, 1, count)  # kCGMouseEventClickState
                cg.CGEventPost(0, event)  # kCGHIDEventTap
            finally:
                cf.CFRelease(event)
    return {"posted": True, "x": x, "y": y, "button": button, "clicks": clicks}


async def type_text(text: str) -> dict:
    _require_accessibility()
    if len(text) > MAX_TEXT or "\x00" in text:
        raise ValueError("text must be <=16000 chars with no NUL")
    await applescript(
        'on run argv\ntell application "System Events" to keystroke (item 1 of argv)\nend run', [text]
    )
    return {
        "typed_chars": len(text),
        "note": "Sent to the currently focused UI element; delivery not independently verified.",
    }


KEYS = {
    "return": 36,
    "enter": 36,
    "tab": 48,
    "space": 49,
    "delete": 51,
    "escape": 53,
    "left": 123,
    "right": 124,
    "down": 125,
    "up": 126,
    "home": 115,
    "end": 119,
    "pageup": 116,
    "pagedown": 121,
}
MODIFIERS = {
    "command": "command down",
    "shift": "shift down",
    "option": "option down",
    "control": "control down",
}


async def press_key(key: str, modifiers: list[str] | None = None) -> dict:
    _require_accessibility()
    mods = [m.lower() for m in (modifiers or [])]
    if any(m not in MODIFIERS for m in mods) or len(mods) != len(set(mods)):
        raise ValueError("modifiers: command, shift, option, control (no duplicates)")
    suffix = " using {" + ", ".join(MODIFIERS[m] for m in mods) + "}" if mods else ""
    if key.lower() in KEYS:
        action = f"key code {KEYS[key.lower()]}"
    elif len(key) == 1 and key.isascii() and key.isalnum():
        action = "keystroke " + json.dumps(key)
    else:
        raise ValueError("key must be a supported named key or one ASCII letter/digit")
    await applescript('tell application "System Events" to ' + action + suffix)
    return {"posted": True, "key": key, "modifiers": mods}


async def clipboard_read() -> dict:
    result = await _exec(["/usr/bin/pbpaste"])
    return {"text": result[:MAX_TEXT], "truncated": len(result) > MAX_TEXT}


async def clipboard_write(text: str) -> dict:
    if len(text) > MAX_TEXT:
        raise ValueError("clipboard text exceeds16000 characters")
    await _exec(["/usr/bin/pbcopy"], input_data=text.encode())
    return {"written_chars": len(text)}


def encode_screenshot(raw: bytes, max_width: int = 1600) -> tuple[bytes, dict]:
    if not 320 <= max_width <= 2400:
        raise ValueError("max_width must be320..2400")
    with PILImage.open(io.BytesIO(raw)) as image:
        original = image.size
        # Cap both dimensions so portrait/rotated displays cannot create giant payloads.
        image.thumbnail((max_width, max_width), PILImage.Resampling.LANCZOS)
        result = io.BytesIO()
        image.convert("RGB").save(result, format="JPEG", quality=75, optimize=True)
        meta = {
            "width": image.width,
            "height": image.height,
            "original_width": original[0],
            "original_height": original[1],
        }
    return result.getvalue(), meta


async def screenshot(display: int = 1, max_width: int = 1600) -> dict:
    if not permission_status()["screen_recording"]:
        raise PermissionError("Screen Recording is not granted. Enable it for the bridge runtime.")
    screens = displays()
    if not 1 <= display <= len(screens):
        raise ValueError("display index is not active")
    if not 320 <= max_width <= 2400:
        raise ValueError("max_width must be320..2400")
    # Private temporary storage, deleted on success and failure. No saved screen history.
    with tempfile.TemporaryDirectory(prefix="vbridge-screen-") as directory:
        target = Path(directory) / "capture.png"
        await _exec(["/usr/sbin/screencapture", "-x", "-t", "png", "-D", str(display), str(target)])
        raw = await asyncio.to_thread(target.read_bytes)
        data, meta = await asyncio.to_thread(encode_screenshot, raw, max_width)
    info = screens[display - 1]
    meta.update(
        {
            "display": info,
            "coordinate_mapping": "Global point x = display.x + image_x * display.width_points / width; same for y.",
            "privacy": "Visible screen contents are sent to the requesting Claude/MCP client.",
        }
    )
    return {"data": base64.b64encode(data).decode(), "mimeType": "image/jpeg", "metadata": meta}
