"""Entry point for the packaged ShortNotesStudio.exe.

A frozen build has no repo on disk: the code lives in a read-only temp folder
that PyInstaller unpacks per run. Everything the app needs to *write* must
therefore be pointed at a real folder first, before ``notesbot.config`` is
imported and starts reading the environment.

Layout the packaged app uses:

    <bundle>/                 read-only, recreated every launch
        fonts/ assets/ public/ client_secret.json sheets.json (seed)
    %USERPROFILE%/.shortnotes_bridge/
        sheets.json           the operator's own sheet list (Studio writes it)
        tokens/               one Google token per signed-in account
        pending/ local-blob/  caches
    <folder holding the .exe>/
        .env                  optional; overrides any default below
        client_secret.json    optional; overrides the bundled OAuth client
"""
from __future__ import annotations

import os
import shutil
import sys
import traceback
from pathlib import Path

STATE_DIR = Path.home() / ".shortnotes_bridge"

# Non-secret generation defaults, matching the values the app is run with
# today. A real environment variable or a .env beside the exe always wins.
DEFAULTS = {
    # Matches .env: Gemini is the default again (2026-09-24). GPT models stay
    # selectable in the Studio's Model dropdown (routed via /api/gpt/generate).
    "PW_PROXY_MODEL": "gemini-3.7-flash",
    "VISUAL_SOURCE_CHUNK_MAX_BYTES": "9437184",
    "MAX_CHUNKS": "16",
    "MAX_IMAGES": "2",
    "MAX_PDF_MB": "100",
    "NOTES_IMAGES": "true",
    "NOTES_DESIGN": "false",
    "NOTES_QC": "false",
    "DIAGRAM_MODE": "auto",
    "SHEETS_BRIDGE_CACHE": "off",
}


def bundle_root() -> Path:
    """The read-only folder holding the bundled fonts/assets/public."""
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        return Path(meipass)
    return Path(__file__).resolve().parents[1]


def exe_dir() -> Path:
    """The folder the operator actually put the .exe in."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def load_env_file(path: Path) -> None:
    """Same shape as notesbot.config._load_dotenv, run before that import."""
    if not path.is_file():
        return
    try:
        text = path.read_text(encoding="utf-8-sig", errors="ignore")
    except OSError:
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _seed(source: Path, destination: Path) -> None:
    """Copy a bundled starter file out to writable storage, once."""
    if destination.exists() or not source.is_file():
        return
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    except OSError:
        pass


def prepare_environment() -> None:
    bundle = bundle_root()
    beside = exe_dir()
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    # Frozen: PyInstaller already has the bundle on sys.path. From source this
    # file sits in packaging/, so the repo root has to be added by hand.
    if str(bundle) not in sys.path:
        sys.path.insert(0, str(bundle))

    # 1. Operator overrides first: a .env next to the exe wins over everything.
    load_env_file(beside / ".env")
    for key, value in DEFAULTS.items():
        os.environ.setdefault(key, value)

    # 2. OAuth client: one beside the exe replaces the bundled one.
    if not os.environ.get("SHEETS_BRIDGE_CLIENT_SECRET"):
        local_client = beside / "client_secret.json"
        bundled_client = bundle / "client_secret.json"
        chosen = local_client if local_client.is_file() else bundled_client
        os.environ["SHEETS_BRIDGE_CLIENT_SECRET"] = str(chosen)

    # 3. sheets.json must be writable: the Studio's "+ Add sheet" saves to it.
    if not os.environ.get("SHEETS_BRIDGE_PROFILES"):
        profiles = STATE_DIR / "sheets.json"
        _seed(bundle / "sheets.json", profiles)
        os.environ["SHEETS_BRIDGE_PROFILES"] = str(profiles)

    # 4. Token cache: per-account files live in STATE_DIR/tokens beside this.
    os.environ.setdefault("SHEETS_BRIDGE_TOKEN", str(STATE_DIR / "token.json"))

    # A frozen build has no console encoding guarantees; Devanagari lecture
    # names go through print() constantly.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="backslashreplace")
            except (ValueError, OSError):
                pass


def self_test() -> int:
    """Render a small PDF offline, proving the packaged build is complete.

    This is the part a frozen build silently gets wrong: the handwriting fonts,
    matplotlib's maths rendering and the watermark logo are all read from disk
    at render time, so a missing data file would only surface once a lecture
    had already been paid for. Costs nothing, needs no network or sign-in.
    """
    import time

    from notesbot import notes_pdf
    from notesbot.chemistry_repair import normalize_chemistry_notation
    from notesbot.math_render import render_latex
    from notesbot.notes_pdf import build_notes_pdf
    from notesbot.prompts import Section, ShortNotes

    started = time.time()
    checks: list[tuple[bool, str]] = []

    font_dir = Path(notes_pdf.FONT_DIR)
    for face in ("PatrickHand-Regular.ttf", "Kalam-Regular.ttf", "Kalam-Bold.ttf"):
        checks.append(((font_dir / face).is_file(), f"font {face}"))
    checks.append((Path(notes_pdf.LOGO_WATERMARK_PATH).is_file(), "watermark logo"))

    png, width, height = render_latex(r"E_{cell} = E^{0} - \frac{RT}{nF}")
    checks.append((bool(png) and width > 0 and height > 0, "maths rendering (matplotlib)"))
    for formula in (r"80\%\text{ Cu},20\%\text{ Zn}", r"\ge 25^\circ\text{C}"):
        png, width, height = render_latex(formula)
        checks.append((bool(png) and width > 0 and height > 0, "percentage/temperature formula " + formula))

    bond = "Double Covalent Bond: an $O_2$ molecule ($= O$) shares two pairs of electrons."
    reaction = r"$Pb(s)+CuCl_2(aq)\rightarrow PbCl_2(aq)+Cu(s)$"
    checks.append((normalize_chemistry_notation(bond) == bond.replace("$= O$", "$=$"),
                   "standalone covalent bond symbol repaired"))
    checks.append((normalize_chemistry_notation(reaction) == reaction.replace("PbCl_2(aq)", "PbCl_2(s)"),
                   "lead chloride displacement product state repaired"))
    for valid in ("Double Covalent Bond: $O=O$.", r"$PbCl_2(s)\rightleftharpoons Pb^{2+}(aq)+2Cl^{-}(aq)$"):
        checks.append((normalize_chemistry_notation(valid) == valid,
                       "valid molecular structure/dissolution preserved"))

    notes = ShortNotes(
        lecture_no="Self test",
        subject="Chemistry",
        title="Packaged build self test",
        sections=[
            Section(
                heading="Rendering check",
                icon="battery",
                points=[
                    "Handwriting font, **emphasis** and bullets.",
                    r"$E_{cell} = E^{0} - \frac{RT}{nF} \ln Q$",
                    "रसायन विज्ञान (Devanagari shaping).",
                    "Simple distillation: boiling point difference at least 25 °C.",
                    r"Brass: $80\%\text{ Cu},20\%\text{ Zn}$.",
                    bond,
                    reaction,
                ],
                tricks=["Trick boxes and doodles draw from bundled assets."],
            )
        ],
    )
    pdf = build_notes_pdf(notes, "Self test", watermark=True, logo_watermark=True)
    checks.append((len(pdf) > 20_000, f"notes PDF rendered ({len(pdf):,} bytes)"))
    from io import BytesIO
    from pypdf import PdfReader
    unnumbered = build_notes_pdf(notes, "Self test", watermark=False, show_page_numbers=False)
    numbered_text = " ".join(p.extract_text() or "" for p in PdfReader(BytesIO(pdf)).pages)
    unnumbered_text = " ".join(p.extract_text() or "" for p in PdfReader(BytesIO(unnumbered)).pages)
    checks.append(("25 °C" in numbered_text and not notes_pdf.rendered_notation_findings(pdf),
                   "rendered notation quality check and real degree symbol"))
    checks.append(("Page 1" in numbered_text and "Page 1" not in unnumbered_text,
                   "page numbers included by default and optional"))
    from sheets_bridge.webui import render_page
    page = render_page().decode("utf-8")
    checks.append(('id="showPageNumbers" checked' in page
                   and page.index('id="showPageNumbers"') < page.index('id="run"'),
                   "automation settings appear before Run"))

    out = STATE_DIR / "self-test.pdf"
    try:
        out.write_bytes(pdf)
        checks.append((True, f"written to {out}"))
    except OSError as exc:
        checks.append((False, f"cannot write {out}: {exc}"))

    failed = 0
    for ok, label in checks:
        print(("[OK]  " if ok else "[!!]  ") + label, flush=True)
        failed += not ok
    verdict = "all checks passed" if not failed else f"{failed} check(s) failed"
    print(f"\n{verdict} in {time.time() - started:.1f}s", flush=True)
    return 1 if failed else 0


def main() -> int:
    prepare_environment()

    argv = sys.argv[1:]
    if argv and argv[0] == "--self-test":
        return self_test()
    if not argv:
        # Double-clicking the exe should open the Studio, not print usage.
        argv = ["--serve"]

    from sheets_bridge.cli import main as cli_main

    return cli_main(argv)


if __name__ == "__main__":
    try:
        code = main()
    except KeyboardInterrupt:
        code = 130
    except SystemExit as exc:
        code = int(exc.code or 0)
    except BaseException:  # noqa: BLE001 - a crash must not close the window
        traceback.print_exc()
        print("\nThe app stopped with the error above.", flush=True)
        try:
            input("Press Enter to close this window ... ")
        except EOFError:
            pass
        code = 1
    sys.exit(code)
