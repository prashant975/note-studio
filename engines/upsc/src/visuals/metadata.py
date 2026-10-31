"""The one record every visuals stage reads and writes.

A `VisualCandidate` is born in the extractor (id, slide, crop, hash), filled in
by the classifier (relevance, type, topic, caption), decided on by the selector
(insert / reason), and finally annotated by the placement pass (section,
placed_by). The registry serialises that lifecycle to ``visual_registry.json``
in the run folder, which makes it the single source the DOCX writer, the
rebuild flow and the visual QC all consult — none of them re-derives anything.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

REGISTRY_FILENAME = "visual_registry.json"
#: Where the cropped figure PNGs live inside a run folder.
VISUALS_DIRNAME = "visuals"


@dataclass
class VisualCandidate:
    """One visual lifted from one slide, through its whole lifecycle."""

    visual_id: str                  # "visual_007_01" — slide 7, first visual
    slide_no: int
    source_type: str = ""           # "pdf" | "pptx"
    image_path: Path | None = None  # cropped PNG under run_dir/visuals/
    width_px: int = 0
    height_px: int = 0
    origin: str = ""                # embedded_image | region_render | full_slide
    phash: str = ""                 # 64-bit average hash, for duplicates
    slide_heading: str = ""

    # --- classification (Gemini, or heuristic when no client is available) ---
    classified: bool = False
    relevance: float = 0.0
    visual_type: str = ""           # flowchart | map | chart | diagram | table | photo | decorative | text
    topic: str = ""
    caption: str = ""
    description: str = ""
    prefer_text_table: bool = False

    # --- decision + placement ------------------------------------------------
    insert: bool = False
    reason: str = ""                # why it is in, or why it is out
    section: str = ""               # heading the figure ended up under
    placed_by: str = ""             # "model" | "section_match" | ""
    original_image_path: Path | None = None  # preserved before optional image cleanup
    image_edit_status: str = "not_requested"  # not_requested | transparent | failed

    def to_dict(self) -> dict:
        data = asdict(self)
        data["image_path"] = str(self.image_path) if self.image_path else None
        data["original_image_path"] = str(self.original_image_path) if self.original_image_path else None
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "VisualCandidate":
        img = data.get("image_path")
        known = {f for f in cls.__dataclass_fields__}  # tolerate future fields
        payload = {k: v for k, v in data.items() if k in known}
        payload["image_path"] = Path(img) if img else None
        original = data.get("original_image_path")
        payload["original_image_path"] = Path(original) if original else None
        return cls(**payload)


@dataclass
class VisualRegistry:
    """All of a run's visuals plus the stage log, in insertion order."""

    visuals: list[VisualCandidate] = field(default_factory=list)
    #: Non-fatal problems, e.g. "Visual extraction failed for slide 4".
    issues: list[str] = field(default_factory=list)
    classifier_status: str = "not_run"   # not_run | completed | failed | heuristic

    def by_id(self, visual_id: str) -> VisualCandidate | None:
        for v in self.visuals:
            if v.visual_id == visual_id:
                return v
        return None

    @property
    def selected(self) -> list[VisualCandidate]:
        return [v for v in self.visuals if v.insert]

    def prompt_entries(self) -> list[dict]:
        """The metadata block the note-generation prompt sees (no image data)."""
        return [
            {
                "visual_id": v.visual_id,
                "slide": v.slide_no,
                "visual_type": v.visual_type or "diagram",
                "topic": v.topic or v.slide_heading,
                "description": v.description or v.caption,
            }
            for v in self.selected
        ]

    def writer_map(self) -> dict[str, dict]:
        """What the DOCX writer needs per id: path, caption, pixel size."""
        return {
            v.visual_id: {
                "path": str(v.image_path) if v.image_path else "",
                "caption": v.caption,
                "width_px": v.width_px,
                "height_px": v.height_px,
                "visual_type": v.visual_type,
                "slide_no": v.slide_no,
            }
            for v in self.selected
            if v.image_path
        }

    def to_dict(self) -> dict:
        return {
            "classifier_status": self.classifier_status,
            "issues": self.issues,
            "visuals": {v.visual_id: v.to_dict() for v in self.visuals},
        }

    def save(self, run_dir: Path) -> Path:
        path = Path(run_dir) / REGISTRY_FILENAME
        path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    @classmethod
    def load(cls, run_dir: Path) -> "VisualRegistry | None":
        path = Path(run_dir) / REGISTRY_FILENAME
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            registry = cls(
                issues=list(data.get("issues") or []),
                classifier_status=data.get("classifier_status", "not_run"),
            )
            for _, item in sorted((data.get("visuals") or {}).items()):
                registry.visuals.append(VisualCandidate.from_dict(item))
            return registry
        except Exception:
            return None
