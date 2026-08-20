from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import ssl
from pathlib import Path

from aiohttp import ClientSession, TCPConnector, web

from .admin import AdminServer
from .database import Database
from .proxy import StreamingProxy

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
LOGGER = logging.getLogger("agent-relay")


class RelayApplication:
    def __init__(self) -> None:
        self.data_dir = Path(os.getenv("RELAY_DATA_DIR", "./data")).resolve()
        self.relay_port = int(os.getenv("RELAY_PORT", "8081"))
        self.admin_port = int(os.getenv("ADMIN_PORT", "8082"))
        self.database = Database(self.data_dir)
        self.stop_event = asyncio.Event()
        self.reload_lock = asyncio.Lock()
        self.cleanup_task: asyncio.Task[None] | None = None
        self.relay_runner: web.AppRunner | None = None
        self.admin_runner: web.AppRunner | None = None
        self.relay_site: web.TCPSite | None = None
        self.admin_site: web.TCPSite | None = None

    def ssl_context(self) -> ssl.SSLContext:
        context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(self.data_dir / "certificate.pem", self.data_dir / "private-key.pem")
        return context

    async def initialize(self) -> None:
        await self.database.initialize()
        self.client_session = ClientSession(
            connector=TCPConnector(limit=0, ttl_dns_cache=300, enable_cleanup_closed=True),
            auto_decompress=False,
        )

        proxy_app = web.Application(client_max_size=1024**4)
        proxy = StreamingProxy(self.database, self.client_session)
        proxy_app.router.add_route("*", "/{path:.*}", proxy.handle)
        self.relay_runner = web.AppRunner(proxy_app, access_log=LOGGER)
        await self.relay_runner.setup()

        static_dir = Path(__file__).parent / "static"
        admin = AdminServer(self.database, static_dir, self.reload_tls)
        self.admin_runner = web.AppRunner(admin.create_app(), access_log=LOGGER)
        await self.admin_runner.setup()

        await self.start_sites()
        self.cleanup_task = asyncio.create_task(self.cleanup_loop())

    async def start_sites(self) -> None:
        settings = await self.database.get_settings()
        context = self.ssl_context() if settings.get("https_enabled") == "true" else None
        self.relay_site = web.TCPSite(self.relay_runner, "0.0.0.0", self.relay_port, ssl_context=context, reuse_address=True)
        await self.relay_site.start()
        if self.admin_site is None:
            self.admin_site = web.TCPSite(self.admin_runner, "0.0.0.0", self.admin_port, reuse_address=True)
            await self.admin_site.start()
        LOGGER.info("Relay listening on %s://0.0.0.0:%s", "https" if context else "http", self.relay_port)
        LOGGER.info("Admin listening on http://0.0.0.0:%s", self.admin_port)

    async def reload_tls(self) -> None:
        async with self.reload_lock:
            if self.relay_site:
                await self.relay_site.stop()
                self.relay_site = None
            try:
                await self.start_sites()
            except Exception:
                LOGGER.exception("Listener reload failed")
                self.stop_event.set()

    async def cleanup_loop(self) -> None:
        while True:
            try:
                settings = await self.database.get_settings()
                deleted = await self.database.cleanup_expired(int(settings.get("retention_days", "10")))
                if deleted:
                    LOGGER.info("Deleted %s expired request logs", deleted)
            except Exception:
                LOGGER.exception("Log cleanup failed")
            await asyncio.sleep(3600)

    async def run(self) -> None:
        await self.initialize()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, self.stop_event.set)
        await self.stop_event.wait()
        await self.shutdown()

    async def shutdown(self) -> None:
        if self.cleanup_task:
            self.cleanup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.cleanup_task
        await self.client_session.close()
        if self.admin_runner:
            await self.admin_runner.cleanup()
        if self.relay_runner:
            await self.relay_runner.cleanup()


async def async_main() -> None:
    application = RelayApplication()
    await application.run()


def main() -> None:
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
