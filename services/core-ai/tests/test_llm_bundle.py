"""LLM generation path: request shape, validation, repair loop.

The HTTP transport is replaced with a scripted responder, so these tests assert
what the service *sends* to Ollama (schema in ``format``, Arabic system prompt,
model name, thinking off) and how it reacts to invalid output — deterministically
and without a GPU. Actual generation quality needs a real model and is exercised
manually against the GPU rig.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from llm.generator import OllamaClient, OllamaError, generate_learning_bundle
from schema import LearningUnitBundle

LECTURE = (
    "Rule 6 requires every vessel to proceed at a safe speed so that she can take proper and "
    "effective action to avoid collision. Rule 7 concerns risk of collision. Rule 8 requires "
    "positive action in ample time. Rule 5 requires a proper look-out by sight and hearing."
)

VALID_BUNDLE: dict[str, Any] = {
    "unit_id": "unit-1",
    "concept_title": "قواعد المناورة الآمنة (Safe Speed)",
    "difficulty_level": 3,
    "challenges": [
        {
            "type": "MATCH_MADNESS",
            "instructions": "طابق كل قاعدة مع موضوعها",
            "seconds": 60,
            "pairs": [
                {"term": "Rule 6", "definition": "Safe speed"},
                {"term": "Rule 7", "definition": "Risk of collision"},
                {"term": "Rule 8", "definition": "Action to avoid collision"},
                {"term": "Rule 5", "definition": "Look-out"},
            ],
        },
        {
            "type": "SPOT_MISTAKE",
            "instructions": "اضغط على الجزء الخاطئ",
            "code_or_text": "Rule 7 requires every vessel to maintain a minimum speed of 20 knots in fog.",
            "mistake_token": "20 knots",
            "explanation": "Rule 7 is about risk of collision only.",
        },
        {
            "type": "CONTEXTUAL_CLOZE",
            "instructions": "أكمل الفراغ",
            "sentence_with_blank": "Rule 6 requires every vessel to proceed at a safe _____ at all times.",
            "correct_answer": "speed",
            "distractors": ["depth", "heading", "mass"],
            "explanation": "القاعدة 6 هي قاعدة السرعة الآمنة.",
        },
        {
            "type": "INTERACTIVE_STORY",
            "instructions": "اختر القرار الصحيح",
            "scenario_title": "المناوبة الليلية",
            "dialogue": [
                {"speaker": "الربان", "text": "الضباب يزداد.", "action_prompt": "ما الإجراء؟",
                 "options": [
                     {"text": "تقليل السرعة", "correct": True, "feedback": "متوافق مع القاعدة 19."},
                     {"text": "الحفاظ على السرعة", "correct": False, "feedback": "غير آمن."},
                 ]},
                {"speaker": "الربان", "text": "هدف على الرادار.", "action_prompt": "ما إجراؤك؟",
                 "options": [
                     {"text": "تقييم خطر التصادم", "correct": True, "feedback": "ممتاز."},
                     {"text": "تجاهله", "correct": False, "feedback": "إهمال."},
                 ]},
            ],
        },
    ],
}

# Two MATCH_MADNESS entries violate the "distinct challenge types" rule.
INVALID_BUNDLE = {**VALID_BUNDLE,
                  "challenges": [VALID_BUNDLE["challenges"][0], VALID_BUNDLE["challenges"][0]]}


class ScriptedClient(OllamaClient):
    """Ollama client whose transport replays a script instead of sending HTTP."""

    def __init__(self, replies: list[str | Exception] | None = None) -> None:
        super().__init__()
        self.replies = list(replies or [json.dumps(VALID_BUNDLE)])
        self.calls: list[dict[str, Any]] = []

    def _post(self, path: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        self.calls.append({"path": path, **payload})
        reply = self.replies.pop(0) if self.replies else json.dumps(VALID_BUNDLE)
        if isinstance(reply, Exception):
            raise reply
        return {"response": reply, "message": {"content": reply}}


def test_bundle_request_is_fully_constrained_by_the_model_schema():
    client = ScriptedClient()
    bundle = asyncio.run(client.generate_bundle(LECTURE, model_name="qwen2.5:7b"))

    call = client.calls[0]
    assert call["path"] == "/api/generate"
    assert call["model"] == "qwen2.5:7b"
    assert call["stream"] is False
    assert call["think"] is False

    # `format` IS the LearningUnitBundle JSON Schema, inlined for Ollama.
    assert set(call["format"]["properties"]) == {
        "unit_id", "concept_title", "difficulty_level", "challenges"
    }
    rendered = json.dumps(call["format"])
    assert "$ref" not in rendered and "$defs" not in rendered
    for challenge_type in ("MATCH_MADNESS", "SPOT_MISTAKE", "CONTEXTUAL_CLOZE", "INTERACTIVE_STORY"):
        assert challenge_type in rendered

    # The Arabic academic prompt is sent, and the lecture text is embedded.
    assert "أستاذ جامعي" in call["system"]
    assert "الدقة العلمية" in call["system"]
    assert "الدعابة" in call["system"] and "دون أي تهوين" in call["system"]
    assert "Rule 6 requires" in call["prompt"]

    assert isinstance(bundle, dict)
    assert sorted(challenge["type"] for challenge in bundle["challenges"]) == [
        "CONTEXTUAL_CLOZE", "INTERACTIVE_STORY", "MATCH_MADNESS", "SPOT_MISTAKE"
    ]
    assert LearningUnitBundle.model_validate(bundle)


def test_rejected_bundle_triggers_a_repair_turn_then_succeeds():
    client = ScriptedClient([json.dumps(INVALID_BUNDLE), json.dumps(VALID_BUNDLE)])
    bundle = asyncio.run(client.generate_bundle(LECTURE, model_name="qwen2.5:7b"))

    assert len(client.calls) == 2
    repair_prompt = client.calls[1]["prompt"]
    assert "rejected by the validator" in repair_prompt
    assert "duplicate" in repair_prompt
    assert len(bundle["challenges"]) == 4


def test_permanently_invalid_output_raises_instead_of_persisting():
    client = ScriptedClient([json.dumps(INVALID_BUNDLE)] * 5)
    with pytest.raises(OllamaError) as excinfo:
        asyncio.run(client.generate_bundle(LECTURE, model_name="qwen2.5:7b"))
    assert "duplicate" in str(excinfo.value)


def test_transport_failure_surfaces_as_ollama_error():
    client = ScriptedClient([OllamaError("cannot reach Ollama")])
    with pytest.raises(OllamaError):
        asyncio.run(client.generate_bundle(LECTURE, model_name="qwen2.5:7b"))


def test_short_lecture_text_is_refused_without_calling_the_model():
    client = ScriptedClient()
    with pytest.raises(OllamaError):
        asyncio.run(client.generate_bundle("short", model_name="qwen2.5:7b"))
    assert client.calls == []


def test_module_level_helper_uses_the_shared_client(monkeypatch):
    import llm.generator as generator_module

    client = ScriptedClient()
    monkeypatch.setattr(generator_module, "_client", client)
    bundle = asyncio.run(
        generate_learning_bundle(LECTURE, model_name="qwen2.5:7b")
    )
    assert len(bundle["challenges"]) == 4
    assert client.calls[0]["model"] == "qwen2.5:7b"


def test_health_reports_missing_chat_model(monkeypatch):
    client = ScriptedClient()

    def fake_get(path: str, timeout: float = 15.0) -> dict[str, Any]:
        assert path == "/api/tags"
        return {"models": [{"name": "qwen3.5:9b"}, {"name": "other:latest"}]}

    monkeypatch.setattr(client, "_get", fake_get)
    health = asyncio.run(client.health())
    assert health["ok"] is True
    assert health["models"] == ["qwen3.5:9b", "other:latest"]
    assert health["embed_model_present"] is False


def test_health_reports_unreachable_ollama(monkeypatch):
    client = ScriptedClient()

    def fake_get(path: str, timeout: float = 15.0) -> dict[str, Any]:
        raise OllamaError("connection refused")

    monkeypatch.setattr(client, "_get", fake_get)
    health = asyncio.run(client.health())
    assert health["ok"] is False
    assert "refused" in health["error"]
