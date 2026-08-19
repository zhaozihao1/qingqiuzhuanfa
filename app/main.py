"""主入口：同时启动代理端口和管理端口，支持 HTTPS，后台定时清理过期日志。"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal

import uvicorn

from .admin import create_admin_app
from .config import AppConfig, load_config
from .database import Database
from .proxy import create_proxy_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("agent-proxy")


async def _cleanup_loop(db: Database, interval_seconds: int = 3600) -> None:
    """每小时清理一次过期请求日志。"""
    while True:
        try:
            deleted = await db.cleanup_expired()
            if deleted:
                logger.info("Cleaned up %d expired request(s)", deleted)
        except Exception as e:
            logger.warning("Cleanup failed: %s", e)
        await asyncio.sleep(interval_seconds)


def _uvicorn_kwargs(cfg, https_cfg) -> dict:
    kw = dict(host=cfg.host, port=cfg.port, log_level="info")
    if https_cfg.enabled and https_cfg.cert and https_cfg.key:
        kw["ssl_keyfile"] = https_cfg.key
        kw["ssl_certfile"] = https_cfg.cert
    return kw


async def run(cfg: AppConfig) -> None:
    db = Database(cfg.db_path)
    await db.init()

    proxy_app = create_proxy_app(cfg, db)
    admin_app = create_admin_app(cfg, db)

    proxy_config = uvicorn.Config(proxy_app, **_uvicorn_kwargs(cfg.proxy, cfg.proxy.https))
    admin_config = uvicorn.Config(admin_app, **_uvicorn_kwargs(cfg.admin, cfg.admin.https))

    proxy_server = uvicorn.Server(proxy_config)
    admin_server = uvicorn.Server(admin_config)

    cleanup_task = asyncio.create_task(_cleanup_loop(db))

    logger.info("Proxy  listening on %s://%s:%d",
                "https" if cfg.proxy.https.enabled else "http",
                cfg.proxy.host, cfg.proxy.port)
    logger.info("Admin  listening on %s://%s:%d",
                "https" if cfg.admin.https.enabled else "http",
                cfg.admin.host, cfg.admin.port)

    try:
        await asyncio.gather(
            proxy_server.serve(),
            admin_server.serve(),
            cleanup_task,
        )
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        cleanup_task.cancel()
        await db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Agent Proxy Gateway")
    parser.add_argument("-c", "--config", default="config.yaml", help="Path to config yaml")
    args = parser.parse_args()

    cfg = load_config(args.config)
    # 确保 data 目录存在
    os.makedirs(os.path.dirname(cfg.db_path) or ".", exist_ok=True)

    try:
        asyncio.run(run(cfg))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
