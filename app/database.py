"""SQLite 异步数据访问层。"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import aiosqlite

DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS targets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    enabled INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    method TEXT,
    path TEXT,
    query TEXT,
    request_headers TEXT,
    request_body BLOB,
    response_status INTEGER,
    response_headers TEXT,
    response_body BLOB,
    duration_ms REAL,
    target_url TEXT,
    error TEXT
);

CREATE INDEX IF NOT EXISTS idx_requests_timestamp ON requests(timestamp);
CREATE INDEX IF NOT EXISTS idx_requests_status ON requests(response_status);
"""

DEFAULT_RETENTION_DAYS = 10


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn: Optional[aiosqlite.Connection] = None

    async def init(self) -> None:
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        self._conn = await aiosqlite.connect(self.db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(DB_SCHEMA)
        await self._conn.commit()
        # 默认保留天数
        if await self.get_setting("retention_days") is None:
            await self.set_setting("retention_days", str(DEFAULT_RETENTION_DAYS))

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        assert self._conn is not None, "Database not initialized"
        return self._conn

    # ---------- settings ----------
    async def get_setting(self, key: str) -> Optional[str]:
        cur = await self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = await cur.fetchone()
        return row["value"] if row else None

    async def set_setting(self, key: str, value: str) -> None:
        await self.conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await self.conn.commit()

    async def get_retention_days(self) -> int:
        v = await self.get_setting("retention_days")
        try:
            return int(v) if v is not None else DEFAULT_RETENTION_DAYS
        except (TypeError, ValueError):
            return DEFAULT_RETENTION_DAYS

    async def set_retention_days(self, days: int) -> None:
        await self.set_setting("retention_days", str(max(1, int(days))))

    # ---------- targets ----------
    async def list_targets(self) -> List[Dict[str, Any]]:
        cur = await self.conn.execute("SELECT * FROM targets ORDER BY id ASC")
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_active_target(self) -> Optional[Dict[str, Any]]:
        cur = await self.conn.execute("SELECT * FROM targets WHERE enabled = 1 LIMIT 1")
        row = await cur.fetchone()
        return dict(row) if row else None

    async def add_target(self, name: str, url: str) -> int:
        cur = await self.conn.execute(
            "INSERT INTO targets(name, url, enabled) VALUES(?, ?, 0)",
            (name, url),
        )
        await self.conn.commit()
        return cur.lastrowid

    async def update_target(self, target_id: int, name: str, url: str) -> None:
        await self.conn.execute(
            "UPDATE targets SET name = ?, url = ? WHERE id = ?",
            (name, url, target_id),
        )
        await self.conn.commit()

    async def delete_target(self, target_id: int) -> None:
        await self.conn.execute("DELETE FROM targets WHERE id = ?", (target_id,))
        await self.conn.commit()

    async def activate_target(self, target_id: int) -> None:
        """只允许一个 target 生效。"""
        async with self.conn.execute("BEGIN"):
            await self.conn.execute("UPDATE targets SET enabled = 0")
            await self.conn.execute("UPDATE targets SET enabled = 1 WHERE id = ?", (target_id,))
        await self.conn.commit()

    # ---------- request log ----------
    async def insert_request(
        self,
        method: str,
        path: str,
        query: str,
        request_headers: Dict[str, str],
        request_body: bytes,
        response_status: Optional[int],
        response_headers: Optional[Dict[str, str]],
        response_body: bytes,
        duration_ms: float,
        target_url: str,
        error: Optional[str] = None,
    ) -> int:
        cur = await self.conn.execute(
            """INSERT INTO requests
               (timestamp, method, path, query, request_headers, request_body,
                response_status, response_headers, response_body, duration_ms,
                target_url, error)
               VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                _now_iso(),
                method,
                path,
                query,
                json.dumps(request_headers, ensure_ascii=False),
                request_body,
                response_status,
                json.dumps(response_headers or {}, ensure_ascii=False),
                response_body,
                duration_ms,
                target_url,
                error,
            ),
        )
        await self.conn.commit()
        return cur.lastrowid

    async def list_requests(
        self,
        page: int = 1,
        size: int = 20,
        failed_only: bool = False,
    ) -> Tuple[List[Dict[str, Any]], int]:
        page = max(1, int(page))
        size = max(1, min(200, int(size)))
        offset = (page - 1) * size

        where = ""
        params: List[Any] = []
        if failed_only:
            where = "WHERE response_status IS NULL OR response_status >= 400 OR error IS NOT NULL"

        cur = await self.conn.execute(
            f"SELECT COUNT(*) AS cnt FROM requests {where}", params
        )
        total_row = await cur.fetchone()
        total = total_row["cnt"] if total_row else 0

        cur = await self.conn.execute(
            f"""SELECT id, timestamp, method, path, response_status, duration_ms,
                       target_url, error, length(response_body) AS resp_size
                FROM requests {where}
                ORDER BY timestamp DESC, id DESC
                LIMIT ? OFFSET ?""",
            params + [size, offset],
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows], total

    async def get_request(self, req_id: int) -> Optional[Dict[str, Any]]:
        cur = await self.conn.execute("SELECT * FROM requests WHERE id = ?", (req_id,))
        row = await cur.fetchone()
        if not row:
            return None
        d = dict(row)
        # 解析 JSON 字段
        for k in ("request_headers", "response_headers"):
            if d.get(k):
                try:
                    d[k] = json.loads(d[k])
                except (json.JSONDecodeError, TypeError):
                    pass
        # BLOB 转 bytes
        for k in ("request_body", "response_body"):
            if d.get(k) is not None and not isinstance(d[k], (bytes, bytearray)):
                d[k] = bytes(d[k])
        return d

    async def cleanup_expired(self) -> int:
        days = await self.get_retention_days()
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        cur = await self.conn.execute(
            "DELETE FROM requests WHERE timestamp < ?", (cutoff,)
        )
        await self.conn.commit()
        return cur.rowcount
