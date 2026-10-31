"""Copy an original note project into Note_Studio/engines/<name>/ (doc 08, recipe step 1).

    python NoteStudio_Kit/tools/copy_engine.py shortnotes [--dry-run]
    python NoteStudio_Kit/tools/copy_engine.py upsc       [--dry-run]

- Copies the WORKING TREE (files git does not track included), byte for byte with
  shutil.copy2. The original is only read.
- Takes only what the engine and its own tests need (the allow-lists below); leaves
  out runs, outputs, data, .env, .streamlit, client_secret*, venvs, builds and caches
  (doc 05). Any file that looks like a secret is skipped even inside an allowed folder.
- Refuses to write into an existing copy: delete nothing, overwrite nothing.
- Writes COPIED_FROM.md: source path, date, file count, git commit + `git status --short`.

Standard library only. Roots come from data_guard.ORIGINALS / ENGINE_COPIES.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import fnmatch
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from data_guard import DEFAULT_PROJECT, ENGINE_COPIES, ORIGINALS  # noqa: E402

# Never copied, at any depth.
SKIP_DIRS = {"__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache", "node_modules", ".git",
             ".venv", "venv", ".streamlit"}
SKIP_FILES = [".env", ".env.*", "client_secret*", "token.json", "secrets.toml", "proxy_session.json",
              "*.pyc", "*.pyo", "*.log"]

# What each engine copy takes from its original (relative to the original's root).
# "dirs": whole folders; "files": single files (may be nested, e.g. web/state.js).
PLANS = {
    "shortnotes": {
        "dirs": ["notesbot", "sheets_bridge", "api", "equation_kit", "fonts", "assets", "tests", "scripts",
                 "docs", "packaging"],
        "files": ["pw_access.py", "requirements.txt", "pyproject.toml", ".python-version", ".gitignore",
                  "README.md", "HANDOFF.md", "DESIGN.md", "verify_onboarding.py",
                  # read by tests/test_remote_pdf.py and the subject cross-check (doc 07 rule 3)
                  "web/state.js"],
        "left_out": {
            "output/, build/, dist/, tmp/": "runs, outputs and builds stay in the original",
            ".env, .env.example, client_secret.json, client_secret.personal.bak": "secrets / local config",
            "sheets.json, sheets.json.example": "data: the sheet list is read in place (~/.shortnotes_bridge)",
            "web/ (except state.js), public/, vercel.json, .vercelignore, package*.json, node_modules/":
                "the website and its JS build; Note Studio has its own pages",
            "pw-app-kit/, equation_kit.zip": "the kit's source and a zip of a folder that is copied",
        },
    },
    "upsc": {
        # data/ = the DPP workbooks (src/dpp_builder.py looks in <engine>/data/); the user asked for them
        # in the copy (4 Oct 2026). The original's data/ stays where it is.
        "dirs": ["src", "prompts", "config", "assets", "reference_documents", "third_party", "tests",
                 "scripts", "dist_assets", "data"],
        "files": ["app.py", "run_cli.py", "pw_access.py", "requirements.txt", ".gitignore", "README.md",
                  "TEAM_SETUP.md", "TROUBLESHOOTING.md", "COLAB_QUICKSTART.md", "dist_README_FIRST.md",
                  "UpscNotesApp.spec", "streamlit_launcher.py", "streamlit_launcher_stable.py",
                  "check_gemini.py", "check_gpt.py", "verify_onboarding.py", "START_WINDOWS.bat"],
        "left_out": {
            "runs/, outputs/, out/": "old runs and outputs are read in place (Runs -> From older apps)",
            ".env, .env.example, .streamlit/": "secrets / local config",
            ".venv/, build/, dist/, dist_staging/": "environment and build artefacts",
            ".agents/, .claude/, .pytest_runtime_*/, notebooks/ (empty), streamlit_run.log":
                "tool state, test leftovers and logs",
            "pw-app-kit/": "the kit's source; the engine uses its own pw_access.py",
        },
    },
    "pw_books": {
        # The working tree on purpose: src/image_filter.py, src/style_config.py, equation_kit/ and two
        # tests are not in git yet but the pipeline imports them (doc 08).
        # pw-app-kit/: tests/test_pw_kit_contract.py compares pw_access.py with the kit's template.
        "dirs": ["src", "prompts", "assets", "equation_kit", "tests", "scripts", "pw-app-kit"],
        "files": ["app.py", "run_cli.py", "pw_access.py", "requirements.txt", ".gitignore", "README.md",
                  "TROUBLESHOOTING.md", "HOW_TO_USE.txt", "TEAM_RELEASE_NOTES.txt", "COLAB_QUICKSTART.md",
                  "HandwrittenNotesAppTeam.spec", "streamlit_launcher_stable.py", "START_WINDOWS.bat",
                  "check_gemini.py"],
        "left_out": {
            "runs/, outputs/, logs/": "old runs and outputs stay in the original",
            ".env, .env.example, .streamlit/": "secrets / local config (tests/test_auth_refresh.py's refresh "
                                               "cases need .streamlit's Google client settings, so they pass "
                                               "only in the original)",
            ".venv/, build/, dist/, .test-tmp*/, notebooks/": "environment, builds and test leftovers",
            ".agents/, HOW_TO_USE_saved.txt, streamlit_run*.log": "tool state, a duplicate and logs",
            "pw-app-kit.zip, equation_kit.zip": "zips of folders that are copied",
        },
    },
    "notes_qc": {
        "dirs": ["src", "config", "reference_documents", "tests", "testdata"],
        "files": ["app.py", "run_cli.py", "launcher.py", "pw_access.py", "requirements.txt", ".gitignore",
                  "README.md", "READ_ME_FIRST.txt", "UPSC_QC.spec", "verify_onboarding.py"],
        "left_out": {
            "runs/, output/": "old runs and reports stay in the original",
            ".env.example, .streamlit/": "secrets / local config",
            ".venv/, build/, dist/": "environment and builds",
            "pw-app-kit/": "the kit's source; the engine uses its own pw_access.py",
        },
    },
}


def _skip_file(name: str) -> bool:
    return any(fnmatch.fnmatch(name, p) for p in SKIP_FILES)


def _git(root: Path, *args: str) -> str:
    try:
        out = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def plan_files(name: str) -> tuple[list[Path], list[str], list[str]]:
    """(relative files to copy, secret-like files skipped, planned entries that are missing)."""
    root = ORIGINALS[name]["root"]
    plan = PLANS[name]
    files, skipped, missing = [], [], []
    for d in plan["dirs"]:
        base = root / d
        if not base.is_dir():
            missing.append(d + "/")
            continue
        for path in sorted(base.rglob("*")):
            rel = path.relative_to(root)
            if any(part in SKIP_DIRS for part in rel.parts[:-1]) or path.is_dir():
                continue
            if _skip_file(path.name):
                skipped.append(rel.as_posix())
                continue
            files.append(rel)
    for f in plan["files"]:
        if (root / f).is_file():
            files.append(Path(f))
        else:
            missing.append(f)
    return files, skipped, missing


def add_missing(name: str, project: Path, dry_run: bool) -> int:
    """Copy planned files that the existing copy does not have yet. Never overwrites."""
    root = ORIGINALS[name]["root"]
    dest = project / ENGINE_COPIES[name]["copy"]
    if not dest.is_dir():
        print(f"STOP: {dest} does not exist yet; run without --add-missing first.")
        return 1
    files, skipped, missing = plan_files(name)
    new = [rel for rel in files if not (dest / rel).exists()]
    total = sum((root / f).stat().st_size for f in new)
    print(f"[{name}] add to {dest}: {len(new)} new files, {total / 1e6:.1f} MB; "
          f"already present (left as they are): {len(files) - len(new)}; missing in original: {missing or 'none'}")
    for rel in new:
        print(f"  + {rel.as_posix()}")
    if dry_run or not new:
        return 0
    for rel in new:
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / rel, target)
    note = dest / "COPIED_FROM.md"
    lines = ["", f"## Added {_dt.datetime.now().isoformat(timespec='seconds')} (`--add-missing`, nothing overwritten)",
             "", f"- Files: {len(new)} ({total / 1e6:.1f} MB)", *[f"- `{rel.as_posix()}`" for rel in new]]
    with note.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print(f"  copied; noted in {note}")
    return 0


def copy_engine(name: str, project: Path, dry_run: bool) -> int:
    root = ORIGINALS[name]["root"]
    dest = project / ENGINE_COPIES[name]["copy"]
    if dest.exists() and not dry_run:
        print(f"STOP: {dest} already exists. Nothing was copied (this tool never overwrites a copy;"
              " use --add-missing to add files the copy does not have yet).")
        return 1
    files, skipped, missing = plan_files(name)
    total = sum((root / f).stat().st_size for f in files)
    print(f"[{name}] {root} -> {dest}")
    print(f"  {len(files)} files, {total / 1e6:.1f} MB; secret-like skipped: {len(skipped)}; missing: {missing or 'none'}")
    if dry_run:
        return 0
    for rel in files:
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / rel, target)
    commit = _git(root, "rev-parse", "HEAD")
    status = _git(root, "status", "--short") if commit else ""
    plan = PLANS[name]
    lines = [
        f"# Copied from `{root}`",
        "",
        f"- Copied: {_dt.datetime.now().isoformat(timespec='seconds')} by "
        "`NoteStudio_Kit/tools/copy_engine.py` (byte for byte, `shutil.copy2`; the original was only read).",
        f"- Files: {len(files)} ({total / 1e6:.1f} MB)",
        f"- Git commit: `{commit}`" if commit else "- Git: the original is not a git repository.",
        "",
        "This is a COPY. Only the files listed in `ENGINE_COPIES['%s']['allowed']` in" % name,
        "`NoteStudio_Kit/tools/data_guard.py` may differ from the original; prompts and rules never.",
        "Check with `python NoteStudio_Kit/tools/data_guard.py drift`.",
        "",
        "## Taken",
        "",
        *[f"- `{d}/`" for d in plan["dirs"] if d + "/" not in missing],
        *[f"- `{f}`" for f in plan["files"] if f not in missing],
        "",
        "## Left out",
        "",
        *[f"- `{k}`: {v}" for k, v in plan["left_out"].items()],
        "- caches at any depth: " + ", ".join(f"`{d}`" for d in sorted(SKIP_DIRS)),
    ]
    if skipped:
        lines += ["", "## Skipped inside copied folders (secret-like or cache names)", "",
                  *[f"- `{s}`" for s in skipped]]
    if commit:
        lines += ["", "## `git status --short` of the original at copy time", "", "```",
                  status or "(clean)", "```"]
    (dest / "COPIED_FROM.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"  copied; wrote {dest / 'COPIED_FROM.md'}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("engine", choices=sorted(PLANS))
    ap.add_argument("--project", type=Path, default=DEFAULT_PROJECT)
    ap.add_argument("--dry-run", action="store_true", help="only list what would be copied")
    ap.add_argument("--add-missing", action="store_true",
                    help="add planned files an existing copy does not have yet (never overwrites)")
    args = ap.parse_args(argv)
    if args.add_missing:
        return add_missing(args.engine, args.project.resolve(), args.dry_run)
    return copy_engine(args.engine, args.project.resolve(), args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
