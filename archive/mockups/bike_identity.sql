-- ============================================================================
-- GearHeadSpecs — bike identity, two levels
--
-- Decision (Aug 2026): a bike is identified by a stable numeric id, never by
-- its name. Names are data, not identity — "CB900F2 919", "2005 Honda CB919"
-- and "Hornet 900" are three names for one machine.
--
-- Two levels:
--   bikes       — a model generation (CB900F2 919, 2002-2007)
--   bike_years  — one concrete model-year inside that generation
--
-- A spec attaches at the level where it is actually constant. Most attach to
-- the generation. A spec that changed mid-generation (starter switch wire
-- colour on later production runs; the FXRS single front disc) attaches to
-- the specific year and overrides the generation value for that year only.
--
-- SQLite. Run once against a copy before running against anything real.
-- ============================================================================

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- Level 1: the generation
-- ---------------------------------------------------------------------------
CREATE TABLE bikes (
  id                INTEGER PRIMARY KEY,
  make              TEXT    NOT NULL,
  model_code        TEXT    NOT NULL,   -- canonical internal code, not a display name
  generation_start  INTEGER,            -- first model year, NULL if unknown
  generation_end    INTEGER,            -- last model year, NULL if still current
  bike_type         TEXT,               -- Street/Sport, Cruiser, Dirt, ...
  created_at        TEXT    NOT NULL DEFAULT (datetime('now')),
  UNIQUE (make, model_code, generation_start)
);

-- ---------------------------------------------------------------------------
-- Names. One bike, many names. This is what fixes the dashboard calling it
-- "2005 Honda CB919" while the browser calls it "CB900F2 919".
-- Search hits this table; display reads is_primary.
-- ---------------------------------------------------------------------------
CREATE TABLE bike_names (
  id          INTEGER PRIMARY KEY,
  bike_id     INTEGER NOT NULL REFERENCES bikes(id) ON DELETE CASCADE,
  name        TEXT    NOT NULL,
  market      TEXT,                     -- 'US', 'EU', ... NULL = everywhere
  is_primary  INTEGER NOT NULL DEFAULT 0 CHECK (is_primary IN (0,1)),
  UNIQUE (bike_id, name, market)
);

CREATE INDEX idx_bike_names_name ON bike_names (name);

-- Exactly one primary name per bike.
CREATE UNIQUE INDEX idx_bike_names_one_primary
  ON bike_names (bike_id) WHERE is_primary = 1;

-- ---------------------------------------------------------------------------
-- Level 2: the model-year. This is what a rider owns and what a garage row
-- points at — you own an '05, not a generation.
-- ---------------------------------------------------------------------------
CREATE TABLE bike_years (
  id       INTEGER PRIMARY KEY,
  bike_id  INTEGER NOT NULL REFERENCES bikes(id) ON DELETE CASCADE,
  year     INTEGER NOT NULL,
  market   TEXT,                        -- promote to its own level only if this hurts
  UNIQUE (bike_id, year, market)
);

CREATE INDEX idx_bike_years_bike ON bike_years (bike_id);

-- ---------------------------------------------------------------------------
-- Specs. bike_id is ALWAYS set. bike_year_id is NULL for a generation-wide
-- value, and set only when that year genuinely differs.
--
-- If you already have a specs table, this is the shape of the change:
--   ALTER TABLE specs ADD COLUMN bike_id      INTEGER REFERENCES bikes(id);
--   ALTER TABLE specs ADD COLUMN bike_year_id INTEGER REFERENCES bike_years(id);
-- ---------------------------------------------------------------------------
CREATE TABLE specs (
  id            INTEGER PRIMARY KEY,
  bike_id       INTEGER NOT NULL REFERENCES bikes(id)      ON DELETE CASCADE,
  bike_year_id  INTEGER          REFERENCES bike_years(id) ON DELETE CASCADE,
  field_key     TEXT    NOT NULL,       -- Spec Tree field id, not a display label
  value         TEXT,                   -- NULL = known field, not yet sourced
  confidence    TEXT,                   -- confirmed | mfr | pending
  spec_type     TEXT,                   -- fixed | pref | community
  created_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- One generation-level value per field...
CREATE UNIQUE INDEX idx_specs_gen_unique
  ON specs (bike_id, field_key) WHERE bike_year_id IS NULL;

-- ...and at most one override per field per year.
CREATE UNIQUE INDEX idx_specs_year_unique
  ON specs (bike_year_id, field_key) WHERE bike_year_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- The resolve. For any model-year: use the year override if one exists,
-- otherwise the generation value. `level` tells the UI which one it got,
-- which is what a manager needs to see before editing.
-- ---------------------------------------------------------------------------
CREATE VIEW resolved_specs AS
SELECT
  by.id      AS bike_year_id,
  by.bike_id AS bike_id,
  by.year    AS year,
  s.id       AS spec_id,
  s.field_key,
  s.value,
  s.confidence,
  s.spec_type,
  CASE WHEN s.bike_year_id IS NULL THEN 'generation' ELSE 'year' END AS level
FROM bike_years by
JOIN specs s
  ON  s.bike_id = by.bike_id
  AND (s.bike_year_id IS NULL OR s.bike_year_id = by.id)
WHERE s.bike_year_id IS NOT NULL
   OR NOT EXISTS (
        SELECT 1 FROM specs ovr
        WHERE ovr.bike_year_id = by.id
          AND ovr.field_key    = s.field_key
      );

-- ---------------------------------------------------------------------------
-- Everything that references a bike references an ID, never a string.
-- Garage rows point at bike_years: a rider owns one machine of one year.
-- ---------------------------------------------------------------------------
CREATE TABLE user_bikes (
  id            INTEGER PRIMARY KEY,
  user_id       INTEGER NOT NULL,
  bike_year_id  INTEGER NOT NULL REFERENCES bike_years(id),
  nickname      TEXT,
  mileage       INTEGER
);

CREATE INDEX idx_user_bikes_user ON user_bikes (user_id);
