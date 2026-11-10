"""Lecture ingestion: PDF -> clean text -> ordered learning chunks -> diagrams."""

from ingestion.parser import (
    Chunk,
    ExtractedDiagram,
    LectureParseResult,
    PageText,
    clean_text,
    derive_lecture_title,
    extract_pages,
    extract_pdf_diagrams,
    iter_chunk_summaries,
    parse_pdf,
    save_diagrams,
    sanitize_concept_title,
)

__all__ = [
    "Chunk",
    "ExtractedDiagram",
    "LectureParseResult",
    "PageText",
    "clean_text",
    "derive_lecture_title",
    "extract_pages",
    "extract_pdf_diagrams",
    "iter_chunk_summaries",
    "parse_pdf",
    "save_diagrams",
    "sanitize_concept_title",
]
