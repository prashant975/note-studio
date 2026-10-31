"""Check one file through an engine folder's QC pipeline the way its app.py does,
for the Check notes parity test (the original's folder against Note Studio's
engine_main in the copy). Basic checks only (no token), the one way to run offline.

    python notes_qc_app_driver.py <engine root> <out dir> <file to check>

The `run_pdf_qc_batch(...)` call is taken from that folder's app.py source and
run as written, with the app's own OUTPUTS_DIR (pointed inside <out>).
"""
from __future__ import annotations

import ast
import json
import os
import sys
from pathlib import Path

root, out, file = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve(), Path(sys.argv[3]).resolve()
os.environ.update(UPSC_PDF_QC_ROOT=str(root), UPSC_PDF_QC_ENV=str(out / "no-env" / ".env"),
                  RUNS_DIR=str(out / "engine_runs"), OUTPUT_DIR=str(out / "engine_outputs"))
for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
    os.environ.pop(name, None)
sys.path.insert(0, str(root))

from src.config import OUTPUTS_DIR  # noqa: E402
from src.pipeline import run_pdf_qc_batch  # noqa: E402

tree = ast.parse((root / "app.py").read_text(encoding="utf-8"))
call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "run_pdf_qc_batch")
ns = {"run_pdf_qc_batch": run_pdf_qc_batch, "paths": [file], "token": "", "OUTPUTS_DIR": OUTPUTS_DIR,
      "_on_file": lambda *a, **k: None, "_on_stage": lambda *a, **k: None}
run = eval(compile(ast.fix_missing_locations(ast.Expression(call)), "app.py", "eval"), ns)
print(json.dumps({"run_dir": str(run.run_dir), "workbook": str(run.workbook_path or "")}))
