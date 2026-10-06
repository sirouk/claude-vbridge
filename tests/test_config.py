import json

import pytest

from vbridge.config import settings
from vbridge.job_store import private_write


def test_private_settings_and_env_overrides(tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setenv("VBRIDGE_HOME", str(state))
    monkeypatch.delenv("VBRIDGE_PUBLIC_URL", raising=False)
    monkeypatch.delenv("VBRIDGE_PORT", raising=False)
    private_write(
        state / "settings.json",
        json.dumps({"public_url": "https://bridge.example.com", "port": 8811, "ui_enabled": True}),
    )
    cfg = settings()
    assert cfg["public_url"] == "https://bridge.example.com" and cfg["port"] == 8811
    assert cfg["ui_enabled"] is True
    monkeypatch.setenv("VBRIDGE_PUBLIC_URL", "https://override.example.com")
    monkeypatch.setenv("VBRIDGE_UI_ENABLED", "false")
    assert settings()["public_url"] == "https://override.example.com"
    assert settings()["ui_enabled"] is False


@pytest.mark.parametrize(
    "url",
    [
        "http://remote.example.com",
        "https://" + "user:pw" + "@example.com",  # Synthetic invalid userinfo, never a credential.
        "https://bridge.example.com/mcp",
        "https://bridge.example.com?secret=x",
    ],
)
def test_invalid_origin(tmp_path, monkeypatch, url):
    monkeypatch.setenv("VBRIDGE_HOME", str(tmp_path))
    monkeypatch.setenv("VBRIDGE_PUBLIC_URL", url)
    with pytest.raises(ValueError):
        settings()


def test_home_symlink_not_silently_resolved(tmp_path, monkeypatch):
    import os

    if os.name == "nt":
        pytest.skip("Native Windows junction behavior covered by storage tests")
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "alias"
    link.symlink_to(target, target_is_directory=True)
    monkeypatch.setenv("VBRIDGE_HOME", str(link))
    cfg = settings()
    assert cfg["home"] == link
    from vbridge.job_store import ensure_private_dir

    with pytest.raises(ValueError):
        ensure_private_dir(cfg["home"])
