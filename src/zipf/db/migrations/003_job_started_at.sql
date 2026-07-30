-- When a job actually began, as distinct from when it was queued.
--
-- Without this, the only available duration is created_at to finished_at, which
-- silently includes queue wait. That is close enough while jobs run seconds
-- after being enqueued, and wrong once a standard-queue SERP task can sit
-- polling for fifteen minutes.

ALTER TABLE job ADD COLUMN started_at TEXT;
