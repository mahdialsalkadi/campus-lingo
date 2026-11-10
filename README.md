# CampusLingo

Duolingo-style micro-learning for university students: upload a lecture PDF, get a
gamified practice unit scheduled by FSRS-4.5.

```
PDF slides ──▶ PyMuPDF ──▶ page-aware chunks ──▶ Qwen GGUF (llama-server, JSON-Schema grammar)
                                                        │
                                                        ▼
   Next.js 14 UI  ◀── FastAPI ◀── PostgreSQL 16 + pgvector (courses, lectures,
   hearts / XP / streak / Mistakes Inbox         nodes, challenges, fsrs_cards)
        │
        └── POST /api/reviews ──▶ FSRSScheduler (DSR) ──▶ updated stability,
                                  difficulty and due_date in Postgres
```

Every challenge payload is validated before it is stored, so an unconstrained
model output can never reach the database or the UI.

---

## Architecture

### Ingestion → generation → persistence

1. **`POST /api/process-lecture`** receives a PDF plus a `course_id`.
   (The web app calls **`POST /api/process-lecture-async`** instead: the upload is
   validated and queued immediately, and progress is polled via
   `GET /api/ingest-jobs/{job_id}` — a 25-minute GPU batch no longer holds an
   HTTP request open, and the UI shows real per-chunk progress.)
2. `ingestion/parser.py` extracts text with PyMuPDF (`extract_pdf_content`),
   stripping running headers/footers, bare page numbers, inline citations and
   hyphenated line breaks, and emits per-page blocks with metadata headers
   (`=== PAGE 2/6 | chars=1180 | heading="…" | boilerplate=1 ===`).
3. `iterate_chunks` splits that into ordered learning chunks with sentence-level
   overlap.
4. For each chunk, `llm/generator.py:generate_learning_bundle()` makes **one**
   `/v1/chat/completions` call to `llama-server` (OpenAI-compatible API) constrained
   by `LearningUnitBundle.model_json_schema()` (refs inlined, discriminators pinned)
   via `response_format: {"type": "json_object"}` plus the GBNF `json_schema` field,
   under a language-locked academic system prompt (output language == source
   language). A rejected bundle is fed back as a repair turn; if the bundle path
   fails entirely, the fallback path generates one challenge per type in parallel,
   each with its own repair loop. Transport is a persistent `httpx.AsyncClient`
   pool, so multi-chunk fan-out reuses warm connections.
5. `database.py:save_pdf_lecture()` writes **one lecture per PDF**, 2-3 numbered
   learning nodes (`"3.1 Paging Mechanics"`), challenges (JSONB), one `fsrs_cards`
   row per challenge, and the node embeddings (`vector(1536)`) when the embedding
   endpoint is available.

### Review loop

`POST /api/reviews` accepts `{user_id, challenge_id, rating}` (1 Again, 2 Hard,
3 Good, 4 Easy), recomputes DSR values with `FSRSScheduler.update_card()`, writes
them via `record_card_review()`, awards XP/gems/streak/league, and returns the new
schedule (`stability`, `difficulty`, `interval_days`, `due_date`, `state`).

`GET /api/queue` returns due cards for a session; `GET /api/mistakes` is the
**Mistakes Inbox** — cards sitting in Learning/Relearning after at least one lapse.

### Challenge types

| Type | Payload | Interaction |
| --- | --- | --- |
| `MATCH_MADNESS` | `pairs[{term, definition}]`, `seconds` (60) | two columns, combo multiplier, shake on mismatch |
| `SPOT_MISTAKE` | `code_or_text`, `mistake_token`, `explanation` | clickable word tokens; wrong token costs a heart and vibrates |
| `CONTEXTUAL_CLOZE` | `sentence_with_blank`, `correct_answer`, `distractors` | word bank, instant evaluation + explanation |
| `MCQ_QUIZ` | `question`, `options[{text, correct}]`, `explanation` | multiple choice with instant feedback |
| `TRUE_FALSE` | `statement`, `answer`, `explanation` | judge the statement (legacy type, render-only) |
| `DIAGRAM_QA` | `image_url`, `diagram_ref`, `questions[{question, options}]` | zoomable slide figure + comprehension |

> The chat/dialogue `INTERACTIVE_STORY` type has been retired. Generation focuses
> on the four high-yield variants: `MCQ_QUIZ`, `SPOT_MISTAKE`,
> `CONTEXTUAL_CLOZE` and `MATCH_MADNESS`.

---

## Directory layout

```
campus-lingo/
├── docker-compose.yml              # PostgreSQL 16 + pgvector, Redis 7
├── .env.example                    # every environment variable, documented
├── README.md
├── packages/
│   └── database/
│       └── schema.sql              # full DDL; auto-applied by the postgres container
├── services/
│   └── core-ai/                    # FastAPI service
│       ├── requirements.txt        # runtime dependencies
│       ├── requirements-dev.txt    # + pytest, pgserver
│       ├── pytest.ini
│       ├── run_tests.sh            # one-command test runner
│       ├── database.py             # async engine, ORM models, DB helpers, DDL bootstrap
│       ├── schema.py               # Pydantic contracts (payloads, bundle, API models)
│       ├── main.py                 # FastAPI app and endpoints
│       ├── ingestion/parser.py     # PyMuPDF extraction, cleaning, chunking
│       ├── llm/generator.py        # llama-server client, prompts, schema-constrained decoding
│       ├── fsrs/scheduler.py       # FSRS-4.5 (DSR) + FSRSScheduler facade
│       └── tests/
│           ├── conftest.py         # path setup + synthetic lecture PDF fixtures
│           ├── test_core_contracts.py   # FSRS, payloads, JSON Schema, ingestion
│           ├── test_llm_bundle.py       # request shape, validation, repair loop
│           └── test_api_e2e.py          # real PostgreSQL + uvicorn end to end
└── apps/
    └── web/                        # Next.js 14 App Router client
        ├── app/page.tsx            # dashboard: HUD, upload, pathway map, Mistakes Inbox
        ├── app/play/[nodeId]/page.tsx  # session loop + end-of-session FSRS modal
        ├── components/             # the four challenge components
        ├── lib/api.ts              # typed client mirroring schema.py
        └── stores/quizStore.ts     # hearts, XP, streak, Web Audio SFX
```

---

## Quickstart

### 1. Database and cache

```bash
docker compose up -d          # PostgreSQL 16 + pgvector on :5432, Redis on :6379
```

`schema.sql` is mounted into `/docker-entrypoint-initdb.d/`, so a fresh volume gets
the full schema. The app also bootstraps idempotently at startup, so a database
created by hand works too.

### 2. LLM runtime (llama.cpp) — four selectable Qwen 3.5 tiers

Each tier of the Qwen 3.5 family runs as its own `llama-server` on a fixed
port, all hardware-tuned for the 14GB-VRAM BC-250. Pick any combination at
boot; the dashboard lets every upload choose which tier generates it, and the
backend routes the request to that tier's endpoint with the generic model id
`"default"` (swapping GGUFs never touches application code).

| Choice | Name | Port | GGUF (`models/`) | `-np` | Best for |
| --- | --- | --- | --- | --- | --- |
| `1` / `800m` | Qwen 3.5 0.8B | 8081 | `qwen3.5-0.8b-instruct-q4_k_m.gguf` | 6 | high concurrency, instant quizzes |
| `2` / `2b` | Qwen 3.5 2B | 8082 | `qwen3.5-2b-instruct-q4_k_m.gguf` | 4 | fast generation, lightweight |
| `3` / `4b` *(default)* | Qwen 3.5 4B (DeltaNet) | 8083 | `qwen3.5-4b-instruct-q4_k_m.gguf` | 2 | DeltaNet 3:1, code challenges & logic |
| `4` / `9b` | Qwen 3.5 9B | 8084 | `qwen3.5-9b-instruct-q4_k_m.gguf` | 1 | deep reasoning, heavy academic lectures |

```bash
./run.sh                      # interactive menu (10s timeout) -> 4B default
./run.sh --model 9b           # CLI: a single tier
./run.sh --model "1 3"        # CLI: several tiers at once
LLAMA_TIERS=all ./run.sh      # env: every tier
```

Every tier launches with the same BC-250 speed flags:

```bash
llama-server \
  -m models/qwen3.5-4b-instruct-q4_k_m.gguf \
  --host 0.0.0.0 --port 8083 \
  -c 4096 \
  -fa on \
  -ctk q4_0 -ctv q4_0 \
  -ngl 99 \
  -np 2 \
  -t 8 \
  --mlock \
  --embeddings --pooling mean
```

`--pooling mean` enables the `/v1/embeddings` endpoint (Qwen 3.5 ships without a
default pooling type). Structured output rides on the top-level `json_schema`
field as a JSON object, and thinking mode is disabled per request
(`chat_template_kwargs.enable_thinking=false`) so completion tokens reach the
JSON answer instead of `reasoning_content`. `-fa on` (Flash Attention) plus the 4-bit KV cache (`-ctk/-ctv q4_0`) cut the memory
bandwidth bottleneck; `-ngl 99` offloads every layer to the GPU; `-np` scales
with the tier (6 parallel slots for the 0.8B down to 1 for the 9B, which gets
the full VRAM budget for deep reasoning); `-c 4096` is fixed across tiers. The
selected tiers are exported as `SELECTED_MODEL_TIER`/`SELECTED_TIERS`, surfaced
per tier in `GET /health` (`llm.tiers`), and offered in the dashboard's upload
form. Tiers whose GGUF is missing are skipped with the expected download path;
if none remain, startup fails fast. Ports and GGUF paths are env-overridable
via `LLAMA_TIER1_PORT..LLAMA_TIER4_PORT` and `LLAMA_TIER1_MODEL..LLAMA_TIER4_MODEL`.
A remote GPU host still works: set `LLAMA_SERVER_URL` and every tier's client
pins to that endpoint.

### 3. Backend

```bash
cd services/core-ai
python -m venv .venv && ./.venv/bin/pip install -r requirements.txt
cp ../../.env.example ../../.env      # optional: edit and export
set -a; source ../../.env; set +a
./.venv/bin/uvicorn main:app --reload --port 8000
```

Check it: `curl localhost:8000/health` reports database, llama-server and FSRS status.

### 4. Web app

```bash
cd apps/web
npm install
npm run dev                  # http://localhost:3000
```

The client talks to `NEXT_PUBLIC_API_URL` (default `http://localhost:8000`).

### 5. Tests

```bash
cd services/core-ai
./run_tests.sh                    # everything, including the PostgreSQL E2E suite
./run_tests.sh -m "not e2e"       # fast: no database, no server process
./run_tests.sh tests/test_core_contracts.py -k fsrs
```

`run_tests.sh` creates `.venv` on first run and installs `requirements-dev.txt`
(pytest + the bundled `pgserver` PostgreSQL binaries, so the end-to-end suite runs
without Docker).

---

## Moving to the GPU rig (RTX 5070s)

The bundle path is schema-constrained, so a larger model mostly buys you *better*
challenges, not different plumbing. What changes on the GPU box:

```bash
# Download the Qwen 3.5 9B instruct GGUF (Q4_K_M is the quality/speed sweet spot)
# and let run.sh start its dedicated server:
LLAMA_TIERS=4 ./run.sh
# ...or point every tier at a remote box already serving llama.cpp:
export LLAMA_SERVER_URL=http://<gpu-host>:8084
```

Then point the service at it:

```bash
export LLAMA_SERVER_URL=http://<gpu-host>:8083    # any tier port, e.g. 4B
```

On CPU-only hardware a 9B model can exceed 300 s per structured request; the
GPU rig is where real generation quality gets validated. `-fa on` + 4-bit KV cache
+ `-ngl 99` are what unlock real token throughput.

---

## Environment variables

`.env.example` lists all of them with defaults. The service reads plain
environment variables — export the file (`set -a; source .env; set +a`) or pass
them through your process manager.

### Backend (`services/core-ai`)

| Variable | Default | Purpose |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql+asyncpg://postgres:password123@localhost:5432/campuslingo` | asyncpg DSN |
| `SQL_ECHO` | *(unset)* | log every SQL statement |
| `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` | `10` / `20` | connection pool sizing |
| `SELECTED_MODEL_TIER` | `4B` | exported by run.sh from the boot menu/CLI/env |
| `SELECTED_MODEL_PATH` | *(set by run.sh)* | GGUF path of the loaded tier |
| `LLAMA_SERVER_URL` | *(unset)* | pins every tier's client to one endpoint (e.g. a remote GPU host); unset = per-tier ports |
| `LLAMA_TIER1_PORT..LLAMA_TIER4_PORT` | `8081..8084` | per-tier llama-server ports (must match run.sh) |
| `LLAMA_TIER1_MODEL..LLAMA_TIER4_MODEL` | Qwen 3.5 GGUFs | per-tier model paths (must match run.sh) |
| `LLAMA_MODEL` | `default` | generic model id echoed in requests (GGUF-agnostic) |
| `MODEL_TIER` | *(unset)* | legacy alias of `LLAMA_TIERS` for run.sh (e.g. `4b`) |
| `MODEL_DIR` | `models` | central GGUF directory used by run.sh |
| `LLAMA_EMBED_MODEL` | *(empty)* | model field for `/v1/embeddings` (defaults to the chat model) |
| `EMBEDDING_DIM` | `1536` | must match `vector(1536)`; shorter vectors are zero-padded |
| `LLAMA_TIMEOUT` | `300` | seconds per generation request |
| `LLAMA_EMBED_TIMEOUT` | `120` | seconds per embedding request |
| `LLAMA_MAX_ATTEMPTS` | `3` | attempts per challenge/unit before it is dropped |
| `LLAMA_MAX_SOURCE_CHARS` | `7000` | lecture text sent per request |
| `LLAMA_TEMPERATURE` | `0.2` | sampling temperature (low for schema-faithful JSON) |
| `LLAMA_TOP_P` | `0.9` | nucleus sampling cutoff |
| `LLAMA_POOL_KEEPALIVE` / `LLAMA_POOL_MAX_CONNECTIONS` | `8` / `16` | httpx connection-pool sizing |
| `MAX_UPLOAD_BYTES` | `83886080` | PDF upload ceiling (80 MB) |
| `MAX_NODES_PER_LECTURE` | `3` | thematic nodes consolidated per upload (2-3) |
| `GENERATION_CONCURRENCY` | `2` | parallel chunk generations during upload |
| `CORS_ORIGINS` | `http://localhost:3000,http://127.0.0.1:3000` | comma-separated allow-list |
| `LOG_LEVEL` | `INFO` | root log level |
| `HOST` / `PORT` / `RELOAD` | `0.0.0.0` / `8000` / *(unset)* | `python -m main` dev entrypoint |

### Frontend (`apps/web`)

| Variable | Default | Purpose |
| --- | --- | --- |
| `NEXT_PUBLIC_API_URL` | `http://localhost:8000` | API base URL used by the browser |
| `BACKEND_ORIGIN` | `http://localhost:8000` | server-side target for the `/backend/*` rewrite proxy |

---

## API reference

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/health` | database, llama-server and FSRS status |
| `POST` | `/api/process-lecture` | multipart `file` + `course_id` → parse, generate (chunks in parallel), persist; returns the node and challenges |
| `POST` | `/api/process-lecture-async` | same pipeline as a background job; returns `202 {job_id}` immediately |
| `GET` | `/api/ingest-jobs/{job_id}` | poll a background ingestion job: status, `done_chunks/total_chunks`, per-chunk results |
| `POST` | `/api/reviews` | `{user_id, challenge_id, rating}` → DSR recalculation + rewards |
| `GET` | `/api/nodes/{node_id}/challenges` | all challenges of one learning node |
| `POST` | `/api/users` | upsert a user by email (idempotent) |
| `GET` | `/api/users/{user_id}/stats` | XP, gems, streak, league, due/relearning counts, accuracy |
| `POST` | `/api/users/{user_id}/hearts/refill` | spend 350 gems for 5 hearts |
| `POST` | `/api/users/{user_id}/gems/spend` | spend gems; `402` when the balance is short |
| `POST` | `/api/courses` | create a course for a user |
| `GET` | `/api/courses` | list a user's courses and lectures |
| `GET` | `/api/courses/{course_id}/nodes` | pathway map data (nodes + challenge counts) |
| `GET` | `/api/lectures/{lecture_id}` | lecture detail with its nodes |
| `DELETE` | `/api/lectures/{lecture_id}` | delete a lecture (cascades to nodes, challenges, cards) |
| `GET` | `/api/nodes/{node_id}` | node detail + sibling nodes for navigation |
| `GET` | `/api/queue` | due cards for a session (`only_due`, `limit`) |
| `GET` | `/api/mistakes` | Mistakes Inbox (lapsed cards in Learning/Relearning) |
| `GET` | `/api/fsrs/preview/{challenge_id}` | interval/state prediction for all four ratings |
| `POST` | `/api/review` | back-compat alias of `/api/reviews` |
| `POST` | `/api/grade` | map a raw attempt onto the 1–4 rating scale (no DB write) |
| `POST` | `/api/courses/{course_id}/lectures` | legacy single-shot ingest (per-type generator) |

Interactive docs: `http://localhost:8000/docs`.

---

## Database

`packages/database/schema.sql` is the source of truth: `users`, `courses`,
`lectures`, `learning_nodes` (with `embedding vector(1536)`), `challenges`
(`payload_json JSONB`), `fsrs_cards` (`UNIQUE (user_id, challenge_id)`,
`idx_fsrs_sched (user_id, due_date)`), extension `vector`.

`database.py:init_db()` re-applies the same DDL idempotently at startup
(`CREATE TABLE IF NOT EXISTS`, `ALTER TABLE … ADD COLUMN IF NOT EXISTS`), probing
optional extensions inside savepoints so a missing `uuid-ossp` degrades to
`gen_random_uuid()` instead of poisoning the transaction. `pgvector` is required —
the service fails fast with a clear message if it is absent.

### FSRS-4.5

19 default weights, `DECAY = -0.5`, `FACTOR = 19/81`, target retention 90%.
States: `1 new`, `2 learning`, `3 review`, `4 relearning`.

* First review: `S₀(G) = w[G-1]`, `D₀(G) = clamp(w[4] - (G-3)·w[5], 1, 10)`.
* Recall: stability grows with `D`, `S`, and `1-R`; Hard/Easy penalties applied.
* Lapse (rating 1): stability drops via the post-lapse term and the card is
  scheduled on the 10-minute relearning step.
* `interval_days` is a whole number of days; a lapse reports `0` and the endpoint
  schedules minutes instead.

---

## Behaviour notes

* **Hearts** live in the client store (5 max, one regenerated every 30 minutes,
  350 gems for an instant refill); XP, gems, streak and league are
  server-authoritative.
* **Mistakes Inbox** is derived, not a separate table: cards with `lapses > 0` in
  Learning/Relearning.
* **End-of-session rating** re-grades the session's last card. FSRS treats a
  re-grade as a normal scheduling decision (it recomputes from current state), so
  the four buttons are functional, not cosmetic.
* **Embeddings are optional.** Without `nomic-embed-text` the unit still persists;
  the failure is reported per chunk in `generation_errors`.
* **`payload_json` is validated twice** — once by the JSON Schema during decoding,
  once by Pydantic before insert. Business rules no JSON Schema can express (a
  mistake token that appears exactly once, one correct option per dialogue node,
  no duplicate challenge types per unit) are enforced by the second pass.
