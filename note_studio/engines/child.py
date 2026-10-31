"""One run of a child-process engine (UPSC, PW Books, ...): request in, events out.

The engine's engine_main.py prints step / cost / warning / output / done; this
turns them into what the run page shows and keeps every other line in
<run>/engine.log.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from .. import engine_host


@dataclass
class ChildResult:
    state: str                      # complete | needs_attention | failed | stopped
    message: str
    detail: str = ""
    sign_in: bool = False
    cost_inr: float = 0.0
    outputs: list = field(default_factory=list)       # [{"path", "kind", "label"}]
    warnings: list = field(default_factory=list)
    pages: int | None = None


def make_notes(engine: str, run_id: int, folder: Path, request: dict, *, token: str, step,
               on_cost=None) -> ChildResult:
    """Run one request; `step(text, pct)` gets the engine's progress words."""
    folder = Path(folder)
    collected = {"cost": 0.0, "outputs": [], "warnings": [], "done": {}}
    log_path = folder / "engine.log"

    with log_path.open("a", encoding="utf-8") as log:
        def on_event(event: dict) -> None:
            kind = event.get("event")
            if kind == "log":
                log.write(str(event.get("text", "")) + "\n")
                log.flush()
                return
            log.write(json.dumps(event, ensure_ascii=False) + "\n")
            if kind == "step":
                step(str(event.get("text") or ""), event.get("pct"))
            elif kind == "cost":
                collected["cost"] += float(event.get("inr") or 0)
                if on_cost:
                    on_cost(round(collected["cost"], 4))
            elif kind == "warning":
                collected["warnings"].append(str(event.get("text") or ""))
            elif kind == "output":
                collected["outputs"].append({"path": str(event.get("path") or ""), "kind": str(event.get("kind") or ""),
                                             "label": str(event.get("label") or "")})
            elif kind == "done":
                collected["done"] = event

        log.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} run {run_id}\n")
        ended = engine_host.run(engine, run_id, folder, request, on_event, token=token)

    done = collected["done"]
    result = ChildResult(state=ended["status"], message=ended["reason"], cost_inr=round(collected["cost"], 4),
                         outputs=collected["outputs"], warnings=collected["warnings"], pages=done.get("pages"),
                         detail=str(done.get("detail") or ""))
    if ended["stopped"]:
        result.message = "Stopped. What was made so far is in the run folder; nothing more is paid for."
    elif result.state == "failed":
        result.sign_in = done.get("code") == "sign_in"
        if not done:
            result.message = "The engine stopped unexpectedly."
            result.detail = "\n".join(ended["tail"][-5:])[:500]
    return result
