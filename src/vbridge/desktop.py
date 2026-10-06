"""Platform dispatch for opt-in UI tools. Native modules remain import-safe.

macOS coordinates are points; Windows coordinates are physical pixels. Callers
must use screenshot metadata, not resized image pixels, for global clicks.
Unsupported platforms fail closed. AppleScript is only available on macOS.
"""

from __future__ import annotations

import importlib
import sys

PLATFORM = sys.platform
UI_SUPPORTED = PLATFORM in {"darwin", "win32"}
APPLESCRIPT_SUPPORTED = PLATFORM == "darwin"


def supports_ui() -> bool:
    return UI_SUPPORTED


def supports_applescript() -> bool:
    return APPLESCRIPT_SUPPORTED


def _backend():
    if PLATFORM == "darwin":
        return importlib.import_module(".macos", __package__)
    if PLATFORM == "win32":
        return importlib.import_module(".windows", __package__)
    raise RuntimeError("UI tools require macOS or Windows; this platform is unsupported")


def permission_status() -> dict:
    if not UI_SUPPORTED:
        return {
            "platform": PLATFORM,
            "accessibility": False,
            "screen_recording": False,
            "ui_supported": False,
            "reason": "UI tools require macOS or Windows",
        }
    return _backend().permission_status()


def displays() -> list[dict]:
    return _backend().displays()


def click(x: float, y: float, button: str = "left", clicks: int = 1) -> dict:
    return _backend().click(x, y, button, clicks)


async def type_text(text: str) -> dict:
    return await _backend().type_text(text)


async def press_key(key: str, modifiers: list[str] | None = None) -> dict:
    return await _backend().press_key(key, modifiers)


async def list_apps() -> list[str]:
    return await _backend().list_apps()


async def focus_app(name: str) -> dict:
    return await _backend().focus_app(name)


async def clipboard_read() -> dict:
    return await _backend().clipboard_read()


async def clipboard_write(text: str) -> dict:
    return await _backend().clipboard_write(text)


async def screenshot(display: int = 1, max_width: int = 1600) -> dict:
    return await _backend().screenshot(display, max_width)


async def applescript(script: str, args: list[str] | None = None) -> str:
    if not APPLESCRIPT_SUPPORTED:
        raise RuntimeError("AppleScript requires macOS")
    return await _backend().applescript(script, args)
