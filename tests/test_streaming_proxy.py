from __future__ import annotations

import asyncio
import gzip
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import pytest
from aiohttp import ClientSession, CookieJar, TCPConnector, web

from agent_relay.admin import AdminServer
from agent_relay.database import Database
from agent_relay.proxy import StreamingProxy


@asynccontextmanager
async def running_app(app: web.Application) -> AsyncIterator[str]:
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    assert site._server is not None
    port = site._server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_sse_reasoning_and_tool_deltas_arrive_before_stream_ends(tmp_path: Path) -> None:
    first_sent = asyncio.Event()
    release_rest = asyncio.Event()
    first = 'data: {"choices":[{"delta":{"reasoning_content":"正在思考"}}]}\n\n'.encode()
    rest = (
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":'
        '{"arguments":"{\\"city\\":\\"北京\\"}"}}]}}]}\n\n'
        'data: [DONE]\n\n'
    ).encode()

    async def upstream_handler(request: web.Request) -> web.StreamResponse:
        assert await request.json() == {"stream": True}
        response = web.StreamResponse(
            headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"}
        )
        await response.prepare(request)
        await response.write(first)
        first_sent.set()
        await release_rest.wait()
        await response.write(rest)
        await response.write_eof()
        return response

    upstream_app = web.Application()
    upstream_app.router.add_post("/v1/chat/completions", upstream_handler)

    async with running_app(upstream_app) as upstream_url:
        database = Database(tmp_path / "data")
        await database.initialize()
        await database.add_access_key("test", "relay-test-key")
        await database.add_base_url("test", f"{upstream_url}/v1")
        async with ClientSession(
            connector=TCPConnector(limit=0), auto_decompress=False
        ) as upstream_session:
            relay_app = web.Application()
            relay_app.router.add_route(
                "*", "/{path:.*}", StreamingProxy(database, upstream_session).handle
            )
            async with running_app(relay_app) as relay_url:
                async with ClientSession(auto_decompress=False) as client:
                    async with client.post(
                        f"{relay_url}/chat/completions", json={"stream": True},
                        headers={"X-Relay-Key": "relay-test-key"},
                    ) as response:
                        await asyncio.wait_for(first_sent.wait(), timeout=2)
                        received_first = await asyncio.wait_for(
                            response.content.readuntil(b"\n\n"), timeout=2
                        )
                        assert received_first == first
                        assert response.headers["Content-Type"].startswith("text/event-stream")
                        assert response.headers["X-Accel-Buffering"] == "no"
                        release_rest.set()
                        assert received_first + await response.read() == first + rest

    logs = await database.list_logs(failed_only=False, sort="desc", page=1, page_size=10)
    assert logs["total"] == 1
    log = await database.get_log(logs["items"][0]["id"])
    assert log is not None
    assert (database.data_dir / log["response_body_path"]).read_bytes() == first + rest


@pytest.mark.asyncio
async def test_admin_body_restores_content_encoding(tmp_path: Path) -> None:
    database = Database(tmp_path / "data")
    await database.initialize()
    log_id = "a" * 32
    response_body = gzip.compress("data: 思考过程\n\n".encode())
    relative_path = f"bodies/{log_id}.response.bin"
    (database.data_dir / relative_path).write_bytes(response_body)
    await database.start_log(
        {
            "id": log_id,
            "started_at": "2026-01-01T00:00:00+00:00",
            "method": "POST",
            "incoming_url": "/v1/chat/completions",
            "upstream_url": "https://example.test/v1/chat/completions",
            "request_headers": [],
            "request_body_path": f"bodies/{log_id}.request.bin",
            "response_body_path": relative_path,
            "client_ip": "127.0.0.1",
        }
    )
    await database.finish_log(
        log_id,
        {
            "completed_at": "2026-01-01T00:00:01+00:00",
            "response_headers": [
                ["Content-Type", "text/event-stream; charset=utf-8"],
                ["Content-Encoding", "gzip"],
            ],
            "request_bytes": 0,
            "response_bytes": len(response_body),
            "status": 200,
            "duration_ms": 1,
            "error": None,
            "failed": 0,
        },
    )

    async def no_reload() -> None:
        return None

    admin = AdminServer(database, tmp_path, no_reload)
    async with running_app(admin.create_app()) as admin_url:
        async with ClientSession(
            auto_decompress=False, cookie_jar=CookieJar(unsafe=True)
        ) as client:
            async with client.post(
                f"{admin_url}/api/login", json={"username": "admin", "password": "admin"}
            ) as login_response:
                assert login_response.status == 200
            async with client.get(f"{admin_url}/api/logs/{log_id}/response-body") as response:
                assert response.status == 200
                assert response.headers["Content-Encoding"] == "gzip"
                assert response.headers["Content-Type"] == "text/event-stream; charset=utf-8"
                assert await response.read() == response_body


@pytest.mark.asyncio
async def test_invalid_access_key_is_silently_disconnected_and_logged(tmp_path: Path) -> None:
    database = Database(tmp_path / "data")
    await database.initialize()
    await database.add_access_key("client", "valid-relay-key")
    await database.set_settings({"access_key_enabled": "true"})
    async with ClientSession() as upstream_session:
        relay_app = web.Application()
        relay_app.router.add_route("*", "/{path:.*}", StreamingProxy(database, upstream_session).handle)
        async with running_app(relay_app) as relay_url:
            async with ClientSession() as client:
                with pytest.raises((Exception, asyncio.CancelledError)):
                    async with client.post(relay_url + "/probe", json={"probe": "scanner-payload"}, headers={"Authorization": "Bearer wrong-key"}) as response:
                        await response.read()
    denied = await database.list_denied_logs(sort="desc", page=1, page_size=10)
    assert denied["total"] >= 1
    detail = await database.get_denied_log(denied["items"][0]["id"])
    assert detail is not None
    assert detail["incoming_url"] == "/probe"
    assert ["Authorization", "[REDACTED]"] in detail["request_headers"]
    assert detail["request_bytes"] > 0
    assert b"scanner-payload" in (database.data_dir / detail["request_body_path"]).read_bytes()


@pytest.mark.asyncio
async def test_disabled_access_key_protection_allows_requests_without_key(tmp_path: Path) -> None:
    async def upstream_handler(request: web.Request) -> web.Response:
        return web.Response(text="ok")

    upstream_app = web.Application()
    upstream_app.router.add_get("/probe", upstream_handler)
    async with running_app(upstream_app) as upstream_url:
        database = Database(tmp_path / "data")
        await database.initialize()
        await database.add_base_url("test", upstream_url)
        async with ClientSession() as upstream_session:
            relay_app = web.Application()
            relay_app.router.add_route("*", "/{path:.*}", StreamingProxy(database, upstream_session).handle)
            async with running_app(relay_app) as relay_url:
                async with ClientSession() as client:
                    async with client.get(relay_url + "/probe") as response:
                        assert response.status == 200
                        assert await response.text() == "ok"
    denied = await database.list_denied_logs(sort="desc", page=1, page_size=10)
    assert denied["total"] == 0


@pytest.mark.asyncio
async def test_change_and_forgot_password_reset_all_data(tmp_path: Path) -> None:
    database = Database(tmp_path / "data")
    await database.initialize()
    await database.add_access_key("client", "valid-relay-key")
    await database.add_base_url("test", "https://example.test/v1")

    async def no_reload() -> None:
        return None

    admin = AdminServer(database, tmp_path, no_reload)
    async with running_app(admin.create_app()) as admin_url:
        async with ClientSession(cookie_jar=CookieJar(unsafe=True)) as client:
            async with client.post(admin_url + "/api/login", json={"username": "admin", "password": "admin"}) as response:
                csrf = (await response.json())["csrf"]
            async with client.put(admin_url + "/api/settings/password", json={"old_password": "admin", "new_password": "new-password"}, headers={"X-CSRF-Token": csrf}) as response:
                assert response.status == 200
            async with client.post(admin_url + "/api/login", json={"username": "admin", "password": "new-password"}) as response:
                assert response.status == 200
            async with client.post(admin_url + "/api/forgot-password", json={"confirm": "RESET ALL DATA"}) as response:
                assert response.status == 200
            async with client.post(admin_url + "/api/login", json={"username": "admin", "password": "admin"}) as response:
                assert response.status == 200
    assert await database.list_access_keys() == []
    assert await database.list_base_urls() == []
