"""Give root-level albums a real identity constraint, and de-duplicate.

``albums`` carried ``UNIQUE (parent_id, scope, name)``.  In SQLite NULL is never
equal to NULL, so that index never fires for root-level albums (``parent_id IS
NULL``) - which is every system album.  ``ensure_system_albums()`` therefore
re-inserted all ten on every startup, compounding forever.

The fix is an expression index over ``COALESCE(parent_id, 0)`` so NULL parents
compare equal, plus a one-time de-duplication that keeps the lowest id per
(parent, scope, name) and re-points album membership at the survivor first.
"""

from __future__ import annotations

import sqlite3

VERSION = 3
NAME = "album_identity"


def up(conn: sqlite3.Connection) -> None:
    groups = conn.execute(
        "SELECT COALESCE(parent_id, 0) AS pid, scope, name, MIN(id) AS keep_id, "
        "COUNT(*) AS n FROM albums GROUP BY pid, scope, name HAVING n > 1"
    ).fetchall()

    for row in groups:
        keep_id = int(row["keep_id"])
        dupes = [
            int(r["id"]) for r in conn.execute(
                "SELECT id FROM albums WHERE COALESCE(parent_id, 0) = ? AND scope = ? "
                "AND name = ? AND id <> ?",
                (row["pid"], row["scope"], row["name"], keep_id),
            ).fetchall()
        ]
        if not dupes:
            continue
        placeholders = ",".join("?" * len(dupes))
        # Move membership to the survivor before deleting anything.  UPDATE OR
        # IGNORE because (album_id, uid) is the primary key: a uid already on the
        # survivor would collide, and the duplicate row is then simply dropped.
        conn.execute(
            f"UPDATE OR IGNORE album_items SET album_id = ? "  # noqa: S608
            f"WHERE album_id IN ({placeholders})",
            (keep_id, *dupes),
        )
        conn.execute(
            f"DELETE FROM album_items WHERE album_id IN ({placeholders})",  # noqa: S608
            dupes,
        )
        conn.execute(
            f"UPDATE outputs SET album_id = ? WHERE album_id IN ({placeholders})",  # noqa: S608
            (keep_id, *dupes),
        )
        conn.execute(
            f"UPDATE albums SET parent_id = ? WHERE parent_id IN ({placeholders})",  # noqa: S608
            (keep_id, *dupes),
        )
        conn.execute(
            f"DELETE FROM albums WHERE id IN ({placeholders})", dupes  # noqa: S608
        )

    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_albums_identity "
        "ON albums(COALESCE(parent_id, 0), scope, name)"
    )
    conn.execute(
        "UPDATE albums SET item_count = COALESCE("
        "(SELECT COUNT(*) FROM album_items ai WHERE ai.album_id = albums.id), 0)"
    )
