"""Offline privacy guard for publication candidates. Reports locations, never values."""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {
    ".git",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "build",
    "dist",
    "graphify-out",
}
SKIP_SUFFIXES = {".pyc", ".pyo"}
PRIVATE_NAMES = {
    ".env",
    "passphrase",
    "oauth.json",
    "settings.json",
    "config.json",
    "internal-token",
    "urltoken",
    "DISABLED",
}
PATTERNS = {
    "absolute user path": re.compile(r"/(?:Users|home)/[A-Za-z0-9_.-]+"),
    "private network hostname": re.compile(r"[A-Za-z0-9_.-]+\.ts\.net", re.I),
    "email address": re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    "private key": re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----"),
    "provider credential": re.compile(
        r"(?:sk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|AKIA[A-Z0-9]{16})"
    ),
    "URL credentials": re.compile(r"https?://[^/\s:@]+:[^/\s@]+@"),
    "URL bearer path": re.compile(r"/k/[A-Za-z0-9_-]{16,}/mcp"),
    "literal secret assignment": re.compile(
        r"(?i)(?:api_key|access_token|refresh_token|client_secret|password|passphrase)\s*[:=]\s*['\"][A-Za-z0-9_+/=-]{20,}['\"]"
    ),
}


def candidates(root: Path) -> list[Path]:
    if (root / ".git").exists():
        result = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=root,
            capture_output=True,
            check=True,
        )
        return sorted({root / name for name in result.stdout.decode().split("\0") if name})
    return sorted(
        p
        for p in root.rglob("*")
        if p.is_file()
        and not any(part in SKIP_DIRS for part in p.relative_to(root).parts)
        and p.suffix not in SKIP_SUFFIXES
        and not any(
            part in {".vbridge", "private-tests", "backups", "artifacts"}
            for part in p.relative_to(root).parts
        )
    )


def audit(root: Path) -> tuple[int, list[str]]:
    findings = []
    paths = candidates(root)
    for path in paths:
        relative = path.relative_to(root)
        if path.name in PRIVATE_NAMES or path.suffix.lower() in {".pem", ".key", ".p12", ".pfx", ".log"}:
            findings.append(f"{relative}: private/generated file candidate")
        raw = path.read_bytes()
        if b"\0" in raw:
            findings.append(f"{relative}: binary candidate requires manual review")
            continue
        text = raw.decode("utf-8", errors="replace")
        for line_number, line in enumerate(text.splitlines(), 1):
            scanned_line = re.sub(r"\\n@[A-Za-z_][A-Za-z0-9_]*\.tool\(", " decorator(", line)
            for label, pattern in PATTERNS.items():
                if pattern.search(scanned_line):
                    findings.append(f"{relative}:{line_number}: {label}")
    return len(paths), findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    count, findings = audit(args.root.resolve())
    for finding in findings:
        print(finding)
    print(f"Privacy audit: {count} candidate files, {len(findings)} findings")
    print("Manual review of content and repository history is still required.")
    return bool(findings)


if __name__ == "__main__":
    raise SystemExit(main())
