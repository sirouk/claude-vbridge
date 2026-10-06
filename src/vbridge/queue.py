"""File-backed task mailbox. One JSON file per task; state moves by atomic rename.

pending/ -> running/ -> done/ (or failed/). Voice writes to pending/.
A local worker claims tasks with os.rename (atomic on one filesystem).
"""

from __future__ import annotations

import json
import os
import secrets
import time
from pathlib import Path

STATES = ("pending", "running", "done", "failed")
MAX_PROMPT = 8000


class TaskQueue:
    def __init__(self, base: Path):
        self.base = base
        for s in STATES:
            (base / s).mkdir(parents=True, exist_ok=True)

    def _find(self, task_id: str):
        for s in STATES:
            p = self.base / s / f"{task_id}.json"
            if p.exists():
                return s, p
        return None, None

    def add(self, prompt: str, project: str | None = None) -> dict:
        prompt = prompt.strip()
        if not prompt:
            raise ValueError("prompt is empty")
        if len(prompt) > MAX_PROMPT:
            raise ValueError(f"prompt over {MAX_PROMPT} chars")
        tid = time.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(3)
        rec = {"id": tid, "prompt": prompt, "project": project, "created": time.time(), "source": "voice"}
        tmp = self.base / "pending" / f".{tid}.tmp"
        tmp.write_text(json.dumps(rec))
        os.replace(tmp, self.base / "pending" / f"{tid}.json")
        return rec

    def get(self, task_id: str) -> dict | None:
        if not task_id.replace("-", "").isalnum():
            return None
        state, p = self._find(task_id)
        if not p:
            return None
        rec = json.loads(p.read_text())
        rec["state"] = state
        return rec

    def list(self, limit: int = 10) -> list[dict]:
        out = []
        for s in STATES:
            for p in (self.base / s).glob("*.json"):
                try:
                    r = json.loads(p.read_text())
                except Exception:
                    continue
                out.append({"id": r["id"], "state": s, "prompt": r["prompt"][:80], "created": r["created"]})
        out.sort(key=lambda r: r["created"], reverse=True)
        return out[:limit]

    def claim(self) -> dict | None:
        """Used by the worker. Oldest pending first."""
        for p in sorted((self.base / "pending").glob("*.json")):
            dst = self.base / "running" / p.name
            try:
                os.rename(p, dst)
            except FileNotFoundError:
                continue
            return json.loads(dst.read_text())
        return None

    def finish(self, task_id: str, ok: bool, result: str) -> None:
        src = self.base / "running" / f"{task_id}.json"
        rec = json.loads(src.read_text())
        rec.update(result=result[-20000:], finished=time.time())
        dst = self.base / ("done" if ok else "failed") / f"{task_id}.json"
        dst.write_text(json.dumps(rec))
        src.unlink()
