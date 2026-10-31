"""Refresh NoteCraft's engine copies from their originals, preserving integration hooks.

Only planned code/assets are copied; credentials, runs and user data are excluded
by copy_engine.plan_files. Changed files are backed up inside _build first.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import shutil
from pathlib import Path

from copy_engine import plan_files
from data_guard import DEFAULT_PROJECT, ENGINE_COPIES, ORIGINALS


def sync(name: str, project: Path, *, dry_run: bool = False) -> int:
    source = ORIGINALS[name]["root"]
    target = project / ENGINE_COPIES[name]["copy"]
    files, skipped, missing = plan_files(name)
    if not source.is_dir() or not target.is_dir():
        raise ValueError(f"Both the original and existing {name} copy must exist.")
    stamp = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5, minutes=30)))
    backup = project / "_build" / "engine_sync_backups" / stamp.strftime("%Y%m%d-%H%M%S") / name
    preserved = set(ENGINE_COPIES[name]["allowed"])
    changed = []
    for rel in files:
        if rel.as_posix() in preserved:
            continue
        old, new = target / rel, source / rel
        if old.is_file():
            with old.open("rb") as before, new.open("rb") as after:
                if hashlib.file_digest(before, "sha256").digest() == hashlib.file_digest(after, "sha256").digest():
                    continue
        changed.append(rel.as_posix())
        if dry_run:
            continue
        if old.is_file():
            saved = backup / rel
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(old, saved)
        old.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(new, old)
    if changed and not dry_run:
        with (target / "COPIED_FROM.md").open("a", encoding="utf-8") as handle:
            handle.write(f"\n## Synced {stamp.isoformat(timespec='seconds')}\n\n"
                         f"- Source: `{source}` (read only).\n"
                         f"- Refreshed {len(changed)} files with `shutil.copy2`; previous copies: `{backup}`.\n"
                         "- Preserved only the listed integration hooks; revalidate them against the original.\n")
            handle.writelines(f"- `{rel}`\n" for rel in changed)
    print(f"{name}: {len(changed)} refreshed; {len(preserved)} integration files preserved; "
          f"{len(skipped)} excluded; missing entries: {missing or 'none'}")
    return len(changed)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("engine", choices=("shortnotes", "upsc"))
    parser.add_argument("--project", type=Path, default=DEFAULT_PROJECT)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    sync(args.engine, args.project.resolve(), dry_run=args.dry_run)
