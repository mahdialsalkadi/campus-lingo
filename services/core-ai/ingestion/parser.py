"""PyMuPDF-backed lecture ingestion.

Pipeline: PDF bytes -> per-page raw text -> cleaned prose -> semantic chunks.

Cleaning removes the artifacts that wreck LLM extraction on slide decks:
running headers/footers, bare page numbers, bullet glyphs, hyphenated line
breaks, and the ragged mid-sentence line wrapping that PDF extraction produces.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

import fitz  # PyMuPDF

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

TARGET_CHARS = 2600
OVERLAP_CHARS = 320
MIN_CHUNK_CHARS = 420
MAX_CHUNK_CHARS = 4200
MAX_PDF_BYTES = 80 * 1024 * 1024

_BULLET_RE = re.compile(r"^[\u2022\u2023\u25cf\u25aa\u25e6\u2043\u2219\-\*\u00b7\u25a0\u25a1>»]+\s*")
_PAGE_NUM_RE = re.compile(r"^\s*(page\s*)?\d{1,4}(\s*/\s*\d{1,4})?\s*$", re.IGNORECASE)
_SLIDE_NUM_RE = re.compile(r"^\s*(slide|slide\s*no\.?|fig(ure)?\.?)\s*\d{1,3}\s*$", re.IGNORECASE)
_HEADING_RE = re.compile(
    r"^\s*(?:"
    r"(?:\d{1,2}\.){1,3}\s+\S.*"           # 3.2 Title
    r"|(?:chapter|section|lecture|topic|module|unit)\s+\d{1,3}[:.\s].*"  # Chapter 4: X
    r")\s*$",
    re.IGNORECASE,
)
_WS_RE = re.compile(r"[ \t\u00a0\u2007\u202f]+")
_MULTI_NEWLINE_RE = re.compile(r"\n{3,}")
_HYPHEN_BREAK_RE = re.compile(r"(\w)-\n(\w)")
_INLINE_CITATION_RE = re.compile(r"\[\s*\d{1,3}(?:\s*[,\u2013-]\s*\d{1,3})*\s*\]")
_URL_RE = re.compile(r"https?://\S+")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9(\u201c\"])")


class LectureParseError(RuntimeError):
    """Raised when a PDF cannot yield usable text."""


@dataclass
class PageText:
    page_number: int
    raw_text: str
    clean_text: str


@dataclass
class Chunk:
    order_index: int
    title: str
    content: str
    page_start: int
    page_end: int

    @property
    def char_count(self) -> int:
        return len(self.content)


@dataclass
class LectureParseResult:
    title: str
    chunks: list[Chunk]
    page_count: int
    char_count: int
    warnings: list[str] = field(default_factory=list)

    @property
    def chunk_count(self) -> int:
        return len(self.chunks)


# ---------------------------------------------------------------------------
# Cleaning
# ---------------------------------------------------------------------------


def _edge_lines(page: PageText, count: int = 2, max_len: int = 90) -> list[str]:
    """First and last ``count`` non-empty lines of a page (header/footer band)."""
    lines = [line.strip() for line in page.raw_text.splitlines() if line.strip()]
    if not lines:
        return []
    edges = lines[:count] + (lines[-count:] if len(lines) > count else [])
    return [line for line in edges if len(line) <= max_len]


def _strip_running_headers(pages: Sequence[PageText], max_len: int = 90) -> list[str]:
    """Drop running titles/footers repeated on >=60% of pages.

    Only the top/bottom two lines of each page are candidates: body text can
    legitimately repeat across slides, whereas a running header cannot be
    anywhere but the page edges.
    """
    if len(pages) < 3:
        return [p.raw_text for p in pages]
    counts: dict[str, int] = {}
    for page in pages:
        for line in set(_edge_lines(page, 2, max_len)):
            counts[line.lower()] = counts.get(line.lower(), 0) + 1
    threshold = max(2, int(0.6 * len(pages) + 0.999))
    boilerplate = {line for line, n in counts.items() if n >= threshold}
    cleaned: list[str] = []
    for page in pages:
        kept = [
            line
            for line in page.raw_text.splitlines()
            if line.strip().lower() not in boilerplate
        ]
        cleaned.append("\n".join(kept))
    return cleaned


def _looks_wrapped(previous: str, following: str) -> bool:
    """True when ``following`` reads as the continuation of ``previous``."""
    if previous.rstrip().endswith(("-", ",", "(", "/")):
        return True
    head = following[:1]
    return bool(head) and (head.islower() or head.isdigit())


def _join_wrapped_lines(text: str) -> str:
    """Rejoin lines broken by PDF layout while keeping list/paragraph breaks."""
    out: list[str] = []
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            out.append("")
            continue
        # Only bullet glyphs mark a list item; numbered lines keep their
        # numbering because slide headings carry meaning in the digits.
        is_list_item = bool(_BULLET_RE.match(stripped))
        collapsed = _BULLET_RE.sub("", stripped).strip() if is_list_item else stripped
        if not collapsed:
            continue
        if out and out[-1] and not is_list_item and _looks_wrapped(out[-1], collapsed):
            joined = f"{out[-1]} {collapsed}"
            if len(joined) <= MAX_CHUNK_CHARS:
                out[-1] = joined
                continue
        out.append(f"{'- ' if is_list_item else ''}{collapsed}")
    return "\n".join(out)


def clean_text(text: str) -> str:
    """Normalise a single page of extracted text into readable prose."""
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\ufb01", "fi").replace("\ufb02", "fl")  # ligatures
    text = text.replace("\u2019", "'").replace("\u2018", "'")
    text = text.replace("\u201c", '"').replace("\u201d", '"')
    text = text.replace("\u2013", "-").replace("\u2014", " - ")
    text = _HYPHEN_BREAK_RE.sub(r"\1\2", text)
    text = _INLINE_CITATION_RE.sub("", text)
    text = _URL_RE.sub("", text)
    text = _WS_RE.sub(" ", text)

    kept: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            kept.append("")
            continue
        if _PAGE_NUM_RE.match(stripped) or _SLIDE_NUM_RE.match(stripped):
            continue
        kept.append(stripped)

    joined = _join_wrapped_lines("\n".join(kept))
    joined = _MULTI_NEWLINE_RE.sub("\n\n", joined)
    joined = re.sub(r"\s+([,.;:!?])", r"\1", joined)
    return joined.strip()


def extract_pages(pdf_bytes: bytes, *, drop_boilerplate: bool = True) -> list[PageText]:
    """Extract text per page with PyMuPDF."""
    if not pdf_bytes:
        raise LectureParseError("empty PDF payload")
    if len(pdf_bytes) > MAX_PDF_BYTES:
        raise LectureParseError(
            f"PDF too large ({len(pdf_bytes)} bytes > {MAX_PDF_BYTES} limit)"
        )
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as exc:  # pragma: no cover - corrupt upload path
        raise LectureParseError(f"cannot open PDF: {exc}") from exc

    try:
        if doc.needs_pass:
            raise LectureParseError("PDF is password protected")
        pages = [
            PageText(page_number=i + 1, raw_text=page.get_text("text"), clean_text="")
            for i, page in enumerate(doc)
        ]
    finally:
        doc.close()

    sources = _strip_running_headers(pages) if drop_boilerplate else [p.raw_text for p in pages]
    for page, source in zip(pages, sources):
        page.clean_text = clean_text(source)
    return pages


# ---------------------------------------------------------------------------
# Titles
# ---------------------------------------------------------------------------

_GENERIC_TITLES = {"", "untitled", "lecture", "slides", "slide", "presentation"}


def _looks_like_heading(line: str) -> bool:
    if not (3 <= len(line) <= 120):
        return False
    if line.endswith((".", ",", ";")):
        return False
    words = line.split()
    if not words or len(words) > 14:
        return False
    letters = [c for c in line if c.isalpha()]
    if not letters:
        return False
    upper_ratio = sum(1 for c in letters if c.isupper()) / len(letters)
    return upper_ratio > 0.6 or bool(_HEADING_RE.match(line)) or line.istitle()


def derive_title(pages: Sequence[PageText], filename: Optional[str]) -> str:
    """Best-effort lecture title: first heading on page 1, else filename stem."""
    if pages:
        for line in pages[0].clean_text.splitlines():
            candidate = line.strip()
            if candidate.lower() in _GENERIC_TITLES:
                continue
            if _looks_like_heading(candidate):
                return candidate[:160]
    if filename:
        stem = re.sub(r"\.pdf$", "", filename, flags=re.IGNORECASE)
        stem = re.sub(r"[_\-]+", " ", stem).strip()
        if stem and stem.lower() not in _GENERIC_TITLES:
            return stem[:160]
    return "Untitled Lecture"


def chunk_title(content: str, order_index: int, lecture_title: str) -> str:
    """Heading of the first line when present, else 'Lecture - Part N'."""
    for line in content.splitlines():
        candidate = line.strip()
        if not candidate:
            continue
        if len(candidate) <= 90 and not candidate.endswith((".", ",", ";")):
            return candidate[:120]
        break
    return f"{lecture_title[:80]} - Part {order_index + 1}"


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------


def _split_sentences(paragraph: str) -> list[str]:
    parts = [p.strip() for p in _SENTENCE_SPLIT_RE.split(paragraph) if p.strip()]
    return parts or ([paragraph.strip()] if paragraph.strip() else [])


def _hard_split(sentence: str, limit: int) -> list[str]:
    if len(sentence) <= limit:
        return [sentence]
    pieces: list[str] = []
    words = sentence.split()
    current: list[str] = []
    length = 0
    for word in words:
        if length + len(word) + 1 > limit and current:
            pieces.append(" ".join(current))
            current, length = [], 0
        current.append(word)
        length += len(word) + 1
    if current:
        pieces.append(" ".join(current))
    return pieces


def chunk_text(
    text: str,
    *,
    target_chars: int = TARGET_CHARS,
    overlap_chars: int = OVERLAP_CHARS,
    min_chars: int = MIN_CHUNK_CHARS,
) -> list[str]:
    """Split prose into paragraph-aware chunks with a sentence-level overlap."""
    text = (text or "").strip()
    if not text:
        return []
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if not paragraphs:
        paragraphs = [text]

    units: list[str] = []
    for para in paragraphs:
        for sentence in _split_sentences(para):
            units.extend(_hard_split(sentence, target_chars))

    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for unit in units:
        if size + len(unit) + 1 > target_chars and current:
            chunks.append(" ".join(current))
            tail: list[str] = []
            tail_size = 0
            for prev in reversed(current):
                if tail_size + len(prev) > overlap_chars:
                    break
                tail.insert(0, prev)
                tail_size += len(prev) + 1
            current, size = tail, tail_size
        current.append(unit)
        size += len(unit) + 1
    if current:
        chunks.append(" ".join(current))

    merged: list[str] = []
    for chunk in chunks:
        if merged and len(chunk) < min_chars:
            merged[-1] = f"{merged[-1]} {chunk}".strip()
        else:
            merged.append(chunk.strip())
    return [c for c in merged if c]


def extract_pdf_content(file_bytes: bytes, *, max_chars_per_page: Optional[int] = None) -> str:
    """Extract a PDF into structured, page-delimited text.

    Each page is emitted as::

        === PAGE 2/6 | chars=1180 | heading="Rule 6: Safe speed" | boilerplate=1 ===
        <cleaned page text>

    The header metadata (page index, character count, detected heading, how many
    running-header lines were dropped) gives the LLM page anchors to cite and
    survives straight into :func:`chunk_text`. PDFs with no extractable text
    still return the metadata headers plus a ``[NO TEXT]`` marker per page so the
    caller can distinguish an empty scan from a failed parse.
    """
    pages = extract_pages(file_bytes)
    total = len(pages)
    blocks: list[str] = []
    for page in pages:
        body = page.clean_text
        if max_chars_per_page is not None and len(body) > max_chars_per_page:
            body = body[:max_chars_per_page].rstrip() + " …"
        heading = ""
        for line in body.splitlines():
            candidate = line.strip()
            if not candidate:
                continue
            if _looks_like_heading(candidate) or len(candidate) <= 90:
                heading = candidate[:90]
                break
        boilerplate = sum(
            1
            for line in page.raw_text.splitlines()
            if line.strip() and line.strip() not in body
        )
        header = (
            f'=== PAGE {page.page_number}/{total} | chars={len(page.clean_text)} '
            f'| heading="{heading}" | boilerplate={boilerplate} ==='
        )
        blocks.append(f"{header}\n{body if body else '[NO TEXT ON THIS PAGE]'}")
    return "\n\n".join(blocks)


def extract_pdf_pages_meta(file_bytes: bytes) -> list[dict]:
    """Per-page metadata (page number, char count, heading) without the text."""
    out: list[dict] = []
    for page in extract_pages(file_bytes):
        heading = ""
        for line in page.clean_text.splitlines():
            if line.strip():
                heading = line.strip()[:90]
                break
        out.append(
            {
                "page_number": page.page_number,
                "chars": len(page.clean_text),
                "heading": heading,
                "has_text": len(page.clean_text) >= 40,
            }
        )
    return out


def iterate_chunks(lecture_text: str) -> list[NodeBlueprint]:
    """Turn page-delimited text (as produced by :func:`extract_pdf_content`)
    into ordered learning chunks, keeping the page headers stripped."""
    from schema import NodeBlueprint

    body = "\n\n".join(
        "\n".join(
            line for line in block.splitlines() if not line.startswith("=== PAGE ")
        ).strip()
        for block in lecture_text.split("\n\n")
        if block.strip()
    )
    chunks = chunk_text(body)
    return [
        NodeBlueprint(
            order_index=index,
            title=chunk_title(chunk, index, "Lecture"),
            content=chunk,
        )
        for index, chunk in enumerate(chunks)
    ]


def parse_pdf(pdf_bytes: bytes, filename: Optional[str] = None) -> LectureParseResult:
    """Full ingestion: bytes in, ordered learning chunks out."""
    pages = extract_pages(pdf_bytes)
    warnings: list[str] = []

    usable = [p for p in pages if len(p.clean_text) >= 40]
    if not usable:
        raise LectureParseError(
            "no extractable text found - the PDF is likely a scanned image; "
            "OCR it before upload"
        )
    if len(usable) < len(pages):
        warnings.append(
            f"{len(pages) - len(usable)} page(s) yielded little or no text (images/tables)"
        )

    title = derive_title(pages, filename)
    body = "\n\n".join(p.clean_text for p in usable if p.clean_text)
    chunk_bodies = chunk_text(body)
    if not chunk_bodies:
        raise LectureParseError("text extracted but chunking produced no content")

    # Map chunks back to page ranges using cumulative page offsets.
    page_offsets: list[tuple[int, int, int]] = []  # (page_number, start, end)
    cursor = 0
    for page in usable:
        if not page.clean_text:
            continue
        start = cursor
        cursor += len(page.clean_text) + 2
        page_offsets.append((page.page_number, start, cursor))

    chunks: list[Chunk] = []
    search_from = 0
    for index, content in enumerate(chunk_bodies):
        probe = content[:80]
        position = body.find(probe, max(0, search_from - OVERLAP_CHARS))
        if position < 0:
            position = body.find(probe)
        if position < 0:
            position = search_from
        search_from = position + len(content)
        page_start = page_offsets[0][0] if page_offsets else 1
        page_end = page_offsets[-1][0] if page_offsets else len(pages)
        for page_number, start, end in page_offsets:
            if start <= position:
                page_start = page_number
            if start <= position + len(content) and page_number >= page_start:
                page_end = page_number
        chunks.append(
            Chunk(
                order_index=index,
                title=chunk_title(content, index, title),
                content=content,
                page_start=page_start,
                page_end=page_end,
            )
        )

    return LectureParseResult(
        title=title,
        chunks=chunks,
        page_count=len(pages),
        char_count=sum(len(c.content) for c in chunks),
        warnings=warnings,
    )


def iter_chunk_summaries(chunks: Iterable[Chunk]) -> list[str]:
    """Cheap extractive summary per chunk (first + last sentences)."""
    summaries: list[str] = []
    for chunk in chunks:
        sentences = _split_sentences(chunk.content)
        if len(sentences) <= 2:
            summaries.append(chunk.content[:600])
            continue
        head = " ".join(sentences[:2])
        tail = sentences[-1]
        summaries.append(f"{head} ... {tail}"[:600])
    return summaries
