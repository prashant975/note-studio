"""Run one lecture through an engine folder's PW Books pipeline the way its
app.py does, for the PW Books parity test (the original's folder against Note
Studio's engine_main in the copy).

    python pw_books_app_driver.py <engine root> <out dir> <request.json>

Nothing about the call is retyped here. Taken from that folder's app.py source
and run as written: the fixed settings (SUBJECT_OPTIONS ... IMAGE_MODEL),
_redraw_enabled, the backend processing-mode block (PROCESSING_MODE ...
_profile), the model-routing block (the body of `if authorized:`) and the
`run_pipeline(...)` call. The only change is allow_mock=True, the one way to run
offline. The PDF converter is stubbed (no Word/LibreOffice), as engine_main does
for tests, so nothing outside <out> is written.
"""
from __future__ import annotations

import ast
import contextlib
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
import src.pipeline  # noqa: E402
from src import model_router  # noqa: E402

src.pipeline.export_docx_to_pdf = lambda *a, **k: (None, "PDF export is stubbed in tests.")

tree = ast.parse((root / "app.py").read_text(encoding="utf-8"))
body = tree.body
first = next(i for i, n in enumerate(body) if ast.unparse(n).startswith("SUBJECT_OPTIONS ="))
last = next(i for i in range(first, len(body)) if ast.unparse(body[i]).startswith("IMAGE_MODEL ="))
redraw = next(n for n in body if isinstance(n, ast.FunctionDef) and n.name == "_redraw_enabled")


def block(start_text: str, end_text: str) -> list:
    """The statements from one to another inside whichever body holds them."""
    for node in ast.walk(tree):
        for field in ("body", "orelse"):
            stmts = getattr(node, field, None)
            if not isinstance(stmts, list):
                continue
            starts = [i for i, s in enumerate(stmts) if ast.unparse(s).startswith(start_text)]
            if starts:
                end = next(i for i in range(starts[0], len(stmts)) if ast.unparse(stmts[i]).startswith(end_text))
                return stmts[starts[0]:end + 1]
    raise LookupError(start_text)


mode_block = block("PROCESSING_MODE = DEFAULT_PROCESSING_MODE", "_profile = mr.MODE_PROFILES")
routing = next(n for n in ast.walk(tree) if isinstance(n, ast.If) and ast.unparse(n.test) == "authorized"
               and ast.unparse(n.body[0]).startswith("_cfg = mr.load_routing_config()")).body
call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "run_pipeline")
for keyword in call.keywords:
    if keyword.arg == "allow_mock":
        keyword.value = ast.copy_location(ast.Constant(True), keyword.value)


class _St:
    """Only what the routing block uses."""

    @staticmethod
    def spinner(*_a, **_k):
        return contextlib.nullcontext()

    @staticmethod
    def error(message):
        raise RuntimeError(message)


ns = {k: v for k, v in vars(src.config).items() if not k.startswith("_")}
ns.update(os=os, mr=model_router, st=_St, run_pipeline=src.pipeline.run_pipeline,
          _model_probe=lambda: model_router.make_probe(""), _manual_overrides=lambda: {})
exec(compile(ast.Module(body=[*body[first:last + 1], redraw, *mode_block, *routing], type_ignores=[]),
             "app.py", "exec"), ns)
# What the operator picks on the app's page (app.py:1183-1242, 1264-1269).
ns.update(tmp_path=Path(request["file"]), SUBJECT=request["subject"],
          LANGUAGE={"english": "English", "hindi": "Hindi"}[request["language"]], MODE=request["mode"],
          EXAM=request.get("exam") or "", MATH_RENDER_MODE=request.get("math_render_mode") or "omml",
          STRICT_MATH=bool(request.get("strict_math", True)), google_token="", retry_notices=[])
result = eval(compile(ast.fix_missing_locations(ast.Expression(call)), "app.py", "eval"), ns)
print(json.dumps({"run_dir": str(result.run_dir)}))
