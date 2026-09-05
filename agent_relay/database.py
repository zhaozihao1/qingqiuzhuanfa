from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


def _hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${digest.hex()}"


def _verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, salt_hex, expected_hex = encoded.split("$", 2)
        if algorithm != "scrypt":
            return False
        actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1, dklen=32)
        return hmac.compare_digest(actual, bytes.fromhex(expected_hex))
    except (ValueError, TypeError):
        return False


def _access_key_hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


class Database:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.body_dir = data_dir / "bodies"
        self.path = data_dir / "relay.db"
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.body_dir.mkdir(parents=True, exist_ok=True)
        await self._run(self._initialize_sync)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _initialize_sync(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS base_urls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    url TEXT NOT NULL UNIQUE,
                    active INTEGER NOT NULL DEFAULT 0 CHECK(active IN (0, 1)),
                    auto_replace_key INTEGER NOT NULL DEFAULT 0 CHECK(auto_replace_key IN (0, 1)),
                    api_key TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_base_url
                    ON base_urls(active) WHERE active = 1;

                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS request_logs (
                    id TEXT PRIMARY KEY,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    method TEXT NOT NULL,
                    incoming_url TEXT NOT NULL,
                    upstream_url TEXT,
                    request_headers TEXT NOT NULL,
                    response_headers TEXT,
                    request_body_path TEXT NOT NULL,
                    response_body_path TEXT NOT NULL,
                    request_bytes INTEGER NOT NULL DEFAULT 0,
                    response_bytes INTEGER NOT NULL DEFAULT 0,
                    status INTEGER,
                    duration_ms INTEGER,
                    client_ip TEXT,
                    error TEXT,
                    failed INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS request_logs_started_at
                    ON request_logs(started_at DESC);
                CREATE INDEX IF NOT EXISTS request_logs_failed_started_at
                    ON request_logs(failed, started_at DESC);

                CREATE TABLE IF NOT EXISTS access_keys (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    key_hash TEXT NOT NULL UNIQUE,
                    key_prefix TEXT NOT NULL,
                    key_suffix TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS denied_request_logs (
                    id TEXT PRIMARY KEY,
                    started_at TEXT NOT NULL,
                    method TEXT NOT NULL,
                    incoming_url TEXT NOT NULL,
                    request_headers TEXT NOT NULL,
                    client_ip TEXT,
                    request_body_path TEXT NOT NULL DEFAULT '',
                    request_bytes INTEGER NOT NULL DEFAULT 0,
                    body_truncated INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS denied_request_logs_started_at
                    ON denied_request_logs(started_at DESC);
                """
            )
            db.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES('retention_days', '10')"
            )
            db.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES('https_enabled', 'false')"
            )
            db.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES('access_key_enabled', 'false')"
            )
            db.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES('admin_password_hash', ?)",
                (_hash_password(os.getenv("ADMIN_PASSWORD", "admin")),),
            )
            columns = {row["name"] for row in db.execute("PRAGMA table_info(base_urls)")}
            if "auto_replace_key" not in columns:
                db.execute("ALTER TABLE base_urls ADD COLUMN auto_replace_key INTEGER NOT NULL DEFAULT 0")
            if "api_key" not in columns:
                db.execute("ALTER TABLE base_urls ADD COLUMN api_key TEXT NOT NULL DEFAULT ''")
            denied_columns = {row["name"] for row in db.execute("PRAGMA table_info(denied_request_logs)")}
            if "request_body_path" not in denied_columns:
                db.execute("ALTER TABLE denied_request_logs ADD COLUMN request_body_path TEXT NOT NULL DEFAULT ''")
            if "request_bytes" not in denied_columns:
                db.execute("ALTER TABLE denied_request_logs ADD COLUMN request_bytes INTEGER NOT NULL DEFAULT 0")
            if "body_truncated" not in denied_columns:
                db.execute("ALTER TABLE denied_request_logs ADD COLUMN body_truncated INTEGER NOT NULL DEFAULT 0")

    async def _run(self, function: Any, *args: Any) -> Any:
        async with self._lock:
            return await asyncio.to_thread(function, *args)

    async def get_settings(self) -> dict[str, str]:
        def query() -> dict[str, str]:
            with self._connect() as db:
                return {row["key"]: row["value"] for row in db.execute("SELECT key, value FROM settings")}

        return await self._run(query)

    async def set_settings(self, values: dict[str, str]) -> None:
        def update() -> None:
            with self._connect() as db:
                db.executemany(
                    "INSERT INTO settings(key, value) VALUES(?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    values.items(),
                )

        await self._run(update)

    async def verify_admin_password(self, password: str) -> bool:
        settings = await self.get_settings()
        return _verify_password(password, settings.get("admin_password_hash", ""))

    async def change_admin_password(self, old_password: str, new_password: str) -> bool:
        if not await self.verify_admin_password(old_password):
            return False
        await self.set_settings({"admin_password_hash": _hash_password(new_password)})
        return True

    async def reset_platform(self) -> None:
        def reset() -> tuple[list[tuple[str, str]], list[str]]:
            with self._connect() as db:
                paths = [(row[0], row[1]) for row in db.execute(
                    "SELECT request_body_path, response_body_path FROM request_logs"
                )]
                db.execute("DELETE FROM request_logs")
                denied_paths = [row[0] for row in db.execute(
                    "SELECT request_body_path FROM denied_request_logs WHERE request_body_path != ''"
                )]
                db.execute("DELETE FROM denied_request_logs")
                db.execute("DELETE FROM access_keys")
                db.execute("DELETE FROM base_urls")
                db.execute("DELETE FROM settings")
                db.executemany(
                    "INSERT INTO settings(key, value) VALUES(?, ?)",
                    [
                        ("retention_days", "10"),
                        ("https_enabled", "false"),
                        ("access_key_enabled", "false"),
                        ("admin_password_hash", _hash_password("admin")),
                    ],
                )
                return paths, denied_paths

        paths, denied_paths = await self._run(reset)
        for pair in paths:
            for relative_path in pair:
                try:
                    (self.data_dir / relative_path).unlink(missing_ok=True)
                except OSError:
                    pass
        for relative_path in denied_paths:
            try:
                (self.data_dir / relative_path).unlink(missing_ok=True)
            except OSError:
                pass
        for name in ("certificate.pem", "private-key.pem"):
            try:
                (self.data_dir / name).unlink(missing_ok=True)
            except OSError:
                pass

    async def list_access_keys(self) -> list[dict[str, Any]]:
        def query() -> list[dict[str, Any]]:
            with self._connect() as db:
                return [dict(row) for row in db.execute(
                    "SELECT id, name, key_prefix, key_suffix, created_at FROM access_keys ORDER BY id"
                )]
        return await self._run(query)

    async def add_access_key(self, name: str, key: str) -> dict[str, Any]:
        def insert() -> dict[str, Any]:
            with self._connect() as db:
                cursor = db.execute(
                    "INSERT INTO access_keys(name, key_hash, key_prefix, key_suffix, created_at) VALUES(?, ?, ?, ?, ?)",
                    (name, _access_key_hash(key), key[:4], key[-4:], datetime.now(UTC).isoformat()),
                )
                row = db.execute(
                    "SELECT id, name, key_prefix, key_suffix, created_at FROM access_keys WHERE id=?",
                    (cursor.lastrowid,),
                ).fetchone()
                return dict(row)
        return await self._run(insert)

    async def delete_access_key(self, item_id: int) -> bool:
        def delete() -> bool:
            with self._connect() as db:
                return db.execute("DELETE FROM access_keys WHERE id=?", (item_id,)).rowcount > 0
        return await self._run(delete)

    async def access_key_allowed(self, key: str) -> bool:
        candidate = _access_key_hash(key)
        def query() -> bool:
            with self._connect() as db:
                hashes = [row[0] for row in db.execute("SELECT key_hash FROM access_keys")]
                return any(hmac.compare_digest(candidate, value) for value in hashes)
        return await self._run(query)

    async def access_key_required(self) -> bool:
        settings = await self.get_settings()
        return settings.get("access_key_enabled", "false") == "true"

    async def add_denied_log(self, values: dict[str, Any]) -> None:
        def insert() -> None:
            with self._connect() as db:
                db.execute(
                    "INSERT INTO denied_request_logs(id, started_at, method, incoming_url, request_headers, client_ip, request_body_path, request_bytes, body_truncated) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (values["id"], values["started_at"], values["method"], values["incoming_url"],
                     json.dumps(values["request_headers"], ensure_ascii=False), values.get("client_ip"),
                     values.get("request_body_path", ""), values.get("request_bytes", 0), int(values.get("body_truncated", False))),
                )
        await self._run(insert)

    async def list_denied_logs(self, *, sort: str, page: int, page_size: int) -> dict[str, Any]:
        def query() -> dict[str, Any]:
            direction = "ASC" if sort == "asc" else "DESC"
            offset = (page - 1) * page_size
            with self._connect() as db:
                total = int(db.execute("SELECT COUNT(*) FROM denied_request_logs").fetchone()[0])
                rows = db.execute(
                    f"SELECT id, started_at, method, incoming_url, client_ip, request_bytes, body_truncated FROM denied_request_logs ORDER BY started_at {direction} LIMIT ? OFFSET ?",
                    (page_size, offset),
                ).fetchall()
                return {"items": [dict(row) for row in rows], "total": total, "page": page, "page_size": page_size}
        return await self._run(query)

    async def get_denied_log(self, log_id: str) -> dict[str, Any] | None:
        def query() -> dict[str, Any] | None:
            with self._connect() as db:
                row = db.execute("SELECT * FROM denied_request_logs WHERE id=?", (log_id,)).fetchone()
                if not row:
                    return None
                result = dict(row)
                result["request_headers"] = json.loads(result["request_headers"] or "[]")
                return result
        return await self._run(query)

    async def delete_all_denied_logs(self) -> int:
        def delete() -> tuple[int, list[str]]:
            with self._connect() as db:
                count = int(db.execute("SELECT COUNT(*) FROM denied_request_logs").fetchone()[0])
                paths = [row[0] for row in db.execute(
                    "SELECT request_body_path FROM denied_request_logs WHERE request_body_path != ''"
                )]
                db.execute("DELETE FROM denied_request_logs")
                return count, paths
        count, paths = await self._run(delete)
        for relative_path in paths:
            try:
                (self.data_dir / relative_path).unlink(missing_ok=True)
            except OSError:
                pass
        return count

    async def list_base_urls(self) -> list[dict[str, Any]]:
        def query() -> list[dict[str, Any]]:
            with self._connect() as db:
                return [dict(row) for row in db.execute("SELECT * FROM base_urls ORDER BY id")]

        return await self._run(query)

    async def get_active_base_url(self) -> dict[str, Any] | None:
        def query() -> dict[str, Any] | None:
            with self._connect() as db:
                row = db.execute("SELECT * FROM base_urls WHERE active = 1").fetchone()
                return dict(row) if row else None

        return await self._run(query)

    async def add_base_url(self, name: str, url: str, auto_replace_key: bool = False, api_key: str = "") -> dict[str, Any]:
        def insert() -> dict[str, Any]:
            with self._connect() as db:
                has_active = db.execute("SELECT 1 FROM base_urls WHERE active = 1").fetchone()
                cursor = db.execute(
                    "INSERT INTO base_urls(name, url, active, auto_replace_key, api_key, created_at) VALUES(?, ?, ?, ?, ?, ?)",
                    (name, url, 0 if has_active else 1, int(auto_replace_key), api_key, datetime.now(UTC).isoformat()),
                )
                row = db.execute("SELECT * FROM base_urls WHERE id = ?", (cursor.lastrowid,)).fetchone()
                return dict(row)

        return await self._run(insert)

    async def update_base_url(self, item_id: int, name: str, url: str, auto_replace_key: bool, api_key: str | None = None) -> bool:
        def update() -> bool:
            with self._connect() as db:
                if api_key is None:
                    cursor = db.execute("UPDATE base_urls SET name=?, url=?, auto_replace_key=? WHERE id=?", (name, url, int(auto_replace_key), item_id))
                else:
                    cursor = db.execute("UPDATE base_urls SET name=?, url=?, auto_replace_key=?, api_key=? WHERE id=?", (name, url, int(auto_replace_key), api_key, item_id))
                return cursor.rowcount > 0
        return await self._run(update)

    async def delete_base_url(self, item_id: int) -> bool:
        def delete() -> bool:
            with self._connect() as db:
                row = db.execute("SELECT active FROM base_urls WHERE id = ?", (item_id,)).fetchone()
                if not row:
                    return False
                db.execute("DELETE FROM base_urls WHERE id = ?", (item_id,))
                if row["active"]:
                    replacement = db.execute("SELECT id FROM base_urls ORDER BY id LIMIT 1").fetchone()
                    if replacement:
                        db.execute("UPDATE base_urls SET active = 1 WHERE id = ?", (replacement["id"],))
                return True

        return await self._run(delete)

    async def activate_base_url(self, item_id: int) -> bool:
        def activate() -> bool:
            with self._connect() as db:
                if not db.execute("SELECT 1 FROM base_urls WHERE id = ?", (item_id,)).fetchone():
                    return False
                db.execute("UPDATE base_urls SET active = 0 WHERE active = 1")
                db.execute("UPDATE base_urls SET active = 1 WHERE id = ?", (item_id,))
                return True

        return await self._run(activate)

    async def start_log(self, values: dict[str, Any]) -> None:
        def insert() -> None:
            with self._connect() as db:
                db.execute(
                    """INSERT INTO request_logs(
                        id, started_at, method, incoming_url, upstream_url,
                        request_headers, request_body_path, response_body_path, client_ip
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        values["id"], values["started_at"], values["method"],
                        values["incoming_url"], values.get("upstream_url"),
                        json.dumps(values["request_headers"], ensure_ascii=False),
                        values["request_body_path"], values["response_body_path"],
                        values.get("client_ip"),
                    ),
                )

        await self._run(insert)

    async def finish_log(self, log_id: str, values: dict[str, Any]) -> None:
        def update() -> None:
            with self._connect() as db:
                db.execute(
                    """UPDATE request_logs SET
                        completed_at = ?, response_headers = ?, request_bytes = ?,
                        response_bytes = ?, status = ?, duration_ms = ?, error = ?, failed = ?
                    WHERE id = ?""",
                    (
                        values["completed_at"],
                        json.dumps(values.get("response_headers", []), ensure_ascii=False),
                        values["request_bytes"], values["response_bytes"], values.get("status"),
                        values["duration_ms"], values.get("error"), values["failed"], log_id,
                    ),
                )

        await self._run(update)

    async def list_logs(
        self, *, failed_only: bool, sort: str, page: int, page_size: int
    ) -> dict[str, Any]:
        def query() -> dict[str, Any]:
            where = "WHERE failed = 1" if failed_only else ""
            direction = "ASC" if sort == "asc" else "DESC"
            offset = (page - 1) * page_size
            with self._connect() as db:
                total = int(db.execute(f"SELECT COUNT(*) FROM request_logs {where}").fetchone()[0])
                rows = db.execute(
                    f"""SELECT id, started_at, completed_at, method, incoming_url, upstream_url,
                               request_bytes, response_bytes, status, duration_ms, client_ip, error, failed
                        FROM request_logs {where}
                        ORDER BY started_at {direction} LIMIT ? OFFSET ?""",
                    (page_size, offset),
                ).fetchall()
                return {"items": [dict(row) for row in rows], "total": total, "page": page, "page_size": page_size}

        return await self._run(query)

    async def get_log(self, log_id: str) -> dict[str, Any] | None:
        def query() -> dict[str, Any] | None:
            with self._connect() as db:
                row = db.execute("SELECT * FROM request_logs WHERE id = ?", (log_id,)).fetchone()
                if not row:
                    return None
                result = dict(row)
                result["request_headers"] = json.loads(result["request_headers"] or "[]")
                result["response_headers"] = json.loads(result["response_headers"] or "[]")
                return result

        return await self._run(query)

    async def delete_all_logs(self) -> int:
        def delete() -> tuple[int, list[tuple[str, str]]]:
            with self._connect() as db:
                rows = db.execute(
                    "SELECT request_body_path, response_body_path FROM request_logs"
                ).fetchall()
                db.execute("DELETE FROM request_logs")
                return len(rows), [(row[0], row[1]) for row in rows]

        count, paths = await self._run(delete)
        for pair in paths:
            for relative_path in pair:
                try:
                    (self.data_dir / relative_path).unlink(missing_ok=True)
                except OSError:
                    pass
        return count

    async def cleanup_expired(self, retention_days: int) -> int:
        cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).isoformat()

        def cleanup() -> tuple[int, list[tuple[str, str]], list[str]]:
            with self._connect() as db:
                rows = db.execute(
                    "SELECT request_body_path, response_body_path FROM request_logs WHERE started_at < ?",
                    (cutoff,),
                ).fetchall()
                db.execute("DELETE FROM request_logs WHERE started_at < ?", (cutoff,))
                denied_paths = [row[0] for row in db.execute(
                    "SELECT request_body_path FROM denied_request_logs WHERE started_at < ? AND request_body_path != ''",
                    (cutoff,),
                )]
                db.execute("DELETE FROM denied_request_logs WHERE started_at < ?", (cutoff,))
                return len(rows), [(row[0], row[1]) for row in rows], denied_paths

        count, paths, denied_paths = await self._run(cleanup)
        for pair in paths:
            for relative_path in pair:
                try:
                    (self.data_dir / relative_path).unlink(missing_ok=True)
                except OSError:
                    pass
        for relative_path in denied_paths:
            try:
                (self.data_dir / relative_path).unlink(missing_ok=True)
            except OSError:
                pass
        return count
