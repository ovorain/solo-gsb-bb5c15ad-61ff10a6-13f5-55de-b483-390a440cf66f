"""SQLite connection handling, schema and initial seeding."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from pathlib import Path

from .revisions import ensure_baseline_revisions
from .seed import seed_items

# Default keeps local runs working out of the box; the Docker image sets this
# to /data/quiz.db (a named volume) so data survives container restarts.
DB_PATH = Path(os.getenv("QUIZ_DB_PATH", "data/quiz.db"))

SCHEMA_SESSIONS = """
CREATE TABLE IF NOT EXISTS sessions (
    id               TEXT PRIMARY KEY,
    theta            REAL NOT NULL,
    current_item_id  TEXT REFERENCES items(id), -- item currently served, not yet answered
    -- Revision version of the pending item as it was served.  /next
    -- re-serves and /responses scores against this frozen snapshot
    -- (item_revisions), so a formal import changing the item before it is
    -- answered cannot re-judge the delivered content.  NULL when no item
    -- is pending and on sessions predating this column (they fall back to
    -- the live bank content).
    current_item_version INTEGER,
    max_items        INTEGER NOT NULL,
    exposure_limit   INTEGER NOT NULL,
    duration_seconds INTEGER, -- optional time limit in seconds (60..86400); NULL = no limit
    expires_at       TEXT,    -- UTC ISO-8601 deadline (recomputed after every resume)
    target_se        REAL,    -- optional precision-stop target SE (positive finite); NULL = off
    min_precision_items INTEGER, -- optional min answers before precision stop (1..max_items)
    -- Pause support.  A paused session keeps status='paused' while the clock is
    -- frozen: paused_at records when the pause began and remaining_seconds caches
    -- the time left at that instant; both are NULL unless the session is paused.
    paused_at        TEXT,
    remaining_seconds INTEGER,
    status           TEXT NOT NULL DEFAULT 'active'
                     CHECK (status IN ('active', 'paused', 'finished')),
    finish_reason    TEXT, -- 'max_items' | 'no_available_items'
                     -- 'time_expired' | 'tag_minimums_unfulfillable'
                     -- 'precision_reached'
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id             TEXT PRIMARY KEY,
    difficulty     REAL NOT NULL,
    discrimination REAL NOT NULL,
    content_tag    TEXT,
    stem           TEXT NOT NULL,
    options        TEXT NOT NULL, -- JSON: [{"key": "A", "text": "..."}]
    answer         TEXT NOT NULL, -- correct option key, e.g. "B"
    exposure_count INTEGER NOT NULL DEFAULT 0,
    -- Item withdrawal: 1 = enabled (eligible for new selections), 0 = withdrawn.
    -- Legacy databases acquire the column via ALTER TABLE ... DEFAULT 1, so
    -- every pre-existing item migrates to "enabled".
    is_active      INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1))
);

""" + SCHEMA_SESSIONS + """
-- Optional per-session minimum answer quotas keyed by content tag.  A new
-- table (created IF NOT EXISTS) also serves as the migration: databases from
-- older versions simply acquire the table on startup and existing sessions
-- have no rows, so their behaviour is unchanged.
CREATE TABLE IF NOT EXISTS session_tag_minimums (
    session_id  TEXT NOT NULL REFERENCES sessions(id),
    content_tag TEXT NOT NULL,
    minimum     INTEGER NOT NULL CHECK (minimum >= 1),
    PRIMARY KEY (session_id, content_tag)
);

CREATE TABLE IF NOT EXISTS responses (
    id                        TEXT PRIMARY KEY, -- client-supplied idempotency key
    session_id                TEXT NOT NULL REFERENCES sessions(id),
    item_id                   TEXT NOT NULL REFERENCES items(id),
    response                  TEXT NOT NULL,
    score                     INTEGER NOT NULL CHECK (score IN (0, 1)),
    correct_answer            TEXT NOT NULL,
    theta_before              REAL NOT NULL,
    theta_after               REAL NOT NULL,
    p_correct                 REAL NOT NULL,
    information_at_theta      REAL NOT NULL,
    exposure_count_at_select  INTEGER NOT NULL,
    -- Content tag frozen at answer time.  Progress tag counts, the /next
    -- quota deficit/endgame restriction and the precision-stop quota gate
    -- all read THIS column, never the live items.content_tag, so a later
    -- import that retags (or a withdrawal of) an already-answered item can
    -- neither rewrite the answer's attribution nor make a quota look
    -- already met.  NULL means the item carried no non-empty tag when it
    -- was answered.  Legacy rows are backfilled exactly once at first
    -- upgrade (see _migrate_response_content_tags) and never recomputed.
    content_tag               TEXT,
    created_at                TEXT NOT NULL,
    UNIQUE (session_id, item_id) -- an item can only be answered once per session
);

CREATE INDEX IF NOT EXISTS idx_responses_session ON responses(session_id);

-- Append-only item revision history.  One continuous version chain per item:
-- version 1 is the startup baseline (also for freshly seeded demo items);
-- formal imports append import_create/import_update versions only on real
-- new content or actual content changes, and the availability PATCH appends
-- withdraw/re_enable versions only when the enabled state actually flips.
-- The snapshot copies the post-change content columns verbatim but never
-- exposure_count (which mutates with every served answer).  No code path
-- ever UPDATEs or DELETEs rows here, so later imports cannot rewrite history.
CREATE TABLE IF NOT EXISTS item_revisions (
    item_id        TEXT NOT NULL REFERENCES items(id),
    version        INTEGER NOT NULL CHECK (version >= 1),
    change_source  TEXT NOT NULL
                   CHECK (change_source IN
                          ('baseline', 'import_create', 'import_update',
                           'withdraw', 're_enable')),
    stem           TEXT NOT NULL,
    options        TEXT NOT NULL, -- JSON: [{"key": "A", "text": "..."}]
    answer         TEXT NOT NULL,
    difficulty     REAL NOT NULL,
    discrimination REAL NOT NULL,
    content_tag    TEXT,
    is_active      INTEGER NOT NULL CHECK (is_active IN (0, 1)),
    created_at     TEXT NOT NULL,  -- UTC ISO-8601, recorded inside the change transaction
    PRIMARY KEY (item_id, version)
) WITHOUT ROWID;

-- Wrong-question spaced review (SM-2).  New tables created IF NOT EXISTS also
-- serve as the migration: databases from older versions simply acquire them
-- on startup and behave exactly as before until a plan is created.
--
-- One finished session maps to at most one plan (UNIQUE(session_id)), so a
-- repeated creation returns the original plan.  Each plan item freezes the
-- stem/options/answer at creation time: later bank edits or withdrawals never
-- change the review content.  due_date is a UTC calendar date (YYYY-MM-DD);
-- items are initially due on the creation day.
CREATE TABLE IF NOT EXISTS review_plans (
    id         TEXT PRIMARY KEY,
    session_id TEXT NOT NULL UNIQUE REFERENCES sessions(id),
    created_at TEXT NOT NULL  -- UTC ISO-8601
);

CREATE TABLE IF NOT EXISTS review_items (
    id            TEXT PRIMARY KEY,
    plan_id       TEXT NOT NULL REFERENCES review_plans(id),
    item_id       TEXT NOT NULL REFERENCES items(id), -- source bank item (content frozen below)
    stem          TEXT NOT NULL,  -- stem frozen at plan creation
    options       TEXT NOT NULL,  -- options JSON frozen at plan creation
    answer        TEXT NOT NULL,  -- correct option key frozen at plan creation
    ease_factor   REAL NOT NULL DEFAULT 2.5,   -- SM-2 ease factor, floor 1.3
    interval_days INTEGER NOT NULL DEFAULT 0,  -- current interval in days
    repetitions   INTEGER NOT NULL DEFAULT 0,  -- consecutive successes (score >= 3)
    due_date      TEXT NOT NULL,  -- UTC date YYYY-MM-DD; initially the creation day
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_review_items_plan ON review_items(plan_id);
CREATE INDEX IF NOT EXISTS idx_review_items_due ON review_items(due_date);

-- Review results: review_id is the client idempotency key.  The before/after
-- snapshots let a replayed submission return the first result verbatim.
CREATE TABLE IF NOT EXISTS review_results (
    review_id           TEXT PRIMARY KEY, -- client-supplied idempotency key
    review_item_id      TEXT NOT NULL REFERENCES review_items(id),
    score               INTEGER NOT NULL CHECK (score BETWEEN 0 AND 5),
    ease_factor_before  REAL NOT NULL,
    ease_factor_after   REAL NOT NULL,
    interval_before     INTEGER NOT NULL,
    interval_after      INTEGER NOT NULL,
    repetitions_before  INTEGER NOT NULL,
    repetitions_after   INTEGER NOT NULL,
    due_date_before     TEXT NOT NULL,
    due_date_after      TEXT NOT NULL,
    created_at          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_review_results_item ON review_results(review_item_id);
"""


def get_db() -> Iterator[sqlite3.Connection]:
    """FastAPI dependency: a per-request SQLite connection.

    ``check_same_thread=False`` is required because FastAPI may run a sync
    dependency's enter, the endpoint and its exit on different threadpool
    workers under load.  Usage is still strictly serial (anyio awaits each
    stage), so a connection is never touched by two threads at once.
    ``busy_timeout`` makes competing BEGIN IMMEDIATE writers wait in a queue
    instead of raising "database is locked" immediately.
    """
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    try:
        yield conn
    finally:
        conn.close()


def _ensure_column(
    conn: sqlite3.Connection, table: str, name: str, decl: str
) -> bool:
    """Add a column to an existing table if it is missing (idempotent).

    Returns ``True`` when the column had to be added (a legacy table),
    ``False`` when it already existed.
    """
    columns = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    if name in columns:
        return False
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    return True


def _migrate_response_content_tags(conn: sqlite3.Connection) -> None:
    """Give legacy ``responses`` rows their frozen ``content_tag`` once.

    The frozen-tag column is absent on databases created before this
    feature: its first startup adds the column and backfills every
    historical answer from the item bank's *current* ``content_tag``
    (``空白标签`` normalized to NULL).  This snapshot is taken exactly once:
    the backfill is coupled to the presence-of-column migration, so on
    every later startup the column already exists and nothing is touched
    -- subsequent imports that retag an item (or item withdrawal) can
    never rewrite an already-frozen answer.
    """
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='responses'"
    ).fetchone()
    if exists is None:
        return
    added = _ensure_column(conn, "responses", "content_tag", "TEXT")
    if not added:
        return
    conn.execute(
        """
        UPDATE responses
           SET content_tag = (
                SELECT CASE
                           WHEN i.content_tag IS NULL OR i.content_tag = ''
                               THEN NULL
                           ELSE i.content_tag
                       END
                  FROM items i
                 WHERE i.id = responses.item_id
            )
         WHERE content_tag IS NULL
        """
    )


# Columns copied verbatim when rebuilding the sessions table for the pause
# migration; kept in sync with the canonical CREATE TABLE above.
_SESSION_COLUMNS = (
    "id, theta, current_item_id, current_item_version, max_items, "
    "exposure_limit, duration_seconds, "
    "expires_at, target_se, min_precision_items, paused_at, remaining_seconds, "
    "status, finish_reason, created_at, updated_at"
)


def _migrate_pause_support(conn: sqlite3.Connection) -> None:
    """Bring pre-pause databases up to date.

    Adds the ``paused_at`` / ``remaining_seconds`` columns and widens the
    ``status`` CHECK constraint to allow ``'paused'``.  A CHECK clause cannot
    be altered in place, so the classic SQLite table rebuild is used: rename
    the old table, create the new one with the canonical schema, copy every
    row, drop the old table.  The rebuild runs in autocommit mode with an
    explicit transaction; Python's implicit DML/DDL transaction handling
    would otherwise defer the statements and leave a dangling
    ``sessions_old`` table behind.

    A brand-new database has no sessions table yet; executescript in
    init_db creates the canonical one, so there is nothing to migrate.
    """
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sessions'"
    ).fetchone()
    if exists is None:
        return

    _ensure_column(conn, "sessions", "paused_at", "TEXT")
    _ensure_column(conn, "sessions", "remaining_seconds", "INTEGER")

    create_sql = next(
        r[0]
        for r in conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='sessions'"
        )
    )
    if "'paused'" in create_sql:
        return  # CHECK constraint already allows the paused status

    previous_isolation = conn.isolation_level
    conn.isolation_level = None  # autocommit: manage the rebuild transaction
    try:
        # Keep responses/session_tag_minimums pointing at the sessions name:
        # a modern SQLite RENAME would otherwise rewrite their foreign-key
        # references to sessions_old and make the final DROP fail.
        conn.execute("PRAGMA legacy_alter_table = ON")
        conn.execute("BEGIN")
        conn.execute("ALTER TABLE sessions RENAME TO sessions_old")
        conn.execute(SCHEMA_SESSIONS)
        conn.execute(
            f"INSERT INTO sessions ({_SESSION_COLUMNS}) "
            f"SELECT {_SESSION_COLUMNS} FROM sessions_old"
        )
        conn.execute("DROP TABLE sessions_old")
        conn.commit()
        conn.execute("PRAGMA legacy_alter_table = OFF")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.isolation_level = previous_isolation


def init_db() -> None:
    """Create tables (idempotent) and seed a deterministic demo bank once."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(DB_PATH)) as conn:
        conn.execute("PRAGMA journal_mode=WAL")

        # Columns added by earlier feature releases must exist *before* the
        # pause migration's table rebuild, because the rebuild copies every
        # column by name (including the ones a legacy database lacks).
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sessions'"
        ).fetchone() is not None:
            _ensure_column(conn, "sessions", "duration_seconds", "INTEGER")
            _ensure_column(conn, "sessions", "expires_at", "TEXT")
            _ensure_column(conn, "sessions", "target_se", "REAL")
            _ensure_column(conn, "sessions", "min_precision_items", "INTEGER")
            # Served-version freeze: legacy sessions acquire the column with
            # NULL (they keep the old live-content behaviour for a pending
            # item); newly served items always record the delivered version.
            _ensure_column(conn, "sessions", "current_item_version", "INTEGER")

        # Item withdrawal: a legacy item bank acquires the is_active column
        # with DEFAULT 1, i.e. every existing item migrates to "enabled".
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='items'"
        ).fetchone() is not None:
            _ensure_column(
                conn, "items", "is_active", "INTEGER NOT NULL DEFAULT 1"
            )

        # Rebuild the sessions table on legacy databases (widening the status
        # CHECK requires it); executescript below then only creates the
        # objects that are still missing.
        _migrate_pause_support(conn)
        conn.executescript(SCHEMA)
        # Frozen answer tags: a legacy responses table acquires the column
        # here and its rows are backfilled from the bank's current tags
        # exactly once; a brand-new table already carries the column, so the
        # migration is a no-op for it.
        _migrate_response_content_tags(conn)
        count = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        if count == 0:
            conn.executemany(
                """
                INSERT INTO items
                    (id, difficulty, discrimination, content_tag, stem,
                     options, answer, exposure_count)
                VALUES
                    (:id, :difficulty, :discrimination, :content_tag, :stem,
                     :options, :answer, 0)
                """,
                seed_items(),
            )
        # Revision history: give every item a version-1 baseline snapshot.
        # This covers both a legacy bank's pre-existing items (their first
        # startup with this module) and the freshly seeded demo bank.  The
        # guard is idempotent: items already having any revision are skipped,
        # so repeated startups never regenerate baselines.
        ensure_baseline_revisions(conn)
        conn.commit()
