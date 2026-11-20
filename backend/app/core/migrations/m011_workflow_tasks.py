"""Schema v11 - what each workflow *does*, and whether it runs locally or on a paid API.

``capability_tags`` record which node families appear in a graph. That is too
coarse to browse by: ``LoadImage`` alone tagged 410 of 689 official templates
``img2img``, and nothing said "removes a background" or "is a utility".
v11 stores an answer from a fixed task list (``parsers/workflow_tasks.py``)
for every workflow, whether it is a template, a bundled example or the owner's
own.

``workflows.tasks_json``
    Every task the workflow performs, main job first.
``workflows.primary_task``
    That main job. Used for grouping and sorting.
``workflows.runs_on``
    ``local`` or ``api``.  ``api`` means a partner-API node does the work on
    someone else's servers, usually for credits.
``workflow_tasks(workflow_id, task)``
    One row per task, so filtering and per-task counts are indexed lookups
    rather than JSON scans.

Forward-only and non-destructive, like v4 and v10: columns appended with
``ALTER TABLE`` and one new table created.  Existing rows are backfilled here
from what the index already holds: the node-class table, the stored graph for
classes inside subgraphs, and the template tags. The owner sees tasks on the
first start, without waiting for a scan. The indexer keeps them current
after that.
"""

from __future__ import annotations

import json
import sqlite3

from ...parsers import workflow_tasks

VERSION = 11
NAME = "workflow_tasks"

TASKS_TABLE = """
CREATE TABLE IF NOT EXISTS workflow_tasks (
    workflow_id  INTEGER NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
    task         TEXT NOT NULL,
    PRIMARY KEY (workflow_id, task)
) WITHOUT ROWID
"""

#: A stored graph bigger than this is not re-parsed for subgraph classes; the
#: top-level classes still classify it.  Keeps the upgrade bounded.
BACKFILL_GRAPH_CAP = 4 * 1024 * 1024


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})")}


def backfill(conn: sqlite3.Connection) -> int:
    """Classify every workflow row from what the index already holds."""
    nodes: dict[int, dict[str, int]] = {}
    for wid, cls, n in conn.execute("SELECT workflow_id, class_type, count FROM workflow_nodes"):
        nodes.setdefault(int(wid), {})[str(cls)] = int(n or 1)
    templates = {
        int(r[0]): r for r in conn.execute(
            "SELECT workflow_id, tags_json, category, open_source FROM workflow_templates")
    }
    done = 0
    rows = conn.execute(
        "SELECT id, name, title, origin, graph_json FROM workflows").fetchall()
    for wid, name, title, origin, graph_json in rows:
        wid = int(wid)
        classes = dict(nodes.get(wid, {}))
        if graph_json and len(graph_json) <= BACKFILL_GRAPH_CAP:
            try:
                for cls in workflow_tasks.graph_classes(json.loads(graph_json)):
                    classes.setdefault(cls, 1)
            except (ValueError, TypeError):
                pass
        t = templates.get(wid)
        try:
            tags = json.loads(t[1]) if t and t[1] else None
        except (ValueError, TypeError):
            tags = None
        result = workflow_tasks.classify(
            classes, names=(name, title),
            is_template=origin == "official_template",
            template_tags=tags, template_category=t[2] if t else None,
            open_source=None if not t or t[3] is None else bool(t[3]))
        write(conn, wid, result)
        done += 1
    return done


def write(conn: sqlite3.Connection, workflow_id: int, result: dict) -> None:
    """Store one classification.  Shared with the indexer so both write alike."""
    tasks = [t for t in result.get("tasks") or [] if t in workflow_tasks.TASK_KEYS]
    conn.execute(
        "UPDATE workflows SET tasks_json = ?, primary_task = ?, runs_on = ? WHERE id = ?",
        (json.dumps(tasks), tasks[0] if tasks else None,
         result.get("runs_on") if result.get("runs_on") in workflow_tasks.RUNS_ON else None,
         int(workflow_id)))
    conn.execute("DELETE FROM workflow_tasks WHERE workflow_id = ?", (int(workflow_id),))
    conn.executemany("INSERT OR IGNORE INTO workflow_tasks(workflow_id, task) VALUES (?, ?)",
                     [(int(workflow_id), t) for t in tasks])


def up(conn: sqlite3.Connection) -> None:
    have = _columns(conn, "workflows")
    if "tasks_json" not in have:
        conn.execute("ALTER TABLE workflows ADD COLUMN tasks_json TEXT")
    if "primary_task" not in have:
        conn.execute("ALTER TABLE workflows ADD COLUMN primary_task TEXT")
    if "runs_on" not in have:
        conn.execute("ALTER TABLE workflows ADD COLUMN runs_on TEXT")
    conn.execute(TASKS_TABLE)
    conn.execute("CREATE INDEX IF NOT EXISTS ix_workflow_tasks_task "
                 "ON workflow_tasks(task, workflow_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_workflows_primary_task "
                 "ON workflows(primary_task)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_workflows_runs_on ON workflows(runs_on)")
    backfill(conn)
