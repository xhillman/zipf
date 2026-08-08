-- A watchlist is durable user intent, not a projection of vendor data.
--
-- Deliberately no foreign key to `keyword`: projections can be rebuilt, while a
-- keyword the user chose to watch must survive that maintenance.
CREATE TABLE watchlist (
  keyword   TEXT PRIMARY KEY,
  added_at  TEXT NOT NULL
);
