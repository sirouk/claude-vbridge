import json

from vbridge import local_tools


def test_windows_appdata_config(tmp_path, monkeypatch):
    monkeypatch.setattr(local_tools.sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.delenv("VBRIDGE_DESKTOP_DIR", raising=False)
    base = tmp_path / "Claude"
    base.mkdir()
    (base / "claude_desktop_config.json").write_text(
        json.dumps(
            {"mcpServers": {"demo": {"command": "node", "args": ["demo.js"], "env": {"TOKEN": "synthetic"}}}}
        )
    )
    servers = local_tools.desktop_servers()
    assert servers["demo"].args == ["demo.js"]


def test_incompatible_extensions_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(local_tools.sys, "platform", "win32")
    monkeypatch.setenv("VBRIDGE_DESKTOP_DIR", str(tmp_path))
    ext = tmp_path / "Claude Extensions" / "demo"
    ext.mkdir(parents=True)
    flags = tmp_path / "Claude Extensions Settings"
    flags.mkdir()
    (flags / "demo.json").write_text('{"isEnabled":true}')
    (ext / "manifest.json").write_text(
        json.dumps(
            {
                "name": "Mac app",
                "compatibility": {"platforms": ["darwin"]},
                "server": {"mcp_config": {"command": "node", "args": ["server.js"]}},
            }
        )
    )
    assert local_tools.desktop_servers() == {}
