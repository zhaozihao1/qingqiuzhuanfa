from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import shlex
import sqlite3
import ssl
import subprocess
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

from aiohttp import web

from .database import Database

SESSION_COOKIE = "relay_admin_session"
PEM_CERTIFICATE = re.compile(r"-----BEGIN CERTIFICATE-----[\s\S]+-----END CERTIFICATE-----")
PEM_PRIVATE_KEY = re.compile(r"-----BEGIN (?:RSA |EC |ENCRYPTED )?PRIVATE KEY-----[\s\S]+-----END (?:RSA |EC |ENCRYPTED )?PRIVATE KEY-----")


class Auth:
    def __init__(self, data_dir: Path) -> None:
        self.username = os.getenv("ADMIN_USERNAME", "admin")
        self.password = os.getenv("ADMIN_PASSWORD", "admin")
        secret_path = data_dir / "session.secret"
        if not secret_path.exists():
            secret_path.write_bytes(secrets.token_bytes(32))
            try:
                secret_path.chmod(0o600)
            except OSError:
                pass
        self.secret = secret_path.read_bytes()
        self.attempts: dict[str, deque[float]] = defaultdict(deque)

    def create_token(self) -> tuple[str, str]:
        csrf = secrets.token_urlsafe(24)
        payload = json.dumps(
            {"user": self.username, "exp": int(time.time()) + 12 * 60 * 60, "csrf": csrf},
            separators=(",", ":"),
        ).encode()
        encoded = base64.urlsafe_b64encode(payload).rstrip(b"=")
        signature = hmac.new(self.secret, encoded, hashlib.sha256).digest()
        return f"{encoded.decode()}.{base64.urlsafe_b64encode(signature).decode()}", csrf

    def verify(self, token: str | None) -> dict[str, Any] | None:
        if not token or "." not in token:
            return None
        encoded_text, signature_text = token.split(".", 1)
        try:
            encoded = encoded_text.encode()
            signature = base64.urlsafe_b64decode(signature_text)
            expected = hmac.new(self.secret, encoded, hashlib.sha256).digest()
            if not hmac.compare_digest(signature, expected):
                return None
            payload = json.loads(base64.urlsafe_b64decode(encoded + b"=" * (-len(encoded) % 4)))
            if payload.get("exp", 0) < time.time() or payload.get("user") != self.username:
                return None
            return payload
        except (ValueError, TypeError, json.JSONDecodeError):
            return None

    def login_allowed(self, client_ip: str) -> bool:
        now = time.time()
        queue = self.attempts[client_ip]
        while queue and queue[0] < now - 300:
            queue.popleft()
        return len(queue) < 10

    def record_failure(self, client_ip: str) -> None:
        self.attempts[client_ip].append(time.time())


class AdminServer:
    def __init__(
        self,
        database: Database,
        static_dir: Path,
        reload_tls: Callable[[], Awaitable[None]],
    ) -> None:
        self.database = database
        self.static_dir = static_dir
        self.reload_tls = reload_tls
        self.auth = Auth(database.data_dir)

    @web.middleware
    async def auth_middleware(self, request: web.Request, handler: Callable[..., Awaitable[web.StreamResponse]]) -> web.StreamResponse:
        public = {"/api/health", "/api/login"}
        if request.path in public or not request.path.startswith("/api/"):
            return await handler(request)
        session = self.auth.verify(request.cookies.get(SESSION_COOKIE))
        if not session:
            raise web.HTTPUnauthorized(text=json.dumps({"error": "Unauthorized"}), content_type="application/json")
        request["session"] = session
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if not hmac.compare_digest(request.headers.get("X-CSRF-Token", ""), session["csrf"]):
                raise web.HTTPForbidden(text=json.dumps({"error": "Invalid CSRF token"}), content_type="application/json")
        return await handler(request)

    def create_app(self) -> web.Application:
        app = web.Application(middlewares=[self.auth_middleware], client_max_size=10 * 1024 * 1024)
        app.add_routes(
            [
                web.get("/", self.index),
                web.get("/api/health", self.health),
                web.post("/api/login", self.login),
                web.post("/api/logout", self.logout),
                web.get("/api/session", self.session),
                web.get("/api/config", self.get_config),
                web.post("/api/base-urls", self.add_base_url),
                web.post(r"/api/base-urls/{item_id:\d+}/activate", self.activate_base_url),
                web.delete(r"/api/base-urls/{item_id:\d+}", self.delete_base_url),
                web.put("/api/settings/retention", self.set_retention),
                web.put("/api/settings/tls", self.set_tls),
                web.get("/api/update/check", self.check_update),
                web.post("/api/update/apply", self.apply_update),
                web.get("/api/logs", self.list_logs),
                web.delete("/api/logs", self.delete_all_logs),
                web.get(r"/api/logs/{log_id:[a-f0-9]{32}}", self.get_log),
                web.get(r"/api/logs/{log_id:[a-f0-9]{32}}/{kind:request|response}-body", self.get_body),
            ]
        )
        return app

    async def index(self, request: web.Request) -> web.FileResponse:
        return web.FileResponse(self.static_dir / "index.html")

    async def health(self, request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def check_update(self, request: web.Request) -> web.Response:
        """Report the configured update command without executing it."""
        command = os.getenv("RELAY_UPDATE_COMMAND", "").strip()
        return web.json_response({
            "configured": bool(command),
            "message": (
                "Update command is configured."
                if command else
                "Set RELAY_UPDATE_COMMAND on the server to enable one-click updates."
            ),
        })

    async def apply_update(self, request: web.Request) -> web.Response:
        command = os.getenv("RELAY_UPDATE_COMMAND", "").strip()
        if not command:
            raise web.HTTPConflict(
                text=json.dumps({"error": "RELAY_UPDATE_COMMAND is not configured"}),
                content_type="application/json",
            )
        try:
            args = shlex.split(command)
            result = await asyncio.to_thread(
                subprocess.run, args, capture_output=True, text=True, timeout=300, check=False
            )
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            raise web.HTTPBadGateway(
                text=json.dumps({"error": f"Update command failed: {exc}"}),
                content_type="application/json",
            )
        output = (result.stdout + result.stderr).strip()[-4000:]
        if result.returncode != 0:
            raise web.HTTPBadGateway(
                text=json.dumps({"error": "Update command returned a failure", "output": output}),
                content_type="application/json",
            )
        return web.json_response({"ok": True, "output": output})

    async def login(self, request: web.Request) -> web.Response:
        client_ip = request.remote or "unknown"
        if not self.auth.login_allowed(client_ip):
            raise web.HTTPTooManyRequests(text=json.dumps({"error": "Too many login attempts"}), content_type="application/json")
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            raise web.HTTPBadRequest(text=json.dumps({"error": "Invalid JSON"}), content_type="application/json")
        username_ok = hmac.compare_digest(str(body.get("username", "")), self.auth.username)
        password_ok = hmac.compare_digest(str(body.get("password", "")), self.auth.password)
        if not (username_ok and password_ok):
            self.auth.record_failure(client_ip)
            raise web.HTTPUnauthorized(text=json.dumps({"error": "Invalid username or password"}), content_type="application/json")
        token, csrf = self.auth.create_token()
        response = web.json_response({"username": self.auth.username, "csrf": csrf})
        response.set_cookie(
            SESSION_COOKIE,
            token,
            httponly=True,
            secure=request.secure,
            samesite="Strict",
            max_age=12 * 60 * 60,
            path="/",
        )
        return response

    async def logout(self, request: web.Request) -> web.Response:
        response = web.json_response({"ok": True})
        response.del_cookie(SESSION_COOKIE, path="/")
        return response

    async def session(self, request: web.Request) -> web.Response:
        session = request["session"]
        return web.json_response({"username": session["user"], "csrf": session["csrf"]})

    async def get_config(self, request: web.Request) -> web.Response:
        settings = await self.database.get_settings()
        certificate_path = self.database.data_dir / "certificate.pem"
        private_key_path = self.database.data_dir / "private-key.pem"
        certificate = certificate_path.read_text(encoding="utf-8") if certificate_path.exists() else ""
        private_key = private_key_path.read_text(encoding="utf-8") if private_key_path.exists() else ""
        return web.json_response(
            {
                "base_urls": await self.database.list_base_urls(),
                "retention_days": int(settings.get("retention_days", "10")),
                "https_enabled": settings.get("https_enabled") == "true",
                "certificate_configured": (self.database.data_dir / "certificate.pem").exists(),
                "certificate": certificate,
                "private_key": private_key,
                "default_credentials": self.auth.username == "admin" and self.auth.password == "admin",
            }
        )

    async def add_base_url(self, request: web.Request) -> web.Response:
        body = await request.json()
        name = str(body.get("name", "")).strip()
        url = str(body.get("url", "")).strip().rstrip("/")
        parsed = urlsplit(url)
        if not name or len(name) > 100:
            raise web.HTTPBadRequest(text=json.dumps({"error": "Name is required (max 100 characters)"}), content_type="application/json")
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.query or parsed.fragment or parsed.username:
            raise web.HTTPBadRequest(text=json.dumps({"error": "A valid HTTP/HTTPS base URL without credentials, query, or fragment is required"}), content_type="application/json")
        try:
            item = await self.database.add_base_url(name, url)
        except sqlite3.IntegrityError:
            raise web.HTTPConflict(text=json.dumps({"error": "Base URL already exists"}), content_type="application/json")
        return web.json_response(item, status=201)

    async def activate_base_url(self, request: web.Request) -> web.Response:
        if not await self.database.activate_base_url(int(request.match_info["item_id"])):
            raise web.HTTPNotFound()
        return web.json_response({"ok": True})

    async def delete_base_url(self, request: web.Request) -> web.Response:
        if not await self.database.delete_base_url(int(request.match_info["item_id"])):
            raise web.HTTPNotFound()
        return web.json_response({"ok": True})

    async def set_retention(self, request: web.Request) -> web.Response:
        body = await request.json()
        try:
            days = int(body.get("days"))
        except (TypeError, ValueError):
            days = 0
        if not 1 <= days <= 3650:
            raise web.HTTPBadRequest(text=json.dumps({"error": "Retention must be between 1 and 3650 days"}), content_type="application/json")
        await self.database.set_settings({"retention_days": str(days)})
        deleted = await self.database.cleanup_expired(days)
        return web.json_response({"ok": True, "deleted": deleted})

    async def set_tls(self, request: web.Request) -> web.Response:
        body = await request.json()
        enabled = bool(body.get("enabled"))
        cert_path = self.database.data_dir / "certificate.pem"
        key_path = self.database.data_dir / "private-key.pem"
        if enabled:
            certificate = str(body.get("certificate", "")).strip()
            private_key = str(body.get("private_key", "")).strip()
            if certificate or private_key:
                if not PEM_CERTIFICATE.fullmatch(certificate) or not PEM_PRIVATE_KEY.fullmatch(private_key):
                    raise web.HTTPBadRequest(text=json.dumps({"error": "Invalid PEM certificate or private key"}), content_type="application/json")
                temporary_cert = self.database.data_dir / ".certificate.pem.tmp"
                temporary_key = self.database.data_dir / ".private-key.pem.tmp"
                temporary_cert.write_text(certificate + "\n", encoding="utf-8")
                temporary_key.write_text(private_key + "\n", encoding="utf-8")
                try:
                    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
                    context.load_cert_chain(temporary_cert, temporary_key)
                    temporary_cert.replace(cert_path)
                    temporary_key.replace(key_path)
                    try:
                        key_path.chmod(0o600)
                    except OSError:
                        pass
                except (ssl.SSLError, OSError) as exc:
                    temporary_cert.unlink(missing_ok=True)
                    temporary_key.unlink(missing_ok=True)
                    raise web.HTTPBadRequest(text=json.dumps({"error": f"Certificate validation failed: {exc}"}), content_type="application/json")
            elif not cert_path.exists() or not key_path.exists():
                raise web.HTTPBadRequest(text=json.dumps({"error": "Certificate and private key are required"}), content_type="application/json")
        await self.database.set_settings({"https_enabled": "true" if enabled else "false"})
        loop = asyncio.get_running_loop()
        loop.call_later(0.5, lambda: loop.create_task(self.reload_tls()))
        return web.json_response({"ok": True, "reconnecting": True})

    async def list_logs(self, request: web.Request) -> web.Response:
        failed_only = request.query.get("failed") == "true"
        sort = "asc" if request.query.get("sort") == "asc" else "desc"
        try:
            page = max(1, int(request.query.get("page", "1")))
            page_size = min(100, max(1, int(request.query.get("page_size", "30"))))
        except ValueError:
            raise web.HTTPBadRequest(text=json.dumps({"error": "Invalid pagination"}), content_type="application/json")
        return web.json_response(await self.database.list_logs(failed_only=failed_only, sort=sort, page=page, page_size=page_size))

    async def delete_all_logs(self, request: web.Request) -> web.Response:
        deleted = await self.database.delete_all_logs()
        return web.json_response({"ok": True, "deleted": deleted})

    async def get_log(self, request: web.Request) -> web.Response:
        item = await self.database.get_log(request.match_info["log_id"])
        if not item:
            raise web.HTTPNotFound()
        item.pop("request_body_path", None)
        item.pop("response_body_path", None)
        return web.json_response(item)

    async def get_body(self, request: web.Request) -> web.StreamResponse:
        item = await self.database.get_log(request.match_info["log_id"])
        if not item:
            raise web.HTTPNotFound()
        kind = request.match_info["kind"]
        relative_path = item[f"{kind}_body_path"]
        path = (self.database.data_dir / relative_path).resolve()
        if self.database.data_dir.resolve() not in path.parents or not path.is_file():
            raise web.HTTPNotFound()
        content_type = "application/octet-stream"
        headers = item[f"{kind}_headers"]
        for name, value in headers:
            if name.lower() == "content-type":
                content_type = value.split(";", 1)[0]
                break
        return web.FileResponse(path, headers={"Content-Type": content_type, "Content-Disposition": "inline"})
