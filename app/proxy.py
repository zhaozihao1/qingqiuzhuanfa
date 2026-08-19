"""代理转发核心：路径原样拼接、头/体原样转发，支持流式输出，全程记录日志。"""
from __future__ import annotations

import time
from typing import Dict, Optional, Tuple

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from .config import AppConfig
from .database import Database

# 这些是 hop-by-hop 头，转发时必须移除（HTTP 规范要求）
HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}


def _filter_headers(headers: Dict[str, str]) -> Dict[str, str]:
    """移除必改/逐跳头，其余原样保留。"""
    return {k: v for k, v in headers.items() if k.lower() not in HOP_BY_HOP_HEADERS}


def _is_stream_response(status: int, headers: Dict[str, str]) -> bool:
    ct = headers.get("content-type", "").lower()
    te = headers.get("transfer-encoding", "").lower()
    return "text/event-stream" in ct or "chunked" in te or "octet-stream" in ct


def _truncate(body: bytes, max_size: int) -> bytes:
    if max_size > 0 and len(body) > max_size:
        return body[:max_size] + b"\n...[truncated]"
    return body


def create_proxy_app(cfg: AppConfig, db: Database) -> FastAPI:
    app = FastAPI(title="Agent Proxy", docs_url=None, redoc_url=None, openapi_url=None)
    # 复用一个 httpx 客户端，连接池复用
    client_holder: Dict[str, httpx.AsyncClient] = {}

    @app.on_event("startup")
    async def _startup() -> None:
        client_holder["c"] = httpx.AsyncClient(
            timeout=httpx.Timeout(cfg.proxy_timeout),
            follow_redirects=False,
            http2=False,
        )

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        c = client_holder.get("c")
        if c:
            await c.aclose()

    @app.api_route(
        "/{path:path}",
        methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"],
    )
    async def proxy(request: Request, path: str) -> Response:
        target = await db.get_active_target()
        if not target:
            return JSONResponse(
                {"error": "No active target. Please configure one in admin panel."},
                status_code=502,
            )

        base = target["url"].rstrip("/")
        url = f"{base}/{path}"
        if request.url.query:
            url = f"{url}?{request.url.query}"

        method = request.method
        req_headers_raw = {k: v for k, v in request.headers.items()}
        req_headers = _filter_headers(req_headers_raw)
        req_body = await request.body()
        start = time.perf_counter()

        client = client_holder["c"]

        try:
            upstream_req = client.build_request(
                method,
                url,
                headers=req_headers,
                content=req_body if method not in ("GET", "HEAD") else None,
            )
            upstream_resp = await client.send(upstream_req, stream=True)
        except Exception as e:
            duration_ms = (time.perf_counter() - start) * 1000
            await db.insert_request(
                method=method,
                path=path,
                query=str(request.url.query),
                request_headers=req_headers_raw,
                request_body=_truncate(req_body, cfg.max_body_log_size),
                response_status=None,
                response_headers=None,
                response_body=b"",
                duration_ms=duration_ms,
                target_url=target["url"],
                error=f"{type(e).__name__}: {e}",
            )
            return JSONResponse(
                {"error": "Upstream connection failed", "detail": str(e)},
                status_code=502,
            )

        resp_headers_raw = {k: v for k, v in upstream_resp.headers.items()}
        resp_headers = _filter_headers(resp_headers_raw)
        status = upstream_resp.status_code
        stream = _is_stream_response(status, resp_headers_raw)

        if stream:
            collected_chunks: list = []

            async def gen():
                try:
                    async for chunk in upstream_resp.aiter_bytes():
                        collected_chunks.append(chunk)
                        yield chunk
                finally:
                    duration_ms = (time.perf_counter() - start) * 1000
                    full = b"".join(collected_chunks)
                    await db.insert_request(
                        method=method,
                        path=path,
                        query=str(request.url.query),
                        request_headers=req_headers_raw,
                        request_body=_truncate(req_body, cfg.max_body_log_size),
                        response_status=status,
                        response_headers=resp_headers_raw,
                        response_body=_truncate(full, cfg.max_body_log_size),
                        duration_ms=duration_ms,
                        target_url=target["url"],
                        error=None,
                    )
                    await upstream_resp.aclose()

            return StreamingResponse(
                gen(),
                status_code=status,
                headers=resp_headers,
                media_type=resp_headers.get("content-type"),
            )
        else:
            resp_body = await upstream_resp.aread()
            await upstream_resp.aclose()
            duration_ms = (time.perf_counter() - start) * 1000
            await db.insert_request(
                method=method,
                path=path,
                query=str(request.url.query),
                request_headers=req_headers_raw,
                request_body=_truncate(req_body, cfg.max_body_log_size),
                response_status=status,
                response_headers=resp_headers_raw,
                response_body=_truncate(resp_body, cfg.max_body_log_size),
                duration_ms=duration_ms,
                target_url=target["url"],
                error=None,
            )
            return Response(
                content=resp_body,
                status_code=status,
                headers=resp_headers,
                media_type=resp_headers.get("content-type"),
            )

    return app
