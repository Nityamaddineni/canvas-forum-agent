import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_items (
    item_id TEXT PRIMARY KEY, kind TEXT, author_id TEXT, author TEXT, first_seen REAL NOT NULL);
CREATE TABLE IF NOT EXISTS offered_items (
    run_id INTEGER NOT NULL, item_id TEXT NOT NULL, kind TEXT, author_id TEXT, author TEXT,
    PRIMARY KEY (run_id, item_id));
CREATE TABLE IF NOT EXISTS actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    intent_key TEXT NOT NULL UNIQUE,
    action TEXT NOT NULL, target_entry_id TEXT, message TEXT NOT NULL, message_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending','posted','failed')),
    entry_id TEXT, detail TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS post_times (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, kind TEXT NOT NULL,
    action TEXT, target_entry_id TEXT, reason TEXT, outcome TEXT NOT NULL, detail TEXT, message TEXT);
CREATE TABLE IF NOT EXISTS counters (name TEXT PRIMARY KEY, value INTEGER NOT NULL DEFAULT 0);
"""


class Memory:
    def __init__(self, path, clock=time.time):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._db = sqlite3.connect(path, isolation_level=None)  # autocommit: writes are durable at once
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)
        self._add_missing_columns()

    def _add_missing_columns(self):
        """Databases created before these columns existed are upgraded in place."""
        for table, column in (("seen_items", "author"), ("offered_items", "author"), ("runs", "message")):
            have = {r["name"] for r in self._db.execute(f"PRAGMA table_info({table})")}
            if column not in have:
                self._db.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")

    def close(self):
        self._db.close()

    # seen_items
    def is_seen(self, item_id):
        return self._db.execute("SELECT 1 FROM seen_items WHERE item_id=?", (item_id,)).fetchone() is not None

    def mark_seen(self, items):
        now = self._clock()
        self._db.executemany(
            "INSERT OR IGNORE INTO seen_items (item_id, kind, author_id, author, first_seen) VALUES (?,?,?,?,?)",
            [(i["id"], i["kind"], i["author_id"], i.get("author"), now) for i in items])

    def record_offered(self, run_id, items):
        self._db.executemany(
            "INSERT OR IGNORE INTO offered_items (run_id, item_id, kind, author_id, author) VALUES (?,?,?,?,?)",
            [(run_id, i["id"], i["kind"], i["author_id"], i.get("author")) for i in items])

    def mark_offered_seen(self):
        """Mark everything the latest completed fetch offered as seen. No such fetch: no-op."""
        row = self._db.execute(
            "SELECT MAX(id) FROM runs WHERE kind='fetch' AND outcome='ok'").fetchone()
        if row[0] is None:
            return 0
        cur = self._db.execute(
            "INSERT OR IGNORE INTO seen_items (item_id, kind, author_id, author, first_seen) "
            "SELECT item_id, kind, author_id, author, ? FROM offered_items WHERE run_id=?",
            (self._clock(), row[0]))
        return cur.rowcount

    def known_author_names(self):
        """Display names of authors the agent has seen, or has been offered but not yet marked seen."""
        rows = self._db.execute(
            "SELECT author FROM seen_items WHERE author IS NOT NULL "
            "UNION SELECT author FROM offered_items WHERE author IS NOT NULL").fetchall()
        return {r["author"] for r in rows}

    # runs
    def abort_stale_runs(self):
        """Runs still 'started' belong to an earlier process that died: mark them aborted."""
        cur = self._db.execute(
            "UPDATE runs SET outcome='aborted', detail='process ended before the run finished' "
            "WHERE outcome='started'")
        return cur.rowcount

    def start_run(self, kind, action=None, target=None, reason=None, message=None):
        cur = self._db.execute(
            "INSERT INTO runs (ts, kind, action, target_entry_id, reason, message, outcome)"
            " VALUES (?,?,?,?,?,?, 'started')",
            (self._clock(), kind, action, target, reason, message))
        return cur.lastrowid

    def finish_run(self, run_id, outcome, detail=None):
        self._db.execute("UPDATE runs SET outcome=?, detail=? WHERE id=?", (outcome, detail, run_id))

    def recent_runs(self, n=10):
        rows = self._db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (n,)).fetchall()
        return [dict(r) for r in rows]

    def recent_decisions(self, n=20):
        rows = self._db.execute(
            "SELECT id, ts, action, target_entry_id, reason, outcome, detail, message FROM runs "
            "WHERE kind='submit' ORDER BY id DESC LIMIT ?", (n,)).fetchall()
        return [dict(r) for r in rows]

    def rejected_streak(self):
        """Submits rejected back to back, with no fetch or other submit outcome since: the current cycle."""
        n = 0
        for r in self._db.execute(
                "SELECT kind, outcome FROM runs WHERE outcome NOT IN ('aborted','started') ORDER BY id DESC"):
            if r["kind"] == "submit" and r["outcome"] == "rejected":
                n += 1
            else:
                break
        return n

    def last_outcomes(self, n):
        rows = self._db.execute("SELECT outcome FROM runs WHERE outcome != 'aborted' ORDER BY id DESC LIMIT ?", (n,)).fetchall()
        return [r["outcome"] for r in rows]

    # actions
    def get_action(self, intent_key):
        row = self._db.execute("SELECT * FROM actions WHERE intent_key=?", (intent_key,)).fetchone()
        return dict(row) if row else None

    def create_action(self, intent_key, action, target, message, message_hash):
        now = self._clock()
        cur = self._db.execute(
            "INSERT INTO actions (intent_key, action, target_entry_id, message, message_hash, status,"
            " created_at, updated_at) VALUES (?,?,?,?,?, 'pending', ?, ?)",
            (intent_key, action, target, message, message_hash, now, now))
        return cur.lastrowid

    def set_action(self, action_id, status, entry_id=None, detail=None):
        self._db.execute(
            "UPDATE actions SET status=?, entry_id=COALESCE(?, entry_id), detail=?, updated_at=? WHERE id=?",
            (status, entry_id, detail, self._clock(), action_id))

    def actions_with_status(self, status):
        rows = self._db.execute("SELECT * FROM actions WHERE status=? ORDER BY id", (status,)).fetchall()
        return [dict(r) for r in rows]

    # post_times
    def record_post(self):
        self._db.execute("INSERT INTO post_times (ts) VALUES (?)", (self._clock(),))

    def count_posts_since(self, ts):
        return self._db.execute("SELECT COUNT(*) FROM post_times WHERE ts>=?", (ts,)).fetchone()[0]

    # counters
    def incr(self, name, by=1):
        self._db.execute(
            "INSERT INTO counters (name, value) VALUES (?, ?) "
            "ON CONFLICT(name) DO UPDATE SET value=value+excluded.value", (name, by))

    def counters(self):
        return {r["name"]: r["value"] for r in self._db.execute("SELECT * FROM counters ORDER BY name")}
