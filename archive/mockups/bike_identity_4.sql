-- ============================================================================
-- GearHeadSpecs — bike identity
--
-- Decision (Aug 2026): a bike is identified by a stable numeric id, never by
-- its name. Names are data, not identity — "CB900F2 919", "2005 Honda CB919"
-- and "Hornet 900" are three names for one machine.
--
--   bikes       — one bike, covering however many model years share its specs
--   bike_years  — the years that bike covers; display, and what a rider owns
--
-- Specs attach to the BIKE, never to a year. One bike has exactly one set of
-- specs. When a year genuinely differs, the bike is SPLIT into two bikes and
-- the years divide between them. There is no inheritance and no override.
--
-- Rejected: a second spec level with per-year overrides. It avoids duplicating
-- specs on a split, but it makes every spec read a two-level resolve and forces
-- managers to understand which level they are editing. A wrong click there
-- silently creates the duplication the design existed to prevent. Splitting is
-- a concept a mechanic already has.
--
-- The cost we accept: a split copies every spec, so two sibling bikes hold
-- near-identical sets, and a later correction applied to one and not the other
-- makes them disagree. `split_from_bike_id` exists so that divergence is
-- detectable rather than silent — see the note on that column.
--
-- SQLite. Run once against a copy before running against anything real.
-- ============================================================================

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- A bike: one spec set, one or more model years.
-- ---------------------------------------------------------------------------
CREATE TABLE bikes (
  id                  INTEGER PRIMARY KEY,
  make                TEXT    NOT NULL,
  model_code          TEXT    NOT NULL,  -- canonical internal code, not a display name
  year_start          INTEGER,           -- first model year covered
  year_end            INTEGER,           -- last model year covered, NULL if current
  bike_type           TEXT,              -- Street/Sport, Cruiser, Dirt, ...

  -- 0 = provisional. Seed data carries no boundaries, so a seeded bike's year
  -- span is a guess until a manager confirms it or splits it.
  years_verified      INTEGER NOT NULL DEFAULT 0
                        CHECK (years_verified IN (0,1)),

  -- Set when this bike was created by splitting another. Two bikes sharing a
  -- lineage started from one identical spec set, so a later edit to one of them
  -- is worth surfacing: "you changed valve clearance on the 2002-05 CB919;
  -- the 2006-07 CB919 still says the old value — intended?" Without this the
  -- two just drift apart and nothing notices.
  split_from_bike_id  INTEGER REFERENCES bikes(id) ON DELETE SET NULL,
  split_at_year       INTEGER,

  created_at          TEXT    NOT NULL DEFAULT (datetime('now')),
  UNIQUE (make, model_code, year_start)
);

CREATE INDEX idx_bikes_split_lineage ON bikes (split_from_bike_id);

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

CREATE UNIQUE INDEX idx_bike_names_one_primary
  ON bike_names (bike_id) WHERE is_primary = 1;

-- ---------------------------------------------------------------------------
-- The years a bike covers. Display ("2002-2007"), the Year filter, and the
-- thing a garage row points at — a rider owns an '05, not a year range.
-- Specs never attach here.
-- ---------------------------------------------------------------------------
CREATE TABLE bike_years (
  id       INTEGER PRIMARY KEY,
  bike_id  INTEGER NOT NULL REFERENCES bikes(id) ON DELETE CASCADE,
  year     INTEGER NOT NULL,
  market   TEXT,
  UNIQUE (bike_id, year, market)
);

CREATE INDEX idx_bike_years_bike ON bike_years (bike_id);

-- A given model-year belongs to exactly one bike. This is what makes a split
-- an actual move rather than a copy, and stops two bikes both claiming 2006.
CREATE UNIQUE INDEX idx_bike_years_no_overlap
  ON bike_years (bike_id, year);

-- ---------------------------------------------------------------------------
-- Specs. One row per bike per field. No levels, no NULLs with hidden meaning.
--
-- If you already have a specs table, this is the shape of the change:
--   ALTER TABLE specs ADD COLUMN bike_id INTEGER REFERENCES bikes(id);
-- ---------------------------------------------------------------------------
CREATE TABLE specs (
  id          INTEGER PRIMARY KEY,
  bike_id     INTEGER NOT NULL REFERENCES bikes(id) ON DELETE CASCADE,
  field_key   TEXT    NOT NULL,        -- Spec Tree field id, not a display label
  value       TEXT,                    -- NULL = known field, not yet sourced
  confidence  TEXT,                    -- confirmed | mfr | pending
  spec_type   TEXT,                    -- fixed | pref | community
  created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
  UNIQUE (bike_id, field_key)
);

-- ---------------------------------------------------------------------------
-- Reading specs for a model-year is now a plain join. No resolve, no fallback.
-- ---------------------------------------------------------------------------
CREATE VIEW year_specs AS
SELECT
  by.id      AS bike_year_id,
  by.year    AS year,
  b.id       AS bike_id,
  b.model_code,
  s.field_key,
  s.value,
  s.confidence,
  s.spec_type
FROM bike_years by
JOIN bikes b ON b.id = by.bike_id
JOIN specs s ON s.bike_id = b.id;

-- ---------------------------------------------------------------------------
-- Sibling divergence: bikes that came from the same split but no longer agree.
-- This is the safety net for the duplication we accepted. Run it in the admin
-- dashboard; each row is a spec worth a human glance.
-- ---------------------------------------------------------------------------
CREATE VIEW sibling_divergence AS
WITH lineage AS (
  SELECT id, COALESCE(split_from_bike_id, id) AS root FROM bikes
)
SELECT
  la.root                AS lineage_root,
  a.bike_id              AS bike_a,
  b.bike_id              AS bike_b,
  a.field_key,
  a.value                AS value_a,
  b.value                AS value_b
FROM specs a
JOIN lineage la ON la.id = a.bike_id
JOIN lineage lb ON lb.root = la.root AND lb.id <> la.id
JOIN specs b ON b.bike_id = lb.id AND b.field_key = a.field_key
WHERE a.bike_id < b.bike_id
  AND IFNULL(a.value,'') <> IFNULL(b.value,'');

-- ---------------------------------------------------------------------------
-- Acknowledging a divergence.
--
-- Most divergence is INTENDED — the wire colour really did change, that is why
-- the bike was split. An admin marks it reviewed and it leaves the queue.
--
-- The subtlety: the acknowledgement is keyed on the VALUES, not just the field.
-- Acknowledge "wire colour: Yellow/Red vs Yellow/Green" and that pair stops
-- nagging. But if someone later edits wire colour again, the values no longer
-- match the acknowledgement and the row comes back. Keying on the field alone
-- would hide every future change to that field forever — which is exactly the
-- silent rot this queue exists to catch.
-- ---------------------------------------------------------------------------
CREATE TABLE divergence_ack (
  id           INTEGER PRIMARY KEY,
  bike_a       INTEGER NOT NULL REFERENCES bikes(id) ON DELETE CASCADE,
  bike_b       INTEGER NOT NULL REFERENCES bikes(id) ON DELETE CASCADE,
  field_key    TEXT    NOT NULL,
  value_a      TEXT,
  value_b      TEXT,
  reviewed_by  TEXT,
  note         TEXT,
  reviewed_at  TEXT    NOT NULL DEFAULT (datetime('now')),
  UNIQUE (bike_a, bike_b, field_key, value_a, value_b)
);

-- The queue an admin actually works: divergence minus what has been reviewed
-- at these exact values.
CREATE VIEW divergence_queue AS
SELECT d.*
FROM sibling_divergence d
WHERE NOT EXISTS (
  SELECT 1 FROM divergence_ack a
  WHERE a.bike_a = d.bike_a
    AND a.bike_b = d.bike_b
    AND a.field_key = d.field_key
    AND IFNULL(a.value_a,'') = IFNULL(d.value_a,'')
    AND IFNULL(a.value_b,'') = IFNULL(d.value_b,'')
);

-- ---------------------------------------------------------------------------
-- Everything that references a bike references an ID, never a string.
-- ---------------------------------------------------------------------------
CREATE TABLE user_bikes (
  id            INTEGER PRIMARY KEY,
  user_id       INTEGER NOT NULL,
  bike_year_id  INTEGER NOT NULL REFERENCES bike_years(id),
  nickname      TEXT,
  mileage       INTEGER
);

CREATE INDEX idx_user_bikes_user ON user_bikes (user_id);
