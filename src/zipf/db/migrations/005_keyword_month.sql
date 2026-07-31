-- Monthly search volume history.
--
-- `keyword.volume` holds one number, which cannot say that a number is a peak.
-- A keyword at 1,500,000 might sit there all year or spike once in May, and the
-- difference decides whether writing about it in July is worth doing.
--
-- Its own table rather than columns on `keyword`: twelve columns would be a
-- fixed window that breaks the first time a vendor returns a different span,
-- and `observation` is the wrong home because that table records positions and
-- mentions at a moment, not a measured quantity over a month.
--
-- A projection like any other: dropped and rebuilt from stored bytes. (R3)
CREATE TABLE keyword_month (
  keyword      TEXT    NOT NULL,
  year         INTEGER NOT NULL,
  month        INTEGER NOT NULL,   -- 1-12
  volume       INTEGER NOT NULL,
  raw_id       INTEGER REFERENCES raw_response(id),
  PRIMARY KEY (keyword, year, month)
);

-- Reading a series is always "this keyword, in time order".
CREATE INDEX idx_keyword_month ON keyword_month (keyword, year, month);
