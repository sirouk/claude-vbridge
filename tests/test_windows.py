"""No test captures a real screen, reads a real clipboard or injects real input.

Portable tests mock DLL calls. Windows-only ABI test loads DLLs and inspects
prototypes, but never calls desktop APIs. Safe to run on a headless Windows CI.
"""

import base64
import ctypes
import io
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PIL import Image

from vbridge import desktop, windows


@pytest.fixture(autouse=True)
def block_real_ui(monkeypatch):
    monkeypatch.setattr(windows, "_native", MagicMock(side_effect=AssertionError("Unmocked native UI")))
    monkeypatch.setattr(
        windows.ImageGrab, "grab", MagicMock(side_effect=AssertionError("Real capture forbidden"))
    )


@pytest.fixture
def native(monkeypatch):
    calls = []

    def send(count, events, size):
        assert size == ctypes.sizeof(windows.INPUT)
        calls.append([windows.INPUT.from_buffer_copy(event) for event in events])
        return count

    user32 = MagicMock()
    user32.SendInput.side_effect = send
    user32.SetThreadDpiAwarenessContext.return_value = 1
    obj = SimpleNamespace(user32=user32, kernel32=MagicMock(), wtsapi32=MagicMock(), calls=calls)
    monkeypatch.setattr(windows, "_native", lambda: obj)
    monkeypatch.setattr(windows, "_require_ui", lambda: None)
    return obj


def screen(x=0, y=0, width=1920, height=1080):
    return {
        "index": 1,
        "id": 1,
        "x": x,
        "y": y,
        "width_points": width,
        "height_points": height,
        "width_pixels": width,
        "height_pixels": height,
        "coordinate_units": "physical_pixels",
        "primary": True,
    }


def test_win32_fixed_width_and_pointer_sized_abi():
    assert ctypes.sizeof(windows.LONG) == 4
    assert ctypes.sizeof(windows.DWORD) == 4
    assert ctypes.sizeof(windows.WORD) == 2
    assert ctypes.sizeof(windows.RECT) == 16
    assert ctypes.sizeof(windows.MONITORINFO) == 40
    if ctypes.sizeof(ctypes.c_void_p) == 8:
        assert ctypes.sizeof(windows.INPUT) == 40
        assert windows.INPUT.event.offset == 8
        assert ctypes.sizeof(windows.MOUSEINPUT) == 32
        assert ctypes.sizeof(windows.KEYBDINPUT) == 24
        assert windows.KEYBDINPUT.dwExtraInfo.offset == 16
    else:
        assert ctypes.sizeof(windows.INPUT) == 28
        assert windows.INPUT.event.offset == 4
        assert ctypes.sizeof(windows.MOUSEINPUT) == 24
        assert ctypes.sizeof(windows.KEYBDINPUT) == 16


def test_loader_declares_64bit_handle_prototypes(monkeypatch):
    libraries = {}

    def loader(name, **kwargs):
        assert kwargs == {"use_last_error": True}
        libraries[name] = MagicMock()
        return libraries[name]

    monkeypatch.setattr(windows.sys, "platform", "win32")
    monkeypatch.setattr(windows.ctypes, "WinDLL", loader, raising=False)
    api = windows._Native()
    assert api.user32.GetClipboardData.restype is ctypes.c_void_p
    assert api.kernel32.GlobalLock.restype is ctypes.c_void_p
    assert api.kernel32.GlobalSize.restype is ctypes.c_size_t
    assert api.user32.SetForegroundWindow.argtypes == [ctypes.c_void_p]
    assert api.user32.SendInput.argtypes[1] == ctypes.POINTER(windows.INPUT)
    assert api.user32.CreateWindowExW.restype is ctypes.c_void_p


@pytest.mark.skipif(sys.platform != "win32", reason="Windows DLL/ABI-only check; no UI calls")
def test_windows_native_dll_exports_only():
    # Constructor only binds prototypes; no input, clipboard, capture or focus.
    api = windows._Native()
    assert api.user32.SendInput.restype is ctypes.c_uint
    assert api.user32.SetThreadDpiAwarenessContext.restype is ctypes.c_void_p
    assert api.wtsapi32.WTSFreeMemory.restype is None


def test_nonwindows_native_rejected(monkeypatch):
    monkeypatch.setattr(windows.sys, "platform", "darwin")
    with pytest.raises(RuntimeError, match="requires Windows"):
        windows._Native()


def test_dispatch_without_native_calls(monkeypatch):
    backend = SimpleNamespace(
        permission_status=lambda: {"accessibility": True},
        displays=lambda: [screen()],
        click=MagicMock(return_value={"posted": True}),
    )
    monkeypatch.setattr(desktop.importlib, "import_module", lambda *args: backend)
    monkeypatch.setattr(desktop, "PLATFORM", "win32")
    monkeypatch.setattr(desktop, "UI_SUPPORTED", True)
    monkeypatch.setattr(desktop, "APPLESCRIPT_SUPPORTED", False)
    assert desktop.supports_ui() and not desktop.supports_applescript()
    assert desktop.permission_status() == {"accessibility": True}
    assert desktop.displays() == [screen()]
    assert desktop.click(1, 2)["posted"]
    backend.click.assert_called_once_with(1, 2, "left", 1)
    monkeypatch.setattr(desktop, "PLATFORM", "linux")
    monkeypatch.setattr(desktop, "UI_SUPPORTED", False)
    assert desktop.permission_status()["ui_supported"] is False
    with pytest.raises(RuntimeError, match="unsupported"):
        desktop.displays()


async def test_windows_no_applescript(monkeypatch):
    monkeypatch.setattr(desktop, "APPLESCRIPT_SUPPORTED", False)
    with pytest.raises(RuntimeError, match="requires macOS"):
        await desktop.applescript("anything")


def configure_session(
    native, *, session=1, state=0, desktop_name="Default", current="Default", station="WinSta0"
):
    state_buffer = ctypes.c_int32(state)
    native._state_buffer = state_buffer

    def session_id(pid, ptr):
        ptr._obj.value = session
        return True

    def wts_query(server, sid, kind, ptr, size):
        assert kind == 8
        ptr._obj.value = ctypes.addressof(state_buffer)
        size._obj.value = 4
        return True

    native.kernel32.ProcessIdToSessionId.side_effect = session_id
    native.wtsapi32.WTSQuerySessionInformationW.side_effect = wts_query
    native.user32.GetProcessWindowStation.return_value = 10
    native.user32.GetThreadDesktop.return_value = 20
    native.user32.OpenInputDesktop.return_value = 30

    def object_info(handle, kind, buf, size, needed):
        buf.value = {10: station, 20: current, 30: desktop_name}[handle]
        return True

    native.user32.GetUserObjectInformationW.side_effect = object_info


def test_permission_preflight_active_default(native):
    configure_session(native)
    result = windows.permission_status()
    assert result["accessibility"] and result["screen_recording"]
    assert result["input_desktop"] == "Default"
    native.wtsapi32.WTSFreeMemory.assert_called_once()
    native.user32.CloseDesktop.assert_called_once_with(30)
    native.user32.SendInput.assert_not_called()
    native.user32.GetClipboardData.assert_not_called()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"session": 0},
        {"state": 4},
        {"desktop_name": "Winlogon"},
        {"current": "Other"},
        {"station": "Service-0x0"},
    ],
)
def test_permission_preflight_fails_closed(native, kwargs):
    configure_session(native, **kwargs)
    result = windows.permission_status()
    assert not result["accessibility"] and not result["screen_recording"]
    native.user32.SendInput.assert_not_called()


def test_inaccessible_input_desktop_denied(native):
    configure_session(native)
    native.user32.OpenInputDesktop.return_value = None
    assert not windows.permission_status()["accessibility"]
    native.user32.CloseDesktop.assert_not_called()


def test_dpi_context_restores_after_failure(native):
    native.user32.SetThreadDpiAwarenessContext.side_effect = [123, 456]
    with pytest.raises(ValueError):
        with windows._dpi_context():
            raise ValueError("test")
    first, second = native.user32.SetThreadDpiAwarenessContext.call_args_list
    assert first.args[0].value == ctypes.c_void_p(-4).value
    assert second.args == (123,)
    native.user32.SetThreadDpiAwarenessContext.side_effect = None
    native.user32.SetThreadDpiAwarenessContext.return_value = None
    with pytest.raises(RuntimeError, match="DPI"):
        with windows._dpi_context():
            pass


def test_displays_primary_first_physical_bounds(native):
    def enumerate_monitors(dc, rect, callback, data):
        assert callback(2, None, None, 0)
        assert callback(1, None, None, 0)
        return True

    def info(handle, ptr):
        ptr._obj.rcMonitor = (
            windows.RECT(-1280, 0, 0, 1024) if handle == 2 else windows.RECT(0, 0, 1920, 1080)
        )
        ptr._obj.dwFlags = int(handle == 1)
        return True

    native.user32.EnumDisplayMonitors.side_effect = enumerate_monitors
    native.user32.GetMonitorInfoW.side_effect = info
    result = windows.displays()
    assert [d["id"] for d in result] == [1, 2]
    assert [d["index"] for d in result] == [1, 2]
    assert result[1]["x"] == -1280
    assert result[1]["width_points"] == result[1]["width_pixels"] == 1280
    assert result[1]["coordinate_units"] == "physical_pixels"


def test_click_negative_origin_absolute_mapping(native, monkeypatch):
    monkeypatch.setattr(windows, "displays", lambda: [screen(-1280, -100, 3200, 1180)])
    native.user32.GetSystemMetrics.side_effect = lambda i: {76: -1280, 77: -100, 78: 3200, 79: 1180}[i]
    result = windows.click(-1280, -100, "right", 2)
    events = native.calls[0]
    assert (events[0].mi.dx, events[0].mi.dy) == (0, 0)
    assert events[0].mi.dwFlags == 0xC001
    assert [e.mi.dwFlags for e in events[1:]] == [8, 16, 8, 16]
    assert result["posted"] and not result["delivery_verified"]
    windows.click(1919, 1079)
    assert native.calls[1][0].mi.dx == 65535 and native.calls[1][0].mi.dy == 65535


@pytest.mark.parametrize("args", [(float("nan"), 0), (float("inf"), 0), (-1, 0), (2000, 0)])
def test_click_invalid_never_posts(native, monkeypatch, args):
    monkeypatch.setattr(windows, "displays", lambda: [screen()])
    with pytest.raises(ValueError):
        windows.click(*args)
    native.user32.SendInput.assert_not_called()


def test_partial_sendinput_raises_and_releases(native):
    native.user32.SendInput.side_effect = [1, 1]
    with pytest.raises(RuntimeError, match="1/2"):
        windows._send_events(
            [windows._keyboard(vk=0x10), windows._keyboard(vk=0x10, flags=2)],
            [windows._keyboard(vk=0x10, flags=2)],
        )
    assert native.user32.SendInput.call_count == 2
    native.user32.SendInput.reset_mock()
    native.user32.SendInput.side_effect = [0]
    with pytest.raises(RuntimeError, match="UAC/UIPI"):
        windows._send_events([windows._keyboard(vk=65)])
    assert native.user32.SendInput.call_count == 1


async def test_unicode_input_uses_utf16_and_surrogate_pairs(native):
    result = await windows.type_text("Aé😀")
    events = native.calls[0]
    assert [e.ki.wScan for e in events[::2]] == [0x41, 0xE9, 0xD83D, 0xDE00]
    assert all(e.ki.wVk == 0 for e in events)
    assert [e.ki.dwFlags for e in events] == [4, 6] * 4
    assert result["typed_chars"] == 3 and not result["delivery_verified"]


async def test_unicode_input_bounded_batches_and_validation(native):
    await windows.type_text("😀" * windows.MAX_TEXT)
    assert all(len(batch) <= 256 for batch in native.calls)
    assert sum(map(len, native.calls)) == windows.MAX_TEXT * 4
    native.user32.SendInput.reset_mock()
    for text in ("a" * (windows.MAX_TEXT + 1), "\x00"):
        with pytest.raises(ValueError):
            await windows.type_text(text)
    with pytest.raises(UnicodeEncodeError):
        await windows.type_text("\ud800")
    native.user32.SendInput.assert_not_called()


async def test_press_key_modifiers_and_extended_key(native):
    result = await windows.press_key("left", ["control", "shift"])
    events = native.calls[0]
    assert [e.ki.wVk for e in events] == [0x11, 0x10, 0x25, 0x25, 0x10, 0x11]
    assert [e.ki.dwFlags for e in events] == [0, 0, 1, 3, 2, 2]
    assert not result["delivery_verified"]
    await windows.press_key("a", ["command", "option"])
    assert [e.ki.wVk for e in native.calls[1]][:3] == [0x5B, 0x12, 65]


async def test_press_key_rejects_alias_duplicates_and_injection(native):
    for key, mods in [
        ("a", ["control", "ctrl"]),
        ("a", ["command", "win"]),
        ("a", ["evil"]),
        ('"; evil', []),
        ("é", []),
    ]:
        with pytest.raises(ValueError):
            await windows.press_key(key, mods)
    native.user32.SendInput.assert_not_called()


def clipboard_data(native, text=None, raw=None):
    if raw is None:
        raw = text.encode("utf-16-le") + b"\0\0"
    buffer = ctypes.create_string_buffer(raw)
    native._clipboard_buffer = buffer
    native.user32.IsClipboardFormatAvailable.return_value = True
    native.user32.GetClipboardData.return_value = 123
    native.kernel32.GlobalSize.return_value = len(raw)
    native.kernel32.GlobalLock.return_value = ctypes.addressof(buffer)
    return buffer


async def test_clipboard_read_unicode_and_bounds(native):
    clipboard_data(native, "秘密😀")
    result = await windows.clipboard_read()
    assert result == {"text": "秘密😀", "truncated": False}
    native.kernel32.GlobalFree.assert_not_called()  # clipboard owns the read handle
    native.kernel32.GlobalUnlock.assert_called_once_with(123)
    native.user32.CloseClipboard.assert_called_once()
    clipboard_data(native, "😀" * windows.MAX_TEXT)
    result = await windows.clipboard_read()
    assert result == {"text": "😀" * windows.MAX_TEXT, "truncated": False}
    clipboard_data(native, "x" * (windows.MAX_TEXT + 100))
    result = await windows.clipboard_read()
    assert result == {"text": "x" * windows.MAX_TEXT, "truncated": True}


async def test_clipboard_empty_invalid_busy(native):
    native.user32.IsClipboardFormatAvailable.return_value = False
    assert await windows.clipboard_read() == {"text": "", "truncated": False}
    clipboard_data(native, raw=b"a\0b\0")
    with pytest.raises(RuntimeError, match="NUL-terminated"):
        await windows.clipboard_read()
    clipboard_data(native, raw=b"a")
    with pytest.raises(RuntimeError, match="invalid"):
        await windows.clipboard_read()
    native.user32.OpenClipboard.return_value = False
    with pytest.raises(RuntimeError, match="busy"):
        await windows.clipboard_read()


def clipboard_allocation(native):
    allocation = ctypes.create_string_buffer((windows.MAX_TEXT * 2 + 1) * 2)
    native._allocation = allocation
    native.kernel32.GlobalAlloc.return_value = 321
    native.kernel32.GlobalLock.return_value = ctypes.addressof(allocation)
    native.user32.CreateWindowExW.return_value = 99
    native.user32.SetClipboardData.return_value = 321
    return allocation


async def test_clipboard_write_owner_and_transfer(native):
    allocation = clipboard_allocation(native)
    assert await windows.clipboard_write("😀é") == {"written_chars": 2}
    assert allocation.raw[:8] == "😀é".encode("utf-16-le") + b"\0\0"
    native.user32.OpenClipboard.assert_called_once_with(99)
    native.user32.SetClipboardData.assert_called_once_with(13, 321)
    native.kernel32.GlobalFree.assert_not_called()
    native.user32.DestroyWindow.assert_called_once_with(99)
    native.user32.CloseClipboard.assert_called_once()


@pytest.mark.parametrize(
    "failure", ["SetClipboardData", "EmptyClipboard", "OpenClipboard", "CreateWindowExW"]
)
async def test_clipboard_write_failure_frees_untransferred_allocation(native, failure):
    clipboard_allocation(native)
    getattr(native.user32, failure).return_value = 0
    with pytest.raises(RuntimeError):
        await windows.clipboard_write("secret")
    native.kernel32.GlobalFree.assert_called_once_with(321)
    if failure != "CreateWindowExW":
        native.user32.DestroyWindow.assert_called_once_with(99)


async def test_clipboard_close_failure_after_transfer_does_not_free(native):
    clipboard_allocation(native)
    native.user32.CloseClipboard.return_value = False
    with pytest.raises(RuntimeError, match="close"):
        await windows.clipboard_write("secret")
    native.kernel32.GlobalFree.assert_not_called()
    native.user32.DestroyWindow.assert_called_once_with(99)


async def test_clipboard_write_rejects_nul_oversize_surrogate(native):
    for text in ("\0", "x" * (windows.MAX_TEXT + 1)):
        with pytest.raises(ValueError):
            await windows.clipboard_write(text)
    with pytest.raises(UnicodeEncodeError):
        await windows.clipboard_write("\ud800")
    native.kernel32.GlobalAlloc.assert_not_called()


def test_enum_apps_uses_executable_not_titles_and_closes_handles(native):
    def enum(callback, data):
        for hwnd in (10, 11, 12):
            assert callback(hwnd, 0)
        return True

    native.user32.EnumWindows.side_effect = enum
    native.user32.IsWindowVisible.side_effect = lambda hwnd: hwnd != 12
    native.user32.GetWindow.return_value = None
    native.kernel32.OpenProcess.return_value = 100

    def process_name(handle, flags, buf, size):
        buf.value = r"C:\Windows\System32\notepad.exe"
        return True

    native.kernel32.QueryFullProcessImageNameW.side_effect = process_name
    assert windows._window_apps() == [(10, "notepad.exe"), (11, "notepad.exe")]
    assert native.kernel32.CloseHandle.call_count == 2
    native.user32.GetWindowTextW.assert_not_called()


async def test_list_apps_deduplicates_and_focus_verifies(native, monkeypatch):
    monkeypatch.setattr(
        windows, "_window_apps", lambda: [(10, "notepad.exe"), (11, "NOTEPAD.exe"), (20, "Calc.exe")]
    )
    assert await windows.list_apps() == ["Calc.exe", "NOTEPAD.exe"]
    native.user32.GetForegroundWindow.return_value = 10
    native.user32.IsIconic.return_value = False
    result = await windows.focus_app("notepad.exe")
    assert result["foreground_confirmed"]
    native.user32.SetForegroundWindow.assert_called_once_with(10)
    native.user32.GetForegroundWindow.return_value = 20
    with pytest.raises(RuntimeError, match="foreground"):
        await windows.focus_app("notepad.exe")
    with pytest.raises(ValueError, match="No visible"):
        await windows.focus_app("missing.exe")
    for name in ("-x", r"C:\evil.exe", "bad\x00"):
        with pytest.raises(ValueError):
            await windows.focus_app(name)


async def test_screenshot_jpeg_mapping_negative_origin_no_files(native, monkeypatch):
    info = screen(-2000, -100, 2000, 3000)
    monkeypatch.setattr(windows, "displays", lambda: [info])
    capture = MagicMock(side_effect=lambda **kwargs: Image.new("RGB", (2000, 3000), "navy"))
    monkeypatch.setattr(windows.ImageGrab, "grab", capture)
    result = await windows.screenshot(max_width=1200)
    capture.assert_called_once_with(bbox=(-2000, -100, 0, 2900), all_screens=True)
    assert result["mimeType"] == "image/jpeg"
    meta = result["metadata"]
    assert (meta["width"], meta["height"]) == (800, 1200)
    assert (meta["original_width"], meta["original_height"]) == (2000, 3000)
    assert meta["display"]["x"] == -2000
    assert "physical pixel" in meta["coordinate_mapping"]
    raw = base64.b64decode(result["data"])
    assert raw.startswith(b"\xff\xd8")
    with Image.open(io.BytesIO(raw)) as image:
        assert image.size == (800, 1200)


async def test_screenshot_invalid_no_capture(native, monkeypatch):
    monkeypatch.setattr(windows, "displays", lambda: [screen()])
    for display, width in [(0, 1600), (2, 1600), (True, 1600), (1, 10), (1, 2401), (1, 400.0)]:
        with pytest.raises(ValueError):
            await windows.screenshot(display, width)
    monkeypatch.setattr(windows, "displays", lambda: [screen(width=100000, height=100000)])
    with pytest.raises(ValueError, match="capture size"):
        await windows.screenshot()
    monkeypatch.setattr(windows, "displays", lambda: [screen(-100000, 0), screen(100000, 0)])
    with pytest.raises(ValueError, match="Virtual desktop"):
        await windows.screenshot()
    windows.ImageGrab.grab.assert_not_called()


async def test_denial_never_sends_captures_or_reads(native, monkeypatch):
    def deny():
        raise PermissionError("secure desktop")

    monkeypatch.setattr(windows, "_require_ui", deny)
    for action in (
        windows.screenshot(),
        windows.type_text("x"),
        windows.press_key("a"),
        windows.clipboard_read(),
        windows.clipboard_write("x"),
        windows.list_apps(),
    ):
        with pytest.raises(PermissionError):
            await action
    with pytest.raises(PermissionError):
        windows.click(0, 0)
    native.user32.SendInput.assert_not_called()
    native.user32.OpenClipboard.assert_not_called()
    windows.ImageGrab.grab.assert_not_called()


def test_enum_display_limit_fails_closed(native, monkeypatch):
    monkeypatch.setattr(windows, "MAX_DISPLAYS", 1)

    def enumerate_monitors(dc, rect, callback, data):
        assert callback(1, None, None, 0)
        assert not callback(2, None, None, 0)
        return False

    def info(handle, ptr):
        ptr._obj.rcMonitor = windows.RECT(0, 0, 1920, 1080)
        return True

    native.user32.EnumDisplayMonitors.side_effect = enumerate_monitors
    native.user32.GetMonitorInfoW.side_effect = info
    with pytest.raises(RuntimeError, match="Too many"):
        windows.displays()


def test_enum_window_limit_fails_closed(native, monkeypatch):
    monkeypatch.setattr(windows, "MAX_WINDOWS", 1)

    def enumerate_windows(callback, data):
        assert callback(10, 0)
        assert not callback(11, 0)
        return False

    native.user32.EnumWindows.side_effect = enumerate_windows
    native.user32.IsWindowVisible.return_value = False
    with pytest.raises(RuntimeError, match="bounded"):
        windows._window_apps()


async def test_screenshot_geometry_change_and_capture_error(native, monkeypatch):
    monkeypatch.setattr(windows, "displays", lambda: [screen()])
    monkeypatch.setattr(windows.ImageGrab, "grab", lambda **kwargs: Image.new("RGB", (10, 10)))
    with pytest.raises(RuntimeError, match="geometry changed"):
        await windows.screenshot()
    capture = MagicMock(side_effect=OSError("capture denied"))
    monkeypatch.setattr(windows.ImageGrab, "grab", capture)
    native.user32.SetThreadDpiAwarenessContext.reset_mock()
    with pytest.raises(OSError, match="capture denied"):
        await windows.screenshot()
    assert native.user32.SetThreadDpiAwarenessContext.call_count == 2
