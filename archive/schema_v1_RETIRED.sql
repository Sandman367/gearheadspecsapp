CREATE TABLE bikes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  make TEXT NOT NULL,
  model TEXT NOT NULL,
  year INTEGER,
  cc INTEGER,
  cc_verified INTEGER DEFAULT 0,
  bike_type TEXT
);

CREATE TABLE bike_specs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  bike_id INTEGER NOT NULL REFERENCES bikes(id),
  category TEXT NOT NULL,
  category2 TEXT,
  label TEXT NOT NULL,
  stock_value TEXT,
  confirmed_fit INTEGER DEFAULT 0,
  votes INTEGER DEFAULT 0,
  flags INTEGER DEFAULT 0,
  request_count INTEGER DEFAULT 0
);

CREATE TABLE alternates (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  spec_id INTEGER NOT NULL REFERENCES bike_specs(id),
  text TEXT NOT NULL,
  submitted_by TEXT,
  votes INTEGER DEFAULT 0,
  flags INTEGER DEFAULT 0,
  confirmed_fit INTEGER DEFAULT 0
);

CREATE TABLE users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT UNIQUE NOT NULL,
  role TEXT NOT NULL DEFAULT 'public'
);

CREATE TABLE bike_managers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT NOT NULL,
  bike_id INTEGER NOT NULL REFERENCES bikes(id),
  specialty TEXT
);

-- Spec value accuracy flags -> routed to the bike manager
CREATE TABLE value_flags (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  spec_id INTEGER NOT NULL REFERENCES bike_specs(id),
  entered_by TEXT,
  flagged_by TEXT NOT NULL,
  reason TEXT NOT NULL,
  detail TEXT,
  status TEXT NOT NULL DEFAULT 'open',
  old_value TEXT,
  new_value TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  resolved_at TEXT
);

-- Spec Tree structural feedback -> routed to admin
CREATE TABLE tree_flags (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  spec_id INTEGER REFERENCES bike_specs(id),
  bike_id INTEGER REFERENCES bikes(id),
  question_text TEXT,
  comment TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- User behavior flags -> routed to admin
CREATE TABLE user_flags (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  flagged_user TEXT NOT NULL,
  flagged_by TEXT NOT NULL,
  reason TEXT NOT NULL,
  detail TEXT,
  status TEXT NOT NULL DEFAULT 'open',
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE not_sure_answers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  bike_id INTEGER NOT NULL REFERENCES bikes(id),
  question_text TEXT NOT NULL,
  submitted_by TEXT,
  status TEXT NOT NULL DEFAULT 'pending',
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE branch_proposals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  field_name TEXT NOT NULL,
  category TEXT NOT NULL,
  bike_id INTEGER REFERENCES bikes(id),
  proposed_by TEXT,
  reasoning TEXT,
  status TEXT NOT NULL DEFAULT 'pending',
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE votes_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  alternate_id INTEGER NOT NULL REFERENCES alternates(id),
  username TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
