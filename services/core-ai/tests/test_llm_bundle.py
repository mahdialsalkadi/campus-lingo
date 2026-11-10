"""LLM generation path: request shape, validation, repair loop.

The HTTP transport is replaced with a scripted responder, so these tests assert
what the service *sends* to llama-server (OpenAI-compatible chat completions,
``response_format: json_object`` + top-level ``json_schema``, language-locked
system prompt, low-temperature sampling) and how it reacts to invalid output —
deterministically and without a GPU. Actual generation quality needs a real
model and is exercised manually against the GPU rig.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from llm.generator import (
    LlamaCppClient,
    LlamaCppError,
    aclose_all_clients,
    get_client,
    get_tier,
    generate_learning_bundle,
    normalize_tier,
)
from schema import LearningUnitBundle

LECTURE = (
    "Rule 6 requires every vessel to proceed at a safe speed so that she can take proper and "
    "effective action to avoid collision. Rule 7 concerns risk of collision. Rule 8 requires "
    "positive action in ample time. Rule 5 requires a proper look-out by sight and hearing."
)

# Two items per type: the bundle contract is 6-8 diverse challenges covering
# every type (the schema enforces the count and type coverage).
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
            "type": "MCQ_QUIZ",
            "instructions": "اختر الإجابة الصحيحة",
            "question": "Which rule requires every vessel to proceed at a safe speed?",
            "options": [
                {"text": "Rule 6", "correct": True},
                {"text": "Rule 7", "correct": False},
                {"text": "Rule 8", "correct": False},
            ],
            "explanation": "Rule 6 is the safe-speed rule.",
        },
        {
            "type": "MATCH_MADNESS",
            "instructions": "طابق كل مقياس مع وحدته",
            "seconds": 60,
            "pairs": [
                {"term": "Knot", "definition": "One nautical mile per hour"},
                {"term": "Cable", "definition": "One tenth of a nautical mile"},
                {"term": "Fathom", "definition": "Six feet of depth"},
                {"term": "League", "definition": "Three nautical miles"},
            ],
        },
        {
            "type": "SPOT_MISTAKE",
            "instructions": "اضغط على الجزء الخاطئ",
            "code_or_text": "Rule 8 requires any alteration of course to be a slow, barely noticeable change.",
            "mistake_token": "barely noticeable",
            "explanation": "Rule 8 demands positive, readily apparent action.",
        },
        {
            "type": "CONTEXTUAL_CLOZE",
            "instructions": "أكمل الفراغ",
            "sentence_with_blank": "Every vessel shall use all available means to determine if risk of _____ exists.",
            "correct_answer": "collision",
            "distractors": ["grounding", "flooding", "listing"],
            "explanation": "القاعدة 7 تتعلق بخطر التصادم.",
        },
        {
            "type": "MCQ_QUIZ",
            "instructions": "اختر الإجابة الصحيحة",
            "question": "Which rule concerns the risk of collision?",
            "options": [
                {"text": "Rule 7", "correct": True},
                {"text": "Rule 5", "correct": False},
                {"text": "Rule 6", "correct": False},
            ],
            "explanation": "Rule 7 concerns risk of collision.",
        },
    ],
}

# Structurally invalid: only 2 challenges (below the min_length=2+size bound
# is fine, but the MATCH pairs still break the unique-term rule). Coverage
# gaps are now SOFT (reported via missing_challenge_types), so repair-turn
# tests need a bundle that fails for a hard reason.
INVALID_BUNDLE = {**VALID_BUNDLE, "challenges": VALID_BUNDLE["challenges"][:1]}


class ScriptedTransport(LlamaCppClient):
    """llama.cpp client whose HTTP layer replays a script instead of sending."""

    def __init__(self, replies: list[str | Exception] | None = None) -> None:
        super().__init__()
        self.replies = list(replies or [json.dumps(VALID_BUNDLE)])
        self.calls: list[dict[str, Any]] = []

    async def _post_json(self, path: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        self.calls.append({"path": path, **payload})
        reply = self.replies.pop(0) if self.replies else json.dumps(VALID_BUNDLE)
        if isinstance(reply, Exception):
            raise reply
        return {"choices": [{"message": {"content": reply}}]}

    async def _get_json(self, path: str, timeout: float = 15.0) -> dict[str, Any]:
        if path == "/health":
            return {"status": "ok"}
        if path == "/v1/models":
            return {"data": [{"id": "qwen3.5-9b"}]}
        return {}


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _single_shot_bundle_enabled(monkeypatch):
    """Exercise the single-shot bundle path by default.

    Production defaults to LLAMA_BUNDLE_ATTEMPTS=0 (small models fail coverage
    and go straight to per-type composition); these tests target the bundle
    request/repair logic, so opt back in. Tests needing another value simply
    monkeypatch the module attribute again inside their body.
    """
    import llm.generator as generator_module
    monkeypatch.setattr(generator_module, "BUNDLE_ATTEMPTS", 1)


def test_bundle_request_is_fully_constrained_by_the_model_schema():
    client = ScriptedTransport()
    bundle = _run(client.generate_bundle(LECTURE))

    call = client.calls[0]
    assert call["path"] == "/v1/chat/completions"
    assert call["stream"] is False if "stream" in call else True  # never streamed
    assert call["response_format"] == {"type": "json_object"}

    # `json_schema` IS the LearningUnitBundle JSON Schema, inlined for llama-server
    # as an *object* — a stringified schema is rejected with HTTP 400.
    schema = call["json_schema"]
    assert isinstance(schema, dict)
    assert set(schema["properties"]) == {
        "unit_id", "concept_title", "difficulty_level", "challenges"
    }
    rendered = json.dumps(schema)
    assert "$ref" not in rendered and "$defs" not in rendered
    for challenge_type in ("MATCH_MADNESS", "SPOT_MISTAKE", "CONTEXTUAL_CLOZE", "MCQ_QUIZ"):
        assert challenge_type in rendered

    # Ultra-low-latency sampling: greedy-leaning, deterministic decoding.
    assert call["temperature"] == pytest.approx(0.1)
    assert call["top_p"] == pytest.approx(0.85)

    # The system prompt carries the language lock and the lecture text is embedded.
    assert "CRITICAL LANGUAGE DIRECTIVE" in call["messages"][0]["content"]
    assert "NEVER translate English source content into Arabic" in call["messages"][0]["content"]
    assert "PEDAGOGICAL COVERAGE" in call["messages"][0]["content"]
    assert "Rule 6 requires" in call["messages"][1]["content"]

    assert isinstance(bundle, dict)
    assert sorted(challenge["type"] for challenge in bundle["challenges"]) == [
        "CONTEXTUAL_CLOZE",
        "CONTEXTUAL_CLOZE",
        "MATCH_MADNESS",
        "MATCH_MADNESS",
        "MCQ_QUIZ",
        "MCQ_QUIZ",
        "SPOT_MISTAKE",
        "SPOT_MISTAKE",
    ]
    assert LearningUnitBundle.model_validate(bundle)


def test_rejected_bundle_triggers_a_repair_turn_then_succeeds(monkeypatch):
    # The default is fail-fast (LLAMA_BUNDLE_ATTEMPTS=1); opt into the repair
    # loop explicitly to exercise the validator-feedback turn.
    import llm.generator as generator_module
    monkeypatch.setattr(generator_module, "BUNDLE_ATTEMPTS", 2)
    client = ScriptedTransport([json.dumps(INVALID_BUNDLE), json.dumps(VALID_BUNDLE)])
    bundle = _run(client.generate_bundle(LECTURE))

    assert len(client.calls) == 2
    # The repair turn replays the conversation with the validator feedback.
    repair_messages = client.calls[1]["messages"]
    assert any("rejected by the validator" in m["content"] for m in repair_messages)
    assert any("at least 2" in m["content"] for m in repair_messages)
    assert len(bundle["challenges"]) == 8


def test_permanently_invalid_output_raises_instead_of_persisting():
    client = ScriptedTransport([json.dumps(INVALID_BUNDLE)] * 5)
    with pytest.raises(LlamaCppError) as excinfo:
        _run(client.generate_bundle(LECTURE))
    assert "at least 2" in str(excinfo.value)


def test_transport_failure_surfaces_as_llamacpp_error():
    client = ScriptedTransport([LlamaCppError("cannot reach llama-server")])
    with pytest.raises(LlamaCppError):
        _run(client.generate_bundle(LECTURE))


def test_zero_bundle_attempts_skips_the_single_shot_call():
    client = ScriptedTransport()
    import llm.generator as generator_module
    monkeypatch_bundles = generator_module
    original = monkeypatch_bundles.BUNDLE_ATTEMPTS
    monkeypatch_bundles.BUNDLE_ATTEMPTS = 0
    try:
        with pytest.raises(LlamaCppError) as excinfo:
            _run(client.generate_bundle(LECTURE))
    finally:
        monkeypatch_bundles.BUNDLE_ATTEMPTS = original
    assert "skipped" in str(excinfo.value)
    assert client.calls == []


def test_request_disables_qwen_thinking_mode():
    """Qwen 3.5 thinking burns the whole completion budget on reasoning_content,
    leaving content empty — structured requests must switch it off."""
    client = ScriptedTransport()
    _run(client.generate_bundle(LECTURE))
    call = client.calls[0]
    assert call["chat_template_kwargs"] == {"enable_thinking": False}


def test_short_lecture_text_is_refused_without_calling_the_model():
    client = ScriptedTransport()
    with pytest.raises(LlamaCppError):
        _run(client.generate_bundle("short"))
    assert client.calls == []


def test_module_level_helper_uses_the_shared_client(monkeypatch):
    import llm.generator as generator_module

    client = ScriptedTransport()
    # generate_learning_bundle() goes through get_client(); pin it to the
    # scripted transport so the test never touches a real endpoint.
    monkeypatch.setattr(generator_module, "get_client", lambda tier=None: client)
    bundle = _run(generate_learning_bundle(LECTURE))
    assert len(bundle["challenges"]) == 8
    assert client.calls[0]["path"] == "/v1/chat/completions"


def test_health_reports_ok_when_server_is_up():
    client = ScriptedTransport()
    health = _run(client.health())
    assert health["ok"] is True
    assert health["base_url"]
    assert health["chat_model_present"] is True


def test_health_reports_unreachable_server():
    client = ScriptedTransport()

    async def fail_get(path: str, timeout: float = 15.0) -> dict[str, Any]:
        raise LlamaCppError("connection refused")

    client._get_json = fail_get  # type: ignore[method-assign]
    health = _run(client.health())
    assert health["ok"] is False
    assert "refused" in health["error"]


# ---------------------------------------------------------------------------
# Model tiers
# ---------------------------------------------------------------------------


def test_tier_registry_exposes_exactly_four_tiers():
    assert set(get_tier_registry_keys()) == {"800M", "2B", "4B", "9B"}
    # 16k context on a DEDICATED single stream per tier: 100% of GPU compute
    # and memory bandwidth focus on the active ingestion.
    slots = {tier: get_tier(tier)["parallel_slots"] for tier in ("800M", "2B", "4B", "9B")}
    assert slots == {"800M": 1, "2B": 1, "4B": 1, "9B": 1}
    ctx = {tier: get_tier(tier)["context_size"] for tier in ("800M", "2B", "4B", "9B")}
    assert ctx == {"800M": 16384, "2B": 16384, "4B": 16384, "9B": 16384}
    # Physical/logical batch sizes shrink with model size (14GB VRAM budget).
    batches = {tier: (get_tier(tier)["batch_size"], get_tier(tier)["ubatch_size"])
               for tier in ("800M", "2B", "4B", "9B")}
    assert batches == {"800M": (4096, 2048), "2B": (4096, 1024),
                       "4B": (4096, 1024), "9B": (2048, 512)}


def test_tier_registry_is_qwen_35_on_fixed_ports():
    expected = {
        "800M": (8081, 1, 16384, "Qwen 3.5 0.8B", "models/qwen3.5-0.8b-instruct-q4_k_m.gguf"),
        "2B": (8082, 1, 16384, "Qwen 3.5 2B", "models/qwen3.5-2b-instruct-q4_k_m.gguf"),
        "4B": (8083, 1, 16384, "Qwen 3.5 4B (DeltaNet)", "models/qwen3.5-4b-instruct-q4_k_m.gguf"),
        "9B": (8084, 1, 16384, "Qwen 3.5 9B", "models/qwen3.5-9b-instruct-q4_k_m.gguf"),
    }
    from llm.generator import MODEL_TIERS

    for tier_id, (port, parallel, ctx, name, path) in expected.items():
        spec = MODEL_TIERS[tier_id]
        assert spec["port"] == port, tier_id
        assert spec["parallel_slots"] == parallel, tier_id
        assert spec["context_size"] == ctx, tier_id
        assert spec["name"] == name, tier_id
        assert spec["model_path"] == path, tier_id


def test_normalize_tier_maps_aliases_to_canonical_ids():
    assert normalize_tier("1") == "800M"
    assert normalize_tier("800M") == "800M"
    assert normalize_tier("2b") == "2B"
    assert normalize_tier("3") == "4B"
    assert normalize_tier("9b") == "9B"
    assert normalize_tier("heavy") == "9B"
    assert normalize_tier("bogus") is None
    assert normalize_tier(None) is None


def test_unknown_tier_falls_back_to_default():
    assert get_tier("99")["id"] == get_tier(None)["id"]
    assert get_tier("")["id"] == get_tier(None)["id"]


def test_get_client_routes_each_tier_to_its_own_port():
    expected_ports = {"800M": 8081, "2B": 8082, "4B": 8083, "9B": 8084}
    for tier_id, port in expected_ports.items():
        client = get_client(tier_id)
        assert client.base_url == f"http://127.0.0.1:{port}", tier_id
        assert client.tier == tier_id
        # Cached per tier: repeated lookups reuse the same pooled client.
        assert get_client(tier_id) is client
    # Alias spellings resolve to the canonical tier's client.
    assert get_client("1") is get_client("800M")
    assert get_client("4") is get_client("9B")
    # None / unknown -> default tier (4B on 8083).
    assert get_client(None).base_url.endswith(":8083")


def test_aclose_all_clients_clears_the_cache():
    first = get_client("2B")

    async def _close():
        await aclose_all_clients()

    asyncio.run(_close())
    assert get_client("2B") is not first


def test_get_client_is_a_single_server_singleton():
    client = get_client()
    # Each tier resolves to its own cached client; the default tier (4B) is
    # stable across calls and speaks to the generic "default" model id.
    assert get_client() is client
    assert client.base_url.endswith(":8083")
    assert client.model == "default"  # generic id; GGUF-agnostic contract


def get_tier_registry_keys() -> list[str]:
    from llm.generator import MODEL_TIERS

    return sorted(MODEL_TIERS)
