# CampusLingo

Duolingo-style micro-learning for university students: upload a lecture PDF, get a
gamified practice unit scheduled by FSRS-4.5.

```
PDF slides ──▶ PyMuPDF ──▶ page-aware chunks ──▶ Qwen (Ollama, JSON-Schema decoding)
                                                        │
                                                        ▼
   Next.js 14 UI  ◀── FastAPI ◀── PostgreSQL 16 + pgvector (courses, nodes,
   hearts / XP / streak / Mistakes Inbox         challenges, fsrs_cards)
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
2. `ingestion/parser.py` extracts text with PyMuPDF (`extract_pdf_content`),
   stripping running headers/footers, bare page numbers, inline citations and
   hyphenated line breaks, and emits per-page blocks with metadata headers
   (`=== PAGE 2/6 | chars=1180 | heading="…" | boilerplate=1 ===`).
3. `iterate_chunks` splits that into ordered learning chunks with sentence-level
   overlap.
4. For each chunk, `llm/generator.py:generate_learning_bundle()` makes **one**
   `/api/generate` call to Ollama constrained by
   `LearningUnitBundle.model_json_schema()` (refs inlined, discriminators pinned),
   under an Arabic academic system prompt. A rejected bundle is fed back as a
   repair turn; if the bundle path fails entirely, the fallback path generates one
   challenge per type in parallel, each with its own repair loop.
5. `database.py:save_lecture_and_challenges()` writes the lecture, node,
   challenges (JSONB) and seeds one `fsrs_cards` row per challenge, plus the
   node embedding (`vector(1536)`) when an embedding model is available.

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
| `INTERACTIVE_STORY` | `scenario_title`, `dialogue[{speaker, text, action_prompt, options[{text, correct, feedback}]}]` | chat bubbles with branching, character reactions |

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
│       ├── llm/generator.py        # Ollama client, prompts, schema-constrained decoding
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

### 2. LLM runtime

```bash
ollama serve                 # http://localhost:11434
ollama pull qwen3.5:9b       # the generation model (see "Moving to the GPU rig")
ollama pull nomic-embed-text # embeddings for node vectors (optional but recommended)
```

### 3. Backend

```bash
cd services/core-ai
python -m venv .venv && ./.venv/bin/pip install -r requirements.txt
cp ../../.env.example ../../.env      # optional: edit and export
set -a; source ../../.env; set +a
./.venv/bin/uvicorn main:app --reload --port 8000
```

Check it: `curl localhost:8000/health` reports database, Ollama and FSRS status.

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
ollama pull qwen3.5:9b          # generation model (~6.6 GB)
ollama pull nomic-embed-text    # embeddings (~275 MB) — currently missing on CPU-only hosts
```

Then point the service at them:

```bash
export OLLAMA_MODEL=qwen3.5:9b          # per-type fallback generator
export OLLAMA_BUNDLE_MODEL=qwen3.5:9b   # primary bundle generator
export OLLAMA_HOST=http://<gpu-host>:11434   # if Ollama runs on another machine
```

On CPU-only hardware a 9B model can exceed 300 s per structured request; the
GPU rig is where real generation quality gets validated. `OLLAMA_THINK=false`
(default) keeps reasoning traces off, which matters most for latency.

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
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama base URL |
| `OLLAMA_MODEL` | `qwen2.5:7b-instruct` | model for the fallback per-type generator |
| `OLLAMA_BUNDLE_MODEL` | `qwen2.5:7b` | model for the primary bundle generator |
| `OLLAMA_EMBED_MODEL` | `nomic-embed-text` | embedding model for node vectors |
| `EMBEDDING_DIM` | `1536` | must match `vector(1536)`; shorter vectors are zero-padded |
| `OLLAMA_TIMEOUT` | `300` | seconds per generation request |
| `OLLAMA_EMBED_TIMEOUT` | `120` | seconds per embedding request |
| `OLLAMA_MAX_ATTEMPTS` | `3` | attempts per challenge/unit before it is dropped |
| `OLLAMA_MAX_SOURCE_CHARS` | `7000` | lecture text sent per request |
| `OLLAMA_THINK` | `false` | set `true` to allow reasoning traces |
| `MAX_UPLOAD_BYTES` | `83886080` | PDF upload ceiling (80 MB) |
| `MAX_NODES_PER_LECTURE` | `25` | chunks generated per upload |
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
| `GET` | `/health` | database, Ollama and FSRS status |
| `POST` | `/api/process-lecture` | multipart `file` + `course_id` → parse, generate, persist; returns the node and challenges |
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
