"""Exam Crunch — the night-before study engine.

Two deliverables per course:

1. **High-yield cheatsheet** — a Markdown reader (top formulas, core
   definitions, common exam traps) synthesized by the LLM and *always*
   available: if the model tier is down, a deterministic extractive cheatsheet
   is built from the lecture digests instead, so the button never dead-ends.
2. **Diagnostic timed mock exam** — 25-40 server-graded questions spread across
   every chapter, with a 45-minute synchronized countdown. Grading produces the
   letter grade, exam-readiness estimate and the per-chapter weakness heatmap.

Everything here is pure and importable without a database; ``main.py`` wires it
to the ORM. Question grading reuses the arena's server-side adapter so an exam
answer is never trusted from the client.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional, Sequence

from arena.rooms import arena_question_from_challenge
from llm.generator import LlamaCppError, get_client

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

EXAM_DURATION_SECONDS = 45 * 60
EXAM_TARGET_MIN = 25
EXAM_TARGET_MAX = 40
# Above this many candidate questions the exam grows toward the max; a small
# course still yields a usable (if shorter) diagnostic rather than empty pads.
EXAM_LARGE_POOL = 120

CHEATSHEET_TEMPERATURE = 0.4
CHEATSHEET_MAX_TOKENS = 1500
CHEATSHEET_DIGEST_CHARS = 12_000

GRADE_BANDS: tuple[tuple[float, str], ...] = (
    (0.90, "A"),
    (0.80, "B"),
    (0.70, "C"),
    (0.60, "D"),
    (0.0, "F"),
)

CHEATSHEET_SYSTEM_PROMPT = (
    "You are CampusLingo's exam-crunch analyst. Turn lecture material into a "
    "ruthlessly high-yield revision sheet. Be concrete, never generic. "
    "Reply in the language of the source material (English slides -> English "
    "cheatsheet). Output GitHub-flavoured Markdown only, with these sections in "
    "order: '## Key Formulas' (a list, each formula in inline code or LaTeX), "
    "'## Core Definitions' (term — one-line definition), "
    "'## Common Exam Traps' (the mistakes students actually make), "
    "'## 60-Second Recap' (5 bullets). Maximum 10 items per section. "
    "Never invent material that is not in the lecture text."
)


# ---------------------------------------------------------------------------
# Cheatsheet
# ---------------------------------------------------------------------------


def _first_sentences(text: str, limit: int = 3) -> str:
    cleaned = " ".join((text or "").split())
    if not cleaned:
        return ""
    parts = cleaned.split(". ")
    return ". ".join(parts[:limit]).strip()[:320]


def fallback_cheatsheet(digests: Sequence[str], course_name: str = "") -> str:
    """Deterministic extractive cheatsheet (no LLM required).

    Used when the model tier is unreachable, so ``/exam-crunch`` still returns a
    printable revision sheet. Content is the leading sentences of each chapter
    digest — lower quality than the model's, but always truthful to the source.
    """
    lines: list[str] = [f"# Exam Crunch — {course_name or 'Course'}", ""]
    lines.append("## Key Formulas")
    lines.append(
        "_No model tier was reachable, so this is an extractive summary of your "
        "chapters. Formulas are the sentences containing numbers or symbols._"
    )
    lines.append("")
    formula_lines: list[str] = []
    definition_lines: list[str] = []
    for digest in digests:
        for raw in (digest or "").splitlines():
            line = raw.strip()
            if not line:
                continue
            if any(ch.isdigit() for ch in line) and any(
                symbol in line for symbol in ("=", "→", "%", "×", "/")
            ):
                formula_lines.append(f"- {line[:220]}")
            elif ":" in line or "—" in line:
                definition_lines.append(f"- {line[:220]}")
        if len(formula_lines) >= 10 and len(definition_lines) >= 10:
            break

    lines.extend(formula_lines[:10] or ["- (no formulas detected in this material)"])
    lines.extend(["", "## Core Definitions"])
    lines.extend(definition_lines[:10] or ["- (no definitions detected in this material)"])
    lines.extend(["", "## Common Exam Traps"])
    lines.append("- Re-read every chapter summary below; exam questions cluster around them.")
    lines.append("- Distinguish similar terms before answering a comparison question.")
    lines.extend(["", "## 60-Second Recap"])
    for index, digest in enumerate(digests[:5], start=1):
        lines.append(f"- **Chapter {index}:** {_first_sentences(digest, 2)}")
    if not digests:
        lines.append("- Upload a lecture to generate your crunch sheet.")
    return "\n".join(lines).strip() + "\n"


def build_digest(chunks: Sequence[Any], *, max_chars: int = CHEATSHEET_DIGEST_CHARS) -> str:
    """Flatten lecture chunks into one budgeted digest for the cheatsheet prompt."""
    parts: list[str] = []
    used = 0
    for chunk in chunks:
        title = getattr(chunk, "title", "") or ""
        content = getattr(chunk, "content", "") or ""
        block = f"### {title}\n{content}".strip()
        if not block:
            continue
        remaining = max_chars - used
        if remaining <= 0:
            break
        parts.append(block[:remaining])
        used += len(parts[-1])
    return "\n\n".join(parts)


async def synthesize_cheatsheet(
    digest: str,
    course_name: str = "",
    *,
    tier: Optional[str] = None,
    max_tokens: int = CHEATSHEET_MAX_TOKENS,
) -> dict[str, Any]:
    """LLM cheatsheet with a guaranteed extractive fallback.

    Returns ``{"markdown": str, "source": "llm"|"fallback", "error": str|None}``.
    A llama-server outage is logged into ``error`` and never raised: the exam
    crunch flow must not be blocked by generation infrastructure.
    """
    digests = [digest] if digest else []
    fallback = fallback_cheatsheet(digests, course_name)
    if not digest.strip():
        return {"markdown": fallback, "source": "fallback", "error": "no lecture text"}

    messages = [
        {"role": "system", "content": CHEATSHEET_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"Course: {course_name or 'Course'}\n"
                "Lecture material:\n"
                f"{digest}\n\n"
                "Write the crunch sheet now."
            ),
        },
    ]
    try:
        client = get_client(tier)
        pieces: list[str] = []
        async for delta in client.stream_chat(
            messages, max_tokens=max_tokens, temperature=CHEATSHEET_TEMPERATURE
        ):
            pieces.append(delta)
        markdown = "".join(pieces).strip()
        if len(markdown) < 80:  # a stub answer is worse than the extractive one
            return {"markdown": fallback, "source": "fallback", "error": "empty completion"}
        return {"markdown": markdown, "source": "llm", "error": None}
    except (LlamaCppError, Exception) as exc:  # noqa: BLE001 - degrade, never fail
        return {"markdown": fallback, "source": "fallback", "error": str(exc)}


# ---------------------------------------------------------------------------
# Diagnostic mock exam
# ---------------------------------------------------------------------------


def exam_target(candidate_count: int, *, minimum: int = EXAM_TARGET_MIN,
                maximum: int = EXAM_TARGET_MAX) -> int:
    """How many questions the mock exam should hold for a given pool size.

    Scales with the material: a small course still gets a real diagnostic (down
    to ``minimum``, or the whole pool when it is smaller), a large one grows to
    ``maximum`` instead of padding to a fixed number.
    """
    if candidate_count <= 0:
        return 0
    if candidate_count < minimum:
        return candidate_count
    if candidate_count >= EXAM_LARGE_POOL:
        return maximum
    ratio = (candidate_count - minimum) / (EXAM_LARGE_POOL - minimum)
    return int(minimum + ratio * (maximum - minimum))


def letter_grade(percent: float) -> str:
    """A/B/C/D/F band for a 0..1 fraction (D exists so F means 'needs work')."""
    for threshold, letter in GRADE_BANDS:
        if percent >= threshold:
            return letter
    return "F"


def _prompt_of(question: dict[str, Any]) -> str:
    return str(question.get("prompt") or "").strip()


def assemble_mock_exam(
    candidates: Sequence[dict[str, Any]],
    *,
    target: Optional[int] = None,
) -> list[dict[str, Any]]:
    """Pick a chapter-balanced exam from gradable challenge candidates.

    ``candidates`` items are ``{challenge_id, node_id, node_title, chapter, type,
    payload_json}`` (raw DB rows). Each is adapted with the arena's server-side
    adapter so the exam holds a prompt, options and the authoritative answer
    index — the answer never leaves the server until grading.

    Questions are drawn round-robin across chapters, so a 5-chapter course is
    never dominated by its first lecture.
    """
    by_chapter: dict[str, list[dict[str, Any]]] = {}
    seen_ids: set[str] = set()
    for candidate in candidates:
        challenge_id = str(candidate.get("challenge_id") or "")
        if not challenge_id or challenge_id in seen_ids:
            continue
        adapted = arena_question_from_challenge(
            str(candidate.get("type") or ""),
            candidate.get("payload_json") or {},
            seed=challenge_id,
        )
        if adapted is None or not _prompt_of(adapted) or len(adapted.get("options", [])) < 2:
            continue
        seen_ids.add(challenge_id)
        chapter = str(candidate.get("chapter") or candidate.get("node_title") or "Chapter")
        adapted.update(
            {
                "challenge_id": challenge_id,
                "node_id": str(candidate.get("node_id") or ""),
                "node_title": str(candidate.get("node_title") or chapter),
                "chapter": chapter,
                "explanation": str((candidate.get("payload_json") or {}).get("explanation") or ""),
            }
        )
        by_chapter.setdefault(chapter, []).append(adapted)

    pool = sum(len(items) for items in by_chapter.values())
    wanted = target if target is not None else exam_target(pool)
    if wanted <= 0:
        return []

    # Longest chapter first so the rarest material still makes the paper.
    chapters = sorted(by_chapter.values(), key=len, reverse=True)
    exam: list[dict[str, Any]] = []
    cursor = 0
    while len(exam) < wanted:
        progressed = False
        for items in chapters:
            if cursor < len(items):
                exam.append(items[cursor])
                progressed = True
                if len(exam) >= wanted:
                    break
        if not progressed:
            break
        cursor += 1
    return exam


def public_exam_questions(questions: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Strip the answer key before sending the exam to the browser."""
    return [
        {
            "index": index,
            "challenge_id": question["challenge_id"],
            "chapter": question.get("chapter"),
            "node_title": question.get("node_title"),
            "type": question.get("type"),
            "prompt": question.get("prompt"),
            "options": question.get("options", []),
        }
        for index, question in enumerate(questions)
    ]


def grade_exam(
    questions: Sequence[dict[str, Any]], answers: dict[str, Any]
) -> dict[str, Any]:
    """Grade a submitted exam and build the weakness heatmap.

    ``answers`` maps ``challenge_id -> selected option index`` (extra keys are
    ignored, missing ones count as unanswered/wrong). Returns score, letter
    grade, exam-readiness estimate, per-chapter heatmap rows and the weak-spot
    list the "Drill Weak Spots" button consumes.
    """
    total = len(questions)
    if total == 0:
        return {
            "score": 0,
            "total": 0,
            "percent": 0.0,
            "grade": "F",
            "readiness": 0.0,
            "chapters": [],
            "weak_spots": [],
            "review": [],
        }

    score = 0
    chapters: dict[str, dict[str, Any]] = {}
    weak_spots: list[dict[str, Any]] = []
    review: list[dict[str, Any]] = []

    for question in questions:
        chapter = str(question.get("chapter") or "Chapter")
        bucket = chapters.setdefault(
            chapter, {"chapter": chapter, "total": 0, "correct": 0, "weak_concepts": []}
        )
        bucket["total"] += 1
        raw = answers.get(question["challenge_id"])
        selected: Optional[int]
        try:
            selected = int(raw) if raw is not None and raw != "" else None
        except (TypeError, ValueError):
            selected = None
        correct = selected is not None and selected == question.get("answer_index")
        if correct:
            score += 1
            bucket["correct"] += 1
        else:
            concept = question.get("node_title") or chapter
            if concept not in bucket["weak_concepts"]:
                bucket["weak_concepts"].append(concept)
            weak_spots.append(
                {
                    "challenge_id": question["challenge_id"],
                    "node_id": question.get("node_id"),
                    "node_title": question.get("node_title"),
                    "chapter": chapter,
                    "type": question.get("type"),
                    "prompt": question.get("prompt"),
                    "correct_index": question.get("answer_index"),
                    "selected_index": selected,
                    "explanation": question.get("explanation") or "",
                }
            )
        review.append(
            {
                "challenge_id": question["challenge_id"],
                "chapter": chapter,
                "correct": bool(correct),
                "selected_index": selected,
                "correct_index": question.get("answer_index"),
            }
        )

    percent = score / total
    grade = letter_grade(percent)
    # Readiness discounts a lucky high score on a short paper: coverage matters
    # as much as accuracy for "am I ready for the real exam?".
    coverage = min(1.0, total / EXAM_TARGET_MIN)
    readiness = round(percent * (0.85 + 0.15 * coverage), 4)

    heatmap = [
        {
            "chapter": row["chapter"],
            "total": row["total"],
            "correct": row["correct"],
            "accuracy": round(row["correct"] / row["total"], 4) if row["total"] else 0.0,
            "risk": (
                "high"
                if row["total"] and row["correct"] / row["total"] < 0.5
                else "medium"
                if row["total"] and row["correct"] / row["total"] < 0.75
                else "low"
            ),
            "weak_concepts": row["weak_concepts"][:3],
        }
        for row in chapters.values()
    ]
    heatmap.sort(key=lambda item: item["accuracy"])

    return {
        "score": score,
        "total": total,
        "percent": round(percent, 4),
        "grade": grade,
        "readiness": readiness,
        "chapters": heatmap,
        "weak_spots": weak_spots,
        "review": review,
    }


# Deterministic "top N struggling concepts" for the instructor dashboard.
def top_struggling_concepts(
    failures: Iterable[dict[str, Any]], *, limit: int = 3
) -> list[dict[str, Any]]:
    """Rank concepts by failure count (used by classroom lecture insights)."""
    tally: dict[str, dict[str, Any]] = {}
    for failure in failures:
        concept = str(failure.get("node_title") or failure.get("chapter") or "Unknown")
        entry = tally.setdefault(concept, {"concept": concept, "failures": 0, "attempts": 0})
        entry["attempts"] += 1
        if not failure.get("correct"):
            entry["failures"] += 1
    ranked = sorted(
        tally.values(),
        key=lambda item: (item["failures"], -item["attempts"]),
        reverse=True,
    )
    return ranked[:limit]
