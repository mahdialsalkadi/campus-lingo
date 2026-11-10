"""Export the challenge GBNF grammar consumed by llama-server.

Single source of truth is ``schema.py``: the exported grammar is generated
from the challenge-type registry and each payload model's ``model_fields``,
covering every payload type (including the micro-games SWIPE_BOOLEAN,
SPOT_IMPOSTER, TAP_TO_FILL and BOSS_FIGHT). A llama-server started with
``--grammar-file`` can physically only emit tokens that form a valid JSON
challenge bundle with a pinned ``type`` field, which eliminates JSON syntax
retries entirely.

llama.cpp GBNF constraints honoured here:
* rule names may only contain ``[a-zA-Z0-9-]`` — underscores are illegal
  (``kv-unit_id`` aborts the server with "expecting name"), so every rule
  name below is hyphenated;
* the boot grammar is a *coarse structural net*. The fine-grained layer is
  the per-request ``json_schema`` sent by ``llm/generator.py`` plus the
  Pydantic validators — field ORDER/OMISSION stays free here so a slightly
  incomplete emission still parses and gets repaired downstream.

NOTE: a boot-time ``--grammar-file`` constrains EVERY completion of that
server to this JSON shape. Plain-text streams (the Socratic tutor) would be
forced into JSON too, so ``run.sh`` keeps boot enforcement opt-in via
``LLAMA_BOOT_GRAMMAR=1`` and relies on the per-request ``json_schema`` by
default — which is the same GBNF restriction, applied only where it belongs.

Usage (from services/core-ai, with the venv active)::

    python scripts/export_gbnf.py [output_path]

Default output: ``llm/grammars/challenge_bundle.gbnf`` (the path run.sh reads).
"""

from __future__ import annotations

import sys
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parents[1]
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

DEFAULT_OUTPUT = SERVICE_DIR / "llm" / "grammars" / "challenge_bundle.gbnf"

# type name -> payload model class name in schema.py. Explicit map: a typo
# fails loudly at export time instead of silently emitting a loose grammar.
PAYLOAD_MODEL_NAMES: dict[str, str] = {
    "MCQ_QUIZ": "McqQuizChallenge",
    "SPOT_MISTAKE": "SpotTheMistakeChallenge",
    "CONTEXTUAL_CLOZE": "ContextualClozeChallenge",
    "MATCH_MADNESS": "MatchMadnessChallenge",
    "TRUE_FALSE": "TrueFalseChallenge",
    "SWIPE_BOOLEAN": "SwipeBooleanChallenge",
    "SPOT_IMPOSTER": "SpotImposterChallenge",
    "TAP_TO_FILL": "TapToFillChallenge",
    "BOSS_FIGHT": "BossFightChallenge",
    "DIAGRAM_QA": "DiagramQaChallenge",
}


# Canonical llama.cpp JSON-string rule (escape sequences + unicode runs).
# Raw string on purpose: every character below lands verbatim in the .gbnf
# file, so there is no second layer of escaping to get wrong.
JSON_STRING_RULE = r'''string ::= "\"" ([^"\\\x00-\x1f] | "\\" ("\"" | "\\" | "/" | "b" | "f" | "n" | "r" | "t" | "u" [0-9a-fA-F] [0-9a-fA-F] [0-9a-fA-F] [0-9a-fA-F]))* "\""
'''


def payload_field_keys() -> list[str]:
    """Every payload field name across the registry (minus pinned keys).

    Derived from the Pydantic models so the grammar and the validators can
    never disagree about which keys a challenge object may carry.
    """
    import schema

    pinned = {"type", "instructions"}
    keys: list[str] = []
    for type_name, class_name in PAYLOAD_MODEL_NAMES.items():
        model = getattr(schema, class_name, None)
        if model is None:
            raise SystemExit(
                f"schema.{class_name} (payload of {type_name}) is missing — "
                "update PAYLOAD_MODEL_NAMES in scripts/export_gbnf.py"
            )
        for field_name in model.model_fields:
            if field_name not in pinned and field_name not in keys:
                keys.append(field_name)
    return keys


def build_grammar() -> str:
    """Compose the GBNF for one challenge-bundle JSON object.

    The grammar mirrors the generator's fallback bundle shape: ``unit_id``,
    ``concept_title``, ``difficulty_level`` (1-5) and a non-empty
    ``challenges`` array whose every member pins ``type`` to one of the known
    registry values — mirroring what ``json_schema_for`` enforces with
    ``const`` so the two constraint layers cannot disagree.
    """
    from schema import ALL_CHALLENGE_TYPES, GENERATION_CHALLENGE_TYPES

    # De-duplicate while preserving order (the generation surface is a subset
    # of the renderable registry, so a plain union would repeat entries).
    ordered: list[str] = []
    for name in (*GENERATION_CHALLENGE_TYPES, *ALL_CHALLENGE_TYPES):
        if name not in ordered:
            ordered.append(name)
    type_alts = " | ".join(f'"\\"{name}\\""' for name in ordered)

    # Allowed keys inside a challenge object (everything except the two
    # keys the grammar pins positionally). Field order/omission stays free.
    field_keys = payload_field_keys()
    if not field_keys:  # defensive: never emit an unconstructable grammar
        raise SystemExit("no payload fields discovered from schema.py")
    key_alts = " | ".join(f'"\\"{key}\\""' for key in field_keys)

    lines = [
        "root ::= object",
        "",
        "ws ::= [ \\t\\n]*",
        'object ::= "{" ws kv-unit-id ws "," ws kv-concept-title ws "," ws kv-difficulty ws "," ws kv-challenges ws "}"',
        'kv-unit-id ::= "\\"unit_id\\"" ws ":" ws string',
        'kv-concept-title ::= "\\"concept_title\\"" ws ":" ws string',
        'kv-difficulty ::= "\\"difficulty_level\\"" ws ":" ws difficulty-int',
        'difficulty-int ::= "1" | "2" | "3" | "4" | "5"',
        'kv-challenges ::= "\\"challenges\\"" ws ":" ws "[" ws challenge ("," ws challenge)* "]"',
        "",
        'challenge ::= "{" ws kv-type ws "," ws kv-instructions ws ("," ws rest-field)* ws "}"',
        'kv-type ::= "\\"type\\"" ws ":" ws type-value',
        f"type-value ::= {type_alts}",
        'kv-instructions ::= "\\"instructions\\"" ws ":" ws string',
        "rest-field ::= json-key ws \":\" ws value",
        f"json-key ::= {key_alts}",
        "value ::= string | number | bool | null | object-g | array",
        "object-g ::= \"{\" ws (string ws \":\" ws value (\",\" ws string ws \":\" ws value)* )? \"}\"",
        'array ::= "[" ws (value ("," ws value)*)? "]"',
        'bool ::= "true" | "false"',
        'null ::= "null"',
        "",
        JSON_STRING_RULE,
        'number ::= "-"? ([0-9] | [1-9] [0-9]*) ("." [0-9]+)? ([eE] "-"? [0-9]+)?',
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    output = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUTPUT
    grammar = build_grammar()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(grammar, encoding="utf-8")
    # The real validation happens when llama-server compiles the grammar at
    # boot; a broken file aborts startup loudly there instead of silently
    # loosening the constraint here.
    print(f"GBNF grammar written to {output} ({len(grammar)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
