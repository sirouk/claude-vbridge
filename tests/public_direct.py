"""Opt-in live OAuth and harmless shell check; no paid inference or screenshots."""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="authorize explicit access to the running bridge")
    args = parser.parse_args()
    if not args.live:
        parser.error("live access requires --live; this registers an OAuth client")
    path = Path(__file__).resolve().parents[1] / "scripts/bridge_admin.py"
    spec = importlib.util.spec_from_file_location("bridge_admin", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    import sys

    sys.argv = [str(path), "check"]
    return module.main()


if __name__ == "__main__":
    raise SystemExit(main())
