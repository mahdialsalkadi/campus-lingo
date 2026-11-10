"""PyMuPDF-backed lecture ingestion.

Pipeline: PDF bytes -> per-page raw text -> cleaned prose -> semantic chunks.

Cleaning removes the artifacts that wreck LLM extraction on slide decks:
running headers/footers, bare page numbers, bullet glyphs, hyphenated line
breaks, and the ragged mid-sentence line wrapping that PDF extraction produces.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

try:  # PyMuPDF >= 1.24 exposes the canonical ``pymupdf`` module
    import pymupdf as fitz
except ImportError:  # older wheels only ship the deprecated ``fitz`` alias
    import fitz

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

TARGET_CHARS = 6500
OVERLAP_CHARS = 500
MIN_CHUNK_CHARS = 1600
MAX_CHUNK_CHARS = 9000
MAX_PDF_BYTES = 80 * 1024 * 1024

# ---------------------------------------------------------------------------
# Token-aware accumulation (the production spec):
#
# * a "token" is the English-lecture-calibrated ~4.0 chars/token estimate;
# * every chunk targets CHUNK_TARGET_TOKENS..CHUNK_MAX_TOKENS (600..1000);
# * decks with <= 6 slides or < CHUNK_SINGLE_TOKENS total compile into ONE
#   chunk ("Part 1") — never starved into empty fragments;
# * long decks (e.g. 80 slides) group into 10-14 balanced semantic chunks,
#   split at slide boundaries and at sentence boundaries when a slide alone
#   would overflow the ceiling, so no thought is cut mid-sentence.
# ---------------------------------------------------------------------------
CHARS_PER_TOKEN = 4.0
CHUNK_TARGET_TOKENS = 600          # preferred density per chunk
CHUNK_MAX_TOKENS = 1000            # hard ceiling before the buffer flushes
CHUNK_MIN_TOKENS = 200             # merge under-runners into their neighbour
CHUNK_SINGLE_TOKENS = 800          # total below this -> the whole deck is Part 1
SINGLE_SLIDE_COUNT = 6             # decks up to this many slides stay one chunk
MAX_SEMANTIC_CHUNKS = 14           # long-deck fan-out ceiling
MIN_SEMANTIC_CHUNKS = 10           # long-deck fan-out floor (80+ slide decks)

# ---------------------------------------------------------------------------
# Slide-aware segmentation — prevents "Lost in the Middle" degradation on long
# slide decks: instead of char-count chunking, the deck is divided into
# balanced parts split at page (slide) boundaries, each prepended with a
# structural header so the LLM keeps positional awareness.
# ---------------------------------------------------------------------------
SLIDE_SPLIT_THRESHOLD = 30   # decks up to this many slides count as "short"
PART_COUNT_SMALL = 2
PART_COUNT_LARGE = 3
# A deck this small is one learning part: splitting a 10-slide summary would
# starve the content-adaptive challenge target and force redundant parts.
SINGLE_PART_CHARS = 3500

_PAGE_HEADER_RE = re.compile(r"^=== PAGE (\d+)/\d+ .*$", re.MULTILINE)
_PAGE_HEADING_RE = re.compile(r'^=== PAGE \d+/\d+ .*?heading="([^"]*)"', re.MULTILINE)

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


# PDF extraction splits kerned glyphs into one span each, so a slide title can
# come back as "C o u r s e   L e c t u r e". This collapses the single-letter
# fragments back into whole words. Only LETTER fragments are joined: restricting
# the class to letters (not `\w`) keeps numeric data intact, so a line like
# "1.2 3.4" is never mangled into "1.23.4". Ordinary prose is untouched because
# it never contains runs of one-character tokens.
_KERNING_SPACE_RE = re.compile(r"(?<=\b[^\W\d_])\s(?=[^\W\d_]\b)")


def fix_kerning_spaces(text: str) -> str:
    """Recombine letter-spaced text: ``C o u r s e`` -> ``Course``.

    Applied to extracted chapter/part titles (see :func:`_clean_title_text`) so
    a mangled heading never reaches the UI or the generation prompt.
    """
    if not text:
        return text
    return _KERNING_SPACE_RE.sub("", text)


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

    @property
    def token_count(self) -> int:
        return estimate_tokens(self.content)


def estimate_tokens(text: str) -> int:
    """Fast token estimate calibrated for academic English prose (~4 chars/token).

    The chunker only needs a consistent density measure — the real BPE count
    is never worth an extra tokenizer pass per slide. Arabic decks skew denser
    (~2.5 chars/token); the 4.0 factor is the documented compromise.
    """
    if not text:
        return 0
    return max(1, round(len(text) / CHARS_PER_TOKEN))


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


def _page_text(page: "fitz.Page") -> str:
    """Extract one page's text block-by-block instead of as a char stream.

    ``get_text("text")`` concatenates spans and inserts a space between every
    glyph run, which is what turns a kerned slide title into ``C o u r s e``.
    ``get_text("blocks")`` keeps each text region (image blocks skipped) so the
    sanitiser sees whole words. Falls back to the plain text mode when a build
    returns no blocks.
    """
    try:
        blocks = page.get_text("blocks") or []
    except Exception:  # noqa: BLE001 - odd builds / malformed pages
        return page.get_text("text")
    chunks: list[str] = []
    for block in blocks:
        # (x0, y0, x1, y1, text, block_no, block_type); type 1 == image.
        if len(block) >= 7 and block[6] == 1:
            continue
        text = (block[4] or "").strip()
        if text:
            chunks.append(text)
    if not chunks:
        return page.get_text("text")
    return "\n".join(chunks)


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
            PageText(page_number=i + 1, raw_text=_page_text(page), clean_text="")
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

_GENERIC_TITLES = {
    "",
    "untitled",
    "lecture",
    "lectures",
    "slides",
    "slide",
    "presentation",
    "chapter",
    "section",
    "part",
    "unit",
    "module",
    "topic",
    "node",
    "overview",
    "agenda",
    "outline",
    "contents",
    "table of contents",
    "introduction",
    "tbd",
    "n/a",
}

# Placeholder-only titles a PDF layout or a model keeps leaving behind:
# "Lecture 1", "Node", "Part 2", "Slide 3", "Chapter 4:", a bare number, ...
_GENERIC_TITLE_RE = re.compile(
    r"^(?:"
    r"(?:part|chapter|section|lecture|topic|module|unit|slide|node|figure|fig|page)"
    r"\s*(?:no\.?|number)?\s*\d*"
    r"|untitled|tbd|n/?a|new\s+(?:document|presentation)"
    r"|\d{1,4}"
    r")\s*[:.\-\u2013\u2014]?\s*$",
    re.IGNORECASE,
)


# Table/figure labels and raw numeric data must never become node titles:
# "TABLE I", "Table 2", "Figure 3", "Fig. 4.2", raw coordinates, percentages.
_TABLE_FIGURE_RE = re.compile(
    r"^\s*(?:table|tab\.?|figure|fig\.?|chart|graph|exhibit|plot|appendix)"
    r"\s*(?:[IVXLC]{1,6}|\d{1,3})\b",
    re.IGNORECASE,
)
_NUMERIC_ONLY_RE = re.compile(r"^[\s\d.,:;%+\-–—_/\\|()\[\]{}<>x×]+$", re.IGNORECASE)
_PERCENT_RE = re.compile(r"\d\s*%")
_DECIMAL_RE = re.compile(r"\d+\.\d+")
# "0.30 0.45 accuracy" — leading graph-axis coordinates.
_COORDINATE_RE = re.compile(r"^\s*\d+(?:\.\d+)?(?:\s+\d+(?:\.\d+)?){1,}\b")


def _looks_like_junk_title(value: str) -> bool:
    """True for table headers, figure captions, raw numbers and graph axes."""
    candidate = (value or "").strip()
    if not candidate:
        return True
    if _TABLE_FIGURE_RE.match(candidate):
        return True
    if _NUMERIC_ONLY_RE.match(candidate):
        return True
    if _PERCENT_RE.search(candidate):
        return True
    if _COORDINATE_RE.match(candidate):
        return True
    # Two or more decimal numbers read as coordinates, not a concept.
    if len(_DECIMAL_RE.findall(candidate)) >= 2:
        return True
    return False


def _clean_title_text(value: str) -> str:
    """Strip the layout noise that survives PDF extraction into a title line."""
    candidate = (value or "").strip()
    candidate = _BULLET_RE.sub("", candidate).strip()
    candidate = re.sub(r"^[\[\(\{<]+", "", candidate)
    candidate = re.sub(r"[\]\)\}>]+$", "", candidate)
    candidate = re.sub(r"\.pdf$", "", candidate, flags=re.IGNORECASE)
    # Trailing path/heading separators: "CPU Scheduling /", "Topic -" .
    candidate = re.sub(r"[/\\|]+", " ", candidate)
    candidate = candidate.strip(" \t-\u2013\u2014:;,")
    candidate = _WS_RE.sub(" ", candidate)
    # Repair kerned-letter fragmentation ("C o u r s e" -> "Course").
    candidate = fix_kerning_spaces(candidate)
    return candidate.strip()


def _is_descriptive_title(value: str) -> bool:
    """True when a title carries real meaning.

    Rejects generic placeholders *and* table/figure labels, raw numbers, graph
    coordinates and percentages — none of those are conceptual topics.
    """
    candidate = (value or "").strip()
    if not candidate:
        return False
    if candidate.strip(".:- ").lower() in _GENERIC_TITLES:
        return False
    if _GENERIC_TITLE_RE.match(candidate):
        return False
    if _looks_like_junk_title(candidate):
        return False
    # Needs at least one real word and more than a stub.
    words = [word for word in re.split(r"\s+", candidate) if any(c.isalpha() for c in word)]
    return len(candidate) >= 4 and bool(words)


def _caption_topic(line: str) -> str:
    """Salvage the descriptive half of a table/figure caption.

    ``"TABLE I: Accuracy on the benchmark"`` -> ``"Accuracy on the benchmark"``.
    """
    if not _looks_like_junk_title(line):
        return ""
    parts = re.split(r"[:.\-\u2013\u2014]\s*", line.strip(), maxsplit=1)
    if len(parts) != 2:
        return ""
    tail = _clean_title_text(parts[1])
    return tail if _is_descriptive_title(tail) else ""


def _fallback_concept_title(context: str, lecture_title: str = "") -> str:
    """Meaningful fallback when a section opens with a table/figure/numbers."""
    lowered = (context or "").lower()
    if re.search(
        r"\b(benchmark|accuracy|precision|recall|f1|results?|evaluation|experiment|"
        r"performance|metric|ablation|comparison|dataset|table|figure|graph)\b",
        lowered,
    ):
        return "Empirical Evaluation"
    topic = _clean_title_text(_strip_lecture_marker(lecture_title))
    if _is_descriptive_title(topic):
        return topic
    return "Core Concepts"


def sanitize_concept_title(value: str, context: str = "", fallback: str = "") -> str:
    """Return a descriptive conceptual title — never a table/figure/number.

    Backend safety net for titles that reach persistence from any path (parser
    headings or a model's ``concept_title``). A rejected candidate is replaced
    by the descriptive half of its caption, then by the provided fallback, then
    by a context-derived topic (e.g. ``Empirical Evaluation``).
    """
    candidate = _clean_title_text(value)
    if _is_descriptive_title(candidate):
        return candidate[:200]
    salvaged = _caption_topic(candidate) if candidate else ""
    if salvaged:
        return salvaged[:200]
    fallback_clean = _clean_title_text(fallback)
    if _is_descriptive_title(fallback_clean):
        return fallback_clean[:200]
    return _fallback_concept_title(context, fallback)[:200]


def _strip_lecture_marker(title: str) -> str:
    """Drop a leading ``Chapter 3:`` / ``Lecture 4 -`` marker from a title."""
    cleaned = re.sub(
        r"(?i)^\s*\d*[.)]?\s*(?:chapter|lecture|section|unit|module|part|topic)\s*\d*\s*[:.\-\u2013\u2014]\s*",
        "",
        (title or "").strip(),
    )
    return cleaned.strip(" -\u2013\u2014:") or (title or "").strip()


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
    """Clean chapter/folder title for one uploaded PDF.

    Priority 1: a descriptive heading extracted from the first two slides.
    Priority 2: the uploaded file name without its extension, prefixed with a
    chapter marker (``Operating Systems Ch3.pdf`` -> ``Chapter: Operating
    Systems Ch3``). Generic placeholders ("Lecture 1", "Node", "Slide 3",
    truncated stubs) are never returned.
    """
    for page in list(pages)[:2]:
        for raw_line in page.clean_text.splitlines():
            candidate = _clean_title_text(raw_line)
            if not _is_descriptive_title(candidate):
                continue
            if _looks_like_heading(raw_line.strip()) or len(candidate) <= 90:
                return candidate[:160]
    return _title_from_filename(filename)


def _title_from_filename(filename: Optional[str]) -> str:
    """Fallback folder title: the uploaded file name without its extension."""
    if not filename:
        return "Untitled Lecture"
    stem = re.sub(r"\.pdf$", "", filename, flags=re.IGNORECASE)
    # Strip download/timestamp noise: "(2024-09-19)", "20240919_120000",
    # trailing "(1)" duplicates and "_v2" revision suffixes.
    stem = re.sub(
        r"(?<![0-9A-Za-z])\d{4}[-_.]\d{2}[-_.]\d{2}"
        r"(?:[-_. T]?\d{2}[-_.:]\d{2}(?:[-_.:]\d{2})?)?(?![0-9A-Za-z])",
        " ",
        stem,
    )
    stem = re.sub(r"(?<![0-9A-Za-z])\d{8}(?:[-_]\d{4,6})?(?![0-9A-Za-z])", " ", stem)
    stem = re.sub(r"[\s_\-]*[\(\[]\s*\d{1,3}\s*[\)\]]\s*", " ", stem)
    stem = re.sub(r"[\s_\-]+v\d+\s*$", " ", stem, flags=re.IGNORECASE)
    stem = re.sub(r"[_\-]+", " ", stem)
    stem = re.sub(r"\s{2,}", " ", stem).strip()
    stem = _clean_title_text(stem)
    if not _is_descriptive_title(stem):
        return "Untitled Lecture"
    # The folder already reads "📁 Chapter: X" in the UI; only add the marker
    # when the filename does not carry one itself.
    if re.search(r"(?i)\b(chapter|lecture|unit|module|section|part|topic)\b", stem):
        return stem[:160]
    return f"Chapter: {stem}"[:160]


def derive_lecture_title(lecture_text: str, filename: Optional[str] = None) -> str:
    """Folder/chapter title from :func:`extract_pdf_content` output + filename.

    Lets the ingestion endpoints name the chapter folder from the first two
    slides' detected headings (falling back to the original file name) without
    re-parsing the PDF or trusting a generated concept title.
    """
    headings = _PAGE_HEADING_RE.findall(lecture_text or "")
    for heading in headings[:2]:
        candidate = _clean_title_text(heading)
        if _is_descriptive_title(candidate):
            return candidate[:160]
    return _title_from_filename(filename)


def chunk_title(content: str, order_index: int, lecture_title: str) -> str:
    """Short concept title for one lecture part.

    Picks the part's most heading-like line, then its first descriptive line,
    then a trimmed first sentence — never a generic marker or a raw filename.
    The platform adds the ``Part {i}:`` prefix itself (see
    :func:`database.node_title_with_number`), so the returned value is the
    concept only.
    """
    lines = [_clean_title_text(line) for line in content.splitlines()]
    # 1) An explicit conceptual heading among the part's opening slides.
    for candidate in lines[:12]:
        if candidate and _looks_like_heading(candidate) and _is_descriptive_title(candidate):
            return candidate[:120]
    # 2) The first short, descriptive, non-junk body line.
    for candidate in lines:
        if candidate and len(candidate) <= 90 and _is_descriptive_title(candidate):
            return candidate[:120]
    # 3) A table/figure caption carries the topic even when it is not a valid
    #    title on its own ("TABLE I: Accuracy on the benchmark").
    for candidate in lines[:12]:
        topic = _caption_topic(candidate)
        if topic:
            return topic[:120]
    # 4) The first sentence, trimmed to a concise concept label.
    for candidate in lines:
        if not candidate:
            continue
        sentence = _SENTENCE_SPLIT_RE.split(candidate)[0].strip()
        words = sentence.split()
        if words and not _looks_like_junk_title(sentence):
            return " ".join(words[:8])[:120]
    # 5) Context-derived fallback (never a table header or graph axis).
    return _fallback_concept_title(content, lecture_title)[:120]


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------


def _part_header(part: int, total: int, page_start: int, page_end: int) -> str:
    """Structural context header, e.g. ``--- [Lecture Part 1/3: Slides 1-30] ---``."""
    return f"--- [Lecture Part {part}/{total}: Slides {page_start}-{page_end}] ---"


def _balanced_ranges(count: int, parts: int) -> list[tuple[int, int]]:
    """``count`` items split into ``parts`` consecutive near-equal ranges.

    80 slides / 3 parts -> [(0, 27), (27, 54), (54, 80)]; the remainder is
    spread over the earliest ranges so later parts never grow larger.
    """
    base, extra = divmod(count, parts)
    ranges: list[tuple[int, int]] = []
    start = 0
    for index in range(parts):
        size = base + (1 if index < extra else 0)
        ranges.append((start, start + size))
        start += size
    return ranges


def _token_part_count(slide_tokens: Sequence[int]) -> int:
    """How many 600-1000-token chunks a token budget divides into.

    For a long deck (e.g. 80 slides x ~180 tokens = ~14k tokens) the target
    lands inside the spec's 10-14 chunk band; the count never exceeds
    MAX_SEMANTIC_CHUNKS and never exceeds the slide count.
    """
    total = sum(slide_tokens)
    ideal = total / CHUNK_TARGET_TOKENS
    parts = int(ideal + 0.999) if ideal > 0 else 1
    if parts < 1:
        parts = 1
    parts = min(parts, MAX_SEMANTIC_CHUNKS)
    # Never emit more parts than slides.
    return max(1, min(parts, len(slide_tokens)))


def _merge_tiny_ranges(
    groups: list[list[int]], slide_tokens: Sequence[int]
) -> list[list[int]]:
    """Fold under-runner groups (< CHUNK_MIN_TOKENS) into their smaller neighbour.

    Keeps every emitted chunk inside the 600-1000 token density band instead of
    shipping starved trailing fragments.
    """
    merged: list[list[int]] = []
    for group in groups:
        tokens = sum(slide_tokens[i] for i in group)
        if merged and tokens < CHUNK_MIN_TOKENS:
            merged[-1].extend(group)
        else:
            merged.append(list(group))
    return merged


def segment_slides_by_tokens(
    pages: Sequence[tuple[int, str]],
    *,
    lecture_title: str = "Lecture",
) -> list[Chunk]:
    """Token-aware sliding-buffer segmentation (the production chunker).

    ``pages`` is ``[(page_number, cleaned_body), ...]``. Slides accumulate in a
    buffer until the next slide would push it past ``CHUNK_MAX_TOKENS``, then
    the buffer flushes as one chunk — so chunks land in the 600-1000 token
    band while whole slides (and whole sentences) stay together. A deck with
    <= ``SINGLE_SLIDE_COUNT`` slides or < ``CHUNK_SINGLE_TOKENS`` total tokens
    compiles into a single ``Part 1`` chunk; an 80-slide deck yields 10-14
    balanced chunks with no thought split mid-sentence.
    """
    usable = [(number, body) for number, body in pages if body and len(body) >= 40]
    if not usable:
        return []

    slide_tokens = [estimate_tokens(body) for _, body in usable]
    total_tokens = sum(slide_tokens)

    # Spec rule: tiny decks are ONE chunk regardless of slide count.
    if len(usable) <= SINGLE_SLIDE_COUNT or total_tokens < CHUNK_SINGLE_TOKENS:
        groups = [list(range(len(usable)))]
    else:
        groups: list[list[int]] = []
        buffer: list[int] = []
        buffer_tokens = 0
        for index, tokens in enumerate(slide_tokens):
            if buffer and buffer_tokens + tokens > CHUNK_MAX_TOKENS:
                groups.append(buffer)
                buffer, buffer_tokens = [], 0
            buffer.append(index)
            buffer_tokens += tokens
        if buffer:
            groups.append(buffer)
        # Re-balance when the fan-out leaves the spec's 10-14 band: more than
        # MAX_SEMANTIC_CHUNKS parts, or a long deck that could carry at least
        # MIN_SEMANTIC_CHUNKS chunks without dropping any below the 600-token
        # target. Rebalancing splits at slide boundaries only — never mid-slide.
        over_band = len(groups) > MAX_SEMANTIC_CHUNKS
        under_band = (
            len(usable) > SLIDE_SPLIT_THRESHOLD
            and len(groups) < MIN_SEMANTIC_CHUNKS
            and total_tokens >= MIN_SEMANTIC_CHUNKS * CHUNK_TARGET_TOKENS
        )
        if over_band or under_band:
            parts = _token_part_count(slide_tokens)
            flat = [i for group in groups for i in group]  # indices stay ordered
            groups = [flat[range_[0]:range_[1]] for range_ in _balanced_ranges(len(flat), parts)]
        groups = _merge_tiny_ranges(groups, slide_tokens)

    chunks: list[Chunk] = []
    for index, group in enumerate(groups):
        slice_ = [usable[i] for i in group]
        page_start, page_end = slice_[0][0], slice_[-1][0]
        body = "\n\n".join(text for _, text in slice_)
        title = chunk_title(body, index, lecture_title)
        content = f"{_part_header(index + 1, len(groups), page_start, page_end)}\n\n{body}"
        chunks.append(
            Chunk(
                order_index=index,
                title=title,
                content=content,
                page_start=page_start,
                page_end=page_end,
            )
        )
    return chunks


def _segment_part_count(page_count: int, total_chars: int) -> int:
    """How many parts a deck splits into.

    * <= 30 slides: 2-3 balanced parts (a 60-slide deck stays 2 x 30; 80
      slides becomes 27/27/26). Decks under ``SINGLE_PART_CHARS`` stay as ONE
      part — a 10-slide summary belongs in a single learning unit, and
      splitting it would starve the content-adaptive challenge target.
    * 31-60 slides: 2 parts.  * 61+: 3 parts.
    """
    if page_count == 0 or total_chars < SINGLE_PART_CHARS:
        return 1
    if page_count <= SLIDE_SPLIT_THRESHOLD:
        return PART_COUNT_LARGE if total_chars > 2 * TARGET_CHARS else PART_COUNT_SMALL
    if page_count <= 2 * SLIDE_SPLIT_THRESHOLD:
        return PART_COUNT_SMALL
    return PART_COUNT_LARGE


def segment_slides(
    pages: Sequence[tuple[int, str]],
    *,
    lecture_title: str = "Lecture",
) -> list[Chunk]:
    """Divide page-delimited slide text into balanced, slide-aligned chunks.

    Production path: delegates to :func:`segment_slides_by_tokens` so every
    chunk lands in the 600-1000 token band (small decks compile to one
    ``Part 1``; long decks group into 10-14 semantic chunks).
    """
    return segment_slides_by_tokens(pages, lecture_title=lecture_title)


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
    into ordered learning chunks, split at slide boundaries.

    The ``=== PAGE n/N ===`` markers are parsed back out to recover the slide
    numbers, then :func:`segment_slides` divides the deck into 2-3 balanced
    parts with ``--- [Lecture Part i/n: Slides a-b] ---`` headers. Text without
    page markers (plain prose) falls back to character-count chunking.
    """
    from schema import NodeBlueprint

    matches = list(_PAGE_HEADER_RE.finditer(lecture_text))
    if not matches:
        chunks = chunk_text(lecture_text)
        return [
            NodeBlueprint(
                order_index=index,
                title=chunk_title(chunk, index, "Lecture"),
                content=chunk,
            )
            for index, chunk in enumerate(chunks)
        ]

    pages: list[tuple[int, str]] = []
    for position, match in enumerate(matches):
        start = match.end()
        end = matches[position + 1].start() if position + 1 < len(matches) else len(lecture_text)
        body = "\n".join(
            line for line in lecture_text[start:end].splitlines()
            if not line.startswith("=== PAGE ")
        ).strip()
        pages.append((int(match.group(1)), body))
    return [
        NodeBlueprint(
            order_index=chunk.order_index,
            title=chunk.title,
            content=chunk.content,
        )
        for chunk in segment_slides(pages)
    ]


def parse_pdf(pdf_bytes: bytes, filename: Optional[str] = None) -> LectureParseResult:
    """Full ingestion: bytes in, ordered slide-aligned learning chunks out.

    Text is extracted page-by-page (slide numbers preserved) and divided into
    2-3 balanced parts at slide boundaries — decks of 40-80+ slides no longer
    collapse into one giant chunk ("Lost in the Middle" guard). Every chunk
    carries a ``--- [Lecture Part i/n: Slides a-b] ---`` structural header.
    """
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
    chunks = segment_slides(
        [(p.page_number, p.clean_text) for p in usable], lecture_title=title
    )
    if not chunks:
        raise LectureParseError("text extracted but chunking produced no content")

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


# ---------------------------------------------------------------------------
# Diagram / figure extraction (DIAGRAM_QA ingestion)
# ---------------------------------------------------------------------------

# Slide decks are full of decorative junk (university logos, bullet icons,
# header strips, dividers, spacers). Only substantial embedded raster figures
# become DIAGRAM_QA challenges; everything else is filtered out here so a logo
# can never trigger a diagram question.
MIN_DIAGRAM_SIDE = 250          # px — reject icons/emblems shorter/narrower than this
MIN_DIAGRAM_ASPECT = 0.28       # width/height — reject tall side rules
MAX_DIAGRAM_ASPECT = 3.5        # width/height — reject header strips / dividers
MIN_DIAGRAM_BYTES = 20 * 1024   # raw bytes — reject tiny compressed glyphs
EDGE_BAND = 0.10                # top/bottom 10% of a slide = header/footer band
# Back-compat: the old area floor, kept as an additional lower bound.
MIN_DIAGRAM_PIXELS = 96 * 96
MAX_DIAGRAM_BYTES = 4 * 1024 * 1024
MAX_DIAGRAMS_PER_DECK = 24

# --- Layer 1: perceptual hashing (pHash) -------------------------------------
# University crests, slide branding and running-header art repeat across many
# slides with pixel-level variation. A pHash Hamming distance <= PHASH_HAMMING
# threshold on >= PHASH_MIN_SLIDES distinct slides blacklists the graphic.
PHASH_HAMMING = 4               # <= this distance counts as "the same image"
PHASH_MIN_SLIDES = 2            # repeats on >= this many distinct slides -> junk

# --- Layer 2: administrative-noise text gate ---------------------------------
# A real diagram sits mid-slide with little or no surrounding text; branding
# blocks are pure boilerplate (department names, emails, course codes...).
_ADMIN_NOISE_RE = re.compile(
    r"(?i)\b("
    r"university|college|faculty|department|school\s+of|"
    r"course\s*(?:code|no\.?|title)|professor|instructor|lecturer|"
    r"e-?mail|email|@|instructor@|office\s+hours|office\s+room|"
    r"semester|term\s+\d|fall\s+\d{4}|spring\s+\d{4}|"
    r"slide\s+no?\.?\s*\d|page\s+\d+|"
    r"[A-Z]{2,4}\s?\d{3,4}"        # bare course codes: CS 202, MATH101
    r")\b"
)
_ADMIN_TEXT_MAX_CHARS = 220     # short text blocks are the suspicious ones


def _phash_of(data: bytes) -> Optional[object]:
    """Perceptual hash of an encoded image (Layer 1); None when unsupported.

    Uses Pillow + imagehash. Import errors, decode errors and truncated
    payloads all degrade to ``None`` so a single unreadable image can never
    fail the upload — the pHash layer is an optimisation, not a gate.
    """
    try:
        import imagehash  # type: ignore
        from PIL import Image  # type: ignore
    except ImportError:  # pragma: no cover - optional dependency path
        return None
    try:
        with Image.open(io.BytesIO(data)) as image:
            return imagehash.phash(image.convert("RGB"))
    except Exception:  # noqa: BLE001 - decode failures are silently skipped
        return None


def _rect_text_is_administrative(page: "fitz.Page", rect: "fitz.Rect") -> bool:
    """Layer 2 text gate: True for branding blocks, False for real diagrams.

    The rect's surroundings are probed in three zones — the rect itself (plus
    a small margin) and the full-height page bands immediately LEFT and RIGHT
    of it, which is where slide letterhead (department / e-mail / course code)
    sits next to a decorative graphic. A SHORT block of administrative noise
    rejects the image; a genuine diagram (silent, captioned, or carrying real
    chart annotation) passes. Word-bbox intersection is used instead of
    ``get_text(clip=...)`` because the latter silently drops lines that merely
    touch the clip edge.
    """
    try:
        page_rect = page.rect
        margin = 12.0
        zones = [
            fitz.Rect(rect.x0 - margin, rect.y0 - margin, rect.x1 + margin, rect.y1 + margin),
            # Left band: from the page edge (or the previous figure) to this one.
            fitz.Rect(page_rect.x0, rect.y0 - margin, max(page_rect.x0, rect.x0 - margin), rect.y1 + margin),
            # Right band: from the figure to the page edge (or the next figure).
            fitz.Rect(min(page_rect.x1, rect.x1 + margin), rect.y0 - margin, page_rect.x1, rect.y1 + margin),
        ]
        words = page.get_text("words") or []
    except Exception:  # noqa: BLE001 - older builds / malformed pages
        return False
    pieces: list[str] = []
    for word in words:
        try:
            box = fitz.Rect(word[0], word[1], word[2], word[3])
        except Exception:  # noqa: BLE001
            continue
        if any(box.intersects(zone) for zone in zones if not zone.is_empty):
            pieces.append(str(word[4]))
    text = " ".join(pieces).strip()
    if not text:
        return False  # a silent figure is a legitimate diagram
    if len(text) > _ADMIN_TEXT_MAX_CHARS:
        return False  # chart labels/annotations, not a branding block
    hits = _ADMIN_NOISE_RE.findall(text)
    # One hit can still be a real caption's course code; two or more means
    # the whole block reads like a letterhead.
    return len(hits) >= 2


def _image_only_in_edge_band(page: "fitz.Page", xref: int) -> bool:
    """True when every placement of ``xref`` sits in the top/bottom 10% band.

    University logos and running header/footer graphics live exclusively in the
    page's header or footer. A genuine diagram placed mid-slide is kept; an
    image with no resolvable bounding box is not filtered (unknown position).
    """
    try:
        rects = page.get_image_rects(xref)
    except Exception:  # noqa: BLE001 - older builds / malformed pages
        return False
    if not rects:
        return False
    height = float(page.rect.height or 0.0)
    if height <= 0:
        return False
    top_limit = height * EDGE_BAND
    bottom_limit = height * (1.0 - EDGE_BAND)
    return all(
        rect.y1 <= top_limit or rect.y0 >= bottom_limit for rect in rects
    )


@dataclass
class ExtractedDiagram:
    """One raster figure pulled out of a slide deck.

    ``ref`` is a stable, filesystem-safe identity (``page-3-2``) that the
    challenge payload and the saved file name both key off, so a DIAGRAM_QA
    challenge can always be re-linked to its image.
    """

    page_number: int
    index_on_page: int
    ref: str
    data: bytes
    extension: str
    width: int
    height: int

    @property
    def pixel_count(self) -> int:
        return self.width * self.height


def extract_pdf_diagrams(
    pdf_bytes: bytes,
    *,
    min_pixels: int = MIN_DIAGRAM_PIXELS,
    max_diagrams: int = MAX_DIAGRAMS_PER_DECK,
) -> list[ExtractedDiagram]:
    """Extract embedded raster diagrams/figures/charts with PyMuPDF.

    Uses ``page.get_images()`` + ``doc.extract_image(xref)`` to pull the raw
    image bytes, then filters out the decorative junk that plagues slide decks:

    * dimension floor — ``width`` or ``height`` below ``MIN_DIAGRAM_SIDE``,
    * aspect ratio — wider than ``MAX_DIAGRAM_ASPECT`` or taller than
      ``MIN_DIAGRAM_ASPECT`` (header strips, dividers, side rules),
    * raw byte floor — compressed payloads below ``MIN_DIAGRAM_BYTES``,
    * position — figures living exclusively in the top/bottom 10% band.

    Duplicate xrefs (the same figure reused across slides) are deduplicated, and
    the result is capped at ``max_diagrams`` per deck. Layer 1 (pHash): any
    image repeating with Hamming distance <= 4 across >= 2 distinct slides is
    blacklisted as branding and discarded. Layer 2: images in the header/footer
    band or wrapped in administrative noise text are rejected. Never raises on
    a single unreadable image — a malformed figure must not fail the whole
    upload.
    """
    if not pdf_bytes or max_diagrams <= 0:
        return []
    try:
        document = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception:  # noqa: BLE001 - unreadable PDF is caught by parse_pdf
        return []

    # ------------------------------------------------------------------
    # Pass 1: collect every image xref ONCE (data + dims + distinct pages),
    # so Layer 1 can see all slides a hash repeats on before deciding.
    # ------------------------------------------------------------------
    try:
        page_count = document.page_count
    except Exception:  # noqa: BLE001
        page_count = 0
    xref_digests: dict[int, str] = {}
    xref_data: dict[int, bytes] = {}
    xref_dims: dict[int, tuple[int, int]] = {}
    xref_extension: dict[int, str] = {}
    xref_pages: dict[int, set[int]] = {}
    for page_index in range(page_count):
        page = document[page_index]
        try:
            images = page.get_images(full=True)
        except Exception:  # noqa: BLE001 - some malformed pages lack images
            continue
        for slot, info in enumerate(images):
            xref = int(info[0]) if info and info[0] else 0
            if not xref:
                continue
            xref_pages.setdefault(xref, set()).add(page_index)
            if xref in xref_data:
                continue
            try:
                extracted = document.extract_image(xref)
            except Exception:  # noqa: BLE001
                continue
            data = extracted.get("image")
            width = int(extracted.get("width") or 0)
            height = int(extracted.get("height") or 0)
            if not data or not width or not height or len(data) > MAX_DIAGRAM_BYTES:
                continue
            xref_digests[xref] = hashlib.sha1(data).hexdigest()
            xref_data[xref] = data
            xref_dims[xref] = (width, height)
            xref_extension[xref] = str(extracted.get("ext") or "png").lower()

    # ------------------------------------------------------------------
    # Layer 1 — perceptual hashing: fold every xref's pHash into a canonical
    # near-duplicate cluster and blacklist any cluster that repeats across
    # >= PHASH_MIN_SLIDES DISTINCT slides (university crests, branding, runs
    # of header art). Unique figures are never touched.
    # ------------------------------------------------------------------
    blacklisted: set[int] = set()
    try:
        import imagehash  # type: ignore
    except ImportError:  # pragma: no cover - optional dependency path
        imagehash = None  # type: ignore[assignment]
    if imagehash is not None and xref_data:
        canonical_of: dict[int, str] = {}
        cluster_pages: dict[str, set[int]] = {}
        for xref, data in xref_data.items():
            phash = _phash_of(data)
            if phash is None:
                continue
            canonical = str(phash)
            for existing_key in cluster_pages:
                try:
                    if imagehash.hex_to_hash(existing_key) - phash <= PHASH_HAMMING:
                        canonical = existing_key
                        break
                except Exception:  # noqa: BLE001
                    continue
            canonical_of[xref] = canonical
            cluster_pages.setdefault(canonical, set()).update(xref_pages.get(xref, set()))
        for xref, canonical in canonical_of.items():
            if len(cluster_pages.get(canonical, set())) >= PHASH_MIN_SLIDES:
                blacklisted.add(xref)

    diagrams: list[ExtractedDiagram] = []
    seen_xrefs: set[int] = set()
    seen_digests: set[str] = set()
    try:
        for page_index in range(page_count):
            page = document[page_index]
            try:
                images = page.get_images(full=True)
            except Exception:  # noqa: BLE001 - some malformed pages lack images
                continue
            for slot, info in enumerate(images):
                xref = int(info[0]) if info and info[0] else 0
                if not xref or xref in seen_xrefs:
                    continue
                dims = xref_dims.get(xref)
                data = xref_data.get(xref)
                if dims is None or not data:
                    continue
                width, height = dims
                # Dimension floor: rejects small icons, emblems and bullet art.
                if width < MIN_DIAGRAM_SIDE or height < MIN_DIAGRAM_SIDE:
                    continue
                # Aspect ratio: rejects wide header strips and tall dividers.
                ratio = width / height
                if ratio > MAX_DIAGRAM_ASPECT or ratio < MIN_DIAGRAM_ASPECT:
                    continue
                # File size: rejects tiny compressed glyphs/logos.
                if len(data) < MIN_DIAGRAM_BYTES:
                    continue
                if width * height < min_pixels:
                    continue
                # Layer 1: pHash blacklist (repeated branding across slides).
                if xref in blacklisted:
                    continue
                # Position: rejects header/footer-only graphics (logos).
                if _image_only_in_edge_band(page, xref):
                    continue
                # Layer 2: administrative-noise text gate around the image rect.
                try:
                    rects = page.get_image_rects(xref)
                except Exception:  # noqa: BLE001
                    rects = []
                if rects and all(
                    _rect_text_is_administrative(page, rect) for rect in rects
                ):
                    continue
                digest = xref_digests.get(xref) or hashlib.sha1(data).hexdigest()
                if digest in seen_digests:
                    seen_xrefs.add(xref)
                    continue
                seen_xrefs.add(xref)
                seen_digests.add(digest)
                extension = xref_extension.get(xref, "png")
                if extension not in ("png", "jpeg", "jpg", "webp"):
                    extension = "png"
                diagrams.append(
                    ExtractedDiagram(
                        page_number=page_index + 1,
                        index_on_page=slot,
                        ref=f"page-{page_index + 1}-{slot}",
                        data=data,
                        extension=extension,
                        width=width,
                        height=height,
                    )
                )
                if len(diagrams) >= max_diagrams:
                    return diagrams
    finally:
        try:
            document.close()
        except Exception:  # noqa: BLE001
            pass
    return diagrams


def save_diagrams(
    diagrams: Sequence[ExtractedDiagram], directory: str, *, url_prefix: str = ""
) -> list[dict]:
    """Write extracted diagrams under ``directory`` and describe them.

    Returns ``[{ref, path, url, page, width, height, extension}]`` — ``url`` is
    the browser-facing path (``/static/uploads/diagrams/<course>/page-3-2.png``)
    used as ``DIAGRAM_QA.image_url``. Files that fail to write are skipped.
    """
    written: list[dict] = []
    for diagram in diagrams:
        try:
            os.makedirs(directory, exist_ok=True)
            filename = f"{diagram.ref}.{diagram.extension}"
            path = os.path.join(directory, filename)
            with open(path, "wb") as handle:
                handle.write(diagram.data)
        except OSError:  # pragma: no cover - disk/permission failure is non-fatal
            continue
        written.append(
            {
                "ref": diagram.ref,
                "path": path,
                "url": f"{url_prefix.rstrip('/')}/{filename}" if url_prefix else path,
                "page": diagram.page_number,
                "width": diagram.width,
                "height": diagram.height,
                "extension": diagram.extension,
            }
        )
    return written


__all__ = [
    "Chunk",
    "ExtractedDiagram",
    "LectureParseError",
    "LectureParseResult",
    "PageText",
    "chunk_text",
    "clean_text",
    "derive_lecture_title",
    "estimate_tokens",
    "extract_pages",
    "extract_pdf_content",
    "extract_pdf_diagrams",
    "iter_chunk_summaries",
    "iterate_chunks",
    "parse_pdf",
    "save_diagrams",
    "sanitize_concept_title",
    "segment_slides",
    "segment_slides_by_tokens",
]
