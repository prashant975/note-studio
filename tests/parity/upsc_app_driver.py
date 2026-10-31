"""Run one class through an engine folder's pipeline the way its app.py does,
for the UPSC prompt-parity test (the original's folder against NoteCraft's
engine_main in the copy).

    python upsc_app_driver.py <engine root> <out dir> <request.json>

Nothing about the call is retyped here: the app's backend defaults (the block
from `SUBJECT = defaults.DEFAULT_SUBJECT` to `IMAGE_MODEL = ...`), its
_default_dcn_logo_path() and its `run_pipeline(...)` call are taken from that
folder's app.py source and run as written. The only change is allow_mock=True,
the one way to run offline, with what the operator types filled in from
request.json. The same test-only guards as engine_main apply: no Word or
LibreOffice (stubbed, as the engine's own tests do) and the per-user cache in
<out>, so nothing outside <out> is written.
"""
from __future__ import annotations

import ast
import json
import os
import sys
from pathlib import Path

root, out = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
request = json.loads(Path(sys.argv[3]).read_text(encoding="utf-8"))
os.environ.update(HANDWRITTEN_NOTES_ROOT=str(root), HANDWRITTEN_NOTES_ENV=str(out / "no-env" / ".env"),
                  RUNS_DIR=str(out / "engine_runs"), OUTPUT_DIR=str(out / "engine_outputs"))
sys.path.insert(0, str(root))

import src.config  # noqa: E402
import src.pdf_exporter  # noqa: E402
import src.pipeline  # noqa: E402
from src import app_defaults  # noqa: E402
from src.qc import paths as qc_paths  # noqa: E402
from src.qc import reference_index  # noqa: E402


def no_pdf(docx_path, output_dir):
    return None, "PDF export is stubbed in tests."


def app_data_dir(*parts):
    path = (out / "engine_state").joinpath(*parts)
    path.mkdir(parents=True, exist_ok=True)
    return path


src.pdf_exporter.export_docx_to_pdf = src.pipeline.export_docx_to_pdf = no_pdf
qc_paths.app_data_dir = reference_index.app_data_dir = app_data_dir

tree = ast.parse((root / "app.py").read_text(encoding="utf-8"))
body = tree.body
logo_def = next(n for n in body if isinstance(n, ast.FunctionDef) and n.name == "_default_dcn_logo_path")
start = next(i for i, n in enumerate(body) if ast.unparse(n) == "SUBJECT = defaults.DEFAULT_SUBJECT")
end = next(i for i in range(start, len(body)) if ast.unparse(body[i]).startswith("IMAGE_MODEL ="))
call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "run_pipeline")
for keyword in call.keywords:
    if keyword.arg == "allow_mock":
        keyword.value = ast.copy_location(ast.Constant(True), keyword.value)

ns = {k: v for k, v in vars(src.config).items() if not k.startswith("_")}
ns.update(defaults=app_defaults, run_pipeline=src.pipeline.run_pipeline)
exec(compile(ast.Module(body=[logo_def, *body[start:end + 1]], type_ignores=[]), "app.py", "exec"), ns)

# What the operator types on the app's page (app.py:2803-2875), and the stage line.
languages = {"english": "English", "hindi": "Hindi", "marathi": "Marathi"}
ns.update(
    SUBJECT=str(request.get("subject") or "") or app_defaults.DEFAULT_SUBJECT,
    LANGUAGE=languages[request["language"]],
    LECTURE_TITLE=str(request.get("lecture_title") or ""), DCN_NO=str(request.get("dcn_no") or ""),
    DMP_NO=str(request.get("dmp_no") or ""), DPP_NO=str(request.get("dpp_no") or ""),
    video_url=str(request.get("video_url") or ""),
    supplied_drive_transcript=str(request.get("transcript_text_override") or "").strip(),
    tmp_path=Path(request["slides"]) if request.get("slides") else None,
    google_token="", _show_stage=lambda label: None,
    # The app fetches these per run from the sign-in's Drive access; no sign-in here.
    drive_token="", drive_account="",
)
result = eval(compile(ast.fix_missing_locations(ast.Expression(call)), "app.py", "eval"), ns)
print(json.dumps({"run_dir": str(result.run_dir)}))
