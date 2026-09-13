-- CampusLingo schema (PostgreSQL 16 + pgvector)
-- Applied automatically by the postgres container on first boot
-- (mounted at /docker-entrypoint-initdb.d/01-schema.sql)
-- and idempotently re-applied by services/core-ai/database.py:init_db().

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "vector";

-- ---------------------------------------------------------------------------
-- users
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    email           TEXT UNIQUE NOT NULL,
    streak_count    INT NOT NULL DEFAULT 0,
    last_active_at  TIMESTAMPTZ,
    xp_points       BIGINT NOT NULL DEFAULT 0,
    gems            INT NOT NULL DEFAULT 500,
    league_tier     SMALLINT NOT NULL DEFAULT 0
);

-- ---------------------------------------------------------------------------
-- courses
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS courses (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    lms_type        TEXT NOT NULL DEFAULT 'manual',
    lms_course_id   TEXT,
    sync_token      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_courses_user ON courses (user_id);

-- ---------------------------------------------------------------------------
-- lectures
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS lectures (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    course_id       UUID NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    title           TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    total_chunks    INT NOT NULL DEFAULT 0,
    source_file     TEXT,
    error_message   TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_lectures_course ON lectures (course_id);

-- ---------------------------------------------------------------------------
-- learning_nodes
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS learning_nodes (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    lecture_id      UUID NOT NULL REFERENCES lectures(id) ON DELETE CASCADE,
    title           TEXT NOT NULL,
    content_summary TEXT NOT NULL,
    order_index     INT NOT NULL DEFAULT 0,
    embedding       vector(1536),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_nodes_lecture ON learning_nodes (lecture_id, order_index);
-- Cosine ANN index; the embedding column must be non-null for rows to be indexed.
CREATE INDEX IF NOT EXISTS idx_nodes_embedding ON learning_nodes
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

-- ---------------------------------------------------------------------------
-- challenges
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS challenges (
    id             UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    node_id        UUID NOT NULL REFERENCES learning_nodes(id) ON DELETE CASCADE,
    type           TEXT NOT NULL,
    payload_json   JSONB NOT NULL,
    difficulty     REAL NOT NULL DEFAULT 0.3,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT challenges_type_check CHECK (
        type IN ('MATCH_MADNESS', 'SPOT_MISTAKE', 'CONTEXTUAL_CLOZE', 'INTERACTIVE_STORY')
    )
);

CREATE INDEX IF NOT EXISTS idx_challenges_node ON challenges (node_id, type);

-- ---------------------------------------------------------------------------
-- fsrs_cards
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fsrs_cards (
    id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id       UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    challenge_id  UUID NOT NULL REFERENCES challenges(id) ON DELETE CASCADE,
    state         SMALLINT NOT NULL DEFAULT 1,
    stability     REAL NOT NULL DEFAULT 0,
    difficulty    REAL NOT NULL DEFAULT 0,
    due_date      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_review   TIMESTAMPTZ,
    reps          INT NOT NULL DEFAULT 0,
    lapses        INT NOT NULL DEFAULT 0,
    last_rating   SMALLINT,
    UNIQUE (user_id, challenge_id)
);

CREATE INDEX IF NOT EXISTS idx_fsrs_sched ON fsrs_cards (user_id, due_date);
