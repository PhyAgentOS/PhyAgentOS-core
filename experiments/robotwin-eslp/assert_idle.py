"""Read-only legacy-run guard; never repair task state by rewriting the database."""

import json
from pathlib import Path
import sqlite3
import sys


def assert_idle(path):
    path = Path(path).resolve()
    if not path.exists():
        return
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as connection:
        records = connection.execute('SELECT status, record_json FROM agent_tasks').fetchall()
    for status, raw in records:
        record = json.loads(raw)
        if status not in {'succeeded', 'failed', 'cancelled'} or any(
            entry.get('status') not in {'succeeded', 'failed', 'stopped', 'cancelled'}
            for revision in record.get('revisions', [])
            for entry in revision.get('execution_records', [])
        ):
            raise RuntimeError('unresolved AgentTask: reconcile the original runtime before another legacy trial')


if __name__ == '__main__':
    assert_idle(sys.argv[1])
