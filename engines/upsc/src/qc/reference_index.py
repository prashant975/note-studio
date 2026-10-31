"""Local standard-book retrieval over a persistent SQLite index.

Why SQLite FTS5 and not embeddings: the QC stage must run inside the
PyInstaller EXE on teammate laptops with no GPU, no model downloads and no
new pip packages (spec: persistent local index, EXE-friendly). FTS5 ships
inside Python's bundled sqlite3, needs no server process, and its bm25
ranking is plenty for "find the passage in an approved book that mentions
this claim". Everything lives in one file (reference.db) so the index can be
copied between machines alongside the app data.

Degradation ladder, so a missing resource never kills the pipeline:

- sqlite build without FTS5  -> plain-table substring scan (same API,
  slower), flagged on the retriever via ``fts5_enabled``.
- no index built yet / empty -> ``available()`` is False and ``search()``
  returns [], so callers warn and fall through to other evidence sources.
- unreadable/unsupported doc -> warning recorded on ``warnings``, count 0,
  and any previously indexed rows for that document are left untouched.
"""
from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

from .models import EvidenceChunk
from .paths import app_data_dir

_DB_NAME = "reference.db"
_SUPPORTED_SUFFIXES = {".pdf", ".txt", ".md"}
_CHUNK_TARGET_CHARS = 1200
_MAX_QUERY_TERMS = 16
_LIKE_SCAN_LIMIT = 1000  # bound the fallback scan so huge indexes stay usable

# Words that carry no retrieval signal for UPSC-style factual claims. Kept
# small on purpose: over-aggressive stopword lists strip entity-ish words
# ("act", "state") that book search actually needs.
_STOPWORDS = {
    "a", "about", "all", "also", "an", "and", "any", "are", "as", "at", "be",
    "been", "being", "between", "but", "by", "can", "could", "did", "do",
    "does", "each", "for", "from", "had", "has", "have", "he", "her", "his",
    "how", "i", "if", "in", "into", "is", "it", "its", "may", "me", "might",
    "more", "most", "must", "my", "no", "not", "of", "on", "or", "our",
    "over", "per", "shall", "she", "should", "so", "some", "such", "than",
    "that", "the", "their", "them", "then", "there", "these", "they", "this",
    "those", "to", "under", "upon", "via", "was", "we", "were", "what",
    "when", "where", "which", "who", "whom", "why", "will", "with", "would",
    "you", "your",
}

_HEADING_PREFIX_RE = re.compile(r"^(chapter|unit|section|part|lesson)\b", re.IGNORECASE)

_SELECT_COLS = (
    "c.id, c.source_id, c.source_name, c.edition, c.publication_year, "
    "c.page_number, c.chapter, c.text"
)


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", str(value).lower()).strip("_")
    return slug or "misc"


def _significant_terms(query: str) -> list[str]:
    """Lowercased content words of the query, deduped, order preserved.

    Tokens are pure [a-z0-9] runs so they can be dropped into an FTS5 quoted
    phrase or a substring scan without any further escaping.
    """
    seen: set[str] = set()
    terms: list[str] = []
    for token in re.findall(r"[a-z0-9]+", str(query or "").lower()):
        if len(token) < 2 and not token.isdigit():
            continue
        if token in _STOPWORDS or token in seen:
            continue
        seen.add(token)
        terms.append(token)
        if len(terms) >= _MAX_QUERY_TERMS:
            break
    return terms


def _is_heading_line(line: str) -> bool:
    """Heuristic for a chapter/section title line.

    This only feeds the ``chapter`` metadata column (nice-to-have context in
    evidence citations), so false negatives are harmless and false positives
    merely mislabel a chunk — never worth crashing or tuning per book.
    """
    line = line.strip()
    if not line or len(line) > 80:
        return False
    if not re.search(r"[a-zA-Z]", line):
        return False  # bare page numbers etc.
    if line.startswith("#"):
        return True
    if _HEADING_PREFIX_RE.match(line):
        return True
    words = line.split()
    if len(words) > 10 or line.endswith((".", ",", ";", "!", "?")):
        return False
    if line.isupper():
        return True
    capitalised = sum(1 for w in words if w[:1].isupper())
    return len(words) >= 2 and capitalised / len(words) >= 0.6


def _clean_heading(line: str) -> str:
    return line.lstrip("#").strip().rstrip(":").strip()


def _split_paragraphs(text: str) -> list[str]:
    """Split on blank lines, but let a heading-like line stand alone even
    without surrounding blank lines — PDF text extraction rarely leaves a
    blank line after chapter titles."""
    paragraphs: list[str] = []
    buf: list[str] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            if buf:
                paragraphs.append("\n".join(buf))
                buf = []
            continue
        if _is_heading_line(line):
            if buf:
                paragraphs.append("\n".join(buf))
                buf = []
            paragraphs.append(line.strip())
            continue
        buf.append(line)
    if buf:
        paragraphs.append("\n".join(buf))
    return paragraphs


def _hard_split(paragraph: str, target: int) -> list[str]:
    """Split an oversized single paragraph near word boundaries. Only kicks
    in for wall-of-text extractions; normal paragraphs pass through whole."""
    if len(paragraph) <= int(target * 1.5):
        return [paragraph]
    pieces: list[str] = []
    start = 0
    while start < len(paragraph):
        end = min(start + target, len(paragraph))
        if end < len(paragraph):
            space = paragraph.rfind(" ", start + target // 2, end)
            if space > start:
                end = space
        piece = paragraph[start:end].strip()
        if piece:
            pieces.append(piece)
        start = end
    return pieces


class _ParagraphChunker:
    """Accumulates paragraphs into ~CHUNK_TARGET_CHARS chunks.

    Stateful across calls so a PDF indexed page by page carries the current
    chapter title forward, while each page still flushes its own chunks (a
    chunk must not span pages or its page_number would lie).
    """

    def __init__(self, target: int = _CHUNK_TARGET_CHARS) -> None:
        self.target = target
        self.chapter = ""

    def chunk(self, text: str) -> list[tuple[str, str]]:
        """Return [(chapter, chunk_text), ...] for one page/file of text."""
        out: list[tuple[str, str]] = []
        buf: list[str] = []
        size = 0

        def flush() -> None:
            nonlocal buf, size
            body = "\n\n".join(buf).strip()
            if body:
                out.append((self.chapter, body))
            buf = []
            size = 0

        for paragraph in _split_paragraphs(text):
            if "\n" not in paragraph and _is_heading_line(paragraph):
                # Flush first so no chunk spans two chapters, then keep the
                # heading text inside the next chunk so its words stay
                # searchable.
                flush()
                self.chapter = _clean_heading(paragraph)
                buf.append(paragraph)
                size += len(paragraph)
                continue
            for piece in _hard_split(paragraph, self.target):
                if size and size + len(piece) > self.target:
                    flush()
                buf.append(piece)
                size += len(piece) + 2
        flush()
        return out


class ReferenceRetriever:
    """Search and build the local standard-book index.

    All methods are non-raising by design: the QC engine treats book evidence
    as best-effort and must keep running when no index exists on a teammate's
    machine. Problems surface through ``warnings`` / empty results instead.
    """

    def __init__(self, index_root: Path | None = None) -> None:
        if index_root is None:
            index_root = self._default_index_root()
        self.index_root = Path(index_root)
        self.db_path = self.index_root / _DB_NAME
        # Optimistic until the sqlite build proves otherwise (CREATE VIRTUAL
        # TABLE is the only reliable probe). Callers may also force False to
        # exercise the fallback path.
        self.fts5_enabled = True
        self.warnings: list[str] = []
        self._conn: sqlite3.Connection | None = None
        self._schema_ready = False

    # ------------------------------------------------------------- lifecycle

    @staticmethod
    def _default_index_root() -> Path:
        if getattr(sys, "frozen", False):
            # The EXE's bundle dir is wiped on exit — the index must live in
            # the writable per-user app data root.
            return app_data_dir("reference_index")
        repo_local = Path(__file__).resolve().parents[2] / "reference_index"
        if repo_local.exists():
            return repo_local
        return app_data_dir("reference_index")

    def close(self) -> None:
        """Release the sqlite handle (Windows keeps the file locked while a
        connection is open, which blocks temp-dir cleanup in tests)."""
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
            self._conn = None
            self._schema_ready = False

    def _connect(self, *, create: bool) -> sqlite3.Connection | None:
        """Shared connection; ``create=False`` never materialises a db file,
        so read paths on a machine with no index leave no artefacts behind."""
        if self._conn is None:
            if not create and not self.db_path.exists():
                return None
            if create:
                self.db_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                self._conn = sqlite3.connect(str(self.db_path))
            except sqlite3.Error as e:
                self.warnings.append(f"could not open reference index {self.db_path}: {e}")
                return None
        if create and not self._schema_ready:
            self._ensure_schema(self._conn)
            self._schema_ready = True
        return self._conn

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS chunks ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " subject TEXT NOT NULL,"
            " source_id TEXT NOT NULL,"
            " source_name TEXT NOT NULL,"
            " edition TEXT NOT NULL DEFAULT '',"
            " publication_year TEXT NOT NULL DEFAULT '',"
            " page_number INTEGER,"
            " chapter TEXT NOT NULL DEFAULT '',"
            " text TEXT NOT NULL,"
            " doc_path TEXT NOT NULL DEFAULT '')"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_subject ON chunks(subject)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(source_id, doc_path)")
        try:
            # A standalone FTS table (not content=) keeps deletes trivially
            # correct at the cost of storing the text twice — acceptable for
            # book-sized corpora and far harder to corrupt than triggers.
            conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(text)")
            # Backfill in case an earlier run on an FTS5-less sqlite build
            # created chunks that never reached the FTS table.
            conn.execute(
                "INSERT INTO chunks_fts(rowid, text)"
                " SELECT id, text FROM chunks"
                " WHERE id NOT IN (SELECT rowid FROM chunks_fts)"
            )
            conn.commit()
            self.fts5_enabled = True
        except sqlite3.OperationalError:
            self.fts5_enabled = False
        conn.commit()

    @staticmethod
    def _fts_table_exists(conn: sqlite3.Connection) -> bool:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'chunks_fts'"
        ).fetchone()
        return row is not None

    # ----------------------------------------------------------------- reads

    def available(self) -> bool:
        """True only when the db exists and holds at least one chunk."""
        conn = self._connect(create=False)
        if conn is None:
            return False
        try:
            row = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()
        except sqlite3.Error:
            return False
        return bool(row and row[0])

    def search(self, query: str, subject: str, top_k: int = 5) -> list[EvidenceChunk]:
        """Best passages for the query within one subject; [] when the index
        is missing, empty, or the query has no content words."""
        if top_k <= 0:
            return []
        terms = _significant_terms(query)
        if not terms:
            return []
        conn = self._connect(create=False)
        if conn is None:
            return []
        try:
            if self.fts5_enabled and self._fts_table_exists(conn):
                try:
                    return self._search_fts(conn, terms, subject, top_k)
                except sqlite3.Error:
                    pass  # e.g. malformed FTS index — degrade, don't crash
            return self._search_like(conn, terms, subject, top_k)
        except sqlite3.Error as e:
            self.warnings.append(f"reference search failed: {e}")
            return []

    def _search_fts(
        self, conn: sqlite3.Connection, terms: list[str], subject: str, top_k: int
    ) -> list[EvidenceChunk]:
        # OR-of-phrases keeps recall high; bm25 rewards chunks matching more
        # terms. Terms are [a-z0-9] runs, so plain quoting is safe.
        match = " OR ".join(f'"{t}"' for t in terms)
        rows = conn.execute(
            f"SELECT {_SELECT_COLS}, bm25(chunks_fts) AS rank"
            " FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid"
            " WHERE chunks_fts MATCH ? AND c.subject = ?"
            " ORDER BY rank, c.id LIMIT ?",
            (match, subject, top_k),
        ).fetchall()
        # bm25() is smaller-is-better; negate so EvidenceChunk.score reads as
        # higher-is-better like every other relevance score in the models.
        return [self._row_to_chunk(row, score=-float(row[8])) for row in rows]

    def _search_like(
        self, conn: sqlite3.Connection, terms: list[str], subject: str, top_k: int
    ) -> list[EvidenceChunk]:
        """Substring fallback when FTS5 is unavailable. Same API, weaker
        ranking: distinct-term coverage dominates, raw frequency breaks
        ties, chunk id keeps the order deterministic."""
        clause = " OR ".join(["instr(lower(c.text), ?) > 0"] * len(terms))
        rows = conn.execute(
            f"SELECT {_SELECT_COLS} FROM chunks c"
            f" WHERE c.subject = ? AND ({clause}) LIMIT ?",
            (subject, *terms, _LIKE_SCAN_LIMIT),
        ).fetchall()
        scored: list[tuple[float, int, tuple]] = []
        for row in rows:
            text_l = (row[7] or "").lower()
            present = sum(1 for t in terms if t in text_l)
            frequency = sum(text_l.count(t) for t in terms)
            scored.append((float(present * 10 + frequency), int(row[0]), row))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [self._row_to_chunk(row, score=score) for score, _, row in scored[:top_k]]

    @staticmethod
    def _row_to_chunk(row: tuple, *, score: float) -> EvidenceChunk:
        return EvidenceChunk(
            source_id=row[1],
            source_name=row[2],
            edition=row[3] or "",
            publication_year=row[4] or "",
            page_number=row[5],
            chapter=row[6] or "",
            text=row[7] or "",
            score=round(float(score), 4),
        )

    # ---------------------------------------------------------------- writes

    def index_document(
        self,
        path: Path,
        *,
        subject: str,
        source_id: str,
        source_name: str,
        edition: str = "",
        publication_year: str = "",
    ) -> int:
        """Index one document; returns the number of chunks stored.

        Re-indexing the same (source_id, resolved path) replaces its previous
        rows so repeated library scans never duplicate evidence. A document
        that yields no text leaves any previously indexed rows untouched — a
        corrupt re-download must not wipe good data.
        """
        path = Path(path)
        if not path.exists():
            self.warnings.append(f"reference document missing, skipped: {path}")
            return 0
        suffix = path.suffix.lower()
        if suffix == ".pdf":
            rows = self._extract_pdf(path)
        elif suffix in {".txt", ".md"}:
            rows = self._extract_text_file(path)
        else:
            self.warnings.append(f"unsupported reference format {suffix!r}, skipped: {path.name}")
            return 0
        if not rows:
            return 0

        conn = self._connect(create=True)
        if conn is None:
            return 0
        doc_path = str(path.resolve())
        with conn:  # one transaction: replace-then-insert is all-or-nothing
            try:
                conn.execute(
                    "DELETE FROM chunks_fts WHERE rowid IN"
                    " (SELECT id FROM chunks WHERE source_id = ? AND doc_path = ?)",
                    (source_id, doc_path),
                )
            except sqlite3.Error:
                pass  # no FTS table on this build
            conn.execute(
                "DELETE FROM chunks WHERE source_id = ? AND doc_path = ?",
                (source_id, doc_path),
            )
            for page_number, chapter, text in rows:
                cur = conn.execute(
                    "INSERT INTO chunks (subject, source_id, source_name, edition,"
                    " publication_year, page_number, chapter, text, doc_path)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        subject, source_id, source_name, edition,
                        publication_year, page_number, chapter, text, doc_path,
                    ),
                )
                if self.fts5_enabled:
                    try:
                        conn.execute(
                            "INSERT INTO chunks_fts(rowid, text) VALUES (?, ?)",
                            (cur.lastrowid, text),
                        )
                    except sqlite3.Error:
                        self.fts5_enabled = False
        return len(rows)

    def _extract_pdf(self, path: Path) -> list[tuple[int | None, str, str]]:
        try:
            import fitz  # heavy import deferred: search-only callers never pay it
        except Exception as e:
            self.warnings.append(f"PyMuPDF unavailable, skipped {path.name}: {e}")
            return []
        rows: list[tuple[int | None, str, str]] = []
        chunker = _ParagraphChunker()
        try:
            with fitz.open(str(path)) as doc:
                for page_index, page in enumerate(doc, start=1):
                    text = page.get_text("text") or ""
                    for chapter, chunk in chunker.chunk(text):
                        rows.append((page_index, chapter, chunk))
        except Exception as e:
            self.warnings.append(f"failed to read PDF {path.name}: {e}")
            return []
        return rows

    def _extract_text_file(self, path: Path) -> list[tuple[int | None, str, str]]:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            self.warnings.append(f"failed to read {path.name}: {e}")
            return []
        return [(None, chapter, chunk) for chapter, chunk in _ParagraphChunker().chunk(text)]

    # --------------------------------------------------------------- library

    def index_library(self, library_root: Path, registry=None) -> dict:
        """Index reference_library/<subject folder>/** into the db.

        Folder names are mapped to registry subject keys via
        SourceRouter.normalise so "Polity", "polity" and "भारतीय राजव्यवस्था"
        folders all land on the same subject; unroutable names fall back to a
        folder-name slug so the content is still indexed rather than lost.
        A missing root is a normal state (teammate without a book library) —
        it returns {} with no warning.
        """
        root = Path(library_root)
        if not root.is_dir():
            return {}
        by_subject: dict[str, int] = {}
        files = 0
        chunks = 0
        for subject_dir in sorted(root.iterdir(), key=lambda p: p.name.lower()):
            if not subject_dir.is_dir() or subject_dir.name.startswith("."):
                continue
            subject = self._normalise_subject(subject_dir.name, registry)
            for doc in sorted(subject_dir.rglob("*"), key=lambda p: str(p).lower()):
                if not doc.is_file() or doc.suffix.lower() not in _SUPPORTED_SUFFIXES:
                    continue
                files += 1
                count = self.index_document(
                    doc,
                    subject=subject,
                    source_id=f"{subject}:{_slug(doc.stem)}",
                    source_name=doc.stem.replace("_", " ").strip() or doc.name,
                )
                chunks += count
                by_subject[subject] = by_subject.get(subject, 0) + count
        return {"files": files, "chunks": chunks, "by_subject": by_subject}

    def _normalise_subject(self, name: str, registry) -> str:
        """Folder name -> subject key, degrading from SourceRouter to the
        registry alias table to a plain slug. Never raises: an unroutable
        folder should still be indexed, just under a folder-derived key."""
        router_key = None
        try:
            # Lazy import: source_router imports qc_config, which some
            # callers of this module also import — keeping this out of module
            # scope avoids any chance of a cycle at package import time.
            from .source_router import SourceRouter
        except Exception:
            SourceRouter = None
        if SourceRouter is not None:
            try:
                reg = registry
                if reg is None:
                    from .qc_config import load_sources
                    reg = load_sources()
                router_key = SourceRouter(registry=reg).normalise(name)
            except Exception:
                router_key = None
        if router_key:
            return str(router_key)
        cleaned = str(name).strip().lower()
        if registry is not None:
            aliases = getattr(registry, "subject_aliases", None) or {}
            if cleaned in aliases:
                return str(aliases[cleaned])
            subjects = getattr(registry, "subjects", None) or {}
            if cleaned in subjects:
                return cleaned
        return _slug(cleaned)
