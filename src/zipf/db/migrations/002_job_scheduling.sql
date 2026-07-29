-- Retry scheduling for the job queue.
--
-- Without a scheduled retry time, a failed job returns to 'queued' and is
-- immediately re-claimed by the same drain loop, burning its three attempts in
-- microseconds and defeating the backoff entirely.

ALTER TABLE job ADD COLUMN next_attempt_at TEXT;

-- The claim query filters on status and readiness together.
DROP INDEX IF EXISTS idx_job_status;
CREATE INDEX idx_job_claimable ON job (status, next_attempt_at, id);
