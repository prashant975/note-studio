"""Data guard for the new Note Studio project.

Note Studio is a NEW project (C:/Users/Laptop/Downloads/Note_Studio) that COPIES
the code of the existing note apps into engines/<name>/ and runs them from one
app. The original projects are never edited. This tool proves it:

    python data_guard.py backup   [--light]          zip every data location
    python data_guard.py snapshot before.json         list every watched file (originals + data)
    python data_guard.py compare  before.json after.json
    python data_guard.py drift    [--project DIR]     what changed in each engine COPY vs its original

`compare` exits 1 when a DATA file disappeared or a JSON list/dict lost entries,
or when an ORIGINAL project's code changed (the originals are frozen).
`drift` exits 1 when a copied engine changed a file that is not in its allowed
list, or changed a protected prompt/rules file at all.

Snapshots hold paths, sizes, SHA-256 hashes and JSON entry counts only - never
file contents. The backup zip DOES contain secrets (.env, Google tokens): keep
it on this computer.

Standard library only (Python 3.10+).
"""
from __future__ import annotations

import argparse
import datetime as _dt
import fnmatch
import hashlib
import json
import os
import io
import subprocess
import tarfile
import tempfile
import sys
import zipfile
from pathlib import Path

HOME = Path.home()
DOWNLOADS = HOME / "Downloads"
DEFAULT_PROJECT = DOWNLOADS / "Note_Studio"
_LOCALAPPDATA = Path(os.environ.get("LOCALAPPDATA") or HOME / "AppData" / "Local")

# ------------------------------------------------------------ the originals
# role "data":   must never disappear or lose JSON entries (may grow: old apps still run)
# role "frozen": the original's code/prompts/rules: must not change at all
ORIGINALS = {
    "shortnotes": {
        "root": DOWNLOADS / "shortnotes-core",
        "data": ["sheets.json", ".env", "client_secret.json", "client_secret.personal.bak", "output"],
        "frozen": ["notesbot", "sheets_bridge", "api", "web", "equation_kit", "packaging", "fonts",
                   "assets", "tests", "pw_access.py", "requirements.txt", "package.json", "vercel.json"],
        "light_skip": [],
    },
    "upsc": {
        "root": DOWNLOADS / "Upsc_Notes_2" / "upsc_Notes",
        "data": ["runs", "outputs", "out", "data", ".env", ".streamlit"],
        "frozen": ["src", "prompts", "config", "reference_documents", "assets", "tests", "app.py",
                   "run_cli.py", "requirements.txt", "pw_access.py"],
        "light_skip": ["runs"],
    },
    "pw_books": {
        "root": DOWNLOADS / "handwritten_notes_automation_complete" / "handwritten_notes_automation_complete",
        "data": ["runs", "outputs", "logs", ".env", ".streamlit"],
        "frozen": ["src", "prompts", "assets", "equation_kit", "tests", "app.py", "run_cli.py",
                   "requirements.txt", "pw_access.py"],
        "light_skip": ["runs"],
    },
    "notes_qc": {
        "root": DOWNLOADS / "upsc_file_QC",
        "data": ["runs", "output", ".streamlit"],
        "frozen": ["src", "config", "reference_documents", "tests", "app.py", "run_cli.py",
                   "requirements.txt", "pw_access.py"],
        "light_skip": ["runs"],
    },
}

USER_DATA = [
    # SHARED with the old Short Notes exe: sheets.json (sheet list), tokens/, token.json,
    # pending/ (notes cache), local-blob/, probe/, last_account.json
    HOME / ".shortnotes_bridge",
    # Note Studio's OWN data: note_studio.db, runs/, queue/, categories/, engines/<name>/ state
    HOME / ".note_studio",
    # per-user state of the originals (UPSC + QC proxy pass, qc_cache, reference_index;
    # PW Books encrypted Google refresh token)
    _LOCALAPPDATA / "UPSC_Notes",
    _LOCALAPPDATA / "PWConciseNotes",
]

# ------------------------------------------------------------ engine copies (drift)
# engine copy folder (inside the project) -> original root. Files are compared by
# relative path. "allowed": the only files that may differ from the original
# (fnmatch patterns, relative to the engine folder). "protected": may never differ.
# Add to "allowed" only with a one-line reason, and say so in the phase report.
ENGINE_COPIES = {
    "shortnotes": {
        "copy": "engines/shortnotes",
        "allowed": {
            "sheets_bridge/google_auth.py": "macOS Keychain encryption; never reuse credentials from another computer",
            "sheets_bridge/macos_credentials.py": "native macOS Keychain encryption adapter",
            "sheets_bridge/windows_credentials.py": "DPAPI sidecars protect new sign-ins while preserving legacy shared files",
            "notesbot/categories.py": "category list read from the packs; labels/prompt_block/fingerprint byte-identical",
            "notesbot/generation_profile.py": "per-run model as a context variable",
            "sheets_bridge/pw_transport.py": "per-run model/account context; current original's budget-stop "
                                             "semantics preserved (8 October 2026 sync)",
        },
        "protected": ["notesbot/prompts.py", "notesbot/concise_policy.py", "notesbot/format_rules/*",
                      "notesbot/output_naming.py"],
    },
    "upsc": {
        "copy": "engines/upsc",
        "allowed": {
            "src/prompt_builder.py": "append-only 'extra instructions' hook (doc 08); prompt without it unchanged",
        },
        "protected": ["prompts/*", "src/dmp/prompts.py", "config/*.yaml"],
    },
    "pw_books": {
        "copy": "engines/pw_books",
        "allowed": {
            "src/pdf_exporter.py": "use local Chrome PDF conversion on macOS; preserve Windows conversion",
            "src/pdf_browser.py": "offline Pandoc MathML and Chrome PDF adapter for macOS",
            "requirements.txt": "conditional macOS browser dependency for PDF export",
            "src/prompt_builder.py": "append-only 'extra instructions' hook",
            "src/pw_auth.py": "token from the Studio (environment) instead of Streamlit",
        },
        "protected": ["prompts/*"],
    },
    "notes_qc": {
        "copy": "engines/notes_qc",
        "allowed": {},
        "protected": ["src/qc_prompts.py", "config/*.yaml"],
    },
}
TEXT_SUFFIXES = {".py", ".txt", ".json", ".yaml", ".yml", ".md", ".toml", ".html", ".js", ".css", ".cfg", ".ini"}

SKIP_DIRS = {"__pycache__", ".pytest_cache", ".ruff_cache", "node_modules", ".git", ".venv", "venv"}
JSON_SUFFIXES = {".json"}


def _targets(extra: list[str]):
    """(path, role, label) for everything snapshot watches."""
    out = []
    for name, spec in ORIGINALS.items():
        root = spec["root"]
        out += [(root / p, "data", name) for p in spec["data"]]
        out += [(root / p, "frozen", name) for p in spec["frozen"]]
    out += [(p, "data", "user") for p in USER_DATA]
    out += [(Path(p), "data", "extra") for p in extra]
    return out


def _walk(root: Path):
    if root.is_file():
        yield root
        return
    if not root.is_dir():
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            yield Path(dirpath) / name


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _json_shape(path: Path):
    """Entry counts of a JSON file (top level and one level down), no values."""
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return None
    if isinstance(data, list):
        return {"type": "list", "count": len(data)}
    if isinstance(data, dict):
        inner = {k: len(v) for k, v in data.items() if isinstance(v, (list, dict))}
        return {"type": "dict", "keys": sorted(data), "inner_counts": inner}
    return {"type": type(data).__name__}


# ------------------------------------------------------------ snapshot / compare
def snapshot(out: Path, extra: list[str]) -> None:
    files, absent = {}, []
    for root, role, label in _targets(extra):
        if not root.exists():
            absent.append(f"{label}: {root}")
            continue
        for f in _walk(root):
            if str(f) in files:
                continue
            try:
                st = f.stat()
                rec = {"size": st.st_size, "sha256": _sha256(f)}
            except OSError as exc:
                rec = {"error": type(exc).__name__}
            rec["role"], rec["project"] = role, label
            if f.suffix.lower() in JSON_SUFFIXES and role == "data":
                rec["json"] = _json_shape(f)
            files[str(f)] = rec
    doc = {"made": _dt.datetime.now().isoformat(timespec="seconds"), "absent_locations": absent, "files": files}
    out.write_text(json.dumps(doc, indent=1, ensure_ascii=False), encoding="utf-8")
    by = {}
    for r in files.values():
        key = f"{r['project']}/{r['role']}"
        n, s = by.get(key, (0, 0))
        by[key] = (n + 1, s + r.get("size", 0))
    print(f"snapshot: {len(files)} files -> {out}")
    for key in sorted(by):
        n, s = by[key]
        print(f"  {key:<22} {n:>6} files {s / 1e6:>9.1f} MB")
    for r in absent:
        print(f"  (not present: {r})")


def compare(before: Path, after: Path) -> int:
    a = json.loads(before.read_text(encoding="utf-8"))["files"]
    b = json.loads(after.read_text(encoding="utf-8"))["files"]
    role = lambda p: (a.get(p) or b.get(p) or {}).get("role", "data")
    gone = sorted(set(a) - set(b))
    new = sorted(set(b) - set(a))
    changed = sorted(p for p in set(a) & set(b) if a[p].get("sha256") != b[p].get("sha256"))
    data_gone = [p for p in gone if role(p) == "data"]
    frozen_hit = [f"{p} (removed)" for p in gone if role(p) == "frozen"]
    frozen_hit += [f"{p} (changed)" for p in changed if role(p) == "frozen"]
    frozen_hit += [f"{p} (added)" for p in new if role(p) == "frozen"]
    lost_entries = []
    for p in sorted(set(a) & set(b)):
        ja, jb = a[p].get("json"), b[p].get("json")
        if not ja or not jb:
            continue
        if ja.get("type") == "list" and jb.get("type") == "list" and jb["count"] < ja["count"]:
            lost_entries.append(f"{p}: list {ja['count']} -> {jb['count']}")
        if ja.get("type") == "dict" and jb.get("type") == "dict":
            for k in set(ja.get("keys", [])) - set(jb.get("keys", [])):
                lost_entries.append(f"{p}: key '{k}' removed")
            for k, n in ja.get("inner_counts", {}).items():
                m = jb.get("inner_counts", {}).get(k)
                if m is not None and m < n:
                    lost_entries.append(f"{p}: '{k}' {n} -> {m} entries")
    data_changed = [p for p in changed if role(p) == "data"]
    data_new = [p for p in new if role(p) == "data"]
    print(f"files before {len(a)}, after {len(b)}")
    print(f"  data gone: {len(data_gone)}   json entries lost: {len(lost_entries)}   "
          f"original projects touched: {len(frozen_hit)}   data changed: {len(data_changed)}   data new: {len(data_new)}")
    for title, rows in (("DATA GONE", data_gone), ("JSON ENTRIES LOST", lost_entries),
                        ("ORIGINAL PROJECT TOUCHED (Note Studio must copy, never edit)", frozen_hit),
                        ("DATA CHANGED", data_changed), ("DATA NEW", data_new)):
        if rows:
            print(f"\n{title}:")
            for r in rows[:200]:
                print(f"  {r}")
            if len(rows) > 200:
                print(f"  ... {len(rows) - 200} more")
    if data_gone or lost_entries or frozen_hit:
        print("\nRESULT: STOP.")
        if data_gone or lost_entries:
            print("  Data was lost: restore it from the backup zip, fix the cause, then compare again.")
        if frozen_hit:
            print("  An original project was edited: undo it (work in Note_Studio/engines/ instead). If the USER")
            print("  edited it on purpose, take a new baseline snapshot and say so in the report.")
        return 1
    print("\nRESULT: nothing lost, no original touched. DATA CHANGED/NEW must be expected "
          "(the old apps may still be in use) - name them in the report.")
    return 0


# ------------------------------------------------------------ drift (copies vs originals)
def _matches(rel: str, patterns) -> bool:
    return any(fnmatch.fnmatch(rel, p) for p in patterns)


def drift(project: Path, normalize_line_endings: bool = False) -> int:
    bad_total = 0
    for name, spec in ENGINE_COPIES.items():
        copy_root = project / spec["copy"]
        orig_root = ORIGINALS[name]["root"]
        if not copy_root.is_dir():
            print(f"\n[{name}] not copied yet ({copy_root})")
            continue
        changed, added, bad = [], [], []
        for f in _walk(copy_root):
            if f.suffix.lower() not in TEXT_SUFFIXES:
                continue
            rel = f.relative_to(copy_root).as_posix()
            src = orig_root / rel
            if not src.exists():
                # New files (engine_main.py, COPIED_FROM.md, new tests) are fine and listed;
                # only a new file inside a protected prompt/rules path is not.
                added.append(rel)
                if _matches(rel, spec["protected"]):
                    bad.append(f"{rel} (added inside a protected prompt/rules path)")
                continue
            if _sha256(f) == _sha256(src):
                continue
            if normalize_line_endings and f.read_bytes().replace(b"\r\n", b"\n") == src.read_bytes().replace(b"\r\n", b"\n"):
                continue
            changed.append(rel)
            if _matches(rel, spec["protected"]):
                bad.append(f"{rel} (PROTECTED - prompts/rules must stay byte-identical)")
            elif rel not in spec["allowed"]:
                bad.append(f"{rel} (not in the allowed list)")
        print(f"\n[{name}] {copy_root}  vs  {orig_root}")
        print(f"  changed: {len(changed)}   added: {len(added)}   not allowed: {len(bad)}")
        for rel in changed:
            reason = spec["allowed"].get(rel)
            print(f"  ~ {rel}" + (f"   (allowed: {reason})" if reason else ""))
        for rel in added:
            print(f"  + {rel}")
        for row in bad:
            print(f"  ! {row}")
        bad_total += len(bad)
    if bad_total:
        print("\nRESULT: STOP - a copied engine changed outside its allowed list. Undo it, or (only if the plan")
        print("needs it) add the file to ENGINE_COPIES[...]['allowed'] with a reason and tell the user.")
        return 1
    print("\nRESULT: every engine copy matches its original except the allowed, listed changes.")
    return 0


# ------------------------------------------------------------ backup
def backup(dest: Path, extra: list[str], light: bool) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out = dest / f"notes-data-{stamp}.zip"
    skips = set()
    if light:
        for spec in ORIGINALS.values():
            skips |= {spec["root"] / s for s in spec.get("light_skip", [])}
    n, total = 0, 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as z:
        for root, role, label in _targets(extra):
            if role != "data" or not root.exists() or root in skips:
                continue
            for f in _walk(root):
                if f.is_relative_to(HOME):
                    arc = f"{label}/{f.relative_to(HOME).as_posix()}"
                else:
                    arc = f"{label}/{f.name}"
                z.write(f, arc)
                n += 1
                total += f.stat().st_size
    print(f"backup: {n} files ({total / 1e6:.0f} MB before compression) -> {out}")
    if light:
        print("light: the old projects' runs/ folders were left out (Note Studio never edits them).")
    print("This zip contains secrets (.env, Google tokens). Keep it on this computer; never share it.")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("snapshot")
    s.add_argument("out", type=Path)
    s.add_argument("--extra", action="append", default=[], help="another data file/folder to watch")
    b = sub.add_parser("backup")
    b.add_argument("--dest", type=Path, default=HOME / "NoteStudio_backups")
    b.add_argument("--light", action="store_true", help="skip the old projects' runs/ folders")
    b.add_argument("--extra", action="append", default=[])
    c = sub.add_parser("compare")
    c.add_argument("before", type=Path)
    c.add_argument("after", type=Path)
    d = sub.add_parser("drift")
    d.add_argument("--project", type=Path, default=DEFAULT_PROJECT)
    d.add_argument("--baseline-ref", help="Compare engine copies with this trusted Git ref when originals are unavailable.")
    args = ap.parse_args(argv)
    if args.cmd == "snapshot":
        snapshot(args.out, args.extra)
    elif args.cmd == "backup":
        backup(args.dest, args.extra, args.light)
    elif args.cmd == "compare":
        return compare(args.before, args.after)
    else:
        if args.baseline_ref:
            project = args.project.resolve()
            archive = subprocess.check_output(["git", "-C", str(project), "archive", args.baseline_ref, "engines"])
            with tempfile.TemporaryDirectory(prefix="notecraft-engine-baseline-") as folder:
                with tarfile.open(fileobj=io.BytesIO(archive)) as source:
                    source.extractall(folder, filter="data")
                for name in ORIGINALS:
                    ORIGINALS[name]["root"] = Path(folder) / "engines" / name
                print(f"Git engine baseline: {args.baseline_ref} (original folders were not compared; checkout line endings normalized)")
                return drift(project, normalize_line_endings=True)
        return drift(args.project.resolve())
    return 0


if __name__ == "__main__":
    sys.exit(main())
