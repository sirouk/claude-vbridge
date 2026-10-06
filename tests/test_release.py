"""Offline release artifact and workflow checks. Never invoke live operators."""

from __future__ import annotations

import importlib.util
import tarfile
import zipfile
from io import BytesIO
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("release_check", ROOT / "scripts/release_check.py")
assert spec and spec.loader
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


def test_license_no_personal_author():
    license_text = (ROOT / "LICENSE").read_text()
    assert "MIT License" in license_text
    assert "Copyright (c) 2026 claude-vbridge contributors" in license_text


def test_ci_matrix_is_locked_offline_and_cross_platform():
    text = (ROOT / ".github/workflows/ci.yml").read_text()
    assert "macos-latest" in text and "windows-latest" in text
    assert "'3.12', '3.13'" in text
    assert "uv sync --locked" in text
    assert "pytest -q" in text and "privacy_audit.py" in text and "uv build" in text
    assert "contents: read" in text
    assert "--live" not in text


def test_windows_wrapper_no_elevation_or_policy_bypass():
    text = (ROOT / "windows-bridge.ps1").read_text()
    assert "python.exe" in text
    assert "Start-Process -Verb RunAs" not in text
    assert "-ExecutionPolicy Bypass" not in text
    assert "$LASTEXITCODE" in text


def create_builds(directory: Path, extra: str | None = None):
    with zipfile.ZipFile(directory / "bridge.whl", "w") as archive:
        archive.writestr("vbridge/__init__.py", "")
        archive.writestr("bridge.dist-info/licenses/LICENSE", "MIT License")
        if extra:
            archive.writestr(extra, "test")
    with tarfile.open(directory / "bridge.tar.gz", "w:gz") as archive:
        raw = b"MIT License"
        info = tarfile.TarInfo("bridge/LICENSE")
        info.size = len(raw)
        archive.addfile(info, BytesIO(raw))


def test_artifact_check_accepts_clean_packages(tmp_path):
    create_builds(tmp_path)
    assert release.check_builds(tmp_path) == 0


def test_artifact_check_rejects_private_state(tmp_path):
    create_builds(tmp_path, "vbridge/settings.json")
    with pytest.raises(RuntimeError, match="Private state"):
        release.check_builds(tmp_path)


def test_artifact_check_requires_both_formats(tmp_path):
    with pytest.raises(RuntimeError, match="both wheel"):
        release.check_builds(tmp_path)
