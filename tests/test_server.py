import importlib

import pytest


@pytest.mark.asyncio
async def test_ui_and_legacy_catalog_gates(tmp_path, monkeypatch):
    monkeypatch.setenv("VBRIDGE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("VBRIDGE_DESKTOP_ENABLED", "0")
    monkeypatch.setenv("VBRIDGE_UI_ENABLED", "0")
    import sys

    if "vbridge.server" in sys.modules:
        server = importlib.reload(sys.modules["vbridge.server"])
    else:
        server = importlib.import_module("vbridge.server")
    try:
        names = {t.name for t in await server.bridged_list_tools()}
        assert "start_job" in names and "permission_status" in names
        assert not server.UI_TOOLS.intersection(names)
        assert not server.WORKER_TOOLS.intersection(names)
        with pytest.raises(ValueError):
            await server.bridged_call_tool("screenshot", {})
        server.SETTINGS["ui_enabled"] = True
        names = {t.name for t in await server.bridged_list_tools()}
        assert server.UI_TOOLS.issubset(names)
    finally:
        await server.jobs.close()


@pytest.mark.asyncio
async def test_job_tool_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("VBRIDGE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("VBRIDGE_DESKTOP_ENABLED", "0")
    import sys

    if "vbridge.server" in sys.modules:
        server = importlib.reload(sys.modules["vbridge.server"])
    else:
        server = importlib.import_module("vbridge.server")
    try:
        command = '[Console]::Write("job-ok")' if sys.platform == "win32" else "printf job-ok"
        job = await server.start_job(command, str(tmp_path), 5)
        result = await server.job_status(job["id"], wait_s=5)
        assert result["state"] == "done" and result["output"] == "job-ok"
        assert (await server.list_jobs())[0]["id"] == job["id"]
    finally:
        await server.jobs.close()
