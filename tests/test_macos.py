import io
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from vbridge import macos


def test_screenshot_encode_is_bounded():
    raw = io.BytesIO()
    Image.new("RGB", (3000, 1800), "navy").save(raw, format="PNG")
    encoded, meta = macos.encode_screenshot(raw.getvalue(), 1200)
    assert meta == {"width": 1200, "height": 720, "original_width": 3000, "original_height": 1800}
    assert encoded[:2] == b"\xff\xd8"
    assert len(encoded) < 200000
    with pytest.raises(ValueError):
        macos.encode_screenshot(raw.getvalue(), 10)


@pytest.mark.asyncio
async def test_screen_denial_never_captures(monkeypatch):
    monkeypatch.setattr(macos, "permission_status", lambda: {"screen_recording": False})
    execute = AsyncMock()
    monkeypatch.setattr(macos, "_exec", execute)
    with pytest.raises(PermissionError):
        await macos.screenshot()
    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_screenshot_cleanup_and_mapping(monkeypatch):
    monkeypatch.setattr(macos, "permission_status", lambda: {"screen_recording": True})
    monkeypatch.setattr(
        macos, "displays", lambda: [{"index": 1, "x": 0, "y": 0, "width_points": 1000, "height_points": 600}]
    )
    paths = []

    async def fake(argv, **kwargs):
        from pathlib import Path

        p = Path(argv[-1])
        paths.append(p)
        Image.new("RGB", (2000, 1200), "black").save(p)
        return ""

    monkeypatch.setattr(macos, "_exec", fake)
    r = await macos.screenshot(max_width=1000)
    assert r["metadata"]["width"] == 1000
    assert "coordinate_mapping" in r["metadata"]
    assert not paths[0].exists()


@pytest.mark.asyncio
async def test_input_validation(monkeypatch):
    monkeypatch.setattr(macos, "_require_accessibility", lambda: None)
    execute = AsyncMock(return_value="")
    monkeypatch.setattr(macos, "_exec", execute)
    with pytest.raises(ValueError):
        await macos.press_key('" & do shell script "evil')
    with pytest.raises(ValueError):
        await macos.press_key("a", ["evil"])
    await macos.press_key("a", ["command"])
    assert (
        execute.await_args.args[0][2]
        == 'tell application "System Events" to keystroke "a" using {command down}'
    )
    execute.reset_mock()
    text = 'quote " and new\nline'
    await macos.type_text(text)
    assert execute.await_args.args[0][-1] == text
    with pytest.raises(ValueError):
        await macos.type_text("\x00")


def test_click_requires_grant(monkeypatch):
    monkeypatch.setattr(macos, "permission_status", lambda: {"accessibility": False})
    with pytest.raises(PermissionError):
        macos.click(5, 5)
