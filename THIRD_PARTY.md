# Third-party software and notices

The project's own source is licensed under MIT; see [LICENSE](LICENSE).
Third-party packages keep their own licenses. The project license does not
relicense those packages, external apps, operating systems, or service content.

## Runtime and development dependencies

`pyproject.toml` lists direct dependencies. `uv.lock` pins the full dependency
set, including transitive packages. Install packages from their normal upstream
sources with `uv sync --locked`; do not copy private installed packages into this
checkout. A built wheel contains the project's package, not a vendored copy of
its dependency tree. A distribution that bundles dependencies must preserve their
individual notices and comply with their licenses.

Key runtime dependencies include the MCP Python SDK 1.30 (MIT), Pillow 12.3
(MIT-CMU), and Uvicorn 0.54 (BSD-3-Clause). Windows uses its native APIs and Pillow; any platform
helper dependencies listed in the lockfile retain their own upstream licenses.
Development tools such as pytest, Ruff, Hatchling, and uv are separate projects.
Check the actual pinned distribution metadata and upstream license before
redistributing a combined binary or installer. This list is not a legal review
or a complete software bill of materials.

## External installed tools and apps

The bridge can discover configured Claude Desktop stdio MCP servers and enabled
extensions on the operator's machine. These remain independently installed
software. No installed Apple app integration, Anthropic extension source, private
Desktop configuration, account credential, or extension payload is bundled here.
Discovery does not grant redistribution rights or imply endorsement. Operators
must install and license their own apps and servers.

Claude and Anthropic product names belong to their respective owners. macOS,
Windows, and Tailscale are external platforms/services. The project is not an
official Anthropic, Apple, Microsoft, or Tailscale product.

## Checked runtime license references

These upstream references support the current pinned set. Recheck after updates.

| Package / version | License | Upstream reference |
| --- | --- | --- |
| mcp 1.30 | MIT | https://github.com/modelcontextprotocol/python-sdk/blob/main/LICENSE |
| Pillow 12.3 | MIT-CMU | https://github.com/python-pillow/Pillow/blob/main/LICENSE |
| Uvicorn 0.54 | BSD-3-Clause | https://github.com/Kludex/uvicorn/blob/main/LICENSE.md |
| Starlette 1.7 | BSD-3-Clause | https://github.com/Kludex/starlette/blob/main/LICENSE.md |
| Pydantic 2.13 | MIT | https://github.com/pydantic/pydantic/blob/main/LICENSE |
| AnyIO 4.15 | MIT | https://github.com/agronholm/anyio/blob/master/LICENSE |

Upstream branches can change; installed distribution metadata and notices at the
locked version are the authority for a release.
