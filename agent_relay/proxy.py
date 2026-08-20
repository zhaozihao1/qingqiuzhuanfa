from __future__ import annotations

import asyncio
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import AsyncIterator
from urllib.parse import urlsplit

from aiohttp import ClientError, ClientSession, ClientTimeout, web
from multidict import CIMultiDict

from .database import Database

HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


def _filtered_headers(raw_headers: tuple[tuple[bytes, bytes], ...], *, request: bool) -> CIMultiDict[str]:
    connection_tokens: set[str] = set()
    for name, value in raw_headers:
        if name.lower() == b"connection":
            connection_tokens.update(token.strip().lower() for token in value.decode("latin-1").split(","))

    result: CIMultiDict[str] = CIMultiDict()
    blocked = HOP_BY_HOP | connection_tokens
    if request:
        blocked.add("host")
    for raw_name, raw_value in raw_headers:
        name = raw_name.decode("latin-1")
        if name.lower() not in blocked:
            result.add(name, raw_value.decode("latin-1"))
    return result


def _headers_for_log(raw_headers: tuple[tuple[bytes, bytes], ...]) -> list[list[str]]:
    return [[name.decode("latin-1"), value.decode("latin-1")] for name, value in raw_headers]


def build_upstream_url(base_url: str, raw_path: str) -> str:
    return base_url.rstrip("/") + (raw_path if raw_path.startswith("/") else f"/{raw_path}")


class StreamingProxy:
    def __init__(self, database: Database, session: ClientSession) -> None:
        self.database = database
        self.session = session

    async def handle(self, request: web.Request) -> web.StreamResponse:
        started = datetime.now(UTC)
        started_monotonic = time.monotonic()
        log_id = uuid.uuid4().hex
        request_relative = f"bodies/{log_id}.request.bin"
        response_relative = f"bodies/{log_id}.response.bin"
        request_path = self.database.data_dir / request_relative
        response_path = self.database.data_dir / response_relative
        base_url = await self.database.get_active_base_url()
        upstream_url = build_upstream_url(base_url, request.raw_path) if base_url else None
        client_ip = request.remote

        await self.database.start_log(
            {
                "id": log_id,
                "started_at": started.isoformat(),
                "method": request.method,
                "incoming_url": request.raw_path,
                "upstream_url": upstream_url,
                "request_headers": _headers_for_log(request.raw_headers),
                "request_body_path": request_relative,
                "response_body_path": response_relative,
                "client_ip": client_ip,
            }
        )

        request_bytes = 0
        response_bytes = 0
        response_headers: list[list[str]] = []
        status: int | None = None
        error: str | None = None
        client_disconnected = False

        async def request_body() -> AsyncIterator[bytes]:
            nonlocal request_bytes
            with request_path.open("wb") as body_file:
                async for chunk in request.content.iter_chunked(64 * 1024):
                    body_file.write(chunk)
                    request_bytes += len(chunk)
                    yield chunk

        try:
            if not base_url:
                status = 503
                error = "No active base URL configured"
                payload = b'{"error":"No active base URL configured"}'
                response_path.write_bytes(payload)
                response_bytes = len(payload)
                return web.Response(status=status, body=payload, content_type="application/json")

            headers = _filtered_headers(request.raw_headers, request=True)
            parsed = urlsplit(base_url)
            headers["Host"] = parsed.netloc
            timeout = ClientTimeout(total=None, connect=30, sock_connect=30, sock_read=None)
            async with self.session.request(
                request.method,
                upstream_url,
                headers=headers,
                data=request_body(),
                allow_redirects=False,
                timeout=timeout,
                auto_decompress=False,
            ) as upstream:
                status = upstream.status
                outgoing_headers = _filtered_headers(upstream.raw_headers, request=False)
                response_headers = _headers_for_log(upstream.raw_headers)
                downstream = web.StreamResponse(status=status, reason=upstream.reason, headers=outgoing_headers)
                await downstream.prepare(request)

                with response_path.open("wb") as body_file:
                    async for chunk in upstream.content.iter_chunked(64 * 1024):
                        body_file.write(chunk)
                        response_bytes += len(chunk)
                        if not client_disconnected:
                            try:
                                await downstream.write(chunk)
                            except (ConnectionError, asyncio.CancelledError):
                                client_disconnected = True
                if not client_disconnected:
                    await downstream.write_eof()
                if client_disconnected:
                    error = "Downstream client disconnected before response completed"
                return downstream
        except (ClientError, TimeoutError, ConnectionError, OSError) as exc:
            error = f"{type(exc).__name__}: {exc}"
            if status is None:
                status = 502
                payload = (f'{{"error":"Upstream request failed","request_id":"{log_id}"}}').encode()
                response_path.write_bytes(payload)
                response_bytes = len(payload)
                return web.Response(status=status, body=payload, content_type="application/json")
            raise
        finally:
            request_path.touch(exist_ok=True)
            response_path.touch(exist_ok=True)
            duration_ms = round((time.monotonic() - started_monotonic) * 1000)
            await self.database.finish_log(
                log_id,
                {
                    "completed_at": datetime.now(UTC).isoformat(),
                    "response_headers": response_headers,
                    "request_bytes": request_bytes,
                    "response_bytes": response_bytes,
                    "status": status,
                    "duration_ms": duration_ms,
                    "error": error,
                    "failed": 1 if error or status is None or status >= 400 else 0,
                },
            )
