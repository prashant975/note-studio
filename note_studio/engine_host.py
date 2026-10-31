"""Child-process engines (doc 07): one process per run, from the engine's own folder.

    <python> engines/<id>/engine_main.py --request <run>/request.json --out <run>    (cwd = engines/<id>)

The child prints one JSON object per line (step / cost / warning / output /
done); anything else is a log line. Adapted from QBG's stream_command and
stop_process:

- UTF-8 everywhere, unbuffered, its own process group, no window;
- its own TEMP inside the run folder (two runs never share temp files);
- the sign-in token only in its environment, never in request.json;
- Stop ends the whole process tree (taskkill /T), and on Windows every child is
  in a job object that ends with NoteCraft, so a run can never go on spending
  after NoteCraft is closed.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import engine_catalog, paths

log = logging.getLogger("note_studio.engine_host")
TOKEN_ENV = "NOTE_STUDIO_PW_TOKEN"
# NoteCraft's own settings are not the child's business.
_PRIVATE_PREFIXES = ("NOTE_STUDIO_", "SHEETS_BRIDGE_")
_running: dict[int, subprocess.Popen] = {}       # run id -> its child
_stopped: set[int] = set()
_lock = threading.Lock()


# ----------------------------------------------------------------- runtime
def runtime(engine_id: str) -> tuple[str, str]:
    """(python.exe, where it comes from) for one engine.

    Installed (Phase 9): the bundled Python on the engine's own library folder,
    _internal/libs/<engine>, a copy of the original's environment (command()
    starts it through engine_boot.py). In the project folder: the original's own
    virtual environment, read in place (no bytecode is written); without it,
    NoteCraft's own Python. NOTE_STUDIO_<ENGINE>_PYTHON overrides (tests)."""
    override = os.environ.get(f"NOTE_STUDIO_{engine_id.upper()}_PYTHON", "").strip()
    if override and Path(override).is_file():
        return override, "the Python set for this engine"
    libs = paths.libs_dir()
    if libs is not None and (libs / engine_id).is_dir():
        return sys.executable, f"{paths.PROJECT_ROOT.name}'s Python with the engine's own libraries"
    original = paths.original(engine_id)
    if original is not None:
        venv = original / ".venv" / "Scripts" / "python.exe"
        if venv.is_file():
            return str(venv), "the original app's own Python"
    return sys.executable, f"{paths.PROJECT_ROOT.name}'s Python"


def _boots(engine_id: str) -> bool:
    """Installed and not overridden: the engine starts through engine_boot.py on its own libraries."""
    override = os.environ.get(f"NOTE_STUDIO_{engine_id.upper()}_PYTHON", "").strip()
    libs = paths.libs_dir()
    return not (override and Path(override).is_file()) and libs is not None and (libs / engine_id).is_dir()


def command(engine_id: str, script: Path, *args: str) -> list[str]:
    """The argv that runs one engine script: through engine_boot.py when installed."""
    python, _where = runtime(engine_id)
    if _boots(engine_id):
        return [python, str(paths.PROJECT_ROOT / "engine_boot.py"), engine_id, str(script), *args]
    return [python, str(script), *args]


_PROBE = ("import importlib.util,sys; missing=[m for m in sys.argv[1:] if importlib.util.find_spec(m) is None]; "
          "print('MISSING|' + '||'.join(missing) if missing else 'PROBE_OK')")


def probe(engine_id: str, modules) -> list[str] | None:
    """Modules the engine's Python cannot import; None when that Python cannot be run."""
    names = [str(m) for m in modules]
    python, _where = runtime(engine_id)
    if _boots(engine_id):
        argv = [python, str(paths.PROJECT_ROOT / "engine_boot.py"), engine_id, "--probe", *names]
    else:
        argv = [python, "-c", _PROBE, *names]
    try:
        out = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
                             env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return None
    lines = (out.stdout or "").strip().splitlines()
    last = lines[-1] if lines else ""
    if last == "PROBE_OK":
        return []
    if last.startswith("MISSING|"):
        return [part.split(" (")[0] for part in last[len("MISSING|"):].split("||") if part]
    return None


def child_env(run_dir: Path, token: str = "", extra: dict | None = None) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(_PRIVATE_PREFIXES)}
    env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1", PYTHONDONTWRITEBYTECODE="1")
    temp = Path(run_dir) / "tmp"
    temp.mkdir(parents=True, exist_ok=True)
    env["TEMP"] = env["TMP"] = str(temp)
    env.pop(TOKEN_ENV, None)
    if token:
        env[TOKEN_ENV] = token
        from . import signin

        env["NOTE_STUDIO_PW_ACCOUNT"] = signin.snapshot().get("email", "")
    env.update(extra or {})
    return env


# -------------------------------------------------------- Windows job object
class _Job:
    """Every child joins one job that ends when NoteCraft ends (KILL_ON_JOB_CLOSE)."""

    handle = None

    @classmethod
    def add(cls, proc: subprocess.Popen) -> None:
        if os.name != "nt":
            return
        try:
            import ctypes
            from ctypes import wintypes

            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            if cls.handle is None:
                class Basic(ctypes.Structure):
                    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                                ("SchedulingClass", wintypes.DWORD)]

                class Counters(ctypes.Structure):
                    _fields_ = [(name, ctypes.c_uint64) for name in
                                ("ReadOps", "WriteOps", "OtherOps", "ReadBytes", "WriteBytes", "OtherBytes")]

                class Extended(ctypes.Structure):
                    _fields_ = [("Basic", Basic), ("Io", Counters), ("ProcessMemoryLimit", ctypes.c_size_t),
                                ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                                ("PeakJobMemoryUsed", ctypes.c_size_t)]

                kernel.CreateJobObjectW.restype = wintypes.HANDLE
                job = kernel.CreateJobObjectW(None, None)
                info = Extended()
                info.Basic.LimitFlags = 0x2000                     # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                if not job or not kernel.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
                    return
                cls.handle = job
            kernel.AssignProcessToJobObject(wintypes.HANDLE(cls.handle), wintypes.HANDLE(int(proc._handle)))
        except Exception:                                        # a safety net, never a reason to fail a run
            log.warning("could not add the engine process to the job object", exc_info=True)


# ------------------------------------------------------------------- run
def parse(line: str) -> dict:
    """One output line as an event; a plain line becomes {"event": "log"}."""
    text = line.rstrip("\r\n")
    if text.startswith("{"):
        try:
            value = json.loads(text)
        except ValueError:
            value = None
        if isinstance(value, dict) and isinstance(value.get("event"), str):
            return value
    return {"event": "log", "text": text}


def run(engine_id: str, run_id: int, run_dir: Path, request: dict, on_event, *, token: str = "",
        extra_env: dict | None = None) -> dict:
    """Write request.json, run the engine's engine_main.py, pass each event on.

    Returns {"status", "reason", "returncode", "tail", "stopped"}: status is the
    child's own "done" status, or "failed" when it ended without one."""
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    request_path = run_dir / "request.json"
    request_path.write_text(json.dumps(request, ensure_ascii=False, indent=2), encoding="utf-8")
    folder = engine_catalog.engine_dir(engine_id)
    argv = command(engine_id, folder / "engine_main.py", "--request", str(request_path), "--out", str(run_dir))
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    with _lock:
        _stopped.discard(run_id)
    proc = subprocess.Popen(argv, cwd=str(folder), env=child_env(run_dir, token, extra_env),
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace", bufsize=1, creationflags=flags)
    _Job.add(proc)
    with _lock:
        _running[run_id] = proc
    done: dict = {}
    tail: list[str] = []
    try:
        for line in proc.stdout:
            event = parse(line)
            if event["event"] == "log":
                tail = (tail + [event["text"]])[-30:]
            if event["event"] == "done":
                done = event
            try:
                on_event(event)
            except Exception:
                log.exception("engine event for run %s", run_id)
    finally:
        proc.stdout.close()
        proc.wait()
        with _lock:
            _running.pop(run_id, None)
            stopped = run_id in _stopped
            _stopped.discard(run_id)
    status = str(done.get("status") or "")
    if stopped:
        status, reason = "stopped", "Stopped."
    elif proc.returncode != 0 or status not in ("complete", "needs_attention", "failed"):
        status, reason = "failed", str(done.get("reason") or "The engine stopped unexpectedly.")
    else:
        reason = str(done.get("reason") or "")
    return {"status": status, "reason": reason, "returncode": proc.returncode, "tail": tail, "stopped": stopped}


def running(run_id: int) -> bool:
    with _lock:
        proc = _running.get(run_id)
    return proc is not None and proc.poll() is None


def stop(run_id: int, wait: float = 10.0) -> bool:
    """End one run's child and everything it started. True once it is gone."""
    with _lock:
        proc = _running.get(run_id)
        if proc is None or proc.poll() is not None:
            return False
        _stopped.add(run_id)
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, timeout=15)
        else:
            proc.kill()
    except Exception:
        log.exception("stopping run %s", run_id)
    deadline = time.time() + wait
    while time.time() < deadline and proc.poll() is None:
        time.sleep(0.1)
    return proc.poll() is not None
