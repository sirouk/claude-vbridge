"""Local worker: claims queued voice tasks and runs them with `claude -p`.

Safety defaults (override in ~/.vbridge/config.json):
  - permission mode `dontAsk`: anything not in allowed_tools is denied
    (this overrides the global bypassPermissions in ~/.claude/settings.json)
  - full shell/file tool allowlist (optional, broad local authority)
  - cwd must be one of the listed projects
  - hard budget and wall-clock limits per task
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from .queue import TaskQueue

HOME = Path(os.environ.get("VBRIDGE_HOME", Path.home() / ".vbridge"))
DEFAULTS = {
    "claude_bin": shutil.which("claude") or "claude",
    "model": "sonnet",
    "projects": {"home": str(Path.home())},  # name -> absolute dir
    "default_project": "home",
    "allowed_tools": [
        "Read",
        "Grep",
        "Glob",
        "Edit",
        "Write",
        "NotebookEdit",
        "Bash",
        "WebSearch",
        "WebFetch",
        "Task",
        "TodoWrite",
    ],
    "max_budget_usd": 5.0,
    "timeout_s": 1800,
    "poll_s": 1.0,
    # extra env for `claude -p`, e.g. ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN to use a gateway
    "env": {},
}


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    p = HOME / "config.json"
    if p.exists():
        cfg.update(json.loads(p.read_text()))
    return cfg


def resolve_cwd(cfg: dict, project: str | None) -> Path:
    name = project or cfg["default_project"]
    if name is None:
        d = HOME / "scratch"
        d.mkdir(parents=True, exist_ok=True)
        return d
    if name not in cfg["projects"]:
        raise ValueError(f"project {name!r} not in config. Known: {sorted(cfg['projects'])}")
    d = Path(cfg["projects"][name]).expanduser().resolve()
    if not d.is_dir():
        raise ValueError(f"project dir missing: {d}")
    return d


def run_one(cfg: dict, rec: dict) -> tuple[bool, str]:
    cwd = resolve_cwd(cfg, rec.get("project"))
    cmd = [
        cfg["claude_bin"],
        "-p",
        rec["prompt"],
        "--permission-mode",
        "dontAsk",
        "--allowedTools",
        *cfg["allowed_tools"],
        "--max-budget-usd",
        str(cfg["max_budget_usd"]),
        "--model",
        cfg["model"],
        "--no-session-persistence",
        "--output-format",
        "text",
    ]
    try:
        p = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=cfg["timeout_s"],
            stdin=subprocess.DEVNULL,
            env={**os.environ, **cfg["env"]},
        )
    except subprocess.TimeoutExpired:
        return False, f"timed out after {cfg['timeout_s']}s"
    out = (p.stdout or "") + (("\n[stderr]\n" + p.stderr) if p.returncode else "")
    return p.returncode == 0, out.strip()


def main() -> None:
    q = TaskQueue(HOME / "queue")
    # tasks left in running/ by a crash: mark failed rather than re-run (could repeat side effects)
    for p in (HOME / "queue" / "running").glob("*.json"):
        q.finish(p.stem, False, "worker restarted while task was running")
    print("vbridge-worker up", flush=True)
    while True:
        cfg = load_config()
        rec = q.claim()
        if not rec:
            time.sleep(cfg["poll_s"])
            continue
        print("run", rec["id"], flush=True)
        try:
            ok, out = run_one(cfg, rec)
        except Exception as e:  # config or spawn error
            ok, out = False, f"{type(e).__name__}: {e}"
        q.finish(rec["id"], ok, out)
        print("end", rec["id"], "ok" if ok else "FAILED", flush=True)


if __name__ == "__main__":
    main()
