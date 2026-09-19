import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from fantasy_mcp.domain import DraftState, Provenance, now
from fantasy_mcp.errors import FantasyError


class Store:
    """Short transactions, separate connections, WAL, immutable normalized history."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS cache (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL,
                    provenance TEXT NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS snapshots (
                    id INTEGER PRIMARY KEY, key TEXT NOT NULL,
                    at TEXT NOT NULL, value TEXT NOT NULL, provenance TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS snapshots_lookup ON snapshots(key, at);
                CREATE TABLE IF NOT EXISTS drafts (id TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT OR IGNORE INTO meta VALUES ('schema_version', '1');
            """)
            version = db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            if version[0] != "1":
                raise FantasyError("SCHEMA_VERSION", "Database is newer than this application.")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=15)
        try:
            with db:
                yield db
        finally:
            db.close()

    def get_meta(self, key: str) -> str | None:
        with self.connect() as db:
            row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return str(row[0]) if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, value))

    def clear_remote_cache(self) -> None:
        """Reauthorization may select another account; do not reuse its predecessor's state."""
        with self.connect() as db:
            db.execute("DELETE FROM cache")
            db.execute("DELETE FROM meta WHERE key LIKE 'league_key:%' OR key LIKE 'my_team:%'")

    def put(self, key: str, value: Any, provenance: Provenance, ttl: int) -> None:
        data, prov = json.dumps(value), provenance.model_dump_json()
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO cache VALUES (?,?,?,?)",
                (key, data, prov, now().timestamp() + ttl),
            )
            db.execute(
                "INSERT INTO snapshots(key,at,value,provenance) VALUES (?,?,?,?)",
                (key, now().isoformat(), data, prov),
            )

    def get(self, key: str) -> tuple[Any, Provenance] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT value,provenance,expires FROM cache WHERE key=?", (key,)
            ).fetchone()
        if not row:
            return None
        prov = Provenance.model_validate_json(row[1])
        prov.cached = True
        prov.stale = now().timestamp() >= row[2]
        return json.loads(row[0]), prov

    def snapshots(self, key: str, limit: int = 30) -> list[dict[str, Any]]:
        if not 1 <= limit <= 365:
            raise FantasyError("INVALID_INPUT", "limit must be 1..365")
        with self.connect() as db:
            rows = db.execute(
                "SELECT at,value,provenance FROM snapshots WHERE key=? ORDER BY id DESC LIMIT ?",
                (key, limit),
            ).fetchall()
        return [
            {"at": r[0], "data": json.loads(r[1]), "provenance": json.loads(r[2])} for r in rows
        ]

    def create_draft(self, draft: DraftState) -> DraftState:
        with self.connect() as db:
            try:
                db.execute(
                    "INSERT INTO drafts VALUES (?,?)", (draft.draft_id, draft.model_dump_json())
                )
            except sqlite3.IntegrityError:
                raise FantasyError(
                    "DRAFT_EXISTS", "Load the existing draft or use a new ID."
                ) from None
            self._draft_snapshot(db, draft)
        return draft

    @staticmethod
    def _draft_snapshot(db: sqlite3.Connection, draft: DraftState) -> None:
        db.execute(
            "INSERT INTO snapshots(key,at,value,provenance) VALUES (?,?,?,?)",
            (
                f"draft:{draft.draft_id}",
                now().isoformat(),
                draft.model_dump_json(),
                Provenance(source="local_draft").model_dump_json(),
            ),
        )

    def load_draft(self, draft_id: str) -> DraftState:
        with self.connect() as db:
            row = db.execute("SELECT value FROM drafts WHERE id=?", (draft_id,)).fetchone()
        if not row:
            raise FantasyError(
                "DRAFT_NOT_INITIALIZED", "Call create_draft with known order and format."
            )
        return DraftState.model_validate_json(row[0])

    def mutate_draft(
        self, draft_id: str, expected_revision: int | None, mutation: Callable[[DraftState], None]
    ) -> DraftState:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT value FROM drafts WHERE id=?", (draft_id,)).fetchone()
            if not row:
                raise FantasyError("DRAFT_NOT_INITIALIZED", "Create or load a draft first.")
            draft = DraftState.model_validate_json(row[0])
            if expected_revision is not None and draft.revision != expected_revision:
                raise FantasyError("DRAFT_CONFLICT", "Draft changed; reload state and retry.")
            mutation(draft)
            draft.revision += 1
            db.execute("UPDATE drafts SET value=? WHERE id=?", (draft.model_dump_json(), draft_id))
            self._draft_snapshot(db, draft)
        return draft

    def prune(self, before: datetime) -> int:
        """Explicit maintenance; preserve current cache and all draft history."""
        with self.connect() as db:
            count = db.execute(
                "DELETE FROM snapshots WHERE at<? AND key NOT LIKE 'draft:%'", (before.isoformat(),)
            ).rowcount
        return int(count)
