"""管理 API：target 管理、保留天数、请求日志查询、静态页面。"""
from __future__ import annotations

import base64
import os
import secrets
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import AppConfig
from .database import Database


def _check_auth(request: Request, cfg: AppConfig) -> bool:
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(auth[6:]).decode("utf-8")
        username, _, password = decoded.partition(":")
        return secrets.compare_digest(username, cfg.admin.username) and secrets.compare_digest(
            password, cfg.admin.password
        )
    except Exception:
        return False


def create_admin_app(cfg: AppConfig, db: Database) -> FastAPI:
    app = FastAPI(title="Agent Proxy Admin", docs_url=None, redoc_url=None, openapi_url=None)
    static_dir = os.path.join(os.path.dirname(__file__), "static")

    def auth_dep(request: Request) -> None:
        if not _check_auth(request, cfg):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Unauthorized",
                headers={"WWW-Authenticate": "Basic realm='agent-proxy-admin'"},
            )

    # ---------- targets ----------
    @app.get("/api/targets", dependencies=[Depends(auth_dep)])
    async def list_targets():
        return {"targets": await db.list_targets()}

    @app.post("/api/targets", dependencies=[Depends(auth_dep)])
    async def add_target(request: Request):
        data = await request.json()
        name = (data.get("name") or "").strip()
        url = (data.get("url") or "").strip()
        if not name or not url:
            raise HTTPException(400, "name and url are required")
        if not (url.startswith("http://") or url.startswith("https://")):
            raise HTTPException(400, "url must start with http:// or https://")
        tid = await db.add_target(name, url)
        return {"id": tid, "name": name, "url": url, "enabled": 0}

    @app.put("/api/targets/{target_id}", dependencies=[Depends(auth_dep)])
    async def update_target(target_id: int, request: Request):
        data = await request.json()
        name = (data.get("name") or "").strip()
        url = (data.get("url") or "").strip()
        if not name or not url:
            raise HTTPException(400, "name and url are required")
        await db.update_target(target_id, name, url)
        return {"ok": True}

    @app.delete("/api/targets/{target_id}", dependencies=[Depends(auth_dep)])
    async def delete_target(target_id: int):
        await db.delete_target(target_id)
        return {"ok": True}

    @app.post("/api/targets/{target_id}/activate", dependencies=[Depends(auth_dep)])
    async def activate_target(target_id: int):
        await db.activate_target(target_id)
        return {"ok": True}

    # ---------- retention ----------
    @app.get("/api/retention", dependencies=[Depends(auth_dep)])
    async def get_retention():
        return {"days": await db.get_retention_days()}

    @app.put("/api/retention", dependencies=[Depends(auth_dep)])
    async def set_retention(request: Request):
        data = await request.json()
        days = data.get("days")
        try:
            days = int(days)
        except (TypeError, ValueError):
            raise HTTPException(400, "days must be an integer")
        if days < 1:
            raise HTTPException(400, "days must be >= 1")
        await db.set_retention_days(days)
        return {"days": days}

    # ---------- requests log ----------
    @app.get("/api/requests", dependencies=[Depends(auth_dep)])
    async def list_requests(
        page: int = 1,
        size: int = 20,
        failed_only: bool = False,
    ):
        items, total = await db.list_requests(page=page, size=size, failed_only=failed_only)
        return {"items": items, "total": total, "page": page, "size": size}

    @app.get("/api/requests/{req_id}", dependencies=[Depends(auth_dep)])
    async def get_request(req_id: int):
        item = await db.get_request(req_id)
        if not item:
            raise HTTPException(404, "Not found")
        # BLOB 转 base64 字符串方便前端展示
        for k in ("request_body", "response_body"):
            v = item.get(k)
            if isinstance(v, (bytes, bytearray)):
                item[k] = base64.b64encode(bytes(v)).decode("ascii")
        return item

    @app.post("/api/cleanup", dependencies=[Depends(auth_dep)])
    async def cleanup():
        deleted = await db.cleanup_expired()
        return {"deleted": deleted}

    # ---------- static ----------
    @app.get("/")
    async def index():
        return FileResponse(os.path.join(static_dir, "index.html"))

    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    return app
