"""Offline package-content guard. Does not install or start a bridge."""

from __future__ import annotations

import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = {".vbridge", ".env", "settings.json", "oauth.json", "passphrase", "internal-token", "server.log"}


def check_builds(directory: Path) -> int:
    wheels = sorted(directory.glob("*.whl"))
    sdists = sorted(directory.glob("*.tar.gz"))
    if not wheels or not sdists:
        raise RuntimeError("Build both wheel and source distribution first: uv build")
    for artifact in wheels + sdists:
        if artifact.suffix == ".whl":
            with zipfile.ZipFile(artifact) as archive:
                names = archive.namelist()
            if not any(name.startswith("vbridge/") for name in names):
                raise RuntimeError("Wheel is missing the vbridge package")
        else:
            with tarfile.open(artifact) as archive:
                names = archive.getnames()
        for name in names:
            if FORBIDDEN.intersection(Path(name).parts):
                raise RuntimeError("Private state included in distribution")
        if not any(Path(name).name == "LICENSE" for name in names):
            raise RuntimeError("Distribution is missing LICENSE")
    print(f"Release content check: {len(wheels)} wheel(s), {len(sdists)} sdist(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(check_builds(ROOT / "dist"))
