-- Reword the append-only guards.
--
-- The original message read "raw_response is append-only (R2)". The invariant
-- name means nothing to whoever hit it, and the message named a rule without
-- naming a remedy. These messages are read by a person holding a shell prompt,
-- so they say what to do instead.

DROP TRIGGER IF EXISTS raw_response_no_update;
DROP TRIGGER IF EXISTS raw_response_no_delete;

CREATE TRIGGER raw_response_no_update BEFORE UPDATE ON raw_response
BEGIN
  SELECT RAISE(
    ABORT,
    'Stored responses cannot be edited. This data was paid for and is kept as the record. To correct a value derived from it, run: zipf db rebuild'
  );
END;

CREATE TRIGGER raw_response_no_delete BEFORE DELETE ON raw_response
BEGIN
  SELECT RAISE(
    ABORT,
    'Stored responses cannot be deleted. Nobody will sell you this data again, so it is kept permanently. Projection tables are safe to delete; rebuild restores them.'
  );
END;
