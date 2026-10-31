"""AI redraw of lecture diagrams into a handwritten notes style.

Uses the image model (IMAGE_MODEL_NAME) to convert a slide diagram (usually light text on a
dark slide) into a clean handwritten note: white background, blue handwritten
text/formulas, original diagram-line colours preserved but darkened where they
would vanish on white.
"""
from __future__ import annotations

from pathlib import Path

from .config import DEFAULT_IMAGE_MODEL

HANDWRITTEN_PROMPT = (
    "Create a handwritten notes-style image with a white background and blue "
    "handwritten text. The diagram's structure should remain the same as in the "
    "original image. All text and formulas should be handwritten, clear, "
    "high-resolution, and in blue. The diagram lines will retain their original "
    "color, but any light colors that would not be visible on a white background "
    "will be changed to a dark color. The image should be high resolution."
)

NOTES_DIAGRAM_PROMPT = (
    "Edit this lecture visual for a printed UPSC notes page. Remove the dark "
    "slide background, decorative background pattern and empty margins. Keep "
    "the original diagram layout, arrow directions, connections, map borders, "
    "colours and EVERY label, abbreviation, formula and number exactly as shown. "
    "Do not add, omit, translate, correct or invent content. Retain the original "
    "handwriting or type style. Darken white, yellow or other light foreground "
    "strokes enough to be readable over white paper. Use a plain pure white "
    "background with a small white margin; the app will turn that background "
    "into real transparency. Never draw a checkerboard, shadows or a textured "
    "paper background. Return a sharp, high-resolution diagram with the original "
    "aspect ratio."
)


def redraw_diagram_handwritten(client, image_path: Path, out_dir: Path, *, image_model: str = DEFAULT_IMAGE_MODEL, transparent_bg: bool = True, prompt: str = HANDWRITTEN_PROMPT) -> Path | None:
    """Redraw one diagram via AI. Returns the new image path, or None on failure.

    When transparent_bg is True the white background is made transparent so the
    PW watermark shows through the inserted diagram.
    """
    image_path = Path(image_path)
    if not image_path.exists():
        return None
    try:
        data = client.generate_image(prompt, image_path, image_model=image_model)
    except Exception:
        return None
    if not data:
        return None
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{image_path.stem}_handwritten.png"
        out_path.write_bytes(data)
        # Validate it's a real image; discard otherwise.
        try:
            from PIL import Image

            with Image.open(out_path) as im:
                im.verify()
        except Exception:
            return None
        if transparent_bg:
            from .image_tools import make_white_transparent

            trans = make_white_transparent(out_path, out_dir / f"{image_path.stem}_handwritten_t.png")
            with Image.open(trans) as image:
                alpha = image.convert("RGBA").getchannel("A")
                if alpha.getextrema()[0] == 255 or alpha.getextrema()[1] == 0:
                    return None
            return trans
        return out_path
    except Exception:
        return None


def redraw_slides_handwritten(client, slides, slide_numbers, out_dir: Path, *, image_model: str = DEFAULT_IMAGE_MODEL, max_workers: int = 4) -> tuple[int, list[str]]:
    """Redraw the images of the given slide numbers in place (mutates image_path).

    Redraws run concurrently (network-bound). Returns (count_redrawn, warnings).
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    warnings: list[str] = []
    count = 0
    targets = [s for s in slides if s.slide_no in slide_numbers and s.image_path and Path(s.image_path).exists()]
    if not targets:
        return 0, warnings

    def _do(s):
        return s, redraw_diagram_handwritten(client, s.image_path, out_dir, image_model=image_model)

    with ThreadPoolExecutor(max_workers=min(max_workers, len(targets))) as ex:
        futures = [ex.submit(_do, s) for s in targets]
        for fut in as_completed(futures):
            s, new = fut.result()  # redraw_diagram_handwritten never raises (returns None)
            if new:
                s.image_path = new
                count += 1
            else:
                warnings.append(f"AI redraw failed for slide {s.slide_no}; kept the original diagram image.")
    return count, warnings


def clean_visuals_for_notes(client, registry, out_dir: Path, *,
                            image_model: str = DEFAULT_IMAGE_MODEL,
                            max_workers: int = 4, stage_callback=None) -> tuple[int, list[str]]:
    """Edit only diagrams that survived placement; retain originals on failure."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from PIL import Image

    targets = [v for v in registry.selected
               if v.image_path and Path(v.image_path).is_file()
               and v.visual_type not in {"photo", "decorative", "text"}]
    if client is None or not targets:
        return 0, []
    warnings = []
    changed = 0

    def progress(done):
        if stage_callback:
            stage_callback(f"Editing diagrams ({done} of {len(targets)})")

    def edit(visual):
        return redraw_diagram_handwritten(
            client, Path(visual.image_path), out_dir,
            image_model=image_model, transparent_bg=True, prompt=NOTES_DIAGRAM_PROMPT)

    progress(0)
    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(targets)))) as pool:
        futures = {pool.submit(edit, visual): visual for visual in targets}
        for done, future in enumerate(as_completed(futures), start=1):
            visual = futures[future]
            try:
                path = future.result()
                if path:
                    with Image.open(path) as image:
                        width, height = image.size
                    visual.original_image_path = visual.original_image_path or visual.image_path
                    visual.image_path = path
                    visual.width_px, visual.height_px = width, height
                    visual.image_edit_status = "transparent"
                    changed += 1
                else:
                    visual.image_edit_status = "failed"
                    warnings.append(f"Diagram cleanup failed for slide {visual.slide_no}; the original image was kept.")
            except Exception:
                visual.image_edit_status = "failed"
                warnings.append(f"Diagram cleanup failed for slide {visual.slide_no}; the original image was kept.")
            progress(done)
    return changed, warnings
