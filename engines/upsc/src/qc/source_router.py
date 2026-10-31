"""Route a subject string to its approved-source configuration.

The app passes subject strings from several places — the UPSC_AREAS picker
("Science & Tech"), SUBJECT_LABELS display names ("Indian Polity &
Governance"), raw detector keys ("ir"), even Hindi labels ("भारतीय
राजव्यवस्था"). All of them must land on exactly one subject entry in
config/upsc_qc_sources.yaml, because verifying a claim against the *wrong*
subject's books is worse than not verifying it at all. So the router either
resolves confidently or reports "unconfigured" — it never guesses and never
substitutes unrelated sources (spec §11).

The one deliberate exception is the "history" umbrella: the area picker lets
an aspirant choose plain "History" before auto-detection has narrowed the
class to ancient/medieval/modern. The YAML marks that with the
"_history_umbrella" sentinel and the router merges all three history subjects,
with a warning so the run artifacts show the broader-than-usual source pool.

local_index paths inside book dicts are passed through untouched — resolving
them against app_data_dir()/repo root is the reference-index module's job.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..config import SUBJECT_LABELS
from . import qc_config
from .qc_config import SourceRegistry

# Sentinel used in the YAML alias table for the app's broad "History" area.
HISTORY_UMBRELLA = "_history_umbrella"

# Merge order matters: books are deduped by id with first occurrence winning,
# so chronological order keeps the merged list stable and predictable.
_HISTORY_PARTS = ("ancient_history", "medieval_history", "modern_history")


@dataclass
class SubjectSources:
    """Approved sources for one resolved subject (or an explicit 'nothing').

    configured=False means "we have no approved sources for this subject";
    downstream must then mark claims SOURCE_UNAVAILABLE rather than fall back
    to arbitrary web content — the blocked_source_types policy has no meaning
    if an unconfigured subject opens the door to anything.
    """

    subject_key: str
    display_name: str
    official_domains: list[str] = field(default_factory=list)
    books: list[dict] = field(default_factory=list)
    official_publications: list[str] = field(default_factory=list)
    claim_types: list[str] = field(default_factory=list)
    configured: bool = False
    warnings: list[str] = field(default_factory=list)


def _unique(values: list[str]) -> list[str]:
    """Order-preserving dedupe: the three history subjects share claim types
    (e.g. 'chronology') and duplicates would make the claim extractor query
    the same type repeatedly."""
    seen: set[str] = set()
    out: list[str] = []
    for v in values:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


class SourceRouter:
    """Resolve app subject strings to SubjectSources from the registry."""

    def __init__(self, registry: SourceRegistry | None = None):
        self._registry = registry if registry is not None else qc_config.load_sources()
        # SUBJECT_LABELS is key -> display label ("polity" -> "Indian Polity &
        # Governance"). Reverse it so a UI label the alias table doesn't list
        # can still resolve via its app key. Built once — the labels are
        # module constants and the registry is cached per process.
        self._label_to_app_key = {
            str(label).strip().casefold(): key for key, label in SUBJECT_LABELS.items()
        }

    def normalise(self, subject: str | None) -> str | None:
        """Return the registry subject key (or the umbrella sentinel), else None.

        Lookup order — most explicit first, so a curated alias always beats a
        coincidental label match:
          1. the YAML alias table,
          2. an exact registry subject key,
          3. a SUBJECT_LABELS display name, whose app key is then mapped
             through the alias table (app keys like "science" are aliases,
             not registry keys).
        """
        if not subject:
            return None
        cleaned = str(subject).strip().casefold()
        if not cleaned:
            return None

        target = self._registry.subject_aliases.get(cleaned)
        if target:
            return target
        if cleaned in self._registry.subjects:
            return cleaned

        app_key = self._label_to_app_key.get(cleaned)
        if app_key:
            target = self._registry.subject_aliases.get(app_key)
            if target:
                return target
            if app_key in self._registry.subjects:
                return app_key
        return None

    def get_sources(self, subject: str | None) -> SubjectSources:
        """Approved sources for a subject string; never raises for bad input."""
        shown = str(subject).strip() if subject else ""
        key = self.normalise(subject)

        if key is None:
            return SubjectSources(
                subject_key=shown,
                display_name=shown,
                configured=False,
                warnings=[
                    f"no approved-source configuration for subject '{shown}'; "
                    "external factual verification unavailable"
                ],
            )

        if key == HISTORY_UMBRELLA:
            return self._merged_history()

        return self._from_config(key, self._registry.subjects[key])

    # ------------------------------------------------------------------ impl

    def _from_config(self, key: str, cfg: dict) -> SubjectSources:
        # Copy every list (and each book dict) so callers can annotate results
        # without mutating the process-wide cached registry.
        return SubjectSources(
            subject_key=key,
            display_name=str(cfg.get("display_name") or key),
            official_domains=list(cfg.get("official_domains") or []),
            books=[dict(b) for b in cfg.get("books") or []],
            official_publications=list(cfg.get("official_publications") or []),
            claim_types=list(cfg.get("claim_types") or []),
            configured=True,
            warnings=[],
        )

    def _merged_history(self) -> SubjectSources:
        merged = SubjectSources(
            subject_key=HISTORY_UMBRELLA,
            display_name="History (combined)",
            configured=True,
        )
        seen_book_ids: set[str] = set()
        merged_parts: list[str] = []
        for part in _HISTORY_PARTS:
            cfg = self._registry.subjects.get(part)
            if cfg is None:
                # A trimmed custom registry is a degraded merge, not a crash.
                merged.warnings.append(
                    f"history umbrella: subject '{part}' missing from the "
                    "source registry; merged without it"
                )
                continue
            part_sources = self._from_config(part, cfg)
            merged_parts.append(part_sources.display_name)
            for book in part_sources.books:
                # Shared books (e.g. Poonam Dalal Dahiya in ancient AND
                # medieval) must appear once, or the retriever would search
                # the same book twice per claim.
                book_id = str(book.get("id") or "")
                if book_id and book_id in seen_book_ids:
                    continue
                seen_book_ids.add(book_id)
                merged.books.append(book)
            merged.official_domains.extend(part_sources.official_domains)
            merged.official_publications.extend(part_sources.official_publications)
            merged.claim_types.extend(part_sources.claim_types)

        merged.official_domains = _unique(merged.official_domains)
        merged.official_publications = _unique(merged.official_publications)
        merged.claim_types = _unique(merged.claim_types)
        merged.warnings.append(
            "subject 'History' is an umbrella: merged approved sources of "
            + ", ".join(merged_parts)
            + "; auto-detection may narrow later runs to a single period"
        )
        return merged
