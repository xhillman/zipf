-- Narrow the delete guard to the data that genuinely cannot be replaced.
--
-- The original rule was "nothing is ever deleted", written when everything
-- stored had been paid for. That stopped being true of every row. The vendor
-- balance lookup is free, returns a price list for every DataForSEO endpoint to
-- report one number, and can be re-fetched on demand at no cost: ten of them
-- held 21% of this database.
--
-- What must never be deleted is data that cost money. Nobody will sell it back
-- at the price already paid, and from the SERP milestone onward a point-in-time
-- observation cannot be re-bought at any price. That is the invariant worth
-- enforcing in the storage engine, where it holds against every writer including
-- a stray sqlite3 session.
--
-- Free rows are only *permitted* to be deleted by this trigger. Which ones
-- actually are is decided by `zipf db prune`, which additionally refuses to
-- touch any capability a projection reads, and always keeps the newest response
-- to each distinct question so a cached answer never disappears.
DROP TRIGGER IF EXISTS raw_response_no_delete;

CREATE TRIGGER raw_response_no_delete BEFORE DELETE ON raw_response
WHEN OLD.cost_usd > 0
BEGIN
  SELECT RAISE(
    ABORT,
    'Stored responses that cost money cannot be deleted. Nobody will sell you this data again at the price already paid. Projection tables are safe to delete; rebuild restores them.'
  );
END;
