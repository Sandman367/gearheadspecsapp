-- ============================================================================
-- GearHeadSpecs — full platform schema
--
-- Builds on the Aug 2026 identity decision (archive/bike_identity_4.sql):
-- a bike is a stable numeric id. Names are data. Specs attach to the BIKE,
-- never to a year. A year that genuinely differs means a SPLIT, not an
-- override.
--
-- Everything else here — users, flags, proposals, tools/links, garages —
-- hangs off that identity and references bike ids, never strings.
--
-- Supersedes archive/schema_v1_RETIRED.sql, which keyed specs to a flat
-- bikes(make, model, year) row and had no way to say "these six model years
-- are one machine".
--
-- SQLite. seed.py drops and rebuilds data.db from this file.
-- ============================================================================

PRAGMA foreign_keys = ON;


-- ===========================================================================
-- SECTION 1 — IDENTITY
-- ===========================================================================

-- A bike: one spec set, one or more model years.
CREATE TABLE bikes (
  id                  INTEGER PRIMARY KEY,
  make                TEXT    NOT NULL,
  model_code          TEXT    NOT NULL,   -- canonical internal code, not a display name
  year_start          INTEGER,
  year_end            INTEGER,            -- NULL = still current
  bike_type           TEXT,

  -- 0 = provisional. Seed data carries no boundaries, so a seeded bike's year
  -- span is a guess until a manager confirms it or splits it.
  years_verified      INTEGER NOT NULL DEFAULT 0 CHECK (years_verified IN (0,1)),

  -- Set when this bike was created by splitting another. Two bikes sharing a
  -- lineage started from one identical spec set, so a later edit to one of
  -- them is worth surfacing rather than letting them drift apart silently.
  split_from_bike_id  INTEGER REFERENCES bikes(id) ON DELETE SET NULL,
  split_at_year       INTEGER,

  created_at          TEXT    NOT NULL DEFAULT (datetime('now')),
  UNIQUE (make, model_code, year_start)
);

CREATE INDEX idx_bikes_split_lineage ON bikes (split_from_bike_id);
CREATE INDEX idx_bikes_make_model    ON bikes (make, model_code);

-- Names. One bike, many names. This is what stops the dashboard calling it
-- "2005 Honda CB919" while the browser calls it "CB900F2 919".
-- Search hits this table; display reads is_primary.
--
-- market is NOT NULL DEFAULT '' rather than nullable: SQLite treats NULLs as
-- distinct in a UNIQUE constraint, so a nullable market would have let the
-- same name be inserted for the same bike without limit.
CREATE TABLE bike_names (
  id          INTEGER PRIMARY KEY,
  bike_id     INTEGER NOT NULL REFERENCES bikes(id) ON DELETE CASCADE,
  name        TEXT    NOT NULL,
  market      TEXT    NOT NULL DEFAULT '',   -- 'US', 'EU', ... '' = everywhere
  is_primary  INTEGER NOT NULL DEFAULT 0 CHECK (is_primary IN (0,1)),
  UNIQUE (bike_id, name, market)
);

CREATE INDEX idx_bike_names_name ON bike_names (name);
CREATE UNIQUE INDEX idx_bike_names_one_primary ON bike_names (bike_id) WHERE is_primary = 1;

-- The years a bike covers. Display ("2002-2007"), the Year filter, and the
-- thing a garage row points at — a rider owns an '05, not a year range.
-- Specs never attach here.
--
-- in_v2 records catalog provenance: the original 991-row Honda catalog vs the
-- 139 rows that survived into the v2 browse page. Kept so the difference is
-- reviewable in the admin queue instead of being decided by whichever file
-- happened to get imported. See the catalog_v2_dropped view.
CREATE TABLE bike_years (
  id       INTEGER PRIMARY KEY,
  bike_id  INTEGER NOT NULL REFERENCES bikes(id) ON DELETE CASCADE,
  year     INTEGER NOT NULL,
  market   TEXT    NOT NULL DEFAULT '',
  in_v2    INTEGER NOT NULL DEFAULT 0 CHECK (in_v2 IN (0,1)),
  UNIQUE (bike_id, year, market)
);

CREATE INDEX idx_bike_years_bike ON bike_years (bike_id);
CREATE INDEX idx_bike_years_year ON bike_years (year);

-- "A given model-year belongs to exactly one bike" — the property that makes a
-- split an actual move rather than a copy.
--
-- A unique index on bike_years alone cannot express this: the thing that must
-- not collide is (make, model_code, year), and make/model_code live on bikes.
-- A unique index on (bike_id, year) only stops one bike listing 2006 twice; it
-- happily lets two sibling bikes both claim 2006, which is exactly the case
-- that matters after a split. Hence a trigger.
CREATE TRIGGER trg_bike_years_no_overlap_ins
BEFORE INSERT ON bike_years
FOR EACH ROW
WHEN EXISTS (
  SELECT 1
  FROM bike_years y
  JOIN bikes b  ON b.id = y.bike_id
  JOIN bikes nb ON nb.id = NEW.bike_id
  WHERE y.year = NEW.year
    AND y.market = NEW.market
    AND y.bike_id <> NEW.bike_id
    AND b.make = nb.make
    AND b.model_code = nb.model_code
)
BEGIN
  SELECT RAISE(ABORT, 'model-year already claimed by a sibling bike');
END;

-- The row being moved is excluded (y.id <> OLD.id): before the update it still
-- sits on the old bike with the same year, and without the exclusion it would
-- collide with itself and no model year could ever change hands -- which is
-- the one thing a split has to do.
CREATE TRIGGER trg_bike_years_no_overlap_upd
BEFORE UPDATE OF bike_id, year, market ON bike_years
FOR EACH ROW
WHEN EXISTS (
  SELECT 1
  FROM bike_years y
  JOIN bikes b  ON b.id = y.bike_id
  JOIN bikes nb ON nb.id = NEW.bike_id
  WHERE y.year = NEW.year
    AND y.market = NEW.market
    AND y.bike_id <> NEW.bike_id
    AND y.id <> OLD.id
    AND b.make = nb.make
    AND b.model_code = nb.model_code
)
BEGIN
  SELECT RAISE(ABORT, 'model-year already claimed by a sibling bike');
END;


-- ===========================================================================
-- SECTION 2 — USERS, AUTH, MANAGERS
-- ===========================================================================

-- role: user    — browse, vote, flag, fill gaps, keep a garage
--       manager — additionally owns the flag queue for bikes assigned to them
--       admin   — additionally owns tree flags, proposals, user flags, splits
--
-- There is deliberately no stored role for an anonymous visitor. "Public" in
-- this codebase means somebody who is not signed in, and that is the absence
-- of a session rather than a row here — so it cannot be granted, and a
-- permission check never has to ask whether a stored 'public' means "signed in
-- with no privileges" or "not signed in at all". That ambiguity is why the
-- signed-in role is called 'user'.
CREATE TABLE users (
  id            INTEGER PRIMARY KEY,
  username      TEXT    NOT NULL UNIQUE,
  display_name  TEXT,
  role          TEXT    NOT NULL DEFAULT 'user'
                  CHECK (role IN ('user','manager','admin')),
  password_hash TEXT    NOT NULL,
  password_salt TEXT    NOT NULL,
  -- Asked for at registration and shown to admin only: it never appears on a
  -- profile, in /api/auth/me, or anywhere a reader can reach. NULL on the
  -- accounts that predate the column.
  email         TEXT,
  suspended     INTEGER NOT NULL DEFAULT 0 CHECK (suspended IN (0,1)),
  created_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX idx_users_email ON users (email COLLATE NOCASE) WHERE email IS NOT NULL;

CREATE TABLE sessions (
  token      TEXT PRIMARY KEY,
  user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  expires_at TEXT NOT NULL
);

CREATE INDEX idx_sessions_user ON sessions (user_id);

-- Which manager is responsible for which bike. This is what makes a value flag
-- routable: it goes to the person closest to the machine, not to a global pile.
CREATE TABLE bike_managers (
  id         INTEGER PRIMARY KEY,
  user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  bike_id    INTEGER NOT NULL REFERENCES bikes(id) ON DELETE CASCADE,
  specialty  TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE (user_id, bike_id)
);

CREATE INDEX idx_bike_managers_bike ON bike_managers (bike_id);
CREATE INDEX idx_bike_managers_user ON bike_managers (user_id);


-- Specs lifted into a bike's hero, on top of the General category that every
-- bike shows there. Which numbers matter at a glance is a judgement about a
-- particular machine -- a dirt bike is not read the way a tourer is -- so it
-- is stored per bike, for the same reason a Fixed tag is. Moving a field into
-- General would change the hero for all 261 bikes, and that is admin's call
-- on the Spec Tree, not something a manager does from one bike's page.
CREATE TABLE bike_header_specs (
  bike_id    INTEGER NOT NULL REFERENCES bikes(id) ON DELETE CASCADE,
  field_key  TEXT    NOT NULL REFERENCES spec_fields(field_key) ON DELETE CASCADE,
  sort_order INTEGER NOT NULL DEFAULT 0,

  -- Whether the spec still appears in its own section further down. By default
  -- it does, and the header is a summary: the quicklist carries no vote, flag
  -- or request controls, so a spec that ONLY appears up there loses them. Some
  -- specs earn the swap anyway -- a headline number nobody argues about reads
  -- better once, at the top -- so it is the manager's call rather than a rule.
  hide_below INTEGER NOT NULL DEFAULT 0 CHECK (hide_below IN (0,1)),

  -- The other direction. General fields are in every bike's header by Spec
  -- Tree rule; this row says "not on THIS bike". The field stays in its
  -- section, only the header entry goes. Pinning adds a non-General field to
  -- the header; hiding removes a General one -- the same table, because both
  -- are one bike's manager overriding the site-wide default for one field.
  hidden     INTEGER NOT NULL DEFAULT 0 CHECK (hidden IN (0,1)),

  pinned_by  INTEGER REFERENCES users(id),
  pinned_at  TEXT    NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (bike_id, field_key)
);

CREATE INDEX idx_bike_header_specs_bike ON bike_header_specs (bike_id);


-- ===========================================================================
-- SECTION 3 — THE SPEC TREE
-- ===========================================================================

-- The field registry. specs.field_key points here, so a field has exactly one
-- display label and category platform-wide — renaming it is one UPDATE, not a
-- sweep across every bike's rows.
--
-- spec_type:  fixed     — one correct value, alternates make no sense
--             pref      — manual gives a baseline, riders legitimately vary
--             community — no manufacturer value exists, inherently open
CREATE TABLE spec_fields (
  field_key        TEXT PRIMARY KEY,
  label            TEXT    NOT NULL,
  category         TEXT    NOT NULL,
  -- 'fixed' is opt-in, not the default. It refuses alternates, so making it
  -- the default silently closed community contribution on nearly every field
  -- and made the tag meaningless — 111 of 120 carried it without anyone
  -- deciding. 'pref' permits alternates while still implying a factory value
  -- exists; an admin marks a field 'fixed' when one answer really is the only
  -- right one.
  spec_type        TEXT    NOT NULL DEFAULT 'pref'
                     CHECK (spec_type IN ('fixed','pref','community')),

  -- What kind of thing the value is, which decides how it is entered and shown.
  -- 'text' is anything typed. 'wire_color' is a colour pair from a fixed list --
  -- stored canonically as colour keys ("yellow/red") and displayed in the
  -- abbreviation the bike's own manufacturer uses, so a Honda owner reads Y/R
  -- and a Kawasaki owner reads Y/R against their own diagram. Free text would
  -- give "yellow w/ red", "Yel/Red" and "YR" for one wire.
  -- 'fuel_octane' is a pump grade from a closed list, stored as "AKI:91" or
  -- "RON:95" -- the grade the manual named, in the system the manual used --
  -- with the other region's equivalent computed on read. 'ethanol' is the
  -- fuel system's ethanol ceiling, E0 to E15. Both are closed sets for the
  -- same reason wire colours are: free text gives "91 or better", "91+",
  -- "premium" and "95 RON" for one fact.
  value_type       TEXT    NOT NULL DEFAULT 'text'
                     CHECK (value_type IN ('text','wire_color','fuel_octane','ethanol')),
  sort_order       INTEGER NOT NULL DEFAULT 0,

  -- 1 = belongs on every bike, whatever the questionnaire says. For specs that
  -- are not conditional on anything — road trip tools, say. A flag rather than
  -- a trigger row per bike, so a bike created tomorrow is covered without
  -- anyone remembering to go back and add it.
  universal        INTEGER NOT NULL DEFAULT 0 CHECK (universal IN (0,1)),

  -- Set when the field entered the tree via an approved branch proposal.
  from_proposal_id INTEGER,
  created_at       TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_spec_fields_category ON spec_fields (category, sort_order);

-- Which questionnaire answer brings a field into a bike's tree.
--
-- data/questionnaire.json defines the built-in branches. This table is how a
-- field added LATER — by an approved branch proposal — joins the same
-- branching, instead of existing in spec_fields and never reaching a bike.
-- Without it an approved proposal produced a field that appeared in no tree
-- and quietly did nothing.
CREATE TABLE field_triggers (
  id           INTEGER PRIMARY KEY,
  field_key    TEXT    NOT NULL REFERENCES spec_fields(field_key) ON DELETE CASCADE,
  question_id  TEXT    NOT NULL,
  option_label TEXT    NOT NULL,
  created_by   INTEGER REFERENCES users(id) ON DELETE SET NULL,
  created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
  UNIQUE (field_key, question_id, option_label)
);

CREATE INDEX idx_field_triggers_answer ON field_triggers (question_id, option_label);

-- What a bike actually answered.
--
-- Kept for two reasons: it is the only record of WHY a bike has the fields it
-- has, and a field attached to a branch later needs to find the bikes that
-- answered that way. One answer per question — re-running the questionnaire
-- replaces an answer rather than accumulating contradictory ones.
--
-- Bikes imported from the bulk catalog have no rows here. They never answered
-- anything, so a branch-attached field does not reach them; assuming a
-- spreadsheet row implies a chain drive would be inventing an answer nobody
-- gave.
CREATE TABLE bike_answers (
  bike_id      INTEGER NOT NULL REFERENCES bikes(id) ON DELETE CASCADE,
  question_id  TEXT    NOT NULL,
  option_label TEXT    NOT NULL,
  answered_by  INTEGER REFERENCES users(id) ON DELETE SET NULL,
  answered_at  TEXT    NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (bike_id, question_id)
);

CREATE INDEX idx_bike_answers_answer ON bike_answers (question_id, option_label);

-- One row per bike per field. No levels, no NULLs with hidden meaning:
-- value IS NULL means "known field, not yet sourced" — a gap, shown as a gap.
--
-- confidence: confirmed — straight from the official manual
--             mfr       — manufacturer press specs / consistent cross-ref
--             pending   — one weak source, shown as a gap not a guess
CREATE TABLE specs (
  id         INTEGER PRIMARY KEY,
  bike_id    INTEGER NOT NULL REFERENCES bikes(id) ON DELETE CASCADE,
  field_key  TEXT    NOT NULL REFERENCES spec_fields(field_key) ON DELETE CASCADE,
  value      TEXT,
  confidence TEXT    CHECK (confidence IN ('confirmed','mfr','pending')),

  -- Per-bike override of spec_fields.spec_type. NULL = inherit the platform
  -- default. This is how a bike's manager marks one spec "Fixed" — one correct
  -- value, closed to alternates and community submissions — without changing
  -- that field for every other bike, which is admin's authority, not theirs.
  spec_type  TEXT    CHECK (spec_type IS NULL OR spec_type IN ('fixed','pref','community')),

  tools      TEXT,                    -- free-text tools note shown with the value

  -- Taken offline by the bike's manager: riders see the field and a notice
  -- instead of the value. The same idea as paused tools and links -- hiding
  -- without destroying -- applied to the spec itself, so a value that has been
  -- flagged as wrong can stop being read as fact while it is checked. The
  -- value, its alternates and their votes all survive the pause.
  -- paused_by/at are kept because a manager's actions are not in admin_actions,
  -- so this row is the only record of who withheld a public value.
  paused     INTEGER NOT NULL DEFAULT 0 CHECK (paused IN (0,1)),
  paused_by  INTEGER REFERENCES users(id) ON DELETE SET NULL,
  paused_at  TEXT,

  entered_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
  updated_at TEXT    NOT NULL DEFAULT (datetime('now')),
  created_at TEXT    NOT NULL DEFAULT (datetime('now')),

  -- Which model years this value is true for. Both NULL means "every year this
  -- bike covers", which is what almost every spec is and what every row was
  -- before this existed.
  --
  -- A bike whose tank grew in 1978 is still the same bike: same engine, same
  -- frame, one page. Splitting it in two would duplicate the twenty-eight
  -- specs that did not change in order to vary the two that did, and then rely
  -- on the sibling-divergence report to notice when those twenty-eight drift
  -- apart. Splitting the SPEC keeps the duplication to the thing that actually
  -- differs. A split bike is for when the machine changed -- different engine,
  -- different frame -- and the specs genuinely should not share a page.
  year_from  INTEGER,
  year_to    INTEGER,
  CHECK ((year_from IS NULL) = (year_to IS NULL)),
  CHECK (year_from IS NULL OR year_from <= year_to)
);

-- One unranged row per field, and one variant per start year. Written as two
-- partial indexes rather than UNIQUE(bike_id, field_key, year_from) because
-- SQLite counts NULLs as distinct, so that form would happily allow five
-- "applies to every year" rows for the same field.
CREATE UNIQUE INDEX idx_specs_one_unranged ON specs (bike_id, field_key)
  WHERE year_from IS NULL;
CREATE UNIQUE INDEX idx_specs_one_per_year ON specs (bike_id, field_key, year_from)
  WHERE year_from IS NOT NULL;

-- Once a field has year variants there is no longer an unranged row for it, so
-- the unique index above stops guarding the case that matters: something that
-- adds fields in bulk -- re-answering the questionnaire, a back-fill, making a
-- field universal -- would cheerfully add a THIRD row covering every year,
-- beside the two that split it. Every one of those callers uses INSERT OR
-- IGNORE and means "only if it is missing", and RAISE(IGNORE) is exactly that
-- answer, so the rule lives here once instead of at a dozen call sites.
CREATE TRIGGER IF NOT EXISTS specs_no_unranged_beside_variants
BEFORE INSERT ON specs
WHEN NEW.year_from IS NULL
 AND EXISTS (SELECT 1 FROM specs
              WHERE bike_id = NEW.bike_id AND field_key = NEW.field_key)
BEGIN
  SELECT RAISE(IGNORE);
END;

CREATE INDEX idx_specs_bike  ON specs (bike_id);
CREATE INDEX idx_specs_field ON specs (field_key);
-- "This bike's value for that field" is the commonest lookup in the app (the
-- model filter reads every bike's displacement). Without a composite index
-- SQLite walks all rows of a field for each bike: fine at 264 bikes, nine
-- seconds at 1,700.
CREATE INDEX idx_specs_bike_field ON specs (bike_id, field_key);

-- Community alternates to a spec's stock value.
-- "Starts offline on this kind of bike." A field can be on a bike's sheet by
-- a branch that is right for other machines -- the belt branch carries the
-- sprocket fields for a Harley's pulleys, and every CVT scooter gets them too.
-- Rather than lose them for the Harleys, a rule per (field, bike type) says
-- the row is created OFFLINE on that type; the manager puts it online on the
-- one machine that really has the part. Enforced by the trigger below, so
-- every path that creates a spec row honours it without knowing about it.
CREATE TABLE field_offline_defaults (
  field_key  TEXT NOT NULL REFERENCES spec_fields(field_key) ON DELETE CASCADE,
  bike_type  TEXT NOT NULL,
  created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (field_key, bike_type)
);

CREATE TRIGGER trg_specs_default_offline
AFTER INSERT ON specs
WHEN EXISTS (
  SELECT 1 FROM field_offline_defaults d
  JOIN bikes b ON b.id = NEW.bike_id
  WHERE d.field_key = NEW.field_key AND d.bike_type = b.bike_type
)
BEGIN
  UPDATE specs SET paused = 1, paused_at = datetime('now') WHERE id = NEW.id;
END;

CREATE TABLE spec_alternates (
  id            INTEGER PRIMARY KEY,
  spec_id       INTEGER NOT NULL REFERENCES specs(id) ON DELETE CASCADE,
  text          TEXT    NOT NULL,
  submitted_by  INTEGER REFERENCES users(id) ON DELETE SET NULL,
  confirmed_fit INTEGER NOT NULL DEFAULT 0 CHECK (confirmed_fit IN (0,1)),
  paused        INTEGER NOT NULL DEFAULT 0 CHECK (paused IN (0,1)),
  created_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_spec_alternates_spec ON spec_alternates (spec_id);

-- Votes are rows, not a counter, so one user cannot vote twice and a vote can
-- be withdrawn. Counts are derived — see alternate_vote_counts.
CREATE TABLE alternate_votes (
  alternate_id INTEGER NOT NULL REFERENCES spec_alternates(id) ON DELETE CASCADE,
  user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (alternate_id, user_id)
);

-- The stock value is votable too. A rider confirming "that part number is
-- right, it fitted mine" is the most useful signal on the page, and until now
-- the only thing anyone could vote on was a competing alternate — so agreement
-- with the manual had nowhere to go, and a well-sourced value looked identical
-- to an unverified one.
CREATE TABLE spec_votes (
  spec_id    INTEGER NOT NULL REFERENCES specs(id) ON DELETE CASCADE,
  user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at TEXT    NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (spec_id, user_id)
);

-- "I want this one filled in." A demand signal on a gap.
--
-- Without it a manager facing 40 empty fields has nothing to say which ones
-- anybody actually needs, so the order of work is guesswork. Rows rather than
-- a counter for the same reason as votes: one request per person, withdrawable,
-- and the count cannot drift from the people who asked.
CREATE TABLE spec_requests (
  spec_id    INTEGER NOT NULL REFERENCES specs(id) ON DELETE CASCADE,
  user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at TEXT    NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (spec_id, user_id)
);

CREATE INDEX idx_spec_requests_spec ON spec_requests (spec_id);


-- ===========================================================================
-- SECTION 4 — FLAGS
--
-- Three kinds, three destinations. Keeping them in separate tables is what
-- lets each queue be a plain SELECT instead of a filtered pile:
--   value_flags — "this value is wrong"        -> the bike's manager
--   tree_flags  — "the tree itself is wrong"   -> admin
--   user_flags  — "this person is a problem"   -> admin
-- ===========================================================================

CREATE TABLE value_flags (
  id           INTEGER PRIMARY KEY,
  spec_id      INTEGER NOT NULL REFERENCES specs(id) ON DELETE CASCADE,
  alternate_id INTEGER REFERENCES spec_alternates(id) ON DELETE CASCADE,
  flagged_by   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  reason       TEXT    NOT NULL
                 CHECK (reason IN ('irrelevant','incorrect','inappropriate','other')),
  detail       TEXT,
  status       TEXT    NOT NULL DEFAULT 'open'
                 CHECK (status IN ('open','fixed','dismissed')),
  -- Filled on resolution so the resolved log can show what actually changed,
  -- and so a dismissed flag is distinguishable from a fixed one in the
  -- flagger's history.
  old_value    TEXT,
  new_value    TEXT,
  resolved_by  INTEGER REFERENCES users(id) ON DELETE SET NULL,
  resolved_at  TEXT,
  created_at   TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_value_flags_spec   ON value_flags (spec_id);
CREATE INDEX idx_value_flags_status ON value_flags (status);

-- Structural feedback on the Spec Tree itself, not on a value.
CREATE TABLE tree_flags (
  id            INTEGER PRIMARY KEY,
  field_key     TEXT    REFERENCES spec_fields(field_key) ON DELETE SET NULL,
  bike_id       INTEGER REFERENCES bikes(id) ON DELETE SET NULL,
  question_text TEXT,
  comment       TEXT    NOT NULL,
  flagged_by    INTEGER REFERENCES users(id) ON DELETE SET NULL,
  status        TEXT    NOT NULL DEFAULT 'open'
                  CHECK (status IN ('open','accepted','rejected')),
  admin_note    TEXT,
  resolved_at   TEXT,
  created_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_tree_flags_status ON tree_flags (status);

CREATE TABLE user_flags (
  id            INTEGER PRIMARY KEY,
  flagged_user  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  flagged_by    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  reason        TEXT    NOT NULL
                  CHECK (reason IN ('spam','harassment','bad-faith','fraud','inappropriate','other')),
  detail        TEXT,
  status        TEXT    NOT NULL DEFAULT 'open'
                  CHECK (status IN ('open','actioned','dismissed')),
  admin_note    TEXT,
  resolved_at   TEXT,
  created_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_user_flags_status ON user_flags (status);
CREATE INDEX idx_user_flags_target ON user_flags (flagged_user);

-- The "Contact" button on the manager dashboard: a manager asking a flagger
-- for more detail. Threaded on the flag, so the conversation stays attached to
-- the thing it is about.
CREATE TABLE flag_messages (
  id            INTEGER PRIMARY KEY,
  value_flag_id INTEGER NOT NULL REFERENCES value_flags(id) ON DELETE CASCADE,
  from_user     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  to_user       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  body          TEXT    NOT NULL,
  created_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_flag_messages_flag ON flag_messages (value_flag_id);
CREATE INDEX idx_flag_messages_to   ON flag_messages (to_user);


-- ===========================================================================
-- SECTION 5 — ADMIN QUEUES
-- ===========================================================================

-- Questions a manager could not answer confidently during setup. Routed to
-- admin because an unanswered question is a gap in the tree, not a bad value.
CREATE TABLE not_sure_answers (
  id            INTEGER PRIMARY KEY,
  bike_id       INTEGER NOT NULL REFERENCES bikes(id) ON DELETE CASCADE,
  field_key     TEXT    REFERENCES spec_fields(field_key) ON DELETE SET NULL,
  question_text TEXT    NOT NULL,
  submitted_by  INTEGER REFERENCES users(id) ON DELETE SET NULL,
  status        TEXT    NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending','confirmed','rejected')),
  admin_note    TEXT,
  resolved_at   TEXT,
  created_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_not_sure_status ON not_sure_answers (status);

-- "This spec is missing from the tree entirely." Admin reviews, because an
-- approved field becomes available for every bike, not just the proposer's.
CREATE TABLE branch_proposals (
  id           INTEGER PRIMARY KEY,
  field_name   TEXT    NOT NULL,
  category     TEXT    NOT NULL,
  bike_id      INTEGER REFERENCES bikes(id) ON DELETE SET NULL,
  proposed_by  INTEGER REFERENCES users(id) ON DELETE SET NULL,
  reasoning    TEXT,
  -- What the values will be, as the proposer sees it: text unless they say
  -- otherwise. Offered to admin as the preselected choice on approval.
  value_type   TEXT    NOT NULL DEFAULT 'text'
                 CHECK (value_type IN ('text','wire_color','fuel_octane','ethanol')),
  status       TEXT    NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending','approved','rejected')),
  admin_note   TEXT,
  -- Set on approval, pointing at the field this proposal created.
  created_field_key TEXT REFERENCES spec_fields(field_key) ON DELETE SET NULL,
  resolved_at  TEXT,
  created_at   TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_branch_proposals_status ON branch_proposals (status);


-- ===========================================================================
-- SECTION 6 — ENRICHMENT: TOOLS AND LINKS
--
-- The Enrich Spec page. Tools and links attach to (bike, field) — the same
-- grain as a spec row — so an oil-change walkthrough hangs off the oil spec of
-- one specific machine rather than floating on the model name.
--
-- `paused` is a manager hiding an entry without destroying it: a dead video
-- link stops showing but the row and its votes survive if it comes back.
-- ===========================================================================

CREATE TABLE spec_tools (
  id         INTEGER PRIMARY KEY,
  bike_id    INTEGER NOT NULL REFERENCES bikes(id) ON DELETE CASCADE,
  field_key  TEXT    NOT NULL REFERENCES spec_fields(field_key) ON DELETE CASCADE,
  text       TEXT    NOT NULL,
  added_by   INTEGER REFERENCES users(id) ON DELETE SET NULL,
  paused     INTEGER NOT NULL DEFAULT 0 CHECK (paused IN (0,1)),
  created_at TEXT    NOT NULL DEFAULT (datetime('now')),
  UNIQUE (bike_id, field_key, text)
);

CREATE INDEX idx_spec_tools_target ON spec_tools (bike_id, field_key);

CREATE TABLE spec_links (
  id         INTEGER PRIMARY KEY,
  bike_id    INTEGER NOT NULL REFERENCES bikes(id) ON DELETE CASCADE,
  field_key  TEXT    NOT NULL REFERENCES spec_fields(field_key) ON DELETE CASCADE,
  link_type  TEXT    NOT NULL CHECK (link_type IN ('yt','forum','doc','other')),
  title      TEXT    NOT NULL,
  url        TEXT    NOT NULL,
  added_by   INTEGER REFERENCES users(id) ON DELETE SET NULL,
  paused     INTEGER NOT NULL DEFAULT 0 CHECK (paused IN (0,1)),
  created_at TEXT    NOT NULL DEFAULT (datetime('now')),
  UNIQUE (bike_id, field_key, url)
);

CREATE INDEX idx_spec_links_target ON spec_links (bike_id, field_key);

CREATE TABLE link_votes (
  link_id    INTEGER NOT NULL REFERENCES spec_links(id) ON DELETE CASCADE,
  user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at TEXT    NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (link_id, user_id)
);

CREATE TABLE link_flags (
  id         INTEGER PRIMARY KEY,
  link_id    INTEGER NOT NULL REFERENCES spec_links(id) ON DELETE CASCADE,
  flagged_by INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  reason     TEXT    NOT NULL,
  detail     TEXT,
  status     TEXT    NOT NULL DEFAULT 'open'
               CHECK (status IN ('open','actioned','dismissed')),
  created_at TEXT    NOT NULL DEFAULT (datetime('now')),
  -- One flag per person per link; withdrawing is a DELETE, which is what the
  -- "flagged by mistake" control on the Enrich page does.
  UNIQUE (link_id, flagged_by)
);

CREATE INDEX idx_link_flags_link ON link_flags (link_id);


-- ===========================================================================
-- SECTION 7 — GARAGES AND SERVICE LOG
--
-- Was localStorage in the mockups, which meant a rider's own maintenance
-- history vanished with their browser cache. It lives here now.
--
-- A garage row points at a bike_year: a rider owns an '05, not a range.
-- ===========================================================================

CREATE TABLE user_bikes (
  id           INTEGER PRIMARY KEY,
  user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  bike_year_id INTEGER NOT NULL REFERENCES bike_years(id) ON DELETE CASCADE,
  nickname     TEXT,
  mileage      INTEGER,
  created_at   TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_user_bikes_user ON user_bikes (user_id);

-- A maintenance task definition, shared platform-wide.
-- interval_field_key points at the spec that states how often to do it, so the
-- interval is read from the spec sheet rather than duplicated here and left to
-- go stale when the spec is corrected.
CREATE TABLE service_tasks (
  task_key           TEXT PRIMARY KEY,
  name               TEXT    NOT NULL,
  interval_field_key TEXT    REFERENCES spec_fields(field_key) ON DELETE SET NULL,
  default_interval_miles INTEGER,
  sort_order         INTEGER NOT NULL DEFAULT 0
);

-- Which specs a task shows alongside it (oil weight and volume for an oil
-- change, etc).
CREATE TABLE service_task_specs (
  task_key   TEXT    NOT NULL REFERENCES service_tasks(task_key) ON DELETE CASCADE,
  field_key  TEXT    NOT NULL REFERENCES spec_fields(field_key) ON DELETE CASCADE,
  sort_order INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (task_key, field_key)
);

CREATE TABLE service_log (
  id           INTEGER PRIMARY KEY,
  user_bike_id INTEGER NOT NULL REFERENCES user_bikes(id) ON DELETE CASCADE,
  task_key     TEXT    NOT NULL REFERENCES service_tasks(task_key) ON DELETE CASCADE,
  performed_on TEXT    NOT NULL,          -- YYYY-MM-DD
  miles        INTEGER,
  note         TEXT,
  created_at   TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_service_log_bike ON service_log (user_bike_id, task_key);


-- A spec shown under more than one heading on one bike.
--
-- Every field has one home category on the Spec Tree; that is where its row
-- lives and where votes, flags and edits happen. A bike's manager can ALSO
-- show it under other headings on that bike -- a spark plug under Electrical
-- as well as Engine -- and the page prints it there as a pointer to the home
-- row, never a second copy of the value. Per bike, like header pins, because
-- what reads naturally next to what depends on the machine.
CREATE TABLE bike_spec_categories (
  bike_id    INTEGER NOT NULL REFERENCES bikes(id) ON DELETE CASCADE,
  field_key  TEXT    NOT NULL REFERENCES spec_fields(field_key) ON DELETE CASCADE,
  category   TEXT    NOT NULL,
  -- shown=1: also show it under this heading. shown=0 is only ever written
  -- for the field's HOME heading and means "not there, on this bike" -- the
  -- home is a default, not a fixture, and a manager can take a spec out of
  -- it as long as it still shows somewhere.
  shown      INTEGER NOT NULL DEFAULT 1 CHECK (shown IN (0,1)),
  placed_by  INTEGER REFERENCES users(id) ON DELETE SET NULL,
  placed_at  TEXT    NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (bike_id, field_key, category)
);

-- The same, site-wide: admin putting a field under extra headings on EVERY
-- bike. A bike's manager cannot undo one of these on their bike -- it is a
-- Spec Tree fact, like the home category -- but can add more of their own.
CREATE TABLE spec_field_categories (
  field_key  TEXT    NOT NULL REFERENCES spec_fields(field_key) ON DELETE CASCADE,
  category   TEXT    NOT NULL,
  shown      INTEGER NOT NULL DEFAULT 1 CHECK (shown IN (0,1)),   -- as above, site-wide
  placed_by  INTEGER REFERENCES users(id) ON DELETE SET NULL,
  placed_at  TEXT    NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (field_key, category)
);

-- ---------------------------------------------------------------------------
-- The bike's photo. One per bike, put there by its manager (or admin), stored
-- as a file under data/photos and served at /photos/<file>. The row is what
-- makes the file findable and says who supplied it; the file itself is the
-- picture. Replacing the photo replaces both.
-- ---------------------------------------------------------------------------
CREATE TABLE bike_photos (
  bike_id      INTEGER PRIMARY KEY REFERENCES bikes(id) ON DELETE CASCADE,
  file         TEXT    NOT NULL,          -- "258.jpg": bike id plus the real type
  mime         TEXT    NOT NULL CHECK (mime IN ('image/jpeg','image/png','image/webp')),
  bytes        INTEGER NOT NULL,
  uploaded_by  INTEGER REFERENCES users(id) ON DELETE SET NULL,
  uploaded_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- ---------------------------------------------------------------------------
-- What a manager changed about their own bike that the admin should hear of.
--
-- Renaming a bike, splitting it at a model year and giving it a photo are open to the bike's
-- manager: they know the machine, and making them ask would be the kind of
-- gate that stops the fix happening. But both change what every rider sees at
-- the top of the page, so when a manager does one the admin is told. A notice
-- is information after the fact, not a request for permission -- the change
-- has already happened -- and it leaves the queue when an admin marks it seen.
-- ---------------------------------------------------------------------------
CREATE TABLE manager_notices (
  id          INTEGER PRIMARY KEY,
  bike_id     INTEGER REFERENCES bikes(id) ON DELETE SET NULL,
  actor       INTEGER REFERENCES users(id) ON DELETE SET NULL,
  kind        TEXT    NOT NULL CHECK (kind IN ('rename','split','photo')),
  summary     TEXT    NOT NULL,
  detail      TEXT,                     -- JSON: what it was, what it is now
  created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
  seen_by     INTEGER REFERENCES users(id) ON DELETE SET NULL,
  seen_at     TEXT
);

CREATE INDEX idx_manager_notices_unseen ON manager_notices (created_at) WHERE seen_at IS NULL;

-- ---------------------------------------------------------------------------
-- Audit trail for admin actions.
--
-- Nothing recorded who did what: approving a proposal left a resolved_at, and
-- deleting a field with sourced values left no trace at all. One row per
-- action, with a summary already written for a human and a JSON detail.
--
-- `detail` earns its place on the destructive actions — a field deletion
-- records the values it destroyed. That is not undo and does not pretend to
-- be; it is the difference between "we can find out what was lost" and "it is
-- gone".
-- ---------------------------------------------------------------------------
CREATE TABLE admin_actions (
  id          INTEGER PRIMARY KEY,
  user_id     INTEGER REFERENCES users(id) ON DELETE SET NULL,
  action      TEXT    NOT NULL,   -- 'field.delete', 'proposal.approve', ...
  target      TEXT,               -- field key, bike id, username
  summary     TEXT    NOT NULL,   -- one sentence, already readable
  detail      TEXT,               -- JSON: counts, destroyed values, ids
  destructive INTEGER NOT NULL DEFAULT 0 CHECK (destructive IN (0,1)),
  created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_admin_actions_when ON admin_actions (created_at DESC);
CREATE INDEX idx_admin_actions_user ON admin_actions (user_id, created_at DESC);


-- ===========================================================================
-- SECTION 8 — VIEWS
-- ===========================================================================

-- Reading specs for a model-year is a plain join. No resolve, no fallback.
CREATE VIEW year_specs AS
SELECT
  y.id    AS bike_year_id,
  y.year  AS year,
  b.id    AS bike_id,
  b.make,
  b.model_code,
  f.category,
  f.label,
  f.spec_type,
  f.sort_order,
  s.field_key,
  s.value,
  s.confidence
FROM bike_years y
JOIN bikes b       ON b.id = y.bike_id
-- A spec covering every year (year_from NULL) lands on all of them; one with a
-- range lands only on the years it covers. Without this a split spec would
-- appear twice on every year, with both of its values.
JOIN specs s       ON s.bike_id = b.id
                  AND (s.year_from IS NULL
                       OR y.year BETWEEN s.year_from AND s.year_to)
JOIN spec_fields f ON f.field_key = s.field_key;

-- Derived vote counts. Nothing caches a total, so a withdrawn vote is
-- immediately correct everywhere.
CREATE VIEW alternate_vote_counts AS
SELECT a.id AS alternate_id, COUNT(v.user_id) AS votes
FROM spec_alternates a
LEFT JOIN alternate_votes v ON v.alternate_id = a.id
GROUP BY a.id;

CREATE VIEW spec_vote_counts AS
SELECT s.id AS spec_id, COUNT(v.user_id) AS votes
FROM specs s
LEFT JOIN spec_votes v ON v.spec_id = s.id
GROUP BY s.id;

-- "This bike should list X." A rider asking for a field the bike does not
-- have. spec_requests is the step after this one ("fill this in" on a field
-- the bike has); this is the field itself missing -- the questionnaire never
-- gave it one, or the bike came from a catalogue and answered nothing. One
-- row per person per (bike, field) so the count is the people who asked; the
-- bike's manager adds the field or declines and every request for that pair
-- resolves together. A field not on the tree at all is a branch proposal.
CREATE TABLE field_requests (
  id          INTEGER PRIMARY KEY,
  bike_id     INTEGER NOT NULL REFERENCES bikes(id) ON DELETE CASCADE,
  field_key   TEXT    NOT NULL REFERENCES spec_fields(field_key) ON DELETE CASCADE,
  user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  reasoning   TEXT,
  status      TEXT    NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending','added','declined')),
  decided_by  INTEGER REFERENCES users(id) ON DELETE SET NULL,
  admin_note  TEXT,
  resolved_at TEXT,
  created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
  UNIQUE (bike_id, field_key, user_id)
);
CREATE INDEX idx_field_requests_bike ON field_requests (bike_id, status);

-- "Add my bike." A rider asking for a machine the catalogue does not have.
-- The request carries what a rider knows -- make, model, years, engine size,
-- kind of bike -- and a reason; admin creates the bike from it (or points
-- the request at a bike that was already there under another name) or
-- declines with a note the riders can read. Riders who want the same bike
-- add their name to the open request rather than filing another, so the
-- count is the people waiting, and one decision settles all of them.
CREATE TABLE bike_requests (
  id           INTEGER PRIMARY KEY,
  user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  make         TEXT    NOT NULL,
  model        TEXT    NOT NULL,
  year_from    INTEGER,
  year_to      INTEGER,
  displacement TEXT,
  bike_type    TEXT,
  reasoning    TEXT,
  source_url   TEXT,
  status       TEXT    NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending','added','declined')),
  -- the bike it became, or was matched to; NULL until then, or if declined
  bike_id      INTEGER REFERENCES bikes(id) ON DELETE SET NULL,
  decided_by   INTEGER REFERENCES users(id) ON DELETE SET NULL,
  admin_note   TEXT,
  resolved_at  TEXT,
  created_at   TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_bike_requests_status ON bike_requests (status, created_at);

-- Everyone waiting on a request, the rider who filed it included.
CREATE TABLE bike_request_supporters (
  request_id INTEGER NOT NULL REFERENCES bike_requests(id) ON DELETE CASCADE,
  user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at TEXT    NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (request_id, user_id)
);

-- The managers' board and their direct messages. Managers are the people
-- closest to the bikes, and they work alone on their own machines; the board
-- is where they compare notes -- a wiring quirk shared across a family, a
-- source for a spec, how another manager handles a flag -- and a message is
-- for one person. Managers and admin only; a rider's way in is the flag,
-- the request and the spec sheet, not a chat.
CREATE TABLE board_threads (
  id           INTEGER PRIMARY KEY,
  author_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  title        TEXT    NOT NULL,
  -- optional: the bike the thread is about, so it can link to the sheet
  bike_id      INTEGER REFERENCES bikes(id) ON DELETE SET NULL,
  pinned       INTEGER NOT NULL DEFAULT 0 CHECK (pinned IN (0,1)),
  locked       INTEGER NOT NULL DEFAULT 0 CHECK (locked IN (0,1)),
  created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
  last_post_at TEXT    NOT NULL DEFAULT (datetime('now')),
  -- the newest post's id: what "unread" is measured against
  last_post_id INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_board_threads_activity ON board_threads (pinned DESC, last_post_at DESC);

CREATE TABLE board_posts (
  id         INTEGER PRIMARY KEY,
  thread_id  INTEGER NOT NULL REFERENCES board_threads(id) ON DELETE CASCADE,
  author_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  body       TEXT    NOT NULL,
  created_at TEXT    NOT NULL DEFAULT (datetime('now')),
  edited_at  TEXT
);
CREATE INDEX idx_board_posts_thread ON board_posts (thread_id, created_at);

-- How far each person has read in each thread: a thread whose newest post
-- is past that is unread for them.
CREATE TABLE board_reads (
  thread_id INTEGER NOT NULL REFERENCES board_threads(id) ON DELETE CASCADE,
  user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  read_post_id INTEGER NOT NULL DEFAULT 0,
  read_at      TEXT    NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (thread_id, user_id)
);

-- One conversation per pair of people, the lower id first so a pair has one
-- row whichever side started it.
CREATE TABLE dm_conversations (
  id              INTEGER PRIMARY KEY,
  user_a          INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  user_b          INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
  last_message_at TEXT,
  CHECK (user_a < user_b),
  UNIQUE (user_a, user_b)
);

CREATE TABLE dm_messages (
  id              INTEGER PRIMARY KEY,
  conversation_id INTEGER NOT NULL REFERENCES dm_conversations(id) ON DELETE CASCADE,
  sender_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  body            TEXT    NOT NULL,
  created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
  -- when the other person opened it; a conversation has one other person
  read_at         TEXT
);
CREATE INDEX idx_dm_messages_conv ON dm_messages (conversation_id, created_at);

CREATE VIEW spec_request_counts AS
SELECT s.id AS spec_id, COUNT(r.user_id) AS requests
FROM specs s
LEFT JOIN spec_requests r ON r.spec_id = s.id
GROUP BY s.id;

CREATE VIEW link_vote_counts AS
SELECT l.id AS link_id, COUNT(v.user_id) AS votes
FROM spec_links l
LEFT JOIN link_votes v ON v.link_id = l.id
GROUP BY l.id;

-- The display name for a bike, with a fallback so a bike missing its primary
-- name still renders as something a human recognises.
CREATE VIEW bike_display AS
SELECT
  b.id AS bike_id,
  b.make,
  b.model_code,
  b.bike_type,
  b.year_start,
  b.year_end,
  b.years_verified,
  COALESCE(
    (SELECT n.name FROM bike_names n WHERE n.bike_id = b.id AND n.is_primary = 1),
    b.make || ' ' || b.model_code
  ) AS display_name,
  CASE
    WHEN b.year_start IS NULL THEN NULL
    WHEN b.year_end IS NULL THEN CAST(b.year_start AS TEXT) || '-present'
    WHEN b.year_end = b.year_start THEN CAST(b.year_start AS TEXT)
    ELSE CAST(b.year_start AS TEXT) || '-' || CAST(b.year_end AS TEXT)
  END AS year_range
FROM bikes b;

-- Per-bike spec completion, driving the manager dashboard's counters.
--
-- Two sets of numbers, because an offline spec is not on the page at all for a
-- rider: counting it would advertise a field they cannot see and make "56 of
-- 65 filled" disagree with the 64 rows in front of them. The plain columns are
-- the manager's view -- everything on the bike, including what is offline,
-- because that is their workload. The _public columns are what a rider is told.
CREATE VIEW bike_spec_progress AS
SELECT
  b.id AS bike_id,
  COUNT(s.id)                                          AS fields_triggered,
  SUM(CASE WHEN s.value IS NOT NULL AND s.value <> '' THEN 1 ELSE 0 END) AS specs_filled,
  SUM(CASE WHEN s.value IS NULL OR s.value = ''  THEN 1 ELSE 0 END)      AS specs_needed,
  SUM(CASE WHEN s.paused = 1 THEN 1 ELSE 0 END)        AS specs_offline,
  SUM(CASE WHEN COALESCE(s.paused,0) = 0 AND s.id IS NOT NULL THEN 1 ELSE 0 END)
                                                       AS fields_triggered_public,
  SUM(CASE WHEN COALESCE(s.paused,0) = 0 AND s.value IS NOT NULL AND s.value <> ''
           THEN 1 ELSE 0 END)                          AS specs_filled_public,
  SUM(CASE WHEN COALESCE(s.paused,0) = 0 AND s.id IS NOT NULL
                AND (s.value IS NULL OR s.value = '') THEN 1 ELSE 0 END)
                                                       AS specs_needed_public
FROM bikes b
LEFT JOIN specs s ON s.bike_id = b.id
GROUP BY b.id;

-- Sibling divergence: bikes from the same split that no longer agree.
-- The safety net for the duplication a split accepts.
CREATE VIEW sibling_divergence AS
WITH lineage AS (
  -- only bikes that are in a split lineage: a bike with a split_from_bike_id,
  -- or the bike one points at. Starting from specs instead (every spec row
  -- against every other spec of its field) ran for minutes at 100k specs and
  -- held a read lock the whole time.
  SELECT id, COALESCE(split_from_bike_id, id) AS root FROM bikes
  WHERE split_from_bike_id IS NOT NULL
     OR id IN (SELECT split_from_bike_id FROM bikes WHERE split_from_bike_id IS NOT NULL)
),
pairs AS (
  SELECT la.root, la.id AS bike_a, lb.id AS bike_b
  FROM lineage la
  JOIN lineage lb ON lb.root = la.root AND la.id < lb.id
)
SELECT
  p.root    AS lineage_root,
  p.bike_a,
  p.bike_b,
  a.field_key,
  a.value   AS value_a,
  b.value   AS value_b
FROM pairs p
JOIN specs a ON a.bike_id = p.bike_a
JOIN specs b ON b.bike_id = p.bike_b AND b.field_key = a.field_key
WHERE IFNULL(a.value,'') <> IFNULL(b.value,'');

-- Acknowledging a divergence.
--
-- Most divergence is INTENDED — the wire colour really did change, that is why
-- the bike was split. An admin marks it reviewed and it leaves the queue.
--
-- The acknowledgement is keyed on the VALUES, not just the field. Acknowledge
-- "wire colour: Yellow/Red vs Yellow/Green" and that pair stops nagging. If
-- someone later edits wire colour again, the values no longer match the
-- acknowledgement and the row comes back. Keying on the field alone would hide
-- every future change to that field forever — the silent rot this queue exists
-- to catch.
CREATE TABLE divergence_ack (
  id          INTEGER PRIMARY KEY,
  bike_a      INTEGER NOT NULL REFERENCES bikes(id) ON DELETE CASCADE,
  bike_b      INTEGER NOT NULL REFERENCES bikes(id) ON DELETE CASCADE,
  field_key   TEXT    NOT NULL,
  value_a     TEXT,
  value_b     TEXT,
  reviewed_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
  note        TEXT,
  reviewed_at TEXT    NOT NULL DEFAULT (datetime('now')),
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

-- Catalog rows that were in the original 991-row import but did not survive
-- into the 139-row v2 browse page. Not a decision — a review list.
CREATE VIEW catalog_v2_dropped AS
SELECT
  d.display_name,
  d.make,
  d.model_code,
  y.year,
  y.id AS bike_year_id
FROM bike_years y
JOIN bike_display d ON d.bike_id = y.bike_id
WHERE y.in_v2 = 0
ORDER BY d.model_code, y.year;

-- Contribution counts behind the user profile cards.
CREATE VIEW user_profile_stats AS
SELECT
  u.id AS user_id,
  u.username,
  u.display_name,
  u.role,
  (SELECT COUNT(*) FROM specs s WHERE s.entered_by = u.id)                       AS specs_entered,
  (SELECT COUNT(*) FROM value_flags vf JOIN specs s ON s.id = vf.spec_id
     WHERE s.entered_by = u.id)                                                  AS specs_flagged,
  (SELECT COUNT(*) FROM value_flags vf WHERE vf.flagged_by = u.id)               AS flags_submitted,
  (SELECT COUNT(*) FROM alternate_votes av WHERE av.user_id = u.id)
    + (SELECT COUNT(*) FROM link_votes lv WHERE lv.user_id = u.id)               AS thumbs_up_given,
  (SELECT COUNT(*) FROM user_bikes ub WHERE ub.user_id = u.id)                   AS garage_count,
  (SELECT COUNT(*) FROM user_flags uf WHERE uf.flagged_user = u.id
     AND uf.status = 'open')                                                     AS open_user_flags
FROM users u;
