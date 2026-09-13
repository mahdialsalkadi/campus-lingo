"""Shared pytest fixtures for the CampusLingo core-ai test suite.

Makes the service package importable regardless of the invocation directory and
provides the synthetic lecture PDF both the parser and end-to-end suites use.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

SERVICE_DIR = pathlib.Path(__file__).resolve().parents[1]
REPO_ROOT = SERVICE_DIR.parents[1]
for path in (SERVICE_DIR, REPO_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

# Three pages with a repeated running header, a page number, and a hyphenated
# line break — i.e. the artefacts parser.py is supposed to clean up.
LECTURE_PAGES: list[list[str]] = [
    [
        "Rule 6 requires every vessel to proceed at a safe speed so that she can take",
        "proper and effective action to avoid collision and be stopped within a distance",
        "appropriate to the prevailing circumstances and conditions.",
    ],
    [
        "Rule 7 concerns the risk of collision and states that every vessel shall use all",
        "available means appropriate to the circumstances to determine if risk of collision",
        "exists. If there is any doubt such risk shall be deemed to exist.",
    ],
    [
        "Rule 8 covers action to avoid collision, requiring that any action taken shall be",
        "positive, made in ample time and with due regard to good seamanship.",
    ],
]

RUNNING_HEADER = "MARITIME SAFETY 2026 - LECTURE 4"


@pytest.fixture(scope="session")
def fitz_module():
    """PyMuPDF, skipped cleanly when the dependency is absent."""
    return pytest.importorskip("fitz", reason="PyMuPDF (pymupdf) is required")


@pytest.fixture(scope="session")
def sample_pdf_bytes(fitz_module) -> bytes:
    """A 3-page synthetic lecture deck with realistic extraction noise."""
    document = fitz_module.open()
    for page_index, lines in enumerate(LECTURE_PAGES):
        page = document.new_page()
        page.insert_text((72, 50), RUNNING_HEADER, fontsize=9)
        page.insert_text((72, 80), f"Rule {6 + page_index}: Navigation and Safety", fontsize=14)
        page.insert_text((72, 110), str(page_index + 1), fontsize=9)
        y = 150
        for line in lines:
            page.insert_text((72, y), line, fontsize=11)
            y += 18
    pdf_bytes = document.tobytes()
    document.close()
    return pdf_bytes


@pytest.fixture(scope="session")
def sample_lecture_text(sample_pdf_bytes) -> str:
    """Page-delimited text as produced by ``extract_pdf_content``."""
    from ingestion.parser import extract_pdf_content

    return extract_pdf_content(sample_pdf_bytes)


@pytest.fixture(scope="session")
def long_lecture_text() -> str:
    """Enough prose to force multi-chunk splitting."""
    return "\n\n".join(
        f"Paragraph {index}. "
        + ("Safe speed and collision avoidance require continuous assessment. " * 12)
        for index in range(12)
    )
