from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class MemeRecord:
    id: int
    file_path: str
    hash: str
    description: str
    tags: list[str]
    emotion: list[str]
    source_group_id: str
    created_at: str
    updated_at: str
    enabled: bool
    use_count: int
    pending_review: bool
    source_user_id: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "MemeRecord":
        return cls(
            id=int(row["id"]),
            file_path=row["file_path"],
            hash=row["hash"],
            description=row["description"] or "",
            tags=parse_json_list(row["tags"]),
            emotion=parse_json_list(row["emotion"]),
            source_group_id=row["source_group_id"] or "",
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            enabled=bool(row["enabled"]),
            use_count=int(row["use_count"]),
            pending_review=bool(row["pending_review"]),
            source_user_id=row["source_user_id"],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "file_path": self.file_path,
            "hash": self.hash,
            "description": self.description,
            "tags": self.tags,
            "emotion": self.emotion,
            "source_group_id": self.source_group_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "enabled": self.enabled,
            "use_count": self.use_count,
            "pending_review": self.pending_review,
            "source_user_id": self.source_user_id,
        }


def parse_json_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


class MemeDatabase:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._conn: sqlite3.Connection | None = None
        self._closed = True
        self._open()
        self.init_db()

    def close(self) -> None:
        with self._lock:
            if self._conn is not None and not self._closed:
                self._conn.close()
            self._closed = True

    def _open(self) -> sqlite3.Connection:
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._closed = False
        return self._conn

    def _conn_for_init(self) -> sqlite3.Connection:
        if self._conn is None or self._closed:
            return self._open()
        return self._conn

    def _conn_or_reopen(self) -> sqlite3.Connection:
        """热重载后旧监听器可能短暂访问已关闭连接，自动重开避免刷屏报错。"""
        if self._conn is None or self._closed:
            conn = self._open()
            self.init_db()
            return conn
        return self._conn

    def init_db(self) -> None:
        with self._lock:
            conn = self._conn_for_init()
            with conn:
                conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_path TEXT NOT NULL,
                    hash TEXT NOT NULL UNIQUE,
                    description TEXT DEFAULT '',
                    tags TEXT DEFAULT '[]',
                    emotion TEXT DEFAULT '[]',
                    source_group_id TEXT DEFAULT '',
                    source_user_id TEXT DEFAULT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    use_count INTEGER NOT NULL DEFAULT 0,
                    pending_review INTEGER NOT NULL DEFAULT 0
                )
                """
                )
                conn.execute(
                """
                CREATE TABLE IF NOT EXISTS group_settings (
                    group_id TEXT PRIMARY KEY,
                    auto_reply_enabled INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
                )
                conn.execute("CREATE INDEX IF NOT EXISTS idx_memes_hash ON memes(hash)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_memes_created ON memes(created_at)")
                self._migrate()

    def _migrate(self) -> None:
        conn = self._conn_for_init()
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(memes)").fetchall()
        }
        if "source_user_id" not in columns:
            conn.execute("ALTER TABLE memes ADD COLUMN source_user_id TEXT DEFAULT NULL")

    def create_meme(
        self,
        *,
        file_path: str,
        hash_value: str,
        description: str,
        tags: list[str],
        emotion: list[str],
        source_group_id: str,
        source_user_id: str | None,
        pending_review: bool,
        enabled: bool = True,
    ) -> MemeRecord:
        now = utc_now_iso()
        with self._lock:
            conn = self._conn_or_reopen()
            with conn:
                cursor = conn.execute(
                """
                INSERT INTO memes (
                    file_path, hash, description, tags, emotion, source_group_id,
                    source_user_id, created_at, updated_at, enabled, pending_review
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    file_path,
                    hash_value,
                    description,
                    json.dumps(tags, ensure_ascii=False),
                    json.dumps(emotion, ensure_ascii=False),
                    source_group_id,
                    source_user_id,
                    now,
                    now,
                    int(enabled),
                    int(pending_review),
                ),
                )
            return self.get_meme(int(cursor.lastrowid))  # type: ignore[return-value]

    def get_meme(self, meme_id: int) -> MemeRecord | None:
        with self._lock:
            conn = self._conn_or_reopen()
            row = conn.execute("SELECT * FROM memes WHERE id = ?", (meme_id,)).fetchone()
        return MemeRecord.from_row(row) if row else None

    def find_by_hash(self, hash_value: str) -> MemeRecord | None:
        with self._lock:
            conn = self._conn_or_reopen()
            row = conn.execute("SELECT * FROM memes WHERE hash = ?", (hash_value,)).fetchone()
        return MemeRecord.from_row(row) if row else None

    def list_memes(
        self,
        *,
        limit: int = 20,
        offset: int = 0,
        query: str = "",
        pending: bool | None = None,
        enabled: bool | None = None,
    ) -> list[MemeRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if query:
            like = f"%{query}%"
            clauses.append("(description LIKE ? OR tags LIKE ? OR emotion LIKE ? OR hash LIKE ?)")
            params.extend([like, like, like, like])
        if pending is not None:
            clauses.append("pending_review = ?")
            params.append(int(pending))
        if enabled is not None:
            clauses.append("enabled = ?")
            params.append(int(enabled))

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM memes {where} ORDER BY id DESC LIMIT ? OFFSET ?"
        params.extend([int(limit), int(offset)])
        with self._lock:
            conn = self._conn_or_reopen()
            rows = conn.execute(sql, params).fetchall()
        return [MemeRecord.from_row(row) for row in rows]

    def list_enabled(self, *, limit: int = 500) -> list[MemeRecord]:
        with self._lock:
            conn = self._conn_or_reopen()
            rows = conn.execute(
                "SELECT * FROM memes WHERE enabled = 1 AND pending_review = 0 ORDER BY use_count ASC, id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [MemeRecord.from_row(row) for row in rows]

    def update_meme(
        self,
        meme_id: int,
        *,
        description: str | None = None,
        tags: list[str] | None = None,
        emotion: list[str] | None = None,
        enabled: bool | None = None,
        pending_review: bool | None = None,
    ) -> MemeRecord | None:
        assignments: list[str] = []
        params: list[Any] = []
        if description is not None:
            assignments.append("description = ?")
            params.append(description)
        if tags is not None:
            assignments.append("tags = ?")
            params.append(json.dumps(tags, ensure_ascii=False))
        if emotion is not None:
            assignments.append("emotion = ?")
            params.append(json.dumps(emotion, ensure_ascii=False))
        if enabled is not None:
            assignments.append("enabled = ?")
            params.append(int(enabled))
        if pending_review is not None:
            assignments.append("pending_review = ?")
            params.append(int(pending_review))
        if not assignments:
            return self.get_meme(meme_id)

        assignments.append("updated_at = ?")
        params.append(utc_now_iso())
        params.append(int(meme_id))
        with self._lock:
            conn = self._conn_or_reopen()
            with conn:
                conn.execute(
                    f"UPDATE memes SET {', '.join(assignments)} WHERE id = ?",
                    params,
                )
        return self.get_meme(meme_id)

    def increment_use_count(self, meme_id: int) -> None:
        with self._lock:
            conn = self._conn_or_reopen()
            with conn:
                conn.execute(
                    "UPDATE memes SET use_count = use_count + 1, updated_at = ? WHERE id = ?",
                    (utc_now_iso(), int(meme_id)),
                )

    def delete_meme(self, meme_id: int, *, delete_file: bool = True) -> bool:
        record = self.get_meme(meme_id)
        if not record:
            return False
        with self._lock:
            conn = self._conn_or_reopen()
            with conn:
                conn.execute("DELETE FROM memes WHERE id = ?", (int(meme_id),))
        if delete_file:
            try:
                Path(record.file_path).unlink(missing_ok=True)
            except OSError:
                pass
        return True

    def set_group_auto_reply(self, group_id: str, enabled: bool) -> None:
        now = utc_now_iso()
        with self._lock:
            conn = self._conn_or_reopen()
            with conn:
                conn.execute(
                """
                INSERT INTO group_settings (group_id, auto_reply_enabled, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(group_id) DO UPDATE SET
                    auto_reply_enabled = excluded.auto_reply_enabled,
                    updated_at = excluded.updated_at
                """,
                (str(group_id), int(enabled), now),
                )

    def get_group_auto_reply(self, group_id: str) -> bool | None:
        with self._lock:
            conn = self._conn_or_reopen()
            row = conn.execute(
                "SELECT auto_reply_enabled FROM group_settings WHERE group_id = ?",
                (str(group_id),),
            ).fetchone()
        if row is None:
            return None
        return bool(row["auto_reply_enabled"])

    def count_saved_today(self) -> int:
        today = datetime.now(timezone.utc).date().isoformat()
        with self._lock:
            conn = self._conn_or_reopen()
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM memes WHERE substr(created_at, 1, 10) = ?",
                (today,),
            ).fetchone()
        return int(row["c"])

    def stats(self) -> dict[str, int]:
        with self._lock:
            conn = self._conn_or_reopen()
            row = conn.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN enabled = 1 THEN 1 ELSE 0 END) AS enabled,
                    SUM(CASE WHEN pending_review = 1 THEN 1 ELSE 0 END) AS pending,
                    SUM(use_count) AS use_count
                FROM memes
                """
            ).fetchone()
        return {
            "total": int(row["total"] or 0),
            "enabled": int(row["enabled"] or 0),
            "pending_review": int(row["pending"] or 0),
            "use_count": int(row["use_count"] or 0),
            "saved_today": self.count_saved_today(),
        }
