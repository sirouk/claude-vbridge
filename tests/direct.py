"""Run with the project interpreter; no paid model calls or app mutations."""

import asyncio
import sys
import tempfile
from pathlib import Path

from mcp import StdioServerParameters

from vbridge.local_tools import DesktopProxy, desktop_servers
from vbridge.native import read_file, run_command, write_file


async def main():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d)
        fake = p / "backend.py"
        fake.write_text("""from mcp.server.fastmcp import FastMCP
m=FastMCP("test")
@m.tool()
def echo(text: str) -> str: return text
if __name__ == "__main__": m.run()
""")
        proxy = DesktopProxy(
            {"test-backend": StdioServerParameters(command=sys.executable, args=[str(fake)])}
        )
        await proxy.start()
        assert not proxy.errors, proxy.errors
        assert "test_backend__echo" in proxy.tools
        results = await asyncio.gather(
            *(proxy.call("test_backend__echo", {"text": str(i)}) for i in range(10))
        )
        assert [r.content[0].text for r in results] == [str(i) for i in range(10)]
        await proxy.close()
        print("PASS persistent stdio session, namespacing, ten concurrent calls, cleanup")
        r = await run_command("printf BRIDGE-OK")
        assert r["exit_code"] == 0 and r["output"] == "BRIDGE-OK", r
        r = await run_command("python3 -c 'print(\"x\" * 100000)'", timeout_s=5)
        assert r["truncated"] and len(r["output"]) <= 64000, r
        # Use a Python wait in the external fixture, not a harness polling loop.
        r = await run_command("python3 -c 'import time; time.sleep(20)'", timeout_s=1)
        assert r["timed_out"]
        write_file(str(p / "a"), "hello")
        assert read_file(str(p / "a"))["text"] == "hello"
        try:
            write_file(str(p / "a"), "bad")
            raise AssertionError("overwrite was not rejected")
        except FileExistsError:
            pass
        print("PASS native command, bounded output, process timeout, files, overwrite protection")
    specs = desktop_servers()
    print("Desktop backend names:", sorted(specs))


asyncio.run(main())
