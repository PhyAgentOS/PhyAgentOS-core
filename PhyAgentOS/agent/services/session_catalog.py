"""Session catalog on the Core store database.

The catalog does not own session events. It adds two small tables to the same
``<workspace>/sessions/core.db`` used by
:class:`~PhyAgentOS.agent.core.session.SQLiteSessionStore`:

* ``session_catalog`` keeps a per-session title and the selected
  provider/model/effort. The session id is independent of the title;
* ``session_counters`` allocates the per-local-day sequence embedded in a new
  session id. A value is only ever incremented inside a ``BEGIN IMMEDIATE``
  transaction and is never deleted or reused, so ids keep increasing across
  restarts.

Sessions that only exist in ``session_events`` (written before the catalog
existed) are still enumerated and keep their original id. Metadata is created
lazily by :meth:`SessionCatalog.ensure_session`.

The class is synchronous because SQLite is; callers in async code should run it
in a worker thread (``await asyncio.to_thread(...)``). The database directory is
owned by the composition root and is never created here. This module imports no
provider, Forge, or runtime module.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..core.model import _CREDENTIAL_PATTERNS
from ..core.session import _open_sqlite_connection

__all__ = [
    "SessionCatalog",
    "SessionInfo",
    "SessionSelection",
    "derive_title",
]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS session_catalog (
    session_id TEXT PRIMARY KEY,
    title TEXT,
    selection TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS session_counters (
    scope TEXT PRIMARY KEY,
    value INTEGER NOT NULL
)
"""

_PREFIX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_TITLE_LIMIT = 36


def derive_title(text: str) -> str | None:
    """Return a short local title for the first input, or ``None``.

    The title is the first readable line, not the whole prompt: control
    characters and ANSI escape starts are removed, whitespace is collapsed,
    credential-shaped text is redacted, and the result is cut to
    :data:`_TITLE_LIMIT` characters. No model request is made.
    """
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    cleaned = text.replace("\r\n", "\n").replace("\r", "\n")
    # Remove ANSI CSI sequences, then any C0/C1 control characters (including
    # a bare ESC) so no escape byte can be persisted in a title.
    cleaned = re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", cleaned)
    cleaned = re.sub(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", " ", cleaned)
    first_line = next((line for line in cleaned.split("\n") if line.strip()), "")
    cleaned = re.sub(r"\s+", " ", first_line).strip()
    if not cleaned:
        return None
    for pattern in _CREDENTIAL_PATTERNS:
        cleaned = pattern.sub("[redacted]", cleaned)
    cleaned = cleaned.strip()
    if not cleaned:
        return None

    sentence_end = len(cleaned)
    for marker in ("。", "！", "？", "!", "?", ";", "；"):
        index = cleaned.find(marker)
        if index != -1:
            sentence_end = min(sentence_end, index)
    candidate = cleaned[:sentence_end].strip(" \t-*#>`").rstrip(" .,;:，。；：")
    if not candidate:
        return None
    if len(candidate) > _TITLE_LIMIT:
        candidate = candidate[:_TITLE_LIMIT].rstrip(" ,.;:，。；：") + "…"
    return candidate


@dataclass(frozen=True, slots=True)
class SessionInfo:
    """One session's catalog metadata.

    ``title`` is ``None`` for a session that has not received its first input
    yet; :attr:`label` then falls back to the id so a display layer always has
    something to show.
    """

    session_id: str
    title: str | None
    created_at: datetime
    updated_at: datetime
    last_sequence: int = 0
    selection: Mapping[str, str | None] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, str) or not self.session_id.strip():
            raise ValueError("session_id must be a non-empty string")
        if self.title is not None and not isinstance(self.title, str):
            raise TypeError("title must be a string or None")
        selection = self.selection or {}
        if not isinstance(selection, Mapping):
            raise TypeError("selection must be a mapping")
        object.__setattr__(self, "selection", dict(selection))

    @property
    def label(self) -> str:
        """Display label: ``title - session_id``, or just the id without a title."""
        return f"{self.title} - {self.session_id}" if self.title else self.session_id


@dataclass(frozen=True, slots=True)
class SessionSelection:
    """The last saved provider/model/effort for one session."""

    provider: str | None = None
    model: str | None = None
    effort: str | None = None

    def as_dict(self) -> dict[str, str]:
        return {
            key: value
            for key, value in (
                ("provider", self.provider),
                ("model", self.model),
                ("effort", self.effort),
            )
            if value is not None
        }


class SessionCatalog:
    """SQLite catalog for session titles, counters and selections.

    ``path`` is the same database file the session store opens
    (``<workspace>/sessions/core.db``). One instance owns one connection; use
    one instance per process role and close it with :meth:`close`.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        busy_timeout_ms: int = 5000,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._path = str(path)
        self._busy_timeout_ms = int(busy_timeout_ms)
        self._clock = clock or datetime.now
        self._closed = False
        self._lock = threading.RLock()
        self._conn = self._connect()

    # -- construction -------------------------------------------------

    def _connect(self):
        return _open_sqlite_connection(self._path, self._busy_timeout_ms, _SCHEMA)

    def _raise_if_closed(self) -> None:
        if self._closed:
            raise RuntimeError(f"session catalog is closed: {self._path}")

    # -- time helpers -------------------------------------------------

    def _now(self) -> datetime:
        now = self._clock()
        if not isinstance(now, datetime):
            raise TypeError("clock must return a datetime")
        if now.tzinfo is None or now.utcoffset() is None:
            # A naive clock reading is local time, like ``datetime.now()``.
            now = now.astimezone()
        return now.astimezone(timezone.utc)

    def _local_day(self) -> str:
        now = self._clock()
        if not isinstance(now, datetime):
            raise TypeError("clock must return a datetime")
        if now.tzinfo is not None and now.utcoffset() is not None:
            now = now.astimezone()
        return now.date().isoformat()

    def _timestamp(self, value: str | None, fallback: datetime) -> datetime:
        if value:
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed
        return fallback

    # -- schema helpers -----------------------------------------------

    def _has_events_table(self) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'session_events'"
        ).fetchone()
        return row is not None

    def _event_aggregate(self, session_id: str | None = None) -> dict[str, tuple[int, str, str]]:
        if not self._has_events_table():
            return {}
        sql = (
            "SELECT session_id, MAX(sequence), MIN(recorded_at), MAX(recorded_at)"
            " FROM session_events"
        )
        params: tuple[object, ...] = ()
        if session_id is not None:
            sql += " WHERE session_id = ?"
            params = (session_id,)
        sql += " GROUP BY session_id"
        return {
            str(row[0]): (int(row[1]), str(row[2]), str(row[3]))
            for row in self._conn.execute(sql, params).fetchall()
        }

    def _row_exists(self, session_id: str) -> bool:
        if self._conn.execute(
            "SELECT 1 FROM session_catalog WHERE session_id = ?", (session_id,)
        ).fetchone():
            return True
        if self._has_events_table() and self._conn.execute(
            "SELECT 1 FROM session_events WHERE session_id = ? LIMIT 1", (session_id,)
        ).fetchone():
            return True
        return False

    def _info(
        self,
        session_id: str,
        title: str | None,
        selection_json: str,
        created_at: str | None,
        updated_at: str | None,
        aggregate: tuple[int, str, str] | None,
    ) -> SessionInfo:
        fallback = self._now()
        if aggregate is None:
            last_sequence = 0
            event_created = event_updated = fallback
        else:
            last_sequence, first_at, last_at = aggregate
            event_created = self._timestamp(first_at, fallback)
            event_updated = self._timestamp(last_at, fallback)
        created = self._timestamp(created_at, event_created)
        updated = self._timestamp(updated_at, event_updated)
        if aggregate is not None:
            # Events can continue without a metadata write; keep list order on
            # the newest activity from either source.
            created = min(created, event_created)
            updated = max(updated, event_updated)
        selection = json.loads(selection_json) if selection_json else {}
        if not isinstance(selection, Mapping):
            raise ValueError(f"session {session_id!r} selection is not an object")
        return SessionInfo(
            session_id=session_id,
            title=title,
            created_at=created,
            updated_at=updated,
            last_sequence=last_sequence,
            selection={str(key): value for key, value in selection.items()},
        )

    # -- session lifecycle --------------------------------------------

    def create_session(self, prefix: str = "cli") -> SessionInfo:
        """Allocate a new id, ``<prefix>:YYYYMMDD-NN``, and its catalog row.

        The counter is per workspace database and per local day. It is
        allocated in one write transaction, so concurrent connections and
        processes cannot receive the same number, and it is never reused after
        a restart.
        """
        if not isinstance(prefix, str) or not _PREFIX.fullmatch(prefix):
            raise ValueError(
                "prefix must start with a letter or digit and contain only "
                "letters, digits, '_', '-', or '.'"
            )
        self._raise_if_closed()
        now = self._now()
        day = self._local_day()
        scope = day
        with self._lock:
            self._raise_if_closed()
            conn = self._conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                while True:
                    row = conn.execute(
                        "INSERT INTO session_counters (scope, value) VALUES (?, 1)"
                        " ON CONFLICT(scope) DO UPDATE SET value = value + 1"
                        " RETURNING value",
                        (scope,),
                    ).fetchone()
                    value = int(row[0])
                    session_id = f"{prefix}:{day.replace('-', '')}-{value:02d}"
                    if not self._row_exists(session_id):
                        break
                conn.execute(
                    "INSERT INTO session_catalog"
                    " (session_id, title, selection, created_at, updated_at)"
                    " VALUES (?, NULL, '{}', ?, ?)",
                    (session_id, now.isoformat(), now.isoformat()),
                )
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        return SessionInfo(
            session_id=session_id,
            title=None,
            created_at=now,
            updated_at=now,
            selection={},
        )

    def ensure_session(self, session_id: str) -> SessionInfo:
        """Return the session's metadata, creating a catalog row when missing.

        Existing event-only sessions keep their original id and are not
        renamed. A brand-new id is accepted here so an explicitly resumed id
        does not fail before its first event is written.
        """
        self._require_session_id(session_id)
        self._raise_if_closed()
        now = self._now()
        with self._lock:
            self._raise_if_closed()
            conn = self._conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT session_id, title, selection, created_at, updated_at"
                    " FROM session_catalog WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                if row is None:
                    conn.execute(
                        "INSERT INTO session_catalog"
                        " (session_id, title, selection, created_at, updated_at)"
                        " VALUES (?, NULL, '{}', ?, ?)",
                        (session_id, now.isoformat(), now.isoformat()),
                    )
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        info = self.get_session(session_id)
        assert info is not None
        return info

    def get_session(self, session_id: str) -> SessionInfo | None:
        """Return one session's metadata, including event-only sessions."""
        self._require_session_id(session_id)
        self._raise_if_closed()
        with self._lock:
            self._raise_if_closed()
            row = self._conn.execute(
                "SELECT session_id, title, selection, created_at, updated_at"
                " FROM session_catalog WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            aggregate = self._event_aggregate(session_id).get(session_id)
            if row is not None:
                return self._info(
                    str(row[0]), row[1], str(row[2]), str(row[3]), str(row[4]), aggregate
                )
            if aggregate is None:
                return None
            return self._info(
                session_id, None, "{}", None, None, aggregate
            )

    def list_sessions(self) -> list[SessionInfo]:
        """List catalog and event-only sessions, most recently updated first."""
        self._raise_if_closed()
        with self._lock:
            self._raise_if_closed()
            rows = self._conn.execute(
                "SELECT session_id, title, selection, created_at, updated_at"
                " FROM session_catalog"
            ).fetchall()
            aggregates = self._event_aggregate()
            infos = [
                self._info(
                    str(row[0]),
                    row[1],
                    str(row[2]),
                    str(row[3]),
                    str(row[4]),
                    aggregates.get(str(row[0])),
                )
                for row in rows
            ]
            known = {info.session_id for info in infos}
            for session_id, aggregate in aggregates.items():
                if session_id not in known:
                    infos.append(self._info(session_id, None, "{}", None, None, aggregate))
        infos.sort(key=lambda info: (info.updated_at.timestamp(), info.session_id), reverse=True)
        return infos

    # -- names and selections -----------------------------------------

    def name_from_first_input(self, session_id: str, text: str) -> str:
        """Set a short local title from the first input and return the name.

        An existing title is never overwritten. When nothing readable can be
        derived, the session keeps its id as the fallback label. The title is
        metadata only; the session id does not change.
        """
        self._require_session_id(session_id)
        title = derive_title(text)
        self._raise_if_closed()
        now = self._now()
        with self._lock:
            self._raise_if_closed()
            conn = self._conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT title FROM session_catalog WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                if row is None:
                    conn.execute(
                        "INSERT INTO session_catalog"
                        " (session_id, title, selection, created_at, updated_at)"
                        " VALUES (?, ?, '{}', ?, ?)",
                        (session_id, title, now.isoformat(), now.isoformat()),
                    )
                elif not row[0] and title:
                    conn.execute(
                        "UPDATE session_catalog SET title = ?, updated_at = ?"
                        " WHERE session_id = ?",
                        (title, now.isoformat(), session_id),
                    )
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        info = self.get_session(session_id)
        assert info is not None
        return info.title or session_id

    def save_selection(
        self,
        session_id: str,
        *,
        provider: str | None = None,
        model: str | None = None,
        effort: str | None = None,
    ) -> SessionSelection:
        """Store this session's provider/model/effort without touching others."""
        self._require_session_id(session_id)
        for name, value in (("provider", provider), ("model", model), ("effort", effort)):
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{name} must be a non-empty string or None")
        selection = SessionSelection(provider=provider, model=model, effort=effort)
        self._raise_if_closed()
        now = self._now()
        with self._lock:
            self._raise_if_closed()
            conn = self._conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT INTO session_catalog"
                    " (session_id, title, selection, created_at, updated_at)"
                    " VALUES (?, NULL, ?, ?, ?)"
                    " ON CONFLICT(session_id) DO UPDATE SET"
                    " selection = excluded.selection, updated_at = excluded.updated_at",
                    (
                        session_id,
                        json.dumps(selection.as_dict(), ensure_ascii=False),
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        return selection

    def load_selection(self, session_id: str) -> SessionSelection | None:
        """Return the saved selection, or ``None`` when none was saved."""
        self._require_session_id(session_id)
        self._raise_if_closed()
        with self._lock:
            self._raise_if_closed()
            row = self._conn.execute(
                "SELECT selection FROM session_catalog WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        raw = json.loads(row[0]) if row[0] else {}
        if not isinstance(raw, Mapping) or not raw:
            return None
        return SessionSelection(
            provider=raw.get("provider"),
            model=raw.get("model"),
            effort=raw.get("effort"),
        )

    # -- lifecycle ----------------------------------------------------

    def close(self) -> None:
        """Close the connection. Idempotent; later calls raise."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._conn.close()

    @staticmethod
    def _require_session_id(session_id: str) -> None:
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("session_id must be a non-empty string")
