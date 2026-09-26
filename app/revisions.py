"""Append-only item revision history.

Every item keeps a continuous sequence of versions.  A version is written
**inside the same write transaction** as the change it records, and only for
real changes:

* ``baseline``       -- one version-1 snapshot per item already present when
                        a legacy database first starts with this module;
                        generated idempotently, so later startups add nothing;
* ``import_create``  -- a formal (non-dry-run) bulk import inserts a new id;
* ``import_update``  -- a formal bulk import overwrites an existing item whose
                        content actually differs (stem/options/answer/
                        difficulty/discrimination/content_tag);
* ``withdraw``       -- PATCH availability flips ``is_active`` 1 -> 0;
* ``re_enable``     -- PATCH availability flips ``is_active`` 0 -> 1.

Same-value retries (re-importing identical content, setting the same
availability state), dry runs and batches that fail validation never produce
a version.

Each version stores a **complete snapshot** of the post-change item: stem,
options, answer, difficulty, discrimination, content tag and enabled state.
The exposure count is deliberately excluded -- it mutates with every answer
served and is not a bank revision.  History rows are append-only: no code
path ever updates or deletes them, so later imports cannot rewrite an older
version.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

SOURCE_BASELINE = "baseline"
SOURCE_IMPORT_CREATE = "import_create"
SOURCE_IMPORT_UPDATE = "import_update"
SOURCE_WITHDRAW = "withdraw"
SOURCE_RE_ENABLE = "re_enable"

CHANGE_SOURCES = (
    SOURCE_BASELINE,
    SOURCE_IMPORT_CREATE,
    SOURCE_IMPORT_UPDATE,
    SOURCE_WITHDRAW,
    SOURCE_RE_ENABLE,
)

# Item-content columns captured verbatim in every snapshot (exposure_count
# is intentionally absent).  Kept in one place so the table DDL, the INSERT
# and the read models cannot drift apart.
SNAPSHOT_COLUMNS = (
    "stem",
    "options",
    "answer",
    "difficulty",
    "discrimination",
    "content_tag",
    "is_active",
)


def utc_now_text() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def snapshot_dict(row: sqlite3.Row) -> dict:
    """Build the JSON-friendly full snapshot of an ``items`` row."""
    return {
        "stem": row["stem"],
        "options": json.loads(row["options"]),
        "answer": row["answer"],
        "difficulty": row["difficulty"],
        "discrimination": row["discrimination"],
        "content_tag": row["content_tag"],
        "is_active": bool(row["is_active"]),
    }


def import_row_changed(old_row: sqlite3.Row, item: object) -> bool:
    """Whether an import payload actually changes an existing item's content.

    Compared fields are exactly the six the upsert overwrites; the preserved
    ``exposure_count`` / ``is_active`` are not part of the decision (an
    availability flip owns its own versions).  Options are compared after
    JSON decoding, matching the normalised list the importer writes.
    """
    return (
        old_row["difficulty"] != item.difficulty
        or old_row["discrimination"] != item.discrimination
        or (old_row["content_tag"] or None) != (item.content_tag or None)
        or old_row["stem"] != item.stem
        or json.loads(old_row["options"]) != item.options
        or old_row["answer"] != item.answer
    )


def record_revision(
    conn: sqlite3.Connection,
    *,
    item_id: str,
    change_source: str,
    now: str | None = None,
) -> int | None:
    """Append the post-change snapshot of one item; return the new version.

    Must be called inside the caller's serialised write transaction.  The
    version number is ``MAX(version) + 1`` computed under the same write
    lock, so concurrent writers can never interleave versions.  Returns
    ``None`` (and writes nothing) if the item does not exist.
    """
    if change_source not in CHANGE_SOURCES:  # defensive: all callers are internal
        raise ValueError(f"unknown revision change_source: {change_source!r}")
    row = conn.execute(
        "SELECT * FROM items WHERE id = ?", (item_id,)
    ).fetchone()
    if row is None:
        return None
    last = conn.execute(
        "SELECT MAX(version) AS m FROM item_revisions WHERE item_id = ?",
        (item_id,),
    ).fetchone()["m"]
    version = (last or 0) + 1
    conn.execute(
        """
        INSERT INTO item_revisions
            (item_id, version, change_source,
             stem, options, answer, difficulty, discrimination,
             content_tag, is_active, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            item_id,
            version,
            change_source,
            row["stem"],
            row["options"],
            row["answer"],
            row["difficulty"],
            row["discrimination"],
            row["content_tag"],
            row["is_active"],
            now or utc_now_text(),
        ),
    )
    return version


def ensure_baseline_revisions(conn: sqlite3.Connection) -> int:
    """Give every item lacking history a version-1 ``baseline`` snapshot.

    Runs at startup, also covering freshly seeded demo banks, and is fully
    idempotent: on every later startup each item already has at least one
    revision row, so the guarded ``INSERT ... SELECT`` inserts nothing.
    Returns the number of baseline rows created.
    """
    cursor = conn.execute(
        """
        INSERT INTO item_revisions
            (item_id, version, change_source,
             stem, options, answer, difficulty, discrimination,
             content_tag, is_active, created_at)
        SELECT i.id, 1, 'baseline',
               i.stem, i.options, i.answer, i.difficulty, i.discrimination,
               i.content_tag, i.is_active, ?
        FROM items i
        WHERE NOT EXISTS (
            SELECT 1 FROM item_revisions r WHERE r.item_id = i.id
        )
        """,
        (utc_now_text(),),
    )
    return cursor.rowcount or 0
