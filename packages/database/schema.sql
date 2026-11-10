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
    password_hash   TEXT,
    streak_count    INT NOT NULL DEFAULT 0,
    last_active_at  TIMESTAMPTZ,
    xp_points       BIGINT NOT NULL DEFAULT 0,
    gems            INT NOT NULL DEFAULT 500,
    league_tier     SMALLINT NOT NULL DEFAULT 0,
    weekly_xp       INT NOT NULL DEFAULT 0,
    streak_freeze_active BOOLEAN NOT NULL DEFAULT FALSE,
    inventory       JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- Google-Classroom roles: 'student' | 'instructor'.
    role            VARCHAR(16) NOT NULL DEFAULT 'student',
    -- $1 Pro Pass: zero ads, unlimited uploads, offline cheatsheets.
    is_pro          BOOLEAN NOT NULL DEFAULT FALSE,
    pro_expires_at  TIMESTAMPTZ,
    -- Creator royalty stash: gems earned when classmates clear shared material.
    creator_gems    INT NOT NULL DEFAULT 0
);

-- ---------------------------------------------------------------------------
-- app_meta: scheduler markers (weekly league rollover week key)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app_meta (
    key             VARCHAR(64) PRIMARY KEY,
    value           TEXT NOT NULL DEFAULT ''
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
    share_code      VARCHAR(8) UNIQUE,
    -- Discovery / marketplace metadata for shared courses.
    is_public       BOOLEAN NOT NULL DEFAULT FALSE,
    category        VARCHAR(50) NOT NULL DEFAULT 'academic', -- academic / language / general
    institution     VARCHAR(100),                            -- e.g. 'UTM'
    major           VARCHAR(100),                            -- e.g. 'AI'
    downloads_count INT NOT NULL DEFAULT 0,                  -- popularity counter
    -- Google Classroom mode: instructor-owned, joined via 6-char code.
    is_classroom    BOOLEAN NOT NULL DEFAULT FALSE,
    enrollment_code VARCHAR(6) UNIQUE,                       -- e.g. 'CS301X'
    instructor_id   UUID REFERENCES users(id) ON DELETE SET NULL,
    -- Classroom metadata (section/term + free-form description).
    section         VARCHAR(120),
    description     TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_courses_classroom ON courses (is_classroom);
CREATE INDEX IF NOT EXISTS idx_courses_instructor ON courses (instructor_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_courses_enrollment_code ON courses (enrollment_code);

-- ---------------------------------------------------------------------------
-- enrollments: a student's membership in an instructor classroom
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS enrollments (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    course_id   UUID NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    student_id  UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (course_id, student_id)
);

CREATE INDEX IF NOT EXISTS idx_enrollments_course ON enrollments (course_id);
CREATE INDEX IF NOT EXISTS idx_enrollments_student ON enrollments (student_id);

-- ---------------------------------------------------------------------------
-- notifications: in-app inbox (the durable channel behind Web Push)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS notifications (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    course_id   UUID REFERENCES courses(id) ON DELETE CASCADE,
    kind        VARCHAR(32) NOT NULL DEFAULT 'classroom',
    title       TEXT NOT NULL DEFAULT '',
    body        TEXT NOT NULL DEFAULT '',
    url         TEXT,
    push_status VARCHAR(64) NOT NULL DEFAULT 'recorded',
    read_at     TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications (user_id, created_at DESC);

-- ---------------------------------------------------------------------------
-- push_subscriptions: Web Push endpoints (delivery needs pywebpush + VAPID)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS push_subscriptions (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    endpoint    TEXT UNIQUE NOT NULL,
    keys        JSONB NOT NULL DEFAULT '{}'::jsonb,
    user_agent  TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_push_subscriptions_user ON push_subscriptions (user_id);

-- ---------------------------------------------------------------------------
-- exam_sessions: Exam Crunch diagnostic mock exams
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS exam_sessions (
    id               UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id          UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    course_id        UUID NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    status           VARCHAR(16) NOT NULL DEFAULT 'active',
    duration_seconds INT NOT NULL DEFAULT 2700,
    questions        JSONB NOT NULL DEFAULT '[]'::jsonb,
    answers          JSONB NOT NULL DEFAULT '{}'::jsonb,
    cheatsheet       TEXT NOT NULL DEFAULT '',
    score            INT,
    total            INT NOT NULL DEFAULT 0,
    grade            VARCHAR(2),
    readiness        REAL,
    started_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at      TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_exam_sessions_user ON exam_sessions (user_id, started_at DESC);

CREATE INDEX IF NOT EXISTS idx_courses_user ON courses (user_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_courses_share_code ON courses (share_code);
CREATE INDEX IF NOT EXISTS idx_courses_public ON courses (is_public);
CREATE INDEX IF NOT EXISTS idx_courses_institution ON courses (institution);
CREATE INDEX IF NOT EXISTS idx_courses_major ON courses (major);

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
    -- Extracted slide assets, e.g. {"diagram": "/static/uploads/diagrams/<c>/p.png"}
    assets         JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT challenges_type_check CHECK (
        type IN (
            'MATCH_MADNESS', 'SPOT_MISTAKE', 'CONTEXTUAL_CLOZE', 'INTERACTIVE_STORY',
            'MCQ_QUIZ', 'TRUE_FALSE', 'DIAGRAM_QA', 'SWIPE_BOOLEAN', 'SPOT_IMPOSTER',
            'TAP_TO_FILL', 'BOSS_FIGHT'
        )
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
