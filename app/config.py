"""应用配置：静态配置从 yaml 加载，运行时配置（targets/保留天数）存数据库。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

import yaml


@dataclass
class HttpsConfig:
    enabled: bool = False
    cert: str = ""
    key: str = ""


@dataclass
class ProxyConfig:
    host: str = "0.0.0.0"
    port: int = 8080
    https: HttpsConfig = field(default_factory=HttpsConfig)


@dataclass
class AdminConfig:
    host: str = "0.0.0.0"
    port: int = 8081
    username: str = "admin"
    password: str = "admin123"
    https: HttpsConfig = field(default_factory=HttpsConfig)


@dataclass
class AppConfig:
    proxy: ProxyConfig = field(default_factory=ProxyConfig)
    admin: AdminConfig = field(default_factory=AdminConfig)
    db_path: str = "data/proxy.db"
    # 单条请求/响应 body 最大记录字节数，超过则截断（不影响转发，只影响日志存储）
    max_body_log_size: int = 10 * 1024 * 1024
    # 代理转发超时（秒）
    proxy_timeout: float = 300.0


def _parse_https(data: dict) -> HttpsConfig:
    if not data:
        return HttpsConfig()
    return HttpsConfig(
        enabled=bool(data.get("enabled", False)),
        cert=str(data.get("cert", "")),
        key=str(data.get("key", "")),
    )


def load_config(path: str = "config.yaml") -> AppConfig:
    cfg = AppConfig()
    if not os.path.exists(path):
        return cfg
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    if "proxy" in data and data["proxy"]:
        p = data["proxy"]
        cfg.proxy.host = str(p.get("host", cfg.proxy.host))
        cfg.proxy.port = int(p.get("port", cfg.proxy.port))
        cfg.proxy.https = _parse_https(p.get("https"))

    if "admin" in data and data["admin"]:
        a = data["admin"]
        cfg.admin.host = str(a.get("host", cfg.admin.host))
        cfg.admin.port = int(a.get("port", cfg.admin.port))
        cfg.admin.username = str(a.get("username", cfg.admin.username))
        cfg.admin.password = str(a.get("password", cfg.admin.password))
        cfg.admin.https = _parse_https(a.get("https"))

    cfg.db_path = str(data.get("db_path", cfg.db_path))
    cfg.max_body_log_size = int(data.get("max_body_log_size", cfg.max_body_log_size))
    cfg.proxy_timeout = float(data.get("proxy_timeout", cfg.proxy_timeout))
    return cfg
