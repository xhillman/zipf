-- Zipf initial schema. See dev/spec.md §4.
--
-- Layering: raw_response is the paid source of truth. Everything below it is a
-- projection that can be dropped and rebuilt for free.

-- ---------------------------------------------------------------------------
-- Source of truth. Append-only. Never updated, never pruned. (R2, R4)
-- ---------------------------------------------------------------------------
CREATE TABLE raw_response (
  id           INTEGER PRIMARY KEY,
  capability   TEXT    NOT NULL,   -- 'labs.ranked_keywords', 'serp.organic', ...
  params_hash  TEXT    NOT NULL,   -- sha256 of normalised params
  params_json  TEXT    NOT NULL,
  body         BLOB    NOT NULL,   -- untouched vendor response
  cost_usd     REAL    NOT NULL,
  fetched_at   TEXT    NOT NULL    -- ISO-8601 UTC, 'Z' suffix
);

CREATE INDEX idx_raw_lookup ON raw_response (capability, params_hash, fetched_at DESC);

-- Budget is derived by summing this column over a month. See spec §6.
CREATE INDEX idx_raw_fetched ON raw_response (fetched_at);

-- Append-only enforced by the storage engine rather than by convention, so it
-- holds against every writer including a stray sqlite3 shell session.
CREATE TRIGGER raw_response_no_update BEFORE UPDATE ON raw_response
BEGIN
  SELECT RAISE(ABORT, 'raw_response is append-only (R2)');
END;

CREATE TRIGGER raw_response_no_delete BEFORE DELETE ON raw_response
BEGIN
  SELECT RAISE(ABORT, 'raw_response is append-only (R2)');
END;

-- ---------------------------------------------------------------------------
-- The differentiator: classic rank and model visibility in one shape. (G2)
-- ---------------------------------------------------------------------------
CREATE TABLE observation (
  id             INTEGER PRIMARY KEY,
  subject        TEXT    NOT NULL,   -- keyword | domain | url
  subject_type   TEXT    NOT NULL,
  surface        TEXT    NOT NULL,   -- google_organic | google_aio | anthropic | openai | gemini
  position       INTEGER,            -- integer rank at a moment, where the surface has one
  mentioned      INTEGER,            -- 0/1, where the surface is generative
  source_url     TEXT,               -- cited or ranking URL
  prompt_version TEXT,               -- LLM surfaces only; keeps a series comparable (D4)
  observed_at    TEXT    NOT NULL,
  raw_id         INTEGER NOT NULL REFERENCES raw_response(id)
);

CREATE INDEX idx_obs ON observation (subject, surface, observed_at DESC);

-- ---------------------------------------------------------------------------
-- Projections. Dropped and rebuilt at will. Never hand-written. (R3)
-- ---------------------------------------------------------------------------
CREATE TABLE keyword (
  keyword      TEXT PRIMARY KEY,
  volume       INTEGER,
  cpc          REAL,
  competition  REAL,
  has_aio      INTEGER,
  updated_at   TEXT,
  raw_id       INTEGER REFERENCES raw_response(id)
);

CREATE TABLE domain_keyword (
  domain       TEXT    NOT NULL,
  keyword      TEXT    NOT NULL,
  position     INTEGER,
  url          TEXT,
  observed_at  TEXT    NOT NULL,
  raw_id       INTEGER REFERENCES raw_response(id),
  PRIMARY KEY (domain, keyword, observed_at)
);

-- Search Console keeps its own table because its position is averaged over a
-- period, not an integer rank at a moment. Mixing the two units into
-- observation.position would make that column mean two things. (D3)
CREATE TABLE gsc_query (
  query        TEXT    NOT NULL,
  page         TEXT    NOT NULL,
  date         TEXT    NOT NULL,
  clicks       INTEGER NOT NULL,
  impressions  INTEGER NOT NULL,
  ctr          REAL    NOT NULL,
  position     REAL    NOT NULL,   -- averaged, NOT a rank
  raw_id       INTEGER NOT NULL REFERENCES raw_response(id),
  PRIMARY KEY (query, page, date)
);

CREATE INDEX idx_gsc_query ON gsc_query (query, date DESC);

-- ---------------------------------------------------------------------------
-- Async work. No request spends money synchronously. (R5)
-- ---------------------------------------------------------------------------
CREATE TABLE job (
  id             INTEGER PRIMARY KEY,
  capability     TEXT    NOT NULL,
  params_json    TEXT    NOT NULL,
  estimated_cost REAL,
  actual_cost    REAL,               -- kept alongside the estimate to measure drift
  status         TEXT    NOT NULL,   -- queued | running | done | failed | cancelled
  attempts       INTEGER NOT NULL DEFAULT 0,
  error          TEXT,
  vendor_task_id TEXT,               -- set once a standard-queue task is submitted
  raw_id         INTEGER REFERENCES raw_response(id),
  created_at     TEXT    NOT NULL,
  finished_at    TEXT
);

CREATE INDEX idx_job_status ON job (status, id);
