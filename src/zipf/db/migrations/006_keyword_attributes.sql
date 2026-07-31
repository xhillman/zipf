-- Two attributes that decide whether a keyword is worth writing about.
--
-- `difficulty` (0-100) is the only organic figure zipf stores. Volume, cpc and
-- competition all describe an advertising market: they say what a click is worth
-- to a bidder, never whether you could rank without paying.
--
-- `intent` says what the searcher wanted — informational, navigational,
-- commercial, or transactional. `intent_probability` (0-1) is kept alongside it
-- because the label alone hides the difference between a keyword classified with
-- 97% confidence and one at 51%, which is a coin toss wearing a label.
--
-- Columns on `keyword` rather than a table of their own: both are single current
-- values for a keyword, like volume, and neither accumulates history.
ALTER TABLE keyword ADD COLUMN difficulty INTEGER;
ALTER TABLE keyword ADD COLUMN intent TEXT;
ALTER TABLE keyword ADD COLUMN intent_probability REAL;
