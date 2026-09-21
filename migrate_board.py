"""
The managers' board and their direct messages.

Managers work alone on their own bikes; the board is where they compare
notes -- a wiring quirk shared across a family, a source for a spec, how
another manager handles a flag -- and a message is for one person. Managers
and admin only. Idempotent.

Run:  py migrate_board.py [path/to/data.db]
"""
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(ROOT, "data.db")

DDL = """
-- The managers' board and their direct messages. Managers are the people
-- closest to the bikes, and they work alone on their own machines; the board
-- is where they compare notes -- a wiring quirk shared across a family, a
-- source for a spec, how another manager handles a flag -- and a message is
-- for one person. Managers and admin only; a rider's way in is the flag,
-- the request and the spec sheet, not a chat.
CREATE TABLE IF NOT EXISTS board_threads (
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
CREATE INDEX IF NOT EXISTS idx_board_threads_activity ON board_threads (pinned DESC, last_post_at DESC);

CREATE TABLE IF NOT EXISTS board_posts (
  id         INTEGER PRIMARY KEY,
  thread_id  INTEGER NOT NULL REFERENCES board_threads(id) ON DELETE CASCADE,
  author_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  body       TEXT    NOT NULL,
  created_at TEXT    NOT NULL DEFAULT (datetime('now')),
  edited_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_board_posts_thread ON board_posts (thread_id, created_at);

-- How far each person has read in each thread: a thread whose newest post
-- is past that is unread for them.
CREATE TABLE IF NOT EXISTS board_reads (
  thread_id INTEGER NOT NULL REFERENCES board_threads(id) ON DELETE CASCADE,
  user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  read_post_id INTEGER NOT NULL DEFAULT 0,
  read_at      TEXT    NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (thread_id, user_id)
);

-- One conversation per pair of people, the lower id first so a pair has one
-- row whichever side started it.
CREATE TABLE IF NOT EXISTS dm_conversations (
  id              INTEGER PRIMARY KEY,
  user_a          INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  user_b          INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
  last_message_at TEXT,
  CHECK (user_a < user_b),
  UNIQUE (user_a, user_b)
);

CREATE TABLE IF NOT EXISTS dm_messages (
  id              INTEGER PRIMARY KEY,
  conversation_id INTEGER NOT NULL REFERENCES dm_conversations(id) ON DELETE CASCADE,
  sender_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  body            TEXT    NOT NULL,
  created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
  -- when the other person opened it; a conversation has one other person
  read_at         TEXT
);
CREATE INDEX IF NOT EXISTS idx_dm_messages_conv ON dm_messages (conversation_id, created_at);
"""


def migrate(db_path):
    if not os.path.exists(db_path):
        raise SystemExit(f"no database at {db_path}")
    conn = sqlite3.connect(db_path, timeout=30)
    conn.executescript(DDL)
    conn.commit()
    conn.close()
    print("board and messages in place")


if __name__ == "__main__":
    migrate(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB)
